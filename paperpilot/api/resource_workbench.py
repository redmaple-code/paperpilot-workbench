"""Expose resource workflows through the existing local application boundaries."""
from fastapi import APIRouter, HTTPException
from contextlib import closing

from paperpilot.domain.term_comparison_schemas import (
    ComparisonIn, ComparisonApplyIn, DomainReferenceSourceIn, DomainReferenceTermBatchIn, DomainReferenceTermDecisionIn,
)
from paperpilot.services import domain_reference_terms, term_comparison

from paperpilot.domain.stage3_schemas import (ClassificationIn, DomainResourceEditIn, DraftCopyIn,
    CorpusMigrationIn, CorpusMigrationDecisionIn, CorpusSourceLinkIn, StructuredOutlineSuggestionIn)
from paperpilot.services import terminology, domain_resources
from paperpilot.services import editor, human_revision
from paperpilot.services import corpus_migrations
from paperpilot.services import outline_suggestions
import json
from paperpilot.services.project_errors import ProjectServiceError


def create_router(db, project_or_404, writable, clean_path, call_ai):
    router = APIRouter()

    def translate(exc):
        return HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message})

    @router.post('/api/projects/{project_id}/terminology/classification-preview')
    def classification_preview(project_id: str, payload: ClassificationIn):
        writable(project_id)
        conn = db()
        try:
            result = terminology.preview(conn, project_id, payload.term_id)
            return {k: v for k, v in result.items() if not k.startswith('_')}
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/terminology/classify')
    def classify(project_id: str, payload: ClassificationIn):
        writable(project_id)
        conn = db()
        try:
            return terminology.classify(conn, project_id, payload.model_dump(), call_ai)
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.get('/api/projects/{project_id}/terminology/provenance')
    def term_origins(project_id: str):
        project_or_404(project_id)
        conn = db()
        try:
            return terminology.provenance(conn, project_id)
        finally:
            conn.close()

    def compare_using(service, *args, write=False):
        try:
            with closing(db()) as conn, conn:
                if not write:
                    conn.execute('BEGIN')
                return service(conn, *args)
        except ProjectServiceError as exc:
            raise translate(exc) from exc

    @router.post('/api/projects/{project_id}/terminology/comparison/preview')
    def preview_term_comparison(project_id: str, payload: ComparisonIn):
        return compare_using(term_comparison.preview, project_id, None, payload.reference_ids)

    @router.post('/api/projects/{project_id}/terminology/comparison/apply')
    def apply_term_comparison(project_id: str, payload: ComparisonApplyIn):
        return compare_using(term_comparison.apply, payload.model_dump(), project_id, write=True)

    @router.get('/api/projects/{project_id}/terminology/comparison/{term_id}/history')
    def term_comparison_history(project_id: str, term_id: str):
        return compare_using(term_comparison.history, project_id, term_id)

    @router.get('/api/projects/{project_id}/references/{reference_id}/terms')
    def reference_terminology(project_id: str, reference_id: str, version: int | None = None):
        return compare_using(term_comparison.reference_terms, project_id, reference_id, version)

    @router.get('/api/references/library/{reference_id}/terms')
    def library_reference_terminology(reference_id: str, version: int | None = None):
        return compare_using(term_comparison.reference_terms, None, reference_id, version)

    @router.get('/api/domains/{domain_id}/reference-terms')
    def domain_reference_candidates(domain_id: str):
        return compare_using(domain_reference_terms.preview, domain_id)

    @router.put('/api/domains/{domain_id}/reference-source-projects/{project_id}')
    def set_domain_reference_source(domain_id: str, project_id: str, payload: DomainReferenceSourceIn):
        return compare_using(domain_reference_terms.set_source_project, domain_id, project_id, payload.enabled, write=True)

    @router.post('/api/domains/{domain_id}/reference-terms/{candidate_id}/decision')
    def decide_domain_reference_candidate(domain_id: str, candidate_id: str, payload: DomainReferenceTermDecisionIn):
        return compare_using(domain_reference_terms.decide, domain_id, candidate_id, payload.model_dump(), write=True)

    @router.post('/api/domains/{domain_id}/reference-terms/batch')
    def publish_domain_reference_batch(domain_id: str, payload: DomainReferenceTermBatchIn):
        return compare_using(domain_reference_terms.publish_batch, domain_id, payload.model_dump(), write=True)

    @router.get('/api/domains/{domain_id}/resources/{resource_id}/versions')
    def shared_history(domain_id: str, resource_id: str):
        conn = db()
        try:
            return domain_resources.history(conn, domain_id, resource_id)
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.put('/api/domains/{domain_id}/resources/{resource_id}')
    def edit_shared(domain_id: str, resource_id: str, payload: DomainResourceEditIn):
        conn = db()
        try:
            with conn:
                return domain_resources.edit(conn, domain_id, resource_id, payload.model_dump())
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.get('/api/projects/{project_id}/domain-recommendations')
    def recommend(project_id: str):
        project_or_404(project_id)
        conn = db()
        try:
            return domain_resources.recommendations(conn, project_id)
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/editor/draft-copy')
    def draft_copy(project_id: str, payload: DraftCopyIn):
        writable(project_id)
        source, target = clean_path(payload.source_path), clean_path(payload.target_path)
        conn = db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                mode = None  # saving a draft as a new file is not stage-bound
                # The shared editor ledger checks exact content/path on replay.
                prior = conn.execute('SELECT target_path,result_json FROM editor_write_events WHERE project_id=? AND request_id=?', (project_id, payload.request_id)).fetchone()
                draft = editor.get_draft(conn, project_id, source)
                if not draft or draft['updated_at'] != payload.draft_updated_at:
                    raise ProjectServiceError(409, 'draft_changed', '草稿已更新或移除，请重新比较后另存。')
                if source == target or (not prior and conn.execute('SELECT 1 FROM files WHERE project_id=? AND path=?', (project_id, target)).fetchone()):
                    raise ProjectServiceError(409, 'draft_copy_target_exists', '另存路径必须是新的文件；原文件和草稿均已保留。')
                result = editor.save(conn, project_id, target, draft['content'], 0, payload.request_id)
                if mode and not result.get('idempotent_replay'):
                    human_revision.record_event(conn, project_id, mode_id=mode['id'], action_type='draft_save_as',
                        scope={'source_path': source, 'target_path': target}, base_file_versions={target: 0},
                        status='checkpointed', request_id=payload.request_id)
                return result
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/corpus/{corpus_id}/migrations')
    def preview_curation_migration(project_id: str, corpus_id: str, payload: CorpusMigrationIn):
        writable(project_id)
        conn = db()
        try:
            with conn:
                return corpus_migrations.create(conn, project_id, corpus_id, payload.model_dump())
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/corpus/{corpus_id}/migrations/{migration_id}/decision')
    def decide_curation_migration(project_id: str, corpus_id: str, migration_id: str, payload: CorpusMigrationDecisionIn):
        writable(project_id)
        conn = db()
        try:
            with conn:
                return corpus_migrations.decide(conn, project_id, corpus_id, migration_id, payload.model_dump())
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/corpus/{corpus_id}/source-links')
    def link_corpus_source(project_id: str, corpus_id: str, payload: CorpusSourceLinkIn):
        writable(project_id)
        conn = db()
        try:
            with conn:
                return corpus_migrations.link(conn, project_id, corpus_id, payload.model_dump())
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.get('/api/projects/{project_id}/outline/candidates')
    def list_outline_candidates(project_id: str):
        project_or_404(project_id)
        conn = db()
        try:
            from paperpilot.services.outline import current_version
            version = current_version(conn, project_id)
            return [{**dict(r), 'operations': json.loads(r['operations_json']), 'stale': r['base_version'] != version}
                for r in conn.execute('SELECT * FROM outline_candidates WHERE project_id=? ORDER BY created_at DESC LIMIT 50', (project_id,))]
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/outline/suggestion-preview')
    def outline_suggestion_preview(project_id: str, payload: StructuredOutlineSuggestionIn):
        writable(project_id)
        conn = db()
        try:
            result = outline_suggestions.preview(conn, project_id, payload.model_dump())
            return {k:v for k,v in result.items() if not k.startswith('_')}
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    @router.post('/api/projects/{project_id}/outline/suggestions')
    def generate_outline_suggestion(project_id: str, payload: StructuredOutlineSuggestionIn):
        writable(project_id)
        conn = db()
        try:
            return outline_suggestions.generate(conn, project_id, payload.model_dump(), call_ai)
        except ProjectServiceError as exc:
            raise translate(exc) from exc
        finally:
            conn.close()

    return router

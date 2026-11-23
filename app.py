from __future__ import annotations

import os
import re
import json
import sqlite3
import threading
import uuid
import hashlib
import time
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# Windows registry associations can label .mjs as text/plain, which browsers
# correctly refuse for module scripts. Serve the pinned local PDF renderer with
# its standard types on every supported host.
mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('application/wasm', '.wasm')

from paperpilot.adapters.latex import compile_project_files
from paperpilot.adapters.openai_compat import call_chat_completion
from paperpilot.core.database import initialize_schema
from paperpilot.domain.schemas import BuildSourceIn, BuildSourceForwardIn, BuildSourceInverseIn
from paperpilot.domain.schemas import BuildRestorePreviewIn, BuildRestoreCommitIn
from paperpilot.domain.schemas import AcademicHandoffIn, AcademicSearchIn, AIContextPreviewIn, AIIn, AIJobIn, AIJobRetryIn, BuildRestoreIn, BuildSnapshotIn, CandidateDecisionIn, CitationCheckIn, CitationSuggestionHandoffIn, CompileIn, ContinuationCommandIn, ContinuationIn, ContinuationSegmentDecisionIn, CopyIn, CorpusCurationIn, CorpusIn, CorpusRefreshDecisionIn, CorpusRefreshIn, DrawIn, DomainAssignIn, DomainIn, DomainResourceCopyIn, DomainResourceIn, DomainSettingsIn, DomainTermImportIn, EditorConversionApplyIn, EditorConversionPreviewIn, EditorDraftIn, FileIn, FormatCheckIn, ImportConfirmIn, ImportIn, LogicGraphIn, LogicIn, LogicSuggestionDecisionIn, LogicSuggestionIn, ModificationCleanupIn, ModificationDecisionIn, ModificationIn, OutlineCandidateDecisionIn, OutlineCandidateIn, OutlineDraftIn, OutlineMigrationCommitIn, OutlineMigrationIn, OutlineNodeCommandIn, OutlineRestoreIn, ProjectIn, ProjectLibrarySearchIn, ProjectLifecycleCommandIn, ProviderBindingIn, ProviderProfileIn, ProviderProfileUpdateIn, RefIn, ReferenceAttachmentIn, ReferenceDraftConfirmIn, ReferenceLinkIn, ReferenceMergeIn, ReferenceRecordIn, ReferenceRefreshIn, ReferenceSearchIn, ReferenceVersionIn, RevisionCheckpointIn, RevisionDraftIn, RevisionExitIn, RevisionModeIn, RevisionRestoreIn, SettingOverrideIn, SettingRestoreIn, SettingSyncIn, TermClassificationDecisionIn, TermClassificationReviewIn, TermIn, TermPromoteCommitIn, TermPromotePreviewIn, TextIn, WorkflowCommandIn, WorkflowItemIn
from paperpilot.domain.schemas import SettingBindingRepairIn, AIJobRecoveryIn
from paperpilot.domain.schemas import ProviderTestIn
from paperpilot.rendering.svg import flow_svg
from paperpilot.services.context import build_ai_context, build_ai_context_snapshot, redact_sensitive_text
from paperpilot.services.candidate_validation import validate_candidate
from paperpilot.services import academic_search
from paperpilot.services.search_sessions import SearchSessionStore, SearchSessionError
from paperpilot.services import imports as oa_imports
from paperpilot.services.conversion import ai_url, bib_keys, latex_to_markdown, markdown_to_latex
from paperpilot.services.ai_jobs import cancel_job, candidate_payload, complete_job, create_job, estimate_tokens, get_candidate, mark_job_failed, retry_job
from paperpilot.services.content_versions import apply_bundle, current_version, delete_content, restore_content, save_content, seed_initial_versions, version_map
from paperpilot.services.human_revision import active_mode, commit_draft, enter_mode, exit_mode, record_event, resolve_pending_drafts, save_draft, update_event_status
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.style_profile import extension_status
from paperpilot.services import provider_config, domain_workspace, provider_runtime
from paperpilot.services import style_learning
from paperpilot.services import build_snapshots, citation_support, continuation, corpus_lifecycle, editor as editor_service, modification, settings
from paperpilot.services import outline as outline_service
from paperpilot.services.workflow import append_snapshot, create_initial_workflow, current_snapshot, execute_command, set_work_item_status, workflow_projection
from paperpilot.services.logic_graph import apply_operations, read_graph, save_graph, validate_graph
from paperpilot.services.project_lifecycle import apply_lifecycle_command, latest_release_check, record_release_check
from paperpilot.services.reference_library import attach_reference_evidence, confirm_draft, create_reference, create_reference_draft, find_possible_duplicates, link_reference, list_references, merge_references, read_reference, refresh_reference_link, update_reference

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.environ.get("PWA_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DB_PATH = Path(os.environ.get("PWA_DB_PATH", str(DATA_DIR / "paper_writer.db"))).expanduser().resolve()
PROJECTS_DIR = Path(os.environ.get("PWA_PROJECTS_DIR", str(DATA_DIR / "projects"))).expanduser().resolve()
STATIC_DIR = ROOT / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
_build_locks_guard = threading.Lock()
_build_dispatchers: set[tuple[str, str]] = set()
_build_wake: set[tuple[str, str]] = set()
_build_waking: set[tuple[str, str]] = set()
_build_wake_slots = threading.BoundedSemaphore(2)
_build_pending_cleanup: set[tuple[str, str, str, str, int, str]] = set()
_build_store = threading.local()
MAX_ACTIVE_BUILDS_PER_PROJECT = 8
_search_session_store = SearchSessionStore()
_search_worker_id = uuid.uuid4().hex

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    stop = threading.Event()
    # Bind this worker to this application store, even during test teardown.
    database_path = DB_PATH
    projects_path = PROJECTS_DIR
    def worker_db():
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    worker = threading.Thread(target=style_learning.maintenance_loop, args=(worker_db, stop), daemon=True)
    worker.start()
    def build_scheduler():
        _build_store.database_path = database_path
        _build_store.projects_path = projects_path
        while not stop.wait(1):
            try:
                conn = worker_db()
                try:
                    from paperpilot.services import build_queue
                    with conn:
                        build_queue.recover_dead_slots(conn, projects_path)
                finally:
                    conn.close()
                _retry_finished_build_slots()
                _wake_pending_builds()
            except (sqlite3.Error, OSError, ValueError, ProjectServiceError):
                import logging
                logging.getLogger(__name__).warning('构建调度暂不可用，将稍后重试。')
    scheduler = threading.Thread(target=build_scheduler, daemon=True)
    scheduler.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=10)
        scheduler.join(timeout=10)


app = FastAPI(title="论文写作助手", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def search_session_worker_affinity(request, call_next):
    path = request.url.path
    if path == '/api/session-search' or '/session-search' in path:
        expected = request.headers.get('X-Search-Worker')
        if expected and expected != _search_worker_id:
            return JSONResponse(status_code=409,content={'detail':{'code':'search_worker_changed',
                'message':'检索会话所在服务已重启或请求被分配到其他工作进程。请检查服务使用单进程或会话粘性路由，刷新后重新搜索；已保存历史可恢复。'}})
        response = await call_next(request)
        response.headers['X-Search-Worker'] = _search_worker_id
        return response
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_request,exc):
    # Pydantic's default `input` echoes can include an API key, including one
    # nested in a malformed changes object. Field locations/messages suffice.
    return JSONResponse(status_code=422,content={'detail':[{k:error[k] for k in ('type','loc','msg') if k in error} for error in exc.errors()]})


@app.exception_handler(sqlite3.Error)
async def storage_error(_request,_exc):
    return JSONResponse(status_code=503,content={'detail':{'code':'storage_unavailable','message':'存储暂时不可用，当前输入保留。请检查可用空间并刷新核对状态；已有模型请求请先核对结果，避免重复发送。'}})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(getattr(_build_store, 'database_path', DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _build_projects_dir():
    return Path(getattr(_build_store, 'projects_path', PROJECTS_DIR))


def init_db() -> None:
    conn = db()
    initialize_schema(conn)
    from paperpilot.services import research_design, agent_runtime, agent_memory
    with conn:
        research_design.initialize_schema(conn)
        from paperpilot.services import innovation_points
        innovation_points.initialize_schema(conn)
        from paperpilot.services import agent_actions
        agent_actions.initialize_schema(conn)
        from paperpilot.services import structure_workspaces
        structure_workspaces.initialize_schema(conn)
        from paperpilot.services import domain_reference_terms, manuscript_import, term_comparison
        manuscript_import.initialize_schema(conn)
        term_comparison.initialize_schema(conn)
        domain_reference_terms.initialize_schema(conn)
        manuscript_import.recover_analyses(conn)
        from paperpilot.services import manuscript_asset_journal
        manuscript_asset_journal.recover(conn,PROJECTS_DIR)
        agent_runtime.initialize_schema(conn)
        agent_memory.initialize_schema(conn)
        from paperpilot.services import agent_execution
        agent_execution.recover_orphans(conn)
    from paperpilot.services import build_cache
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT DISTINCT project_id FROM build_jobs WHERE status='succeeded'").fetchall():
            build_cache.recover_staged(conn, PROJECTS_DIR, row['project_id'])
    from paperpilot.services.tex_runtime import owner_state
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        tests = conn.execute("SELECT id,owner_pid,owner_token FROM ai_provider_tests WHERE status IN ('queued','running')").fetchall()
        for test in tests:
            if owner_state(dict(test)) == 'dead':
                conn.execute("UPDATE ai_provider_tests SET status='failed',error_code='interrupted_no_resend',updated_at=? WHERE id=? AND status IN ('queued','running') AND owner_pid=? AND owner_token=?",(now(),test['id'],test['owner_pid'],test['owner_token']))
        interrupted = conn.execute("SELECT id,project_id,request_id,status,cancel_requested,scope_json,owner_pid,owner_token FROM ai_jobs WHERE status IN ('running','cancel_requested')").fetchall()
        for row in interrupted:
            if owner_state(dict(row)) != 'dead':
                continue
            status = "cancelled" if row["cancel_requested"] or row["status"] == "cancel_requested" else "failed"
            interrupted_scope=json.loads(row['scope_json'] or '{}')
            if interrupted_scope.get('egress_state')!='not_sent':interrupted_scope['egress_state']='unknown'
            changed = conn.execute(
                "UPDATE ai_jobs SET status=?,error_code='interrupted',updated_at=? WHERE project_id=? AND request_id=? AND status IN ('running','cancel_requested') AND owner_pid=? AND owner_token=?",
                (status, now(), row["project_id"], row["request_id"], row['owner_pid'], row['owner_token']),
            ).rowcount
            if not changed:
                continue
            conn.execute(
                "UPDATE assist_action_events SET status=? WHERE project_id=? AND request_id=? AND status='running'",
                (status, row["project_id"], row["request_id"]),
            )
            conn.execute('UPDATE ai_jobs SET scope_json=? WHERE project_id=? AND request_id=?',(json.dumps(interrupted_scope,ensure_ascii=False),row['project_id'],row['request_id']))
            conn.execute("UPDATE ai_provider_audit SET status=?,error_code='interrupted',updated_at=? WHERE id=? AND status='admitted'",('unknown' if interrupted_scope.get('egress_state')=='unknown' else 'failed',now(),row['id']))
        _recover_oa_fetches(conn)
        build_snapshots.recover_startup(conn, PROJECTS_DIR)
        from paperpilot.services import build_queue
        build_queue.recover_dead_slots(conn, PROJECTS_DIR)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        _oa_drain_cleanup(conn)
    conn.close()


def project_or_404(project_id: str) -> sqlite3.Row:
    conn = db()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "项目不存在")
    return row


def require_project_writable(project_id: str) -> sqlite3.Row:
    project = project_or_404(project_id)
    if project["lifecycle_state"] == "published":
        raise HTTPException(
            409,
            detail={"code": "project_published_read_only", "message": "已发布论文为只读状态；撤回发布后才能编辑。"},
        )
    return project


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?",
        (now(), project_id),
    )


def require_live_project_write(conn: sqlite3.Connection, project_id: str) -> None:
    """Writing is stage-independent; lifecycle and concurrency checks stay atomic."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    project = conn.execute("SELECT lifecycle_state FROM projects WHERE id=?", (project_id,)).fetchone()
    if project and project["lifecycle_state"] == "published":
        raise ProjectServiceError(409, "project_published_read_only", "已发布论文为只读状态；撤回发布后才能编辑。")
    from paperpilot.services.agent_actions import require_writable
    require_writable(conn, project_id)


def has_workflow(project_id: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM workflow_snapshots WHERE project_id=? LIMIT 1", (project_id,)).fetchone()
    conn.close()
    return row is not None


def clean_path(path: str) -> str:
    path = path.replace("\\", "/").strip("/")
    if not path or ".." in Path(path).parts or path.startswith("."):
        raise HTTPException(400, "非法工程文件路径")
    return path


def project_payload(project_id: str) -> dict[str, Any]:
    conn = db()
    try:
        conn.execute('BEGIN')
        return _project_payload_from_connection(conn,project_id)
    finally:conn.close()


def _project_payload_from_connection(conn,project_id):
    project=conn.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
    if not project:raise HTTPException(404,'项目不存在')
    files = [dict(x) for x in conn.execute("SELECT path,content,updated_at FROM files WHERE project_id=? ORDER BY path", (project_id,))]
    versions = version_map(conn, project_id)
    for item in files:
        item["version"] = versions.get(item["path"], 0)
    outline = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    outline_version = versions.get("__outline__", 0)
    terms = [dict(x) for x in conn.execute("SELECT * FROM terms WHERE project_id=? ORDER BY term", (project_id,))]
    corpus = [dict(x) for x in conn.execute("SELECT * FROM corpus WHERE project_id=? ORDER BY title", (project_id,))]
    context_omissions=[]
    for item in corpus:
        source=conn.execute('SELECT version,permission_state,status FROM corpus_source_versions WHERE corpus_id=? ORDER BY version DESC LIMIT 1',(item['id'],)).fetchone()
        item['context_eligible']=bool(source and source['permission_state']=='authorized' and source['status']=='active')
        if not item['context_eligible']:context_omissions.append({'kind':'语料授权','omitted':1,'reason':item['title']+' 的授权未知、已撤回或版本失效，不会发送。'})
    refs = [dict(x) for x in conn.execute("SELECT * FROM refs WHERE project_id=? ORDER BY cite_key", (project_id,))]
    library_refs = []
    for row in conn.execute(
        "SELECT reference_id,reference_version,cite_key,citation_snapshot_json,link_status FROM project_reference_links WHERE project_id=? ORDER BY cite_key",
        (project_id,),
    ):
        snapshot = json.loads(row["citation_snapshot_json"] or "{}")
        library_refs.append({
            "id": row["reference_id"], "cite_key": row["cite_key"],
            "title": snapshot.get("title", ""), "authors": snapshot.get("authors", ""),
            "year": snapshot.get("year", ""), "bibtex": snapshot.get("bibtex", ""),
            "source_project_id": "", "source_resource_id": row["reference_id"],
            "reference_version": row["reference_version"],
            "full_text_presence": snapshot.get("full_text_presence", "metadata_only"),
            "confidence": snapshot.get("confidence", "lowered"), "link_status": row["link_status"],
        })
    refs.extend(library_refs)
    logic = [dict(x) for x in conn.execute("SELECT * FROM logic_nodes WHERE project_id=? ORDER BY rowid", (project_id,))]
    workflow_enabled = conn.execute("SELECT 1 FROM workflow_snapshots WHERE project_id=? LIMIT 1", (project_id,)).fetchone() is not None
    setting_view = settings.read(conn, project_id)
    payload = {"project": dict(project), "files": files, "outline": outline["content"] if outline else "", "outline_version": outline_version, "terms": terms, "corpus": corpus, "refs": refs, "logic": logic, "workflow_enabled": workflow_enabled, "settings": setting_view}
    payload["ai_binding"] = provider_config.project_binding(conn, project_id)
    payload["domain"] = dict(conn.execute("SELECT * FROM domains WHERE id=?", (project["domain_id"],)).fetchone() or {})
    payload["style_profile"] = asdict(style_learning.effective_profile(conn, project_id))
    from paperpilot.services.context import material_fingerprint
    from paperpilot.services import innovation_points
    payload['materials_fingerprint']=material_fingerprint(conn,project_id)
    payload['context_omissions']=context_omissions
    innovations = innovation_points.context(conn, project_id)
    if innovations:
        payload["innovation_context"] = innovations
    return payload


@app.get("/", response_class=HTMLResponse)
@app.get("/outline", response_class=HTMLResponse)
@app.get("/terms", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/conformance", response_class=HTMLResponse)
def conformance_page() -> str:
    return (STATIC_DIR / "conformance.html").read_text(encoding="utf-8")


@app.get("/innovations", response_class=HTMLResponse)
def innovation_page() -> str:
    return (STATIC_DIR / "innovations.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    conn=db()
    try:registered=bool(conn.execute("SELECT 1 FROM ai_provider_tests t JOIN ai_provider_profiles p ON p.id=t.profile_id WHERE t.status='ready' AND p.status NOT IN ('disabled','revoked') LIMIT 1").fetchone())
    finally:conn.close()
    return {"ok": True, "service": "paper-writing-assistant", "ai_configured": registered}


@app.get("/api/config")
def config() -> dict[str, Any]:
    conn=db()
    try:
        default=conn.execute("SELECT * FROM ai_provider_defaults WHERE id='default'").fetchone()
        current=provider_runtime.configuration(conn,default['profile_id'],default['config_revision']) if default else None
        return {'base_url':current['base_url'] if current else '', 'model':default['model_id'] if default else '',
            'configured':bool(health()['ai_configured']),'provider':'OpenAI-compatible','message':'正式调用以当前论文明确绑定并通过相关测试的版本为准；环境字段不会绕过登记与预检。'}
    finally:conn.close()


@app.get("/api/ai/profiles")
def list_ai_profiles() -> list[dict[str, Any]]:
    conn = db()
    try:
        return provider_config.list_profiles(conn)
    finally:
        conn.close()


@app.post("/api/ai/profiles")
def create_ai_profile(payload: ProviderProfileIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return provider_config.create_profile(conn, payload.model_dump(), payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/ai/profiles/{profile_id}")
def update_ai_profile(profile_id: str, payload: ProviderProfileUpdateIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return provider_config.update_profile(conn, profile_id, payload.base_version, payload.changes)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/ai/profiles/{profile_id}/test")
def test_ai_profile(profile_id: str, background_tasks: BackgroundTasks, payload: ProviderTestIn | None = None) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            if payload is None:
                return provider_config.test_profile(conn, profile_id)
            result=provider_runtime.queue_test(conn,profile_id,payload.model_dump())
        if result['status']=='queued' and not result.get('idempotent_replay'):
            background_tasks.add_task(provider_runtime.process_test,db,profile_id,result['id'],payload.temporary_key or None)
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get('/api/ai/profiles/{profile_id}/tests')
def list_provider_tests(profile_id: str):
    conn=db()
    try:
        if not conn.execute('SELECT 1 FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone():raise HTTPException(404,'提供方配置不存在。')
        return [provider_runtime.public_test(row) for row in conn.execute('SELECT * FROM ai_provider_tests WHERE profile_id=? ORDER BY created_at DESC LIMIT 100',(profile_id,))]
    finally:conn.close()


@app.get("/api/domains")
def get_domains() -> list[dict[str, Any]]:
    conn = db()
    try:
        return domain_workspace.list_domains(conn)
    finally:
        conn.close()


@app.post("/api/domains")
def post_domain(payload: DomainIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            result = domain_workspace.create_domain(conn, payload.name, payload.description)
            settings.ensure_domain_settings(conn, result["id"])
            return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/domains/{domain_id}/resources")
def get_domain_resources(domain_id: str) -> list[dict[str, Any]]:
    conn = db()
    try:
        return domain_workspace.list_domain_resources(conn, domain_id)
    finally:
        conn.close()


@app.get("/api/domains/{domain_id}/terms")
def get_domain_terms(domain_id: str, version: int | None = None) -> list[dict[str, Any]]:
    from paperpilot.services import domain_reference_terms

    conn = db()
    try:
        terms = domain_workspace.list_domain_terms(conn, domain_id, version)
        return domain_reference_terms.with_provenance(conn, domain_id, terms)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/domains/{domain_id}/settings")
def update_domain_settings(domain_id: str, payload: DomainSettingsIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return settings.update_domain_settings(conn, domain_id, payload.settings, payload.expected_version, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/domains/{domain_id}/resources")
def post_domain_resource(domain_id: str, payload: DomainResourceIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return domain_workspace.add_domain_resource(conn, domain_id, payload.resource_type, payload.title, payload.content, payload.source_project_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    conn = db()
    rows = [dict(x) for x in conn.execute("SELECT * FROM projects ORDER BY updated_at DESC")]
    conn.close()
    return rows


def seed_project_workspace(conn, project_id, payload, domain_name, stamp):
    """Initialize a project in the caller's transaction, including imports."""
    main_tex = "\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n\\begin{document}\n\\section{Introduction}\n\n\\end{document}\n"
    references = "% Add BibTeX entries here\n"
    outline_text = "1. Introduction\n2. Related Work\n3. Methods\n4. Results\n5. Conclusion"
    conn.execute("INSERT INTO projects(id,name,domain,style,citation_style,created_at,updated_at,model,ai_base_url,ai_soft_budget_tokens,domain_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (project_id,payload.name,domain_name,payload.style,payload.citation_style,stamp,stamp,
                  payload.model,payload.ai_base_url,payload.ai_soft_budget_tokens,payload.domain_id))
    files = [("main.tex",main_tex),("references.bib",references)]
    conn.executemany("INSERT INTO files VALUES (?,?,?,?)",[(project_id,path,text,stamp) for path,text in files])
    conn.execute("INSERT INTO outline VALUES (?,?)",(project_id,outline_text))
    settings.ensure_project_baseline(conn,project_id)
    seed_initial_versions(conn,project_id,files,outline_text,stamp)
    create_initial_workflow(conn,project_id,outline_text,stamp)
    provider_runtime.apply_default(conn,project_id)


@app.post("/api/projects")
def create_project(payload: ProjectIn) -> dict[str, Any]:
    request_id = payload.request_id or uuid.uuid4().hex
    request_body = {
        "name": payload.name,
        "domain": payload.domain,
        "style": payload.style,
        "citation_style": payload.citation_style,
        "model": payload.model,
        "ai_base_url": payload.ai_base_url,
        "ai_soft_budget_tokens": payload.ai_soft_budget_tokens,
        "domain_id": payload.domain_id,
        "template_version": "v1",
    }
    request_fingerprint = hashlib.sha256(
        json.dumps(request_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    conn = db()
    stamp = now()
    response_info: tuple[str, dict[str, Any]] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM workspace_runs WHERE request_id=?", (request_id,)).fetchone()
        if existing and existing["request_fingerprint"] and existing["request_fingerprint"] != request_fingerprint:
            raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "request_id 已用于不同的论文创建请求。"})
        if existing and existing["status"] == "completed" and existing["project_id"]:
            conn.commit()
            response_info = (existing["project_id"], dict(existing))
        else:
            domain_name = payload.domain
            if payload.domain_id:
                selected_domain = conn.execute("SELECT name FROM domains WHERE id=?", (payload.domain_id,)).fetchone()
                if not selected_domain:
                    raise HTTPException(404, detail={"code": "domain_missing", "message": "所选领域不存在；请刷新领域列表后重试。"})
                domain_name = selected_domain["name"]
            run_id = existing["id"] if existing else uuid.uuid4().hex
            if existing:
                conn.execute(
                    "UPDATE workspace_runs SET status='running',error_code='',request_fingerprint=?,updated_at=? WHERE id=?",
                    (request_fingerprint, stamp, run_id),
                )
            else:
                conn.execute(
                    "INSERT INTO workspace_runs(id,project_id,request_id,request_fingerprint,template_version,status,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, None, request_id, request_fingerprint, "v1", "running", "", stamp, stamp),
                )
            project_id = uuid.uuid4().hex[:12]
            seed_project_workspace(conn,project_id,payload,domain_name,stamp)
            conn.execute(
                "UPDATE workspace_runs SET project_id=?,template_version='v1',status='completed',error_code='',updated_at=? WHERE id=?",
                (project_id, stamp, run_id),
            )
            conn.commit()
    except HTTPException:
        conn.rollback()
        conn.close()
        raise
    except Exception as exc:
        conn.rollback()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM workspace_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing and existing["status"] == "completed":
                response_info = (existing["project_id"], dict(existing))
                conn.commit()
            else:
                if existing:
                    conn.execute(
                        "UPDATE workspace_runs SET status='failed',error_code=?,updated_at=? WHERE id=?",
                        (type(exc).__name__, now(), existing["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO workspace_runs(id,project_id,request_id,request_fingerprint,template_version,status,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, None, request_id, request_fingerprint, "v1", "failed", type(exc).__name__, stamp, now()),
                    )
                conn.commit()
        except Exception:
            conn.rollback()
        if response_info is None:
            conn.close()
            raise HTTPException(500, "论文工作区创建失败；请求记录已保留，可使用相同 request_id 重试。") from exc
    if response_info is not None:
        conn.close()
        result = project_payload(response_info[0])
        result["workspace_run"] = response_info[1]
        return result
    conn.close()
    result = project_payload(project_id)
    run_conn = db()
    result["workspace_run"] = dict(run_conn.execute("SELECT * FROM workspace_runs WHERE request_id=?", (request_id,)).fetchone())
    run_conn.close()
    return result


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return project_payload(project_id)


@app.get("/api/projects/{project_id}/settings")
def get_project_settings(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            return settings.read(conn, project_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/settings/{key}")
def update_project_setting(project_id: str, key: str, payload: SettingOverrideIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return settings.update_override(conn, project_id, key, payload.value, payload.expected_project_revision, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/settings/{key}/restore")
def restore_project_setting(project_id: str, key: str, payload: SettingRestoreIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return settings.restore_override(conn, project_id, key, payload.expected_project_revision, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/settings/sync-preview")
def preview_project_setting_sync(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            return settings.sync_preview(conn, project_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/settings/sync")
def sync_project_settings(project_id: str, payload: SettingSyncIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return settings.sync_commit(conn, project_id, payload.expected_baseline_version, payload.selected_keys, payload.request_id, payload.expected_domain_version)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/settings/binding-repair")
def repair_project_setting_binding(project_id: str, payload: SettingBindingRepairIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return settings.repair_baseline_binding(conn, project_id, payload.expected_project_revision, payload.expected_baseline_version,
                payload.expected_project_domain_id, payload.expected_baseline_domain_id, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/ai-binding")
def bind_ai_profile(project_id: str, payload: ProviderBindingIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            result = provider_config.bind_project(conn, project_id, payload.profile_id, payload.model_id, payload.base_project_revision, payload.config_revision)
        return result | {"binding": provider_config.project_binding(conn, project_id)}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/domain")
def assign_domain(project_id: str, payload: DomainAssignIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.assign_project_domain(conn, project_id, payload.domain_id, payload.base_project_revision)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/domain-resources/copy")
def copy_domain_resource(project_id: str, payload: DomainResourceCopyIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.copy_domain_resource(conn, project_id, payload.resource_id, payload.base_project_revision)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terms/import")
def import_domain_term(project_id: str, payload: DomainTermImportIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.import_domain_term(
                conn, project_id, payload.domain_term_id, payload.source_domain_version,
                payload.base_project_revision, payload.request_id,
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terms/promote/preview")
def preview_term_promotion(project_id: str, payload: TermPromotePreviewIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.preview_term_promotion(conn, project_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terms/promote/commit")
def commit_term_promotion(project_id: str, payload: TermPromoteCommitIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.commit_term_promotion(conn, project_id, payload.preview_id, payload.request_id,
                selected_term_ids=payload.selected_term_ids, conflict_decisions=payload.conflict_decisions)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/terminology/reviews")
def list_term_reviews(project_id: str, status: str | None = None) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return domain_workspace.list_classification_reviews(conn, project_id, status)
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terminology/reviews")
def create_term_review(project_id: str, payload: TermClassificationReviewIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.create_classification_review(conn, project_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terminology/reviews/{review_id}/decision")
def decide_term_review(project_id: str, review_id: str, payload: TermClassificationDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return domain_workspace.decide_classification_review(conn, project_id, review_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, payload: dict[str, str]) -> dict[str, Any]:
    allowed = {"name", "domain", "style", "citation_style", "model", "ai_base_url", "ai_soft_budget_tokens"}
    changes: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "ai_soft_budget_tokens":
            changes[key] = int(value) if value not in (None, "") else None
        else:
            changes[key] = str(value)
    if not changes:
        raise HTTPException(400, "没有可更新的项目字段")
    stamp = now(); conn = db()
    try:
        with conn:
            project = conn.execute("SELECT lifecycle_state FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise HTTPException(404, "项目不存在")
            if project["lifecycle_state"] == "published":
                raise HTTPException(409, detail={"code": "project_published_read_only", "message": "已发布论文为只读状态；撤回发布后才能编辑。"})
            assignments = ",".join(f"{key}=?" for key in changes)
            conn.execute(f"UPDATE projects SET {assignments},updated_at=?,project_revision=project_revision+1 WHERE id=?", [*changes.values(), stamp, project_id])
    finally:
        conn.close()
    return project_payload(project_id)


@app.put("/api/projects/{project_id}/archive")
def archive_project(project_id: str) -> dict[str, Any]:
    project = project_or_404(project_id)
    archived = 0 if int(project["archived"] or 0) else 1
    stamp = now(); conn = db()
    jobs_to_cancel = []
    try:
        with conn:
            conn.execute("UPDATE projects SET archived=?,updated_at=? WHERE id=?", (archived, stamp, project_id))
            if archived:
                conn.execute("UPDATE academic_searches SET status='superseded',updated_at=? WHERE project_id=? AND status='loading'",(stamp,project_id))
                jobs_to_cancel = [r['id'] for r in conn.execute("SELECT id FROM build_jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,))]
                for job_id in jobs_to_cancel:
                    build_snapshots.cancel_job(conn, project_id, job_id, 'archive-' + stamp)
    finally:
        conn.close()
    if jobs_to_cancel:
        from paperpilot.adapters.latex import cancel_compile
        for job_id in jobs_to_cancel:
            cancel_compile(job_id)
        conn = db()
        try:
            with conn:
                build_snapshots.recover_project_runtime(conn, project_id, PROJECTS_DIR, job_ids=set(jobs_to_cancel), force=True)
        finally:
            conn.close()
    return {"ok": True, "archived": bool(archived)}


@app.get("/api/projects/{project_id}/lifecycle")
def get_project_lifecycle(project_id: str) -> dict[str, Any]:
    project = project_or_404(project_id)
    conn = db()
    try:
        return {
            "lifecycle_state": project["lifecycle_state"],
            "project_revision": project["project_revision"],
            "archived": bool(project["archived"]),
            "release_check": latest_release_check(conn, project_id),
        }
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/lifecycle")
def update_project_lifecycle(project_id: str, payload: ProjectLifecycleCommandIn) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            result = apply_lifecycle_command(
                conn, project_id, command=payload.command, request_id=payload.request_id,
                base_version=payload.base_version, continue_reason=payload.continue_reason,
            )
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/library/projects")
def search_library_projects(query: str = "", scope: str = "current", current_project_id: str | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    if scope not in {"current", "global", "archived"}:
        raise HTTPException(422, detail={"code": "invalid_scope", "message": "scope 必须是 current、global 或 archived。"})
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    text = str(query or "").strip()
    conn = db()
    try:
        filters: list[str] = []
        params: list[Any] = []
        if scope == "current":
            if not current_project_id:
                return {"scope": scope, "items": [], "total": 0, "limit": limit, "offset": offset}
            filters.append("id=?")
            params.append(current_project_id)
        elif scope == "archived":
            filters.append("archived=1")
        else:
            filters.append("archived=0")
        if text:
            like = f"%{text}%"
            filters.append("(lower(name) LIKE ? OR lower(domain) LIKE ? OR lower(style) LIKE ?)")
            params.extend([like, like, like])
        where = " AND ".join(filters) if filters else "1=1"
        items = [dict(row) for row in conn.execute(f"SELECT * FROM projects WHERE {where} ORDER BY updated_at DESC,id LIMIT ? OFFSET ?", [*params, limit, offset])]
        total = int(conn.execute(f"SELECT COUNT(*) FROM projects WHERE {where}", params).fetchone()[0])
        return {"scope": scope, "items": items, "total": total, "limit": limit, "offset": offset}
    finally:
        conn.close()


def _reference_draft_payload(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
    item["source_trace"] = json.loads(item.pop("source_trace_json", "{}") or "{}")
    return item


@app.get("/api/references")
def get_reference_library(query: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
    conn = db()
    try:
        return list_references(conn, query, limit=min(max(limit, 1), 100), offset=max(offset, 0))
    finally:
        conn.close()


@app.post("/api/references")
def create_library_reference(payload: ReferenceRecordIn) -> dict[str, Any]:
    metadata = payload.model_dump()
    conn = db()
    try:
        with conn:
            duplicates = find_possible_duplicates(conn, metadata)
            record = create_reference(conn, metadata)
            record["possible_duplicates"] = duplicates
        return record
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/references/drafts/{draft_id}")
def read_reference_draft(draft_id: str) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute("SELECT * FROM reference_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "待确认参考文献不存在")
        result = _reference_draft_payload(row)
        result["possible_duplicates"] = find_possible_duplicates(conn, result["metadata"])
        return result
    finally:
        conn.close()


@app.post("/api/references/drafts/{draft_id}/confirm")
def confirm_reference_draft(draft_id: str, payload: ReferenceDraftConfirmIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return confirm_draft(conn, draft_id, payload.request_id, payload.decision, payload.merge_into_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/reference-drafts")
def list_project_reference_drafts(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return [_reference_draft_payload(row) for row in conn.execute("SELECT * FROM reference_drafts WHERE project_id=? ORDER BY created_at DESC", (project_id,))]
    finally:
        conn.close()


@app.get("/api/references/{reference_id}")
def get_library_reference(reference_id: str) -> dict[str, Any]:
    conn = db()
    try:
        return read_reference(conn, reference_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/references/{reference_id}")
def revise_library_reference(reference_id: str, payload: ReferenceVersionIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            result = update_reference(conn, reference_id, payload.base_version, payload.changes)
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/references/{reference_id}/merge")
def merge_library_references(reference_id: str, payload: ReferenceMergeIn) -> dict[str, Any]:
    conn = db()
    try:
        with conn:
            return merge_references(conn, reference_id, payload.target_reference_id, payload.base_version, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/references/{reference_id}")
def add_project_reference(project_id: str, reference_id: str, payload: ReferenceLinkIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return link_reference(conn, project_id, reference_id, payload.reference_version, payload.cite_key, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, detail={"code": "reference_link_conflict", "message": "引用键已存在或参考文献版本不可用。"}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/references/{reference_id}/refresh")
def refresh_project_reference(project_id: str, reference_id: str, payload: ReferenceRefreshIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return refresh_reference_link(conn, project_id, reference_id, payload.reference_version, payload.base_version, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/references/{reference_id}/attachments")
def attach_reference_full_text(project_id: str, reference_id: str, payload: ReferenceAttachmentIn) -> dict[str, Any]:
    if payload.project_id != project_id:
        raise HTTPException(422, detail={"code": "project_scope_mismatch", "message": "附件项目必须与路径项目一致。"})
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return attach_reference_evidence(conn, project_id, reference_id, payload.external_evidence_id, payload.base_version)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/references/{reference_id}")
def unlink_project_reference(project_id: str, reference_id: str) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            link = conn.execute("SELECT * FROM project_reference_links WHERE project_id=? AND reference_id=?", (project_id, reference_id)).fetchone()
            if not link:
                raise HTTPException(404, "论文未关联该参考文献")
            if link["link_status"] == "unlinked":
                return {"ok": True, "project_id": project_id, "reference_id": reference_id, "status": "unlinked", "idempotent_replay": True}
            stamp = now()
            conn.execute("UPDATE project_reference_links SET link_status='unlinked',updated_at=? WHERE project_id=? AND reference_id=?", (stamp, project_id, reference_id))
            conn.execute("INSERT INTO project_reference_link_history(id,project_id,reference_id,reference_version,cite_key,citation_snapshot_json,request_id,action,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, reference_id, link["reference_version"], link["cite_key"], link["citation_snapshot_json"], f"unlink:{uuid.uuid4().hex}", "unlinked", stamp))
            conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (now(), project_id))
        return {"ok": True, "project_id": project_id, "reference_id": reference_id, "status": "unlinked"}
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/files")
def save_file(project_id: str, payload: FileIn) -> dict[str, Any]:
    require_project_writable(project_id)
    path = clean_path(payload.path)
    stamp = now(); conn = db()
    try:
        require_live_project_write(conn, project_id)
        if has_workflow(project_id):
            if payload.base_version is None:
                raise ProjectServiceError(422, "base_version_required", "工作流项目保存文件必须携带 base_version。")
            with conn:
                result = save_content(conn, project_id, "file", path, payload.content, expected_version=payload.base_version)
                mode = conn.execute("SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'", (project_id,)).fetchone()
                if mode and not result["unchanged"]:
                    version = result["version"]
                    record_event(conn, project_id, mode_id=mode["id"], action_type="manual_edit", scope={"kind": "file", "path": path}, base_file_versions={path: version - 1}, status="checkpoint")
            version = result["version"]
        else:
            conn.execute("INSERT INTO files(project_id,path,content,updated_at) VALUES(?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at", (project_id, path, payload.content, stamp))
            touch_project(conn, project_id); conn.commit(); version = None
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()
    return {"ok": True, "path": path, "updated_at": stamp, "version": version}


@app.put("/api/projects/{project_id}/editor/save")
def save_editor_file(project_id: str, payload: FileIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if payload.base_version is None or not payload.request_id:
        raise HTTPException(422, detail={"code": "editor_save_identity_required", "message": "编辑器保存必须携带 base_version 和 request_id。"})
    path = clean_path(payload.path)
    conn = db()
    try:
        with conn:
            require_live_project_write(conn, project_id)
            mode = None
            if has_workflow(project_id):
                mode = conn.execute(
                    "SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'",
                    (project_id,),
                ).fetchone()
            result = editor_service.save(
                conn, project_id, path, payload.content, payload.base_version, payload.request_id,
            )
            if mode and not result.get("idempotent_replay") and not result.get("unchanged"):
                version = int(result["version"])
                record_event(
                    conn, project_id, mode_id=mode["id"], action_type="manual_edit",
                    scope={"kind": "file", "path": path},
                    base_file_versions={path: int(payload.base_version)}, status="checkpoint",
                )
        result["updated_at"] = now()
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/editor/draft")
def save_editor_draft(project_id: str, payload: EditorDraftIn) -> dict[str, Any]:
    require_project_writable(project_id)
    path = clean_path(payload.path)
    conn = db()
    try:
        with conn:
            return editor_service.save_draft(
                conn, project_id, path, payload.base_version, payload.base_content, payload.content,
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/editor/draft")
def read_editor_draft(project_id: str, path: str) -> dict[str, Any]:
    project_or_404(project_id)
    clean = clean_path(path)
    conn = db()
    try:
        return editor_service.get_draft(conn, project_id, clean) or {"path": clean, "exists": False}
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/editor/draft")
def delete_editor_draft(project_id: str, path: str) -> dict[str, Any]:
    require_project_writable(project_id)
    clean = clean_path(path)
    conn = db()
    try:
        with conn:
            return editor_service.discard_draft(conn, project_id, clean)
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/editor/conversions/preview")
def preview_editor_conversion(project_id: str, payload: EditorConversionPreviewIn) -> dict[str, Any]:
    require_project_writable(project_id)
    source_path, target_path = clean_path(payload.source_path), clean_path(payload.target_path)
    if source_path == target_path:
        raise HTTPException(422, detail={"code": "editor_conversion_same_target", "message": "转换目标必须是另一个文件。"})
    conn = db()
    try:
        with conn:
            return editor_service.preview_conversion(
                conn, project_id, request_id=payload.request_id, source_path=source_path,
                source_version=payload.source_version, target_path=target_path, direction=payload.direction,
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/editor/conversions/{preview_id}/apply")
def apply_editor_conversion(project_id: str, preview_id: str, payload: EditorConversionApplyIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            require_live_project_write(conn, project_id)
            mode = None
            if has_workflow(project_id):
                mode = conn.execute(
                    "SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'",
                    (project_id,),
                ).fetchone()
            preview = conn.execute(
                "SELECT * FROM editor_conversion_previews WHERE project_id=? AND id=?",
                (project_id, preview_id),
            ).fetchone()
            if preview is None:
                raise ProjectServiceError(404, "editor_conversion_missing", "转换预览不存在。")
            result = editor_service.apply_conversion(conn, project_id, preview_id, payload.request_id)
            if mode and not result.get("idempotent_replay") and not result.get("unchanged"):
                record_event(
                    conn, project_id, mode_id=mode["id"], action_type="manual_edit",
                    scope={"kind": "file", "path": result["path"]},
                    base_file_versions={result["path"]: int(preview["target_version"])}, status="checkpoint",
                )
            return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/files/{path:path}")
def delete_file(project_id: str, path: str, base_version: int | None = None) -> dict[str, Any]:
    require_project_writable(project_id)
    path = clean_path(path)
    if path == "main.tex":
        raise HTTPException(400, "不能删除主文件 main.tex")
    conn = db()
    try:
        require_live_project_write(conn, project_id)
        if has_workflow(project_id):
            if base_version is None:
                raise ProjectServiceError(422, "base_version_required", "工作流项目删除文件必须携带 base_version。")
            with conn:
                result = delete_content(conn, project_id, path, base_version)
                mode = conn.execute("SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'", (project_id,)).fetchone()
                if mode:
                    record_event(conn, project_id, mode_id=mode["id"], action_type="file_delete", scope={"kind": "file", "path": path}, base_file_versions={path: base_version}, status="checkpoint")
            return {"ok": True, "path": path, **result}
        result = conn.execute("DELETE FROM files WHERE project_id=? AND path=?", (project_id, path))
        if result.rowcount:
            touch_project(conn, project_id)
        conn.commit()
        if not result.rowcount:
            raise HTTPException(404, "工程文件不存在")
        return {"ok": True, "path": path}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/outline")
def save_outline(project_id: str, payload: TextIn) -> dict[str, Any]:
    if payload.base_version is None:
        raise HTTPException(422,detail={"code":"base_version_required","message":"保存大纲需要基础版本；请刷新后比较内容再重试。"})
    conn=db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        require_live_project_write(conn, project_id)
        project=conn.execute('SELECT lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
        if not project:raise HTTPException(404,'项目不存在')
        if project['lifecycle_state']=='published':
            raise HTTPException(409,detail={'code':'project_published_read_only','message':'已发布论文为只读状态；撤回发布后才能编辑。'})
        workflow=conn.execute("SELECT 1 FROM workflow_snapshots WHERE project_id=? LIMIT 1",(project_id,)).fetchone()
        if workflow:
            result=save_content(conn,project_id,"outline","",payload.content,expected_version=payload.base_version)
            mode=conn.execute("SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'",(project_id,)).fetchone()
            if mode and not result['unchanged']:
                record_event(conn,project_id,mode_id=mode['id'],action_type='manual_edit',scope={'kind':'outline'},base_file_versions={'__outline__':result['version']-1},status='checkpoint')
        else:
            latest=current_version(conn,project_id,'outline','')
            if latest!=payload.base_version:
                raise ProjectServiceError(409,'content_conflict','大纲版本已变化；当前输入未覆盖，请刷新并比较后再保存。')
            if latest==0:
                old=conn.execute('SELECT content FROM outline WHERE project_id=?',(project_id,)).fetchone()
                original=old['content'] if old else ''
                if not old:conn.execute('INSERT INTO outline(project_id,content) VALUES(?,?)',(project_id,original))
                conn.execute("INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,project_id,'outline','',1,None,None,'legacy_outline_baseline',original,now()))
                latest=1
            result=save_content(conn,project_id,'outline','',payload.content,expected_version=latest,source='legacy_outline_edit')
        conn.commit()
        return {'ok':True,**result}
    except ProjectServiceError as exc:
        conn.rollback();raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
    except Exception:
        conn.rollback();raise
    finally:conn.close()


@app.get("/api/projects/{project_id}/outline/structured")
def get_structured_outline(project_id: str, version: int | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        graph = outline_service.read_outline(conn, project_id, version)
        refs = _innovation_points.usage_references(conn, project_id, "outline", str(graph["version"]), graph["version"])
        if refs:
            graph["innovation_refs"] = refs
            graph["innovation_review_required"] = not _innovation_points.references_current(conn, project_id, refs)
        return graph
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/outline/versions")
def list_outline_versions(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return outline_service.list_versions(conn, project_id, limit)
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/outline/resources")
def list_outline_resources(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return outline_service.list_linkable_resources(conn, project_id)
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/versions/{version}/restore")
def restore_outline_version(project_id: str, version: int, payload: OutlineRestoreIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.restore_version(
                conn, project_id, version, payload.base_version, payload.request_id
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/migration/preview")
def preview_outline_migration(project_id: str, payload: OutlineMigrationIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.preview_migration(conn, project_id, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/migration/commit")
def commit_outline_migration(project_id: str, payload: OutlineMigrationCommitIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.commit_migration(conn, project_id, payload.draft_id, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/nodes")
def outline_node_command(project_id: str, payload: OutlineNodeCommandIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.node_command(conn, project_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/candidates")
def create_outline_candidate(project_id: str, payload: OutlineCandidateIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.create_candidate(conn, project_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/outline/candidates/{candidate_id}/decision")
def decide_outline_candidate(project_id: str, candidate_id: str, payload: OutlineCandidateDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            return outline_service.decide_candidate(conn, project_id, candidate_id, payload.model_dump())
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/outline/draft-recovery")
def list_outline_recovery_drafts(project_id: str, kind: str, node_id: str = '', offset: int = 0, limit: int = 30) -> dict[str, Any]:
    project_or_404(project_id)
    if kind not in {'editor','node'} or (kind=='node' and not node_id) or offset<0 or not 1<=limit<=100:
        raise HTTPException(422,detail={'code':'outline_draft_query_invalid','message':'请选择有效的草稿类型、节点和页码。'})
    conn=db()
    try:
        if kind=='editor':
            where="(draft_key='editor' OR draft_key GLOB 'tab:*:editor')"
            args=[project_id]
        else:
            where="(draft_key=? OR (draft_key GLOB 'tab:*' AND CASE WHEN json_valid(content_json) THEN json_extract(content_json,'$.node_id') END=?))"
            args=[project_id,'node:'+node_id,node_id]
        rows=conn.execute(f"SELECT draft_key,base_version,content_json,updated_at FROM outline_local_drafts WHERE project_id=? AND {where} ORDER BY updated_at DESC,draft_key DESC LIMIT ? OFFSET ?",(*args,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            try:content=json.loads(row['content_json'] or '{}')
            except (TypeError,ValueError):content={}
            if not isinstance(content,dict):content={}
            node=content.get('node') if isinstance(content.get('node'),dict) else {}
            preview=str(content.get('text','') if kind=='editor' else node.get('title',''))[:100]
            items.append({'draft_key':row['draft_key'],'base_version':int(row['base_version']),'updated_at':row['updated_at'],'preview':preview})
        return {'drafts':items,'offset':offset,'limit':limit,'has_more':len(rows)>limit}
    finally:conn.close()


@app.get("/api/projects/{project_id}/outline/drafts/{draft_key}")
def read_outline_draft(project_id: str, draft_key: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        result = outline_service.get_local_draft(conn, project_id, draft_key)
        if result is None:
            raise HTTPException(404, "本地大纲草稿不存在")
        return result
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/outline/drafts/{draft_key}")
def delete_outline_draft(project_id: str, draft_key: str, if_updated_at: str | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            if if_updated_at is None:
                deleted = conn.execute("DELETE FROM outline_local_drafts WHERE project_id=? AND draft_key=?",(project_id,draft_key))
            else:
                deleted = conn.execute("DELETE FROM outline_local_drafts WHERE project_id=? AND draft_key=? AND updated_at=?",(project_id,draft_key,if_updated_at))
                if not deleted.rowcount and conn.execute("SELECT 1 FROM outline_local_drafts WHERE project_id=? AND draft_key=?",(project_id,draft_key)).fetchone():
                    raise HTTPException(409,detail={'code':'outline_draft_changed','message':'草稿已被其他页面更新；请刷新列表并重新确认。'})
        return {"ok": True, "deleted": deleted.rowcount == 1}
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/outline/drafts/{draft_key}")
def write_outline_draft(project_id: str, draft_key: str, payload: OutlineDraftIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if payload.draft_key != draft_key:
        raise HTTPException(422, "draft_key 与路径不一致")
    conn = db()
    try:
        with conn:
            return outline_service.save_local_draft(conn, project_id, draft_key, payload.base_version, payload.content)
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/terms")
def add_term(project_id: str, payload: TermIn) -> dict[str, Any]:
    from paperpilot.services import project_resources
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            return project_resources.add_term(conn, project_id, payload.model_dump())
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/corpus")
def add_corpus(project_id: str, payload: CorpusIn) -> dict[str, Any]:
    from paperpilot.services import project_resources
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            result = project_resources.add_corpus(conn, project_id, payload.model_dump())
        return result | {"source_version": result["source_version"]["version"]}
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/corpus/{corpus_id}/lineage")
def get_corpus_lineage(project_id: str, corpus_id: str) -> dict[str, Any]:
    project_or_404(project_id); conn = db()
    try:
        with conn:
            return corpus_lifecycle.lineage(conn, project_id, corpus_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/corpus/{corpus_id}/index")
def rebuild_corpus_index(project_id: str, corpus_id: str, chunk_size: int = 1200) -> dict[str, Any]:
    require_project_writable(project_id); conn = db()
    try:
        with conn:
            return corpus_lifecycle.rebuild_index(conn, project_id, corpus_id, chunk_size=chunk_size)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/corpus/search")
def search_corpus(project_id: str, query: str, corpus_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    project_or_404(project_id); conn = db()
    try:
        with conn:
            return corpus_lifecycle.search_index(conn, project_id, query, corpus_id, limit)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/corpus/{corpus_id}/curations")
def create_corpus_curation(project_id: str, corpus_id: str, payload: CorpusCurationIn) -> dict[str, Any]:
    require_project_writable(project_id); conn = db()
    try:
        with conn:
            return corpus_lifecycle.create_curation(conn, project_id, corpus_id, payload.base_source_version, payload.content, payload.notes, payload.kind)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/corpus/{corpus_id}/refresh")
def create_corpus_refresh(project_id: str, corpus_id: str, payload: CorpusRefreshIn) -> dict[str, Any]:
    require_project_writable(project_id); conn = db()
    try:
        with conn:
            return corpus_lifecycle.create_refresh_candidate(conn, project_id, corpus_id, payload.base_source_version, payload.content, payload.source, payload.permission_state, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/corpus/{corpus_id}/refresh/{candidate_id}/decision")
def decide_corpus_refresh(project_id: str, corpus_id: str, candidate_id: str, payload: CorpusRefreshDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id); conn = db()
    try:
        with conn:
            candidate = conn.execute("SELECT corpus_id FROM corpus_refresh_candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None or candidate["corpus_id"] != corpus_id:
                raise HTTPException(404, detail={"code": "refresh_candidate_missing", "message": "刷新候选不存在。"})
            return corpus_lifecycle.decide_refresh(conn, project_id, candidate_id, payload.decision, payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/refs")
def add_ref(project_id: str, payload: RefIn) -> dict[str, Any]:
    require_project_writable(project_id); item_id = uuid.uuid4().hex[:12]; conn = db()
    conn.execute("INSERT INTO refs(id,project_id,cite_key,title,authors,year,bibtex,source_project_id,source_resource_id) VALUES (?,?,?,?,?,?,?,?,?)", (item_id, project_id, payload.cite_key, payload.title, payload.authors, payload.year, payload.bibtex, '', '')); touch_project(conn, project_id); conn.commit(); conn.close()
    return {"id": item_id, **payload.model_dump()}


@app.get("/api/projects/{project_id}/logic")
def get_logic(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id); conn = db(); rows = [dict(x) for x in conn.execute("SELECT * FROM logic_nodes WHERE project_id=? ORDER BY rowid", (project_id,))]; conn.close(); return rows


def _sync_legacy_logic_projection(conn: sqlite3.Connection, project_id: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    node_ids = {item["id"] for item in nodes}
    for node_id in node_ids:
        owner = conn.execute("SELECT project_id FROM logic_nodes WHERE id=?", (node_id,)).fetchone()
        if owner and owner["project_id"] != project_id:
            raise ProjectServiceError(409, "node_scope_conflict", "节点 ID 属于另一篇论文，不能跨项目引用或覆盖。")
    if node_ids:
        placeholders = ",".join("?" for _ in node_ids)
        conn.execute(f"DELETE FROM logic_nodes WHERE project_id=? AND id NOT IN ({placeholders})", [project_id, *sorted(node_ids)])
    else:
        conn.execute("DELETE FROM logic_nodes WHERE project_id=?", (project_id,))
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["from_node_id"], []).append(edge["to_node_id"])
    for node in nodes:
        links = ",".join(outgoing.get(node["id"], [])) or str(node.get("legacy_links") or "")
        conn.execute(
            "INSERT INTO logic_nodes(id,project_id,kind,label,details,links) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,label=excluded.label,details=excluded.details,links=excluded.links",
            (node["id"], project_id, node["kind"], node["label"], node.get("details", ""), links),
        )


@app.post("/api/projects/{project_id}/logic")
def add_logic(project_id: str, payload: LogicIn) -> dict[str, Any]:
    require_project_writable(project_id); item_id = uuid.uuid4().hex[:12]; conn = db()
    try:
        with conn:
            graph = read_graph(conn, project_id)
            linked_ids = list(dict.fromkeys(value.strip() for value in payload.links.split(",") if value.strip()))
            existing_ids = {node["id"] for node in graph["nodes"]}
            valid_ids = [value for value in linked_ids if value in existing_ids]
            unresolved_ids = [value for value in linked_ids if value not in existing_ids]
            nodes = [*graph["nodes"], {
                "id": item_id, "kind": payload.kind, "label": payload.label,
                "details": payload.details, "status": "active", "legacy_links": ",".join(unresolved_ids),
            }]
            edges = [
                *graph["edges"],
                *[
                    {"id": uuid.uuid4().hex, "from_node_id": item_id, "to_node_id": target, "edge_type": "supports"}
                    for target in valid_ids
                ],
            ]
            saved = save_graph(conn, project_id, base_version=graph["version"], request_id=f"legacy-add:{uuid.uuid4().hex}", nodes=nodes, edges=edges)
            _sync_legacy_logic_projection(conn, project_id, saved["nodes"], saved["edges"])
        return {"id": item_id, **payload.model_dump()}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/logic/graph")
def get_logic_graph(project_id: str, version: int | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        return read_graph(conn, project_id, version)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/logic/graph")
def put_logic_graph(project_id: str, payload: LogicGraphIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            saved = save_graph(
                conn, project_id, base_version=payload.base_version,
                request_id=payload.request_id, nodes=payload.nodes, edges=payload.edges,
            )
            _sync_legacy_logic_projection(conn, project_id, saved["nodes"], saved["edges"])
        return saved
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


def _logic_suggestion_preview(project_id,payload):
    require_project_writable(project_id)
    if not has_workflow(project_id):raise HTTPException(409,detail={'code':'workflow_required','message':'逻辑建议须在有效论文工作流内生成，不能使用未登记的旧接口直接外发。'})
    bundle=project_payload(project_id);conn=db()
    try:
        graph=read_graph(conn,project_id)
        snapshot=build_ai_context_snapshot(bundle['project'],bundle,workflow=workflow_projection(conn,project_id),task={'action':'term_logic_check','prompt':payload.prompt,'content':json.dumps(graph,ensure_ascii=False)})
        admission=provider_runtime.request_provider(conn,project_id,snapshot['model'])
        prompt=('检查此论文逻辑图并只返回 JSON 对象 {"operations":[...]}。可用操作：add_node {node:{id,kind,label,details,status}}、update_node {id,changes}、add_edge {edge:{id,from_node_id,to_node_id,edge_type}}、remove_edge {id}。建议须引用现有节点ID；不改变正文，没有建议则operations为空。\n用户补充：'+payload.prompt)
        prompt,_=redact_sensitive_text(prompt);content,_=redact_sensitive_text(json.dumps(graph,ensure_ascii=False))
        estimated=estimate_tokens(prompt+content+snapshot['text'])+256
        output=_positive_env_int('PWA_AI_MAX_OUTPUT_TOKENS',4000);limit=_positive_env_int('PWA_AI_MAX_JOB_TOKENS',16000)
        capacity=provider_runtime.admit_tokens({'model_limits':admission['model_limits']},admission['model'],estimated,output)
        fp=hashlib.sha256(json.dumps({'context':snapshot['version'],'graph':content,'prompt':prompt,'provider':admission},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return {'preview_fingerprint':fp,'provider':admission,'sources':snapshot['sources'],'omissions':snapshot['omissions'],'capacity':capacity,'cost_status':'unknown',
            'estimated_input_tokens':estimated,'max_output_tokens':output,'max_job_tokens':limit,'generation_allowed':estimated+output<=limit,
            'outbound_preview':prompt+'\n逻辑图：'+content+'\n论文上下文：'+snapshot['text'],'_prompt':prompt,'_content':content,'_context':snapshot['text'],'_graph':graph}
    except ProjectServiceError as exc:raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
    finally:conn.close()


@app.post('/api/projects/{project_id}/logic/suggestions/preview')
def preview_logic_suggestion(project_id:str,payload:LogicSuggestionIn):
    return {k:v for k,v in _logic_suggestion_preview(project_id,payload).items() if not k.startswith('_')}


@app.post("/api/projects/{project_id}/logic/suggestions")
def create_logic_suggestion(project_id: str, payload: LogicSuggestionIn) -> dict[str, Any]:
    project = require_project_writable(project_id)
    input_hash = hashlib.sha256(json.dumps({'prompt':payload.prompt,'preview_fingerprint':payload.preview_fingerprint},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    conn = db()
    try:
        existing = conn.execute("SELECT * FROM logic_suggestions WHERE project_id=? AND request_id=?", (project_id, payload.request_id)).fetchone()
        if existing:
            if existing["input_hash"] and existing["input_hash"] != input_hash:
                raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "request_id 已用于不同的逻辑建议请求。"})
            operations = json.loads(existing["operations_json"] or "[]")
            return {"id": existing["id"], "base_version": existing["base_version"], "operations": operations, "status": existing["status"], "idempotent_replay": True}
        graph = read_graph(conn, project_id)
    finally:
        conn.close()
    preview=_logic_suggestion_preview(project_id,payload)
    if not payload.consent_to_send or payload.preview_fingerprint!=preview['preview_fingerprint']:
        raise HTTPException(409,detail={'code':'logic_preflight_required','message':'请先预览接收方、完整数据范围和预算，并明确授权；旧预览不能继续发送。'})
    graph=preview['_graph'];conn=db()
    try:
        response=provider_runtime.execute_call(conn,project_id,payload.request_id,preview['provider'],'term_logic_check',preview['_prompt'],preview['_content'],context=preview['_context'],
            max_tokens=preview['max_output_tokens'],max_job_tokens=preview['max_job_tokens'],temporary_key=payload.temporary_key or None,call=call_ai,scope={'preview_fingerprint':preview['preview_fingerprint']},
            guard=lambda:_logic_suggestion_preview(project_id,payload)['preview_fingerprint']==preview['preview_fingerprint'])
        if _logic_suggestion_preview(project_id,payload)['preview_fingerprint']!=preview['preview_fingerprint']:
            raise HTTPException(409,detail={'code':'logic_result_stale','message':'生成期间图、资料授权或配置发生变化，结果未保存；请重新预览。'})
    except ProjectServiceError as exc:raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
    finally:conn.close()
    if not isinstance(response, dict) or not response.get("ok"):
        message = response.get("error", "AI 建议暂时不可用。") if isinstance(response, dict) else "AI 建议响应格式无效。"
        raise HTTPException(502, detail={"code": "logic_suggestion_failed", "message": message})
    text = str(response.get("content") or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise ValueError("suggestion must be an object")
        operations = decoded.get("operations", [])
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        preview_nodes, preview_edges = apply_operations(graph, operations)
    except (TypeError, ValueError, json.JSONDecodeError, ProjectServiceError) as exc:
        message = exc.message if isinstance(exc, ProjectServiceError) else "AI 返回的逻辑建议格式无效。"
        raise HTTPException(502, detail={"code": "invalid_logic_suggestion", "message": message}) from exc
    suggestion_id = uuid.uuid4().hex
    stamp = now()
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current_project = conn.execute("SELECT lifecycle_state FROM projects WHERE id=?", (project_id,)).fetchone()
            if current_project["lifecycle_state"] == "published":
                raise HTTPException(409, detail={"code": "project_published_read_only", "message": "论文已发布；建议未写入，请撤回发布后重试。"})
            conn.execute(
                "INSERT INTO logic_suggestions(id,project_id,request_id,base_version,input_hash,operations_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (suggestion_id, project_id, payload.request_id, graph["version"], input_hash, json.dumps(operations, ensure_ascii=False), "pending", stamp),
            )
            refs = _innovation_points.manifest_references(preview["sources"])
            if not _innovation_points.references_current(conn, project_id, refs):
                raise HTTPException(409, detail={"code": "innovation_context_stale", "message": "创新点已变化，请重新生成论证建议。"})
            _innovation_points.record_usage(conn, project_id, refs, "logic_candidate", suggestion_id,
                                            graph["version"], "论证建议")
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT * FROM logic_suggestions WHERE project_id=? AND request_id=?", (project_id, payload.request_id)).fetchone()
        if row is None:
            raise
        if row["input_hash"] != input_hash:
            raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "request_id 已用于不同的逻辑建议请求。"})
        return {"id": row["id"], "base_version": row["base_version"], "operations": json.loads(row["operations_json"] or "[]"), "status": row["status"], "idempotent_replay": True}
    finally:
        conn.close()
    return {"id": suggestion_id, "base_version": graph["version"], "operations": operations, "preview": {"nodes": preview_nodes, "edges": preview_edges}, "status": "pending"}


@app.post("/api/projects/{project_id}/logic/suggestions/{suggestion_id}/decision")
def decide_logic_suggestion(project_id: str, suggestion_id: str, payload: LogicSuggestionDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            row = conn.execute("SELECT * FROM logic_suggestions WHERE id=? AND project_id=?", (suggestion_id, project_id)).fetchone()
            if row is None:
                raise ProjectServiceError(404, "logic_suggestion_missing", "逻辑建议不存在。")
            decision = {"decision": payload.decision, "selected_indices": payload.selected_indices, "base_version": payload.base_version}
            decision_json = json.dumps(decision, ensure_ascii=False, sort_keys=True)
            if row["decision_request_id"]:
                if row["decision_request_id"] != payload.request_id or row["decision_json"] != decision_json:
                    raise ProjectServiceError(409, "suggestion_already_decided", "该建议已被另一项操作处理。")
                graph = read_graph(conn, project_id)
                return {"ok": True, "decision": payload.decision, "graph": graph, "idempotent_replay": True}
            if row["status"] != "pending":
                raise ProjectServiceError(409, "suggestion_not_open", "该建议已不处于待审阅状态。")
            graph = read_graph(conn, project_id)
            if payload.decision == "reject":
                conn.execute("UPDATE logic_suggestions SET status='rejected',decision_request_id=?,decision_json=?,decided_at=? WHERE id=?", (payload.request_id, decision_json, now(), suggestion_id))
                return {"ok": True, "decision": "reject", "graph": graph}
            refs = _innovation_points.usage_references(conn, project_id, "logic_candidate", suggestion_id)
            if not _innovation_points.references_current(conn, project_id, refs):
                raise ProjectServiceError(409, "innovation_context_stale", "创新点已变化，请重新生成论证建议。")
            if graph["version"] != payload.base_version or int(row["base_version"]) != payload.base_version:
                raise ProjectServiceError(409, "suggestion_stale", "逻辑图已变化；此建议不能直接应用，请重新生成。")
            operations = json.loads(row["operations_json"] or "[]")
            selected = None
            if payload.decision == "partial_apply":
                if not payload.selected_indices:
                    raise ProjectServiceError(422, "selection_required", "部分采纳必须至少选择一项建议。")
                selected = set(payload.selected_indices)
                if min(selected) < 0 or max(selected) >= len(operations):
                    raise ProjectServiceError(422, "invalid_selection", "所选建议序号无效。")
            nodes, edges = apply_operations(graph, operations, selected)
            saved = save_graph(conn, project_id, base_version=payload.base_version, request_id=f"logic-suggestion:{suggestion_id}:{payload.request_id}", nodes=nodes, edges=edges)
            _innovation_points.record_usage(conn, project_id, refs, "logic", str(saved["version"]),
                                            saved["version"], "已采纳的论证关系")
            _sync_legacy_logic_projection(conn, project_id, saved["nodes"], saved["edges"])
            status = "partially_applied" if payload.decision == "partial_apply" else "applied"
            conn.execute("UPDATE logic_suggestions SET status=?,decision_request_id=?,decision_json=?,decided_at=? WHERE id=?", (status, payload.request_id, decision_json, now(), suggestion_id))
            return {"ok": True, "decision": payload.decision, "graph": saved}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/draw")
def draw(project_id: str, payload: DrawIn) -> dict[str, Any]:
    require_project_writable(project_id)
    # Compatibility entry creates a recommendation; only explicit candidate save creates assets.
    from paperpilot.services import visual_jobs
    conn = db()
    try:
        with conn:
            run = visual_jobs.create(conn, project_id, {"title": payload.title, "intent": payload.title + "：" + "，".join(payload.labels), "request_id": uuid.uuid4().hex})
        return {"ok": True, "run": run, "body_written": False, "message": "请在图像工作台确认绘图类型，审阅候选后保存。"}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/convert")
def convert(payload: dict[str, str]) -> dict[str, str]:
    source = payload.get("source", "")
    direction = payload.get("direction", "markdown_to_latex")
    return {"content": markdown_to_latex(source) if direction == "markdown_to_latex" else latex_to_markdown(source)}


@app.post("/api/projects/{project_id}/build-snapshots")
def create_build_snapshot(project_id: str, payload: BuildSnapshotIn) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            return build_snapshots.create(conn, project_id, payload.request_id, payload.purpose, payload.expected_project_revision, asset_storage_root=PROJECTS_DIR, draft_source=payload.draft_source, candidate_id=payload.candidate_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/build-snapshots")
def list_build_snapshots(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return build_snapshots.list_snapshots(conn, project_id, limit)
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/build-snapshots/{snapshot_id}")
def read_build_snapshot(project_id: str, snapshot_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        return build_snapshots.get(conn, project_id, snapshot_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


def _wake_pending_builds():
    from paperpilot.services import build_queue, tex_runtime
    owner = tex_runtime.current_owner()
    database_path=Path(getattr(_build_store, 'database_path', DB_PATH))
    projects_path=_build_projects_dir()
    conn = db()
    try:
        if conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0] >= build_queue.GLOBAL_BUILD_LIMIT:
            return
        rows = conn.execute("SELECT b.project_id, MIN(b.rowid) AS first_row FROM build_jobs b JOIN projects p ON p.id=b.project_id WHERE b.status='queued' AND p.archived=0 AND b.owner_pid=? AND b.owner_token=? GROUP BY b.project_id ORDER BY first_row LIMIT 32", (owner['owner_pid'],owner['owner_token'])).fetchall()
    finally:
        conn.close()
    for row in rows:
        project_id=row['project_id']
        key=(str(database_path.resolve()), project_id)
        with _build_locks_guard:
            if key in _build_dispatchers or key in _build_waking or not _build_wake_slots.acquire(blocking=False):
                continue
            _build_waking.add(key)
        def run(pid=project_id, store_key=key, db_path=database_path, root=projects_path):
            _build_store.database_path=db_path
            _build_store.projects_path=root
            progressed=False
            try:
                progressed=bool(execute_build_job(pid))
            finally:
                with _build_locks_guard:
                    _build_waking.discard(store_key)
                _build_wake_slots.release()
            if progressed:
                try:
                    _wake_pending_builds()
                except (sqlite3.Error,OSError,ValueError,ProjectServiceError):
                    pass
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            with _build_locks_guard:
                _build_waking.discard(key)
            _build_wake_slots.release()
            raise


def _finish_build_slot(database_path, projects_path, project_id, job_id, owner_pid, owner_token):
    from paperpilot.services import build_queue
    conn=sqlite3.connect(database_path)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            ready=conn.execute('UPDATE build_global_slots SET cleanup_pending=1 WHERE project_id=? AND job_id=? AND owner_pid=? AND owner_token=?',
                               (project_id,job_id,owner_pid,owner_token))
            if not ready.rowcount:
                return True
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM build_global_slots WHERE project_id=? AND job_id=? AND owner_pid=? AND owner_token=?',
                                (project_id,job_id,owner_pid,owner_token)).fetchone():
                return True
            cleanup=build_snapshots.recover_project_runtime(conn, project_id, Path(projects_path), job_ids={job_id}, force=True, build_only=True)
            if any(not item.get('cleanup_verified') for item in cleanup):
                return False
            build_queue.release_slot(conn, project_id, job_id, {'owner_pid':owner_pid,'owner_token':owner_token}, projects_path)
            return True
    finally:
        conn.close()


def _retry_finished_build_slots():
    with _build_locks_guard:
        database_path=str(Path(getattr(_build_store, 'database_path', DB_PATH)).resolve())
        pending=[item for item in _build_pending_cleanup if item[0] == database_path]
    for slot in pending:
        try:
            if _finish_build_slot(*slot):
                with _build_locks_guard:
                    _build_pending_cleanup.discard(slot)
        except (sqlite3.Error, OSError, ValueError, ProjectServiceError):
            pass


def execute_build_job(
    project_id: str,
    job_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
    project_revision: int | None = None,
    citation_result: dict[str, Any] | None = None,
    logic_result: dict[str, Any] | None = None,
) -> None:
    # Coalesce wakeups instead of occupying HTTP worker threads waiting on a
    # per-paper lock. The guard closes the enqueue/drainer-exit lost-wakeup gap.
    key=(str(Path(getattr(_build_store, 'database_path', DB_PATH)).resolve()), project_id)
    with _build_locks_guard:
        _build_wake.add(key)
        if key in _build_dispatchers:
            return
        _build_dispatchers.add(key)
    registered = True
    progressed = False
    try:
        while True:
            with _build_locks_guard:
                _build_wake.discard(key)
            progressed = bool(_drain_build_queue(project_id)) or progressed
            with _build_locks_guard:
                if key in _build_wake:
                    continue
                _build_dispatchers.discard(key)
                registered = False
                return progressed
    finally:
        if registered:
            with _build_locks_guard:
                _build_dispatchers.discard(key)


def _drain_build_queue(project_id):
    from paperpilot.services import build_queue
    progressed=False
    while True:
        conn = db()
        claimed = None
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                cleanup = build_snapshots.recover_project_runtime(conn, project_id, _build_projects_dir())
                if any(item['status'] != 'active_owner' and not item['cleanup_verified'] for item in cleanup):
                    return progressed
                claimed = build_queue.claim_next(conn, project_id, _build_projects_dir())
                if claimed is None:
                    return progressed
                frozen = build_snapshots.get(conn, project_id, claimed['snapshot_id'])
                checks = json.loads(claimed['checks_json'])
                citations = checks.get('citations') or citation_report(conn, project_id, snapshot=frozen)
                logic = checks.get('logic') or read_graph(conn, project_id)
            _execute_build_job_locked(project_id, claimed['id'], frozen, frozen['project_revision'], citations, logic)
            try:
                outcome = _prune_build_cache(project_id)
                if outcome['blocked'] or outcome['pending']:
                    import logging
                    logging.getLogger(__name__).warning('构建缓存有待处理项；可在历史中检查并重试。')
            except (sqlite3.Error, OSError, ValueError):
                import logging
                logging.getLogger(__name__).warning('构建缓存清理暂不可用；可在历史中重试。')
        except Exception:
            conn.rollback()
            if claimed is None:
                raise
            with conn:
                from paperpilot.services.tex_runtime import current_owner
                owner = current_owner()
                closed = conn.execute("UPDATE build_jobs SET status='failed',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND project_id=? AND (status='queued' OR (status='running' AND owner_pid=? AND owner_token=?))", (
                    json.dumps([{'code': 'build_worker_failed', 'message': '构建执行中断，旧PDF保留。请核对文件/存储和原快照后明确重试，不会自动重跑此任务。'}], ensure_ascii=False), now(), now(), claimed['id'], project_id, owner['owner_pid'], owner['owner_token']))
                if not closed.rowcount:
                    return progressed
                build_snapshots.recover_project_runtime(conn, project_id, _build_projects_dir(), job_ids={claimed['id']}, force=True)
        finally:
            conn.close()
            if claimed is not None:
                slot=(str(Path(getattr(_build_store, 'database_path', DB_PATH)).resolve()),str(_build_projects_dir().resolve()),project_id,claimed['id'],claimed['owner_pid'],claimed['owner_token'])
                try:
                    released=_finish_build_slot(*slot)
                except (sqlite3.Error,OSError,ValueError,ProjectServiceError):
                    released=False
                if released:
                    progressed=True
                    try:
                        _wake_pending_builds()
                    except (sqlite3.Error,OSError,ValueError):
                        pass  # the bounded periodic scheduler retries wakeups
                else:
                    with _build_locks_guard:
                        _build_pending_cleanup.add(slot)


def _execute_build_job_locked(
    project_id: str,
    job_id: str,
    snapshot: dict[str, Any],
    project_revision: int,
    citation_result: dict[str, Any],
    logic_result: dict[str, Any],
) -> None:
    projects_dir = _build_projects_dir()
    run_dir = projects_dir / project_id / "build-runs" / job_id
    try:
        from paperpilot.services import visual_assets
        build_files = list(snapshot["files"])
        asset_conn = db()
        try:
            for item in snapshot.get("manifest", {}).get("assets", []):
                row = visual_assets.version(asset_conn, project_id, item["version_id"])
                if row["build_name"] != item["path"] or row["build_sha256"] != item["sha256"] or row["asset_id"] != item["asset_id"]:
                    raise ProjectServiceError(409, "asset_manifest_conflict", "构建图像清单与固定版本不一致。")
                source = visual_assets.checked_file(asset_conn, projects_dir, project_id, item["version_id"])
                build_files.append({"path": item["path"], "content_bytes": source.read_bytes()})
        finally:
            asset_conn.close()
        def build_cancelled():
            guard = db()
            try:
                row = guard.execute("SELECT b.status,p.archived FROM build_jobs b JOIN projects p ON p.id=b.project_id WHERE b.id=? AND b.project_id=?", (job_id, project_id)).fetchone()
                return not row or row['status'] != 'running' or bool(row['archived'])
            finally:
                guard.close()
        result = compile_project_files(
            build_files, projects_dir / project_id, clean_path, run_dir, job_id=job_id, cancel_check=build_cancelled,
            profile=snapshot['manifest'].get('build_profile'),
        )
    except ProjectServiceError as exc:
        result = {"ok": False, "code": exc.code, "log": exc.message}
    except Exception as exc:
        result = {"ok": False, "log": f"编译执行失败（{type(exc).__name__}）。"}
    run_pdf = run_dir / "main.pdf"
    from paperpilot.services import build_source_maps
    if result.get('ok') and run_pdf.exists():
        result['source_map'] = build_source_maps.seal(run_dir, snapshot)
    current_pdf = projects_dir / project_id / "build" / "main.pdf"
    staged_pdf = current_pdf.with_suffix(f".{job_id}.pdf.tmp")
    if result.get("ok") and run_pdf.exists():
        try:
            current_pdf.parent.mkdir(parents=True, exist_ok=True)
            staged_pdf.write_bytes(run_pdf.read_bytes())
        except OSError:
            result = {'ok': False, 'code': 'artifact_storage_failed', 'log': 'PDF暂存失败，旧成功PDF保留。请检查磁盘空间和目录权限，再按此快照重试。'}
    diagnostics = [{"code": result.get("code", "compile_failed"), "message": result.get("log", "")}] if not result.get("ok") else []
    if not result.get('ok'):
        diagnostics.extend(build_source_maps.source_diagnostics(snapshot, result.get('log', '')))
    if result.get("cancelled"):
        diagnostics = [{"code": "cancelled", "message": result.get("log", "构建已取消。")}]
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            current_job = conn.execute(
                "SELECT status FROM build_jobs WHERE id=? AND project_id=?", (job_id, project_id),
            ).fetchone()
            if not current_job or current_job["status"] != "running":
                if staged_pdf.exists():
                    staged_pdf.unlink()
                return
            final_status = "succeeded" if result.get("ok") and run_pdf.exists() else ("cancelled" if result.get("cancelled") else "failed")
            freshness = build_snapshots.snapshot_current(conn, project_id, snapshot)
            submitted_purpose = snapshot['purpose'] in {'submitted', 'release'}
            artifact_sha = hashlib.sha256(run_pdf.read_bytes()).hexdigest() if final_status == "succeeded" else ""
            updated = conn.execute(
                "UPDATE build_jobs SET status=?,diagnostics_json=?,artifact_path=?,artifact_sha256=?,finished_at=?,updated_at=?,promoted=? "
                "WHERE id=? AND status='running'",
                (final_status, json.dumps(diagnostics, ensure_ascii=False), str(run_pdf) if final_status == "succeeded" else "", artifact_sha, now(), now(), int(final_status == 'succeeded' and submitted_purpose and freshness['current']), job_id),
            )
            if updated.rowcount == 1 and final_status == "succeeded" and submitted_purpose and freshness['current']:
                staged_pdf.replace(current_pdf)
                result["pdf_url"] = f"/api/projects/{project_id}/pdf"
            elif staged_pdf.exists():
                staged_pdf.unlink()
            if updated.rowcount == 1 and final_status in {"succeeded", "failed"} and submitted_purpose and freshness['current']:
                result = {**result, 'ok': final_status == 'succeeded', 'purpose': snapshot['purpose'],
                    'build_job_id': job_id, 'snapshot_id': snapshot['id']}
                record_release_check(
                    conn, project_id, compile_result=result, citation_result=citation_result,
                    logic_result=logic_result, expected_revision=snapshot['project_revision'],
                )
    except OSError:
        conn.rollback()
        diagnostic = {'code': 'artifact_storage_failed', 'message': 'PDF晋级或清理失败，任务已结束，旧提交PDF保留。请检查磁盘空间/权限后按原快照重试。'}
        with conn:
            closed = conn.execute("UPDATE build_jobs SET status='failed',promoted=0,artifact_path='',artifact_sha256='',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND project_id=? AND status='running'",
                (json.dumps([diagnostic], ensure_ascii=False), now(), now(), job_id, project_id))
            if closed.rowcount and snapshot['purpose'] in {'submitted', 'release'} and build_snapshots.snapshot_current(conn, project_id, snapshot)['current']:
                record_release_check(conn, project_id, compile_result={'ok': False, 'code': 'artifact_storage_failed', 'build_job_id': job_id, 'snapshot_id': snapshot['id'], 'purpose': snapshot['purpose']},
                    citation_result=citation_result, logic_result=logic_result, expected_revision=snapshot['project_revision'])
        try:
            staged_pdf.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        conn.close()


def _prune_build_cache(project_id):
    from paperpilot.services import build_cache
    conn = db()
    try:
        return build_cache.prune_project(conn, _build_projects_dir(), project_id)
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/compile")
def compile_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    payload: CompileIn | None = None,
) -> dict[str, Any]:
    project_or_404(project_id)
    payload = payload or CompileIn()
    conn = db()
    request_id = payload.request_id or uuid.uuid4().hex
    from paperpilot.services import build_queue
    fingerprint_exclusions = {'request_id'} | (set() if payload.force_rebuild else {'force_rebuild'})
    request_fingerprint = build_queue.fingerprint(payload.model_dump(exclude=fingerprint_exclusions))
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if payload.force_rebuild and payload.trigger != 'explicit':
                raise ProjectServiceError(422, 'invalid_build_force', '自动预览不能强制重建；请在构建历史明确选择重新编译。')
            replayed = build_queue.replay(conn, project_id, request_id, request_fingerprint)
            if replayed:
                frozen = build_snapshots.get(conn, project_id, replayed['snapshot_id'])
                return {'ok': True, 'log': '已返回原请求对应任务，没有重复提交。', 'build_job': replayed, 'snapshot': frozen,
                    'status_url': f"/api/projects/{project_id}/builds/{replayed['id']}"}
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise HTTPException(404, "项目不存在")
            if project['archived']:
                raise ProjectServiceError(409, 'project_archived', '论文已归档；请先恢复论文，再提交新构建。旧PDF和历史仍可查看。')
            cleanup = build_snapshots.recover_project_runtime(conn, project_id, PROJECTS_DIR)
            if any(item['status'] != 'active_owner' and not item['cleanup_verified'] for item in cleanup):
                raise ProjectServiceError(503, 'runtime_cleanup_pending', '上次编译容器清理尚未核实，请恢复Docker服务并对账后重试；不会追加运行或回退到宿主机。')
            if payload.snapshot_id:
                snapshot = build_snapshots.get(conn, project_id, payload.snapshot_id)
                if not snapshot['manifest'].get('build_profile'):
                    raise ProjectServiceError(409, 'snapshot_environment_unknown', '旧快照缺少固定编译配置，不能自动按当前工具重跑。请先查看历史源码并准备修订草稿，确认后创建新快照。')
                if snapshot['purpose'] != payload.purpose:
                    raise ProjectServiceError(409, 'build_purpose_mismatch', '构建用途必须与固定快照一致，请选择对应用途后重试；预览不能冒充提交或发布证据。')
                source = snapshot['manifest'].get('source', {})
                if (payload.candidate_id and source.get('candidate_id') != payload.candidate_id) or (payload.draft_source and source.get('draft_source') != payload.draft_source):
                    raise ProjectServiceError(409, 'build_source_mismatch', '来源选择与固定快照不一致，请核对原快照或创建新的快照。')
            else:
                snapshot = build_snapshots.create(conn, project_id, request_id, payload.purpose, int(project["project_revision"]), asset_storage_root=PROJECTS_DIR, draft_source=payload.draft_source or 'all', candidate_id=payload.candidate_id)
            citation_result = citation_report(conn, project_id, snapshot=snapshot)
            logic_result = read_graph(conn, project_id)
            job = build_snapshots.create_job(conn, project_id, snapshot["id"], request_id, payload.purpose,
                trigger=payload.trigger, force_rebuild=payload.force_rebuild, max_active=MAX_ACTIVE_BUILDS_PER_PROJECT, request_fingerprint=request_fingerprint, checks={'citations': citation_result, 'logic': logic_result}, asset_storage_root=PROJECTS_DIR)
            snapshot = build_snapshots.get(conn, project_id, job['snapshot_id'])
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()

    if job['status'] == 'queued':
        background_tasks.add_task(execute_build_job, project_id)
    return {
        "ok": True, "log": "已复用相同快照/用途的任务。" if job.get('deduplicated') else "构建任务已排队。", "build_job": job, "snapshot": snapshot,
        "status_url": f"/api/projects/{project_id}/builds/{job['id']}",
    }


def _release_stream_pin(database_path, project_id, job_id, lease_id):
    from paperpilot.services import build_cache
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            build_cache.release(conn, project_id, job_id, lease_id)
    finally:
        conn.close()


class LeasedPdfResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        # pathsend hands a filename to the server and can return before the
        # server has finished opening/reading it. Stream through ASGI instead,
        # so the background release follows the last awaited body send.
        if 'http.response.pathsend' in scope.get('extensions', {}):
            scope = dict(scope)
            scope['extensions'] = {k: v for k, v in scope['extensions'].items() if k != 'http.response.pathsend'}
        await super().__call__(scope, receive, send)


@app.get("/api/projects/{project_id}/pdf")
def pdf(project_id: str) -> FileResponse:
    project_or_404(project_id)
    conn = db()
    stream_pin = 'd-' + uuid.uuid4().hex
    held = False
    try:
        # The submitted artifact stays preferred while draft/candidate previews
        # remain in their own job URLs. An old success is still readable/stale.
        from paperpilot.services import build_cache
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM build_jobs WHERE project_id=? AND status='succeeded' AND (purpose='draft_preview' OR promoted=1) ORDER BY CASE WHEN purpose IN ('submitted','release') THEN 0 ELSE 1 END,created_at DESC LIMIT 1", (project_id,)).fetchone()
            if row:
                build_cache.hold(conn, project_id, row['id'], stream_pin, persistent=True)
                held = True
        if row:
            job = build_snapshots.get_job(conn, project_id, row['id'])
            path = Path(job['artifact_path'])
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != job['artifact_sha256']:
                raise HTTPException(409, detail={'code': 'build_artifact_unavailable', 'message': '成功构建的PDF缺失或哈希不符；请按固定快照重新构建，未返回其他版本冒充结果。'})
            return LeasedPdfResponse(path, media_type='application/pdf', filename='main.pdf', headers={
                'X-Build-Job': job['id'], 'X-Build-Purpose': job['purpose'], 'X-Build-Eligibility': job['artifact_eligibility']},
                background=BackgroundTask(_release_stream_pin, DB_PATH, project_id, job['id'], stream_pin))
    except ProjectServiceError as exc:
        if held:
            with conn: build_cache.release(conn, project_id, row['id'], stream_pin)
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    except Exception:
        if held:
            with conn: build_cache.release(conn, project_id, row['id'], stream_pin)
        raise
    finally:
        conn.close()
    path = PROJECTS_DIR / project_id / 'build/main.pdf'
    if not path.is_file():
        raise HTTPException(404, '尚未生成 PDF')
    return FileResponse(path, media_type='application/pdf', filename='main.pdf', headers={'X-Build-Eligibility': 'legacy_unverified'})


@app.get("/api/projects/{project_id}/builds")
def list_build_jobs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return build_snapshots.list_jobs(conn, project_id, limit)
    finally:
        conn.close()


@app.get('/api/projects/{project_id}/build-environment')
def get_build_environment(project_id: str):
    project_or_404(project_id)
    conn=db()
    try:
        row=conn.execute('SELECT version FROM project_build_profile_overrides WHERE project_id=?',(project_id,)).fetchone()
        return {'overridden':bool(row),'version':row['version'] if row else None,
                'effective':build_snapshots.build_profile(conn,project_id),'local_default':build_snapshots.build_profile()}
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/build-environment/reset')
def reset_build_environment(project_id: str, payload: SettingRestoreIn):
    from paperpilot.services import build_environment
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
            if not project:raise ProjectServiceError(404,'project_missing','论文不存在。')
            if project['archived'] or project['lifecycle_state']=='published':
                raise ProjectServiceError(409,'build_profile_read_only','论文已归档或发布；请先通过论文状态管理解除只读，再修改编译环境。')
            return build_environment.reset(conn,project_id,payload.expected_project_revision,payload.request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/builds/reconcile")
def reconcile_build_jobs(project_id: str, stale_after_seconds: int = 300) -> dict[str, Any]:
    """Reconcile queued/running rows left by a process restart."""
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            cleanup = build_snapshots.recover_project_runtime(conn, project_id, PROJECTS_DIR)
            return build_snapshots.reconcile_jobs(conn, project_id, stale_after_seconds=stale_after_seconds, storage_root=PROJECTS_DIR) | {'runtime_cleanup': cleanup}
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/builds/cache-inventory")
def read_build_cache_inventory(project_id: str) -> dict[str, Any]:
    from paperpilot.services import build_cache
    conn = db()
    try:
        return build_cache.inventory(conn, project_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/builds/cache-cleanup")
def cleanup_build_cache(project_id: str):
    project_or_404(project_id)
    try:
        return _prune_build_cache(project_id)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise HTTPException(503, detail={'code': 'build_cache_cleanup_unavailable', 'message': '构建缓存暂未清理；请检查存储和数据库后重试。正文和历史记录保留。'}) from exc


@app.get("/api/projects/{project_id}/builds/{job_id}")
def read_build_job(project_id: str, job_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        return build_snapshots.get_job(conn, project_id, job_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get('/api/projects/{project_id}/builds/{job_id}/reading')
def read_build_reading(project_id: str, job_id: str):
    from paperpilot.services import build_source_maps
    conn = db()
    try:
        return build_source_maps.reading(conn, PROJECTS_DIR, project_id, job_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/builds/{job_id}/read-lease')
def renew_build_read_lease(project_id: str, job_id: str, lease_id: str, persistent: bool = False):
    from paperpilot.services import build_cache
    conn = db()
    try:
        with conn:
            if persistent and not re.fullmatch(r'r-[0-9a-f]{32}', lease_id):
                raise ProjectServiceError(422, 'build_read_lease_invalid', '阅读保护编号无效；请刷新阅读器。')
            return build_cache.hold(conn, project_id, job_id, lease_id, persistent=persistent)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/builds/{job_id}/read-lease-release')
def release_build_read_lease(project_id: str, job_id: str, lease_id: str):
    from paperpilot.services import build_cache
    if not re.fullmatch(r'r-[0-9a-f]{32}', lease_id):
        raise HTTPException(422, detail={'code':'build_read_lease_invalid','message':'阅读保护编号无效。'})
    conn = db()
    try:
        with conn:
            return {'released': bool(build_cache.release(conn, project_id, job_id, lease_id))}
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/builds/{job_id}/clear-read-pins')
def clear_build_read_pins(project_id: str, job_id: str):
    from paperpilot.services import build_cache
    conn = db()
    try:
        with conn:
            return {'released': build_cache.clear_pins(conn, project_id, job_id)}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code':exc.code,'message':exc.message}) from exc
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/builds/{job_id}/source-map/source')
def read_build_source(project_id: str, job_id: str, payload: BuildSourceIn):
    from paperpilot.services import build_source_maps
    conn = db()
    try:
        return build_source_maps.source(conn, project_id, job_id, payload.path, payload.line)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        conn.close()


@app.post('/api/projects/{project_id}/builds/{job_id}/source-map/forward')
def forward_build_source(project_id: str, job_id: str, payload: BuildSourceForwardIn):
    return query_build_source(project_id, job_id, 'forward', payload.model_dump())


@app.post('/api/projects/{project_id}/builds/{job_id}/source-map/inverse')
def inverse_build_source(project_id: str, job_id: str, payload: BuildSourceInverseIn):
    return query_build_source(project_id, job_id, 'inverse', payload.model_dump())


def query_build_source(project_id, job_id, direction, payload):
    from paperpilot.services import build_source_maps, build_cache
    conn = db()
    pin = 'q-' + uuid.uuid4().hex
    held = False
    try:
        with conn:
            build_cache.hold(conn, project_id, job_id, pin, persistent=True)
            held = True
        return build_source_maps.query(conn, PROJECTS_DIR, project_id, job_id, direction=direction, **payload)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        try:
            if held:
                with conn:
                    build_cache.release(conn, project_id, job_id, pin)
        finally:
            conn.close()


@app.get("/api/projects/{project_id}/builds/{job_id}/pdf")
def read_build_pdf(project_id: str, job_id: str) -> FileResponse:
    project_or_404(project_id)
    conn = db()
    stream_pin = 'd-' + uuid.uuid4().hex
    held = False
    try:
        from paperpilot.services import build_cache
        with conn:
            build_cache.hold(conn, project_id, job_id, stream_pin, persistent=True)
            held = True
            job = build_snapshots.get_job(conn, project_id, job_id)
        path = Path(job.get("artifact_path") or "")
        if job.get("status") != "succeeded" or not path.exists():
            raise HTTPException(404, "该构建任务没有可用 PDF 产物")
        if hashlib.sha256(path.read_bytes()).hexdigest() != job['artifact_sha256']:
            raise HTTPException(409, detail={'code': 'build_artifact_corrupt', 'message': '构建PDF哈希不符，请按原快照重新构建；历史记录保留。'})
        return LeasedPdfResponse(path, media_type="application/pdf", filename=f"{job_id}.pdf", headers={'X-Build-Purpose': job['purpose'], 'X-Build-Eligibility': job['artifact_eligibility']},
                            background=BackgroundTask(_release_stream_pin, DB_PATH, project_id, job_id, stream_pin))
    except ProjectServiceError as exc:
        if held:
            with conn: build_cache.release(conn, project_id, job_id, stream_pin)
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except Exception:
        if held:
            with conn: build_cache.release(conn, project_id, job_id, stream_pin)
        raise
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/builds/{job_id}/cancel")
def cancel_build_job(project_id: str, job_id: str, request_id: str = "") -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            result = build_snapshots.cancel_job(conn, project_id, job_id, request_id or uuid.uuid4().hex)
        from paperpilot.adapters.latex import cancel_compile
        cancel_compile(job_id)
        with conn:
            cleanup = build_snapshots.recover_project_runtime(conn, project_id, PROJECTS_DIR, job_ids={job_id}, force=True)
        return build_snapshots.get_job(conn, project_id, job_id) | {'runtime_cleanup': cleanup}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/builds/{job_id}/restore-preview")
def preview_build_restore(project_id: str, job_id: str, payload: BuildRestorePreviewIn) -> dict[str, Any]:
    from paperpilot.services import build_restores
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN')
            return build_restores.preview(conn, project_id, job_id, payload.include_paths, remove_paths=payload.remove_paths, setting_keys=payload.setting_keys, restore_build_environment=payload.restore_build_environment, storage_root=PROJECTS_DIR)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/human-revision/build-restores/{restore_id}/commit")
def commit_build_restore(project_id: str, restore_id: str, payload: BuildRestoreCommitIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    from paperpilot.services.human_revision import commit_build_restore as apply_restore
    from paperpilot.services import build_queue
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            result = apply_restore(conn, project_id, restore_id, payload.request_id, payload.candidate_fingerprint, storage_root=PROJECTS_DIR)
            if result.get('idempotent_replay'):
                return result
            rebuild_request = uuid.uuid4().hex
            snapshot = build_snapshots.create(conn, project_id, rebuild_request, 'submitted', asset_storage_root=PROJECTS_DIR)
            job = build_snapshots.create_job(conn, project_id, snapshot['id'], rebuild_request, 'submitted',
                checks={'citations': citation_report(conn, project_id, snapshot=snapshot), 'logic': read_graph(conn, project_id)},
                max_active=MAX_ACTIVE_BUILDS_PER_PROJECT, asset_storage_root=PROJECTS_DIR)
            result.update(build_job=job, status_url=f"/api/projects/{project_id}/builds/{job['id']}")
            candidate_row = conn.execute('SELECT result_json FROM build_restore_events WHERE id=? AND project_id=?', (restore_id, project_id)).fetchone()
            candidate = json.loads(candidate_row['result_json'])
            candidate['commit'] = {'request_id': payload.request_id, 'result': result}
            conn.execute('UPDATE build_restore_events SET result_json=? WHERE id=? AND project_id=?', (json.dumps(candidate, ensure_ascii=False), restore_id, project_id))
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={'code': exc.code, 'message': exc.message}) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, detail={'code': 'build_restore_write_failed', 'message': '恢复写入失败，全部文件和版本已回滚，当前正文与待审草稿保留。请检查本地存储后重试。'}) from exc
    finally:
        conn.close()
    if job['status'] == 'queued':
        background_tasks.add_task(execute_build_job, project_id)
    return result


@app.post("/api/projects/{project_id}/builds/{job_id}/restore")
def restore_build_source(project_id: str, job_id: str, payload: BuildRestoreIn) -> dict[str, Any]:
    """Prepare a historical build as human-revision drafts.

    REQ-011 owns the immutable build snapshot; REQ-001 owns applying source
    changes.  This endpoint therefore never writes files directly.
    """
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            return build_snapshots.prepare_restore(conn, project_id, job_id, payload.request_id, payload.include_paths,
                remove_paths=payload.remove_paths, setting_keys=payload.setting_keys, restore_build_environment=payload.restore_build_environment, preview_fingerprint=payload.preview_fingerprint, storage_root=PROJECTS_DIR)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(503, detail={'code': 'build_restore_write_failed', 'message': '准备恢复草稿失败，本次全部操作已回滚；现稿和原草稿保留，请检查本地存储后重试。'}) from exc
    finally:
        conn.close()


def _citation_check_preview(conn: sqlite3.Connection, project_id: str, check: CitationCheckIn) -> dict[str, Any]:
    from paperpilot.services import citation_semantic

    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(404, "项目不存在")
    report = citation_support.analyze(conn, project_id, check.content, check.target_path)
    source_pool = {str(item["source_id"]): item for item in report.get("source_pool", [])}
    source_scope = list(dict.fromkeys(check.source_ids)) if check.source_ids else sorted(source_pool)
    if set(source_scope) - set(source_pool):
        raise HTTPException(422, detail={"code": "citation_source_scope_invalid", "message": "来源范围包含不属于当前论文的来源。"})

    binding = provider_config.project_binding(conn, project_id)
    model=check.model or (binding or {}).get('model_id') or project['model'] or ''
    profile_id=(binding or {}).get('profile_id','');profile_version=int((binding or {}).get('ai_config_revision',0))
    base_url=(binding or {}).get('base_url','');admission=None;provider_error=''
    try:
        admission=provider_runtime.request_provider(conn,project_id,model)
        if admission['auth_mode']!='temporary':provider_config.resolve_credentials(conn,profile_id,profile_version)
        provider_ready=True
    except ProjectServiceError as exc:
        if exc.code=='model_not_registered':raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        provider_ready=False;provider_error=exc.message
    parsed_url = urlparse(str(base_url or ""))
    host = parsed_url.hostname or ""
    local_endpoint = host.lower() in {"localhost", "127.0.0.1", "::1"}
    payload = citation_semantic.model_input(report, source_scope)
    outbound_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    redacted_json, redactions = redact_sensitive_text(outbound_json)
    outbound = json.loads(redacted_json)
    prompt = citation_semantic.prompt_for(outbound)
    outbound_user_message = (
        "任务：将选定内容与提供的参考文献上下文进行对照，标出可核对的问题，不伪造文献。\n"
        f"用户补充：{prompt}\n论文上下文：\n\n当前内容：\n\n选中内容："
    )
    outbound_system_message = "你是严谨的学术写作助手。不要伪造参考文献；不确定时明确说明。输出可编辑结果。"
    estimated_tokens = estimate_tokens(outbound_system_message + outbound_user_message)
    max_input_chars = _positive_env_int("PWA_AI_MAX_INPUT_CHARS", 400000)
    max_output_tokens = _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000)
    max_job_tokens = _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000)
    if admission:
        try:provider_runtime.admit_tokens({'model_limits':admission['model_limits']},model,estimated_tokens+256,max_output_tokens)
        except ProjectServiceError as exc:provider_ready=False;provider_error=exc.message
    if len(prompt) > max_input_chars:
        budget_status = "input_too_large"
    else:
        budget_status = "within_limit" if estimated_tokens + max_output_tokens <= max_job_tokens else "budget_exceeded"
    current_content = check.content if check.content is not None else citation_support._source_content(conn, project_id, check.target_path)
    current_dependencies = citation_support.dependencies(conn, project_id, check.target_path)
    identity = {
        "target_path": check.target_path or "__all_tex__",
        "content_sha256": hashlib.sha256(current_content.encode("utf-8")).hexdigest(),
        "dependency_fingerprint": current_dependencies["fingerprint"],
        "source_scope": source_scope,
        "model": model,
        "endpoint_host": host,
        "profile_id": profile_id,
        "profile_version": profile_version,
        'provider_fingerprint':admission['config_fingerprint'] if admission else None,
        "prompt_sha256": hashlib.sha256((outbound_system_message + outbound_user_message).encode("utf-8")).hexdigest(),
        "rule_version": "citation-semantics-v2",
    }
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "request_id": check.request_id or uuid.uuid4().hex,
        "preview_fingerprint": fingerprint,
        "target_path": check.target_path or "__all_tex__",
        "input_kind": "request_content" if check.content is not None else "saved_content",
        "content_sha256": identity["content_sha256"],
        "model": {
            "id": model, "endpoint_host": host,
            "endpoint_type": "local" if local_endpoint else "remote",
            "profile_id": profile_id, "profile_version": profile_version,
            "ready": provider_ready,
        },
        "provider_error": provider_error if not provider_ready else "",
        "source_scope": source_scope,
        "source_count": len(source_scope),
        "claim_count": len(report.get("claims", [])),
        "redacted_secret_count": redactions,
        "estimated_input_tokens": estimated_tokens,
        "max_input_chars": max_input_chars,
        "max_output_tokens": max_output_tokens,
        "max_job_tokens": max_job_tokens,
        "budget_status": budget_status,
        "outbound_preview": outbound,
        "system_prompt_preview": outbound_system_message,
        "outbound_prompt_preview": outbound_user_message,
        "_provider_base_url": base_url,
        '_provider_snapshot':admission,
    }


@app.post("/api/projects/{project_id}/compare")
def compare(project_id: str, payload: CitationCheckIn | None = None) -> dict[str, Any]:
    project_or_404(project_id); conn = db()
    try:
        result = citation_report(conn, project_id)
        check = payload or CitationCheckIn()
        result["semantic_review"] = citation_support.analyze(conn, project_id, check.content, check.target_path)
        return result
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/citation-check/preview")
def citation_check_preview(project_id: str, payload: CitationCheckIn | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        preview = _citation_check_preview(conn, project_id, payload or CitationCheckIn())
        preview.pop("_provider_base_url", None)
        return preview
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/citation-check")
def citation_check(project_id: str, payload: CitationCheckIn | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        check = payload or CitationCheckIn()
        request_id = check.request_id or uuid.uuid4().hex
        preview = _citation_check_preview(conn, project_id, check)
        semantic_response = None
        semantic_error = ""
        semantic_model = str(preview["model"].get("id") or "")
        analysis_identity = {
            "mode": "local", "model": "", "source_scope": preview["source_scope"],
            "preview_fingerprint": preview["preview_fingerprint"],
            "rule_version": "citation-semantics-v2",
        }
        if check.consent_to_send:
            if not check.preview_fingerprint or check.preview_fingerprint != preview["preview_fingerprint"]:
                raise HTTPException(409, detail={"code": "citation_preview_stale", "message": "输入、来源授权或模型配置已变化；请重新查看外发预览。"})
            existing = conn.execute(
                "SELECT * FROM citation_check_runs WHERE project_id=? AND request_id=?",
                (project_id, request_id),
            ).fetchone()
            if existing:
                existing_report = json.loads(existing["result_json"] or "{}")
                existing_identity = existing_report.get("analysis_identity", {})
                expected_input = citation_support._input_fingerprint(
                    citation_support.dependencies(conn, project_id, check.target_path)["fingerprint"],
                    check.content if check.content is not None else citation_support._source_content(conn, project_id, check.target_path),
                )
                if existing["source_fingerprint"] != expected_input:
                    raise HTTPException(409, detail={"code": "citation_check_idempotency_conflict", "message": "request_id 已用于不同的正文或来源版本。"})
                if existing_identity.get("mode") == "semantic_model" and existing_identity.get("preview_fingerprint") == check.preview_fingerprint:
                    return citation_support.citation_run_payload(conn, existing)
                raise HTTPException(409, detail={"code": "citation_check_idempotency_conflict", "message": "request_id 已用于不同的核验授权或模型配置。"})
            if not preview["model"]["ready"]:
                raise HTTPException(409, detail={"code": "citation_provider_unavailable", "message": preview["provider_error"]})
            if preview["budget_status"] != "within_limit":
                raise HTTPException(413, detail={"code": "budget_exceeded", "message": "本次语义核验超过任务 token 上限；没有发送内容。"})
            from paperpilot.services import citation_semantic
            result = provider_runtime.execute_call(conn,project_id,request_id,preview['_provider_snapshot'],'citation_compare',
                citation_semantic.prompt_for(preview['outbound_preview']),max_tokens=preview['max_output_tokens'],max_job_tokens=preview['max_job_tokens'],
                temporary_key=check.temporary_key or None,call=call_ai,scope={'source_ids':preview['source_scope'],'preview_fingerprint':preview['preview_fingerprint']},
                guard=lambda:_citation_check_preview(conn,project_id,check)['preview_fingerprint']==preview['preview_fingerprint'])
            current_preview = _citation_check_preview(conn, project_id, check)
            if current_preview["preview_fingerprint"] != preview["preview_fingerprint"]:
                raise HTTPException(409, detail={"code": "citation_result_stale_after_provider", "message": "模型调用期间正文、来源授权或配置发生变化；结果已丢弃，请重新预览和核验。"})
            analysis_identity = {
                "mode": "semantic_model", "model": semantic_model,
                "endpoint_host": preview["model"]["endpoint_host"],
                "profile_id": preview["model"]["profile_id"],
                "profile_version": preview["model"]["profile_version"],
                "preview_fingerprint": preview["preview_fingerprint"],
                "consent_to_send": True,
                "consented_at": now(),
                "estimated_input_tokens": preview["estimated_input_tokens"],
                "rule_version": "citation-semantics-v2",
            }
            if result.get("ok"):
                usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}
                try:
                    actual_output = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
                    actual_total = int(usage.get("total_tokens", 0) or 0)
                except (TypeError, ValueError):
                    actual_output = actual_total = 0
                if actual_output > preview["max_output_tokens"] or actual_total > preview["max_job_tokens"]:
                    semantic_error = "提供方报告的实际 token 用量超过本次预算；模型结果未采用。"
                else:
                    semantic_response = str(result.get("content", ""))
                    analysis_identity["usage"] = usage
            else:
                semantic_error = str(result.get("error", "AI 语义核验失败；未采用模型判断。"))[:500]
        elif check.preview_fingerprint and check.preview_fingerprint != preview["preview_fingerprint"]:
            raise HTTPException(409, detail={"code": "citation_preview_stale", "message": "输入、来源授权或模型配置已变化；请重新生成检查预览。"})
        with conn:
            return citation_support.run_report(
                conn, project_id, request_id=request_id,
                content=check.content, target_path=check.target_path,
                semantic_response=semantic_response, semantic_model=semantic_model,
                allowed_source_ids=preview["source_scope"], analysis_identity=analysis_identity,
                semantic_error=semantic_error,
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/citation-check/runs")
def list_citation_check_runs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM citation_check_runs WHERE project_id=? AND expires_at>? ORDER BY created_at DESC LIMIT ?",
            (project_id, now(), min(max(int(limit), 1), 100)),
        ).fetchall()
        return [citation_support.citation_run_payload(conn, row) for row in rows]
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/citation-check/runs/{run_id}")
def read_citation_check_run(project_id: str, run_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM citation_check_runs WHERE project_id=? AND id=?",
            (project_id, run_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "引用审查报告不存在")
        try:
            return citation_support.citation_run_payload(conn, row)
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/citation-check/runs/{run_id}/claims/{claim_id}/handoff")
def handoff_citation_suggestion(
    project_id: str,
    run_id: str,
    claim_id: str,
    payload: CitationSuggestionHandoffIn,
) -> dict[str, Any]:
    """Create a REQ-001 human-revision draft; never commit manuscript text."""
    require_project_writable(project_id)
    if not payload.confirm_handoff:
        raise HTTPException(422, detail={"code": "citation_handoff_confirmation_required", "message": "请先确认将建议送入人工修订草稿。"})
    conn = db()
    try:
        with conn:
            previous = conn.execute(
                "SELECT * FROM assist_action_events WHERE project_id=? AND request_id=?",
                (project_id, payload.request_id),
            ).fetchone()
            if previous:
                if previous["action_type"] != "citation_suggestion_handoff":
                    raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "request_id 已用于其他人工修订事件。"})
                return {
                    "ok": True, "idempotent_replay": True, "event_id": previous["id"],
                    "scope": json.loads(previous["scope_json"] or "{}"),
                    "status": previous["status"], "automatic_write": False,
                }
            row = conn.execute(
                "SELECT * FROM citation_check_runs WHERE id=? AND project_id=?", (run_id, project_id)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "引用审查报告不存在")
            report = citation_support.citation_run_payload(conn, row)
            if report["stale"]:
                raise HTTPException(409, detail={"code": "citation_report_stale", "message": "报告依赖已变化，请重新检查后再交接。"})
            target_path = str(row["target_path"] or "")
            if not target_path:
                raise HTTPException(409, detail={"code": "citation_handoff_target_ambiguous", "message": "整篇报告未绑定单一文件，不能直接建立目标文件修订草稿。"})
            claim = next((item for item in report.get("claims", []) if item.get("id") == claim_id), None)
            if claim is None:
                raise HTTPException(404, "报告中不存在该论断")
            semantic = claim.get("semantic_review") or {}
            suggestion = semantic.get("suggestion") or {}
            suggested_text = str(suggestion.get("text") or "")
            if not suggested_text or not suggestion.get("quote_preserved") or not suggestion.get("citation_keys_valid"):
                raise HTTPException(409, detail={"code": "citation_suggestion_unavailable", "message": "该论断没有可安全交接的保意建议。"})
            if suggestion.get("status") == "permission_revoked":
                raise HTTPException(409, detail={"code": "citation_source_permission_revoked", "message": "建议所依赖的来源授权已撤回。"})
            file_row = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, target_path)).fetchone()
            if file_row is None:
                raise HTTPException(404, "目标文件不存在")
            current_content = str(file_row["content"])
            source_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            if source_hash != report.get("source", {}).get("content_sha256"):
                raise HTTPException(409, detail={"code": "citation_target_content_conflict", "message": "报告目标正文与当前保存正文不同。"})
            actual_version = current_version(conn, project_id, "file", target_path)
            if actual_version != payload.base_version or actual_version != int(row["source_version"]):
                raise HTTPException(409, detail={"code": "citation_target_version_conflict", "message": "目标文件版本已变化，请重新预览和检查。"})
            if conn.execute(
                "SELECT 1 FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?",
                (project_id, target_path),
            ).fetchone():
                raise HTTPException(409, detail={"code": "human_revision_draft_exists", "message": "该文件已有人工修订草稿；请先处理现有草稿，避免覆盖。"})
            start, end = int(claim["char_start"]), int(claim["char_end"])
            if start < 0 or end < start or end > len(current_content):
                raise HTTPException(409, detail={"code": "citation_claim_anchor_invalid", "message": "论断位置已无法映射到当前文件。"})
            original_span = current_content[start:end]
            if " ".join(original_span.split()) != str(claim.get("text", "")):
                raise HTTPException(409, detail={"code": "citation_claim_anchor_conflict", "message": "论断锚点与当前正文不一致。"})
            mode = active_mode(conn, project_id)
            draft_content = current_content[:start] + suggested_text + current_content[end:]
            save_draft(conn, project_id, "file", target_path, draft_content, actual_version)
            source_refs = [
                {
                    "source_id": item.get("source_id"), "cite_key": item.get("cite_key"),
                    "evidence_id": item.get("evidence_id"), "fragment_id": item.get("fragment_id"),
                    "relation": item.get("relation"), "role": item.get("role"),
                }
                for item in semantic.get("sources", [])
            ]
            scope = {
                "kind": "file", "path": target_path, "claim_id": claim_id,
                "report_id": run_id, "source_version": actual_version,
                "suggestion_sha256": hashlib.sha256(suggested_text.encode("utf-8")).hexdigest(),
                "source_refs": source_refs, "handoff_only": True,
            }
            event_id = record_event(
                conn, project_id, mode_id=mode["id"], action_type="citation_suggestion_handoff",
                scope=scope, base_file_versions={target_path: actual_version},
                status="draft_saved_for_review", request_id=payload.request_id,
            )
            return {
                "ok": True, "event_id": event_id, "run_id": run_id,
                "claim_id": claim_id, "target_path": target_path,
                "base_version": actual_version, "status": "draft_saved_for_review",
                "automatic_write": False, "workflow_commit_required": True,
            }
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/format-check")
def format_check(project_id: str, payload: FormatCheckIn | None = None) -> dict[str, Any]:
    """Run the paper's effective format policy as a read-only warning pass."""
    project_or_404(project_id)
    check = payload or FormatCheckIn()
    if check.content is not None:
        raise HTTPException(
            409,
            detail={
                "code": "unsaved_draft_not_checkable",
                "message": "格式检查只读取已保存文件版本；请先保存草稿后重新检查。",
            },
        )
    conn = db()
    try:
        files = [dict(row) for row in conn.execute("SELECT path,content FROM files WHERE project_id=? ORDER BY path", (project_id,))]
        if check.target_path and not any(item["path"] == check.target_path for item in files):
            raise HTTPException(404, "目标文件不存在")
        from paperpilot.services import format_check as format_checker
        versions = version_map(conn, project_id)
        for item in files:
            item["version"] = versions.get(item["path"], 0)
        view = settings.read(conn, project_id)
        result = format_checker.analyze(files, view.get("effective", {}), target_path=check.target_path)
        dependencies = format_checker.dependencies(conn, project_id, check.target_path)
        request_id = check.request_id or uuid.uuid4().hex
        existing = conn.execute(
            "SELECT * FROM format_check_runs WHERE project_id=? AND request_id=?",
            (project_id, request_id),
        ).fetchone()
        if existing:
            if existing["fingerprint"] != dependencies["fingerprint"]:
                raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "request_id 已用于不同的格式检查输入。"})
            return _format_run_response(conn, existing)
        run_id = uuid.uuid4().hex
        stamp = now()
        with conn:
            conn.execute(
                "INSERT INTO format_check_runs(id,project_id,request_id,target_path,source_project_revision,dependencies_json,fingerprint,status,result_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, project_id, request_id, check.target_path or "",
                    dependencies["project_revision"], json.dumps(dependencies, ensure_ascii=False),
                    dependencies["fingerprint"], "completed", json.dumps(result, ensure_ascii=False),
                    stamp, stamp,
                ),
            )
        row = conn.execute("SELECT * FROM format_check_runs WHERE id=?", (run_id,)).fetchone()
        return _format_run_response(conn, row)
    finally:
        conn.close()


def _format_run_response(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    from paperpilot.services import format_check as format_checker
    stored = json.loads(row["dependencies_json"] or "{}")
    current = format_checker.dependencies(conn, row["project_id"], row["target_path"] or None)
    stale_parts = [
        key for key in ("files", "settings", "terms")
        if stored.get(key) != current.get(key)
    ]
    result = json.loads(row["result_json"] or "{}")
    result.update({
        "run_id": row["id"],
        "request_id": row["request_id"],
        "status": "stale" if stale_parts else row["status"],
        "stale": bool(stale_parts),
        "stale_reason": stale_parts,
        "source_project_revision": int(row["source_project_revision"]),
        "dependency_fingerprint": row["fingerprint"],
    })
    return result


@app.get("/api/projects/{project_id}/format-check/runs")
def list_format_check_runs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT * FROM format_check_runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
            (project_id, min(max(int(limit), 1), 100)),
        ).fetchall()
        return [_format_run_response(conn, row) for row in rows]
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/format-check/runs/{run_id}")
def read_format_check_run(project_id: str, run_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        row = conn.execute(
            "SELECT * FROM format_check_runs WHERE project_id=? AND id=?",
            (project_id, run_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "格式检查结果不存在")
        return _format_run_response(conn, row)
    finally:
        conn.close()


def citation_report(conn: sqlite3.Connection, project_id: str, *, snapshot: dict | None = None) -> dict[str, Any]:
    from paperpilot.services import citation_engineering
    from paperpilot.services.content_versions import version_map
    versions = version_map(conn, project_id)
    files = snapshot['files'] if snapshot else [dict(r) | {'version': versions.get(r['path'], 0)} for r in conn.execute('SELECT path,content FROM files WHERE project_id=?', (project_id,))]
    scanned = citation_engineering.scan(files)
    refs = snapshot['references'] if snapshot else conn.execute("SELECT cite_key FROM refs WHERE project_id=?", (project_id,)).fetchall()
    library_refs = [] if snapshot else conn.execute("SELECT cite_key FROM project_reference_links WHERE project_id=? AND link_status IN ('linked','update_available')", (project_id,)).fetchall()
    engineering_defined = set(scanned['engineering_defined'])
    library_available = {row["cite_key"] for row in refs} | {row["cite_key"] for row in library_refs}
    cited = set(scanned['cited'])
    missing = sorted(cited - engineering_defined)
    unused = sorted(engineering_defined - cited)
    library_only = sorted((cited & library_available) - engineering_defined)
    summary = f"已引用 {len(cited)} 项，工程参考文献未定义 {len(missing)} 项，工程中未使用 {len(unused)} 项。"
    if library_only:
        summary += f"其中 {len(library_only)} 项仅存在于文献库，尚未加入 references.bib，不视为可编译引用。"
    return {
        **scanned,
        "cited": sorted(cited), "available": sorted(engineering_defined),
        "engineering_defined": sorted(engineering_defined),
        "library_available": sorted(library_available), "library_only": library_only,
        "missing": missing, "unused": unused, "summary": summary,
    }


def _require_search_writable(conn: sqlite3.Connection, project_id: str) -> None:
    project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
    if not project:raise HTTPException(404,'项目不存在')
    if project['archived'] or project['lifecycle_state']=='published':
        raise HTTPException(409,detail={'code':'search_project_read_only','message':'论文已归档或发布，只能查看检索历史；恢复或撤回发布后可继续操作。'})


def _search_row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["filters"] = json.loads(item.pop("filters_json", "{}") or "{}")
    item["saved"] = bool(item.get("saved"))
    item.pop("generation", None)
    item.pop("summary_json", None)
    return item


def _candidate_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for field in ("match_reasons_json", "sources_json"):
        target = field.removesuffix("_json")
        item[target] = json.loads(item.pop(field, "[]") or "[]")
    item.pop("raw_json", None)
    item["saved"] = item.get("status") == "saved"
    return item


def _stored_academic_summary(row, candidates):
    try:
        summary=json.loads(row['summary_json'] or '')
        if isinstance(summary,dict) and row['status']=='partial' and summary.get('partial_saved_candidates') and summary.get('rule_version')==row['rule_version'] and 'counts' in summary:
            return summary
        if isinstance(summary,dict) and summary.get("rule_version")==row["rule_version"] and {"counts","source_failures","provider_diagnostics","truncated","retrieved_at","expanded_queries","pages_fetched","raw_count"}<=summary.keys():return summary
    except (ValueError,TypeError):pass
    # Older snapshots did not preserve diagnostics. Do not invent a clean run.
    summary=academic_search.search_summary(candidates,row['query'],json.loads(row['filters_json'] or '{}'),row['provider'])
    summary.update(rule_version=row['rule_version'],diagnostics_unavailable=True,pages_fetched=None,raw_count=None,truncated=None,retrieved_at=None)
    return summary


def _search_session_key(token: str) -> str:
    if not isinstance(token,str) or not re.fullmatch(r'[0-9a-f]{32,128}',token):
        raise HTTPException(400,detail={'code':'invalid_session','message':'检索会话无效，请刷新页面重试。'})
    namespace=os.path.normcase(str(Path(DB_PATH).resolve()))
    return hashlib.sha256((namespace+'\0'+token).encode('utf-8')).hexdigest()


def _session_search_response(item: dict[str, Any]) -> dict[str, Any]:
    search={key:value for key,value in item.items() if key not in {'candidates','summary','error'}}
    return {'ok':True,'status':item['status'],'search':search,'candidates':item['candidates'],
            'next_cursor':item['next_cursor'],'summary':item['summary']}


@app.post("/api/projects/{project_id}/session-search")
def session_academic_search(project_id: str, payload: AcademicSearchIn,
                            x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    key=_search_session_key(x_search_session)
    if payload.save:
        raise HTTPException(409,detail={'code':'session_save_not_ready','message':'会话结果的持久保存尚未接入。'})
    provider=academic_search.provider_from_env()
    try:
        conn=db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
                if payload.search_id:
                    begun=_search_session_store.begin_page(key,project_id,payload.search_id,query=payload.query,
                            filters=payload.filters,provider=provider.name,rule_version=academic_search.RULE_VERSION,cursor=payload.cursor)
                else:
                    begun=_search_session_store.begin(key,project_id,payload.query,payload.filters,provider.name,
                            academic_search.RULE_VERSION,payload.request_id or uuid.uuid4().hex)
        finally:conn.close()
        item=begun['search'];attempt=begun['attempt'];search_id=item['id']
        try:
            result=provider.search(item['query'],item['filters'],payload.cursor,payload.limit)
            candidates=[academic_search.normalize_candidate(raw,raw.get('__provider') or provider.name)
                        for raw in result.get('results',[]) if isinstance(raw,dict)]
            conn=db();sync_warning=None
            try:
                conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
                with _search_session_store.locked():
                    completed=_search_session_store.complete(key,project_id,search_id,attempt,candidates=candidates,
                         next_cursor=result.get('next_cursor'),expanded_queries=result.get('expanded_queries'),
                         diagnostics=result.get('diagnostics'))
                    with _search_session_store.locked_snapshot(key,project_id,search_id) as snapshot:
                        new_version=None
                        conn.execute('SAVEPOINT saved_candidate_refresh')
                        try:
                            new_version=_refresh_saved_session_candidates(conn,project_id,search_id,snapshot)
                            conn.execute('RELEASE SAVEPOINT saved_candidate_refresh')
                        except (sqlite3.Error,HTTPException,SearchSessionError):
                            conn.execute('ROLLBACK TO SAVEPOINT saved_candidate_refresh')
                            conn.execute('RELEASE SAVEPOINT saved_candidate_refresh')
                            sync_warning={'code':'saved_candidate_refresh_failed','message':'新结果已显示，但已保存候选的历史未更新；请查看历史并重新搜索后再保存。'}
                        try:
                            conn.commit()
                        except sqlite3.Error:
                            conn.rollback();new_version=None
                            sync_warning={'code':'saved_candidate_refresh_failed','message':'新结果已显示，但已保存候选的历史未更新；请检查存储后重新搜索。'}
                        if new_version and not sync_warning:
                            _search_session_store.mark_durable_version(key,project_id,search_id,new_version)
            finally:conn.close()
            response=_session_search_response(completed)
            if sync_warning:response['warning']=sync_warning
            return response
        except academic_search.AcademicSearchError as exc:
            failed=_search_session_store.fail(key,project_id,search_id,attempt,code=exc.code,message=str(exc),retryable=exc.retryable)
            return {'ok':False,'status':'failed','search_id':search_id,'candidates':failed['candidates'],
                    'next_cursor':failed['next_cursor'],'error':failed['error']}
        except HTTPException:
            try:_search_session_store.fail(key,project_id,search_id,attempt,code='project_read_only',message='论文已变为只读，检索结果未采用。',retryable=False)
            except SearchSessionError:pass
            raise
        except sqlite3.Error:
            try:_search_session_store.fail(key,project_id,search_id,attempt,code='storage_unavailable',message='存储暂不可用，请检查后重试。',retryable=True)
            except SearchSessionError:pass
            raise
        except SearchSessionError:
            raise
        except Exception:
            failed=_search_session_store.fail(key,project_id,search_id,attempt,code='provider_invalid_response',message='学术来源返回异常，请稍后重试。',retryable=True)
            return {'ok':False,'status':'failed','search_id':search_id,'candidates':failed['candidates'],
                    'next_cursor':failed['next_cursor'],'error':failed['error']}
    except (SearchSessionError,academic_search.AcademicSearchError) as exc:
        raise HTTPException(409,detail={'code':exc.code,'message':str(exc)}) from exc


@app.get("/api/projects/{project_id}/session-search/{search_id}")
def get_session_academic_search(project_id: str, search_id: str,
                                x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    project_or_404(project_id);key=_search_session_key(x_search_session)
    try:return _session_search_response(_search_session_store.get(key,project_id,search_id))
    except SearchSessionError as exc:raise HTTPException(404,detail={'code':exc.code,'message':str(exc)}) from exc


@app.post("/api/projects/{project_id}/session-search/{search_id}/save")
def save_session_academic_search(project_id: str, search_id: str,
                                 x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    key=_search_session_key(x_search_session);conn=db()
    try:
        conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
        with _search_session_store.locked_snapshot(key,project_id,search_id) as snapshot:
            if not snapshot['summary']:
                raise HTTPException(409,detail={'code':'session_result_missing','message':'检索尚无可保存的结果，请重新搜索。'})
            existing=conn.execute('SELECT * FROM academic_searches WHERE id=?',(search_id,)).fetchone()
            if not existing and snapshot['_durable_version']:
                raise HTTPException(409,detail={'code':'search_saved_snapshot_changed','message':'已保存的检索已删除，请重新搜索。'})
            if existing and (existing['project_id']!=project_id or existing['request_id']!=snapshot['request_id']
                             or existing['query']!=snapshot['query'] or existing['provider']!=snapshot['provider']
                             or existing['rule_version']!=snapshot['rule_version']
                             or json.loads(existing['filters_json'] or '{}')!=snapshot['filters']):
                raise HTTPException(409,detail={'code':'search_identity_conflict','message':'检索标识冲突，未保存；请重新搜索。'})
            if existing and (not snapshot['_durable_version'] or existing['updated_at']!=snapshot['_durable_version']):
                raise HTTPException(409,detail={'code':'search_saved_snapshot_changed','message':'已保存的检索已变化；请查看已保存历史后重新搜索。'})
            generation=(int(existing['generation']) if existing else
                        int(conn.execute('SELECT COALESCE(MAX(generation),0)+1 FROM academic_searches WHERE project_id=?',(project_id,)).fetchone()[0]))
            status='succeeded' if snapshot['candidates'] else 'empty'
            save_stamp=now()
            if existing:
                conn.execute("UPDATE academic_searches SET filters_json=?,provider=?,rule_version=?,next_cursor=?,status=?,saved=1,summary_json=?,updated_at=? WHERE id=? AND project_id=?",(json.dumps(snapshot['filters'],ensure_ascii=False),snapshot['provider'],snapshot['rule_version'],snapshot['next_cursor'],status,json.dumps(snapshot['summary'],ensure_ascii=False),save_stamp,search_id,project_id))
            else:
                conn.execute("INSERT INTO academic_searches(id,project_id,query,filters_json,provider,rule_version,request_id,generation,next_cursor,status,saved,created_at,updated_at,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(search_id,project_id,snapshot['query'],json.dumps(snapshot['filters'],ensure_ascii=False),snapshot['provider'],snapshot['rule_version'],snapshot['request_id'],generation,snapshot['next_cursor'],status,1,snapshot['created_at'],save_stamp,json.dumps(snapshot['summary'],ensure_ascii=False)))
            for item in snapshot['candidates']:
                collision=conn.execute('SELECT search_id,dedupe_key FROM academic_candidates WHERE id=?',(item['id'],)).fetchone()
                deduped=conn.execute('SELECT id FROM academic_candidates WHERE search_id=? AND dedupe_key=?',(search_id,item['dedupe_key'])).fetchone()
                if ((collision and (collision['search_id']!=search_id or collision['dedupe_key']!=item['dedupe_key']))
                    or (deduped and deduped['id']!=item['id'])):
                    raise HTTPException(409,detail={'code':'candidate_identity_conflict','message':'候选标识冲突，未保存；请重新搜索。'})
                conn.execute("INSERT INTO academic_candidates(id,search_id,project_id,title,authors,year,venue,doi,canonical_url,abstract,provider,access_status,oa_url,match_reasons_json,sources_json,dedupe_key,rank_score,status,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(search_id,dedupe_key) DO UPDATE SET title=excluded.title,authors=excluded.authors,year=excluded.year,venue=excluded.venue,doi=excluded.doi,canonical_url=excluded.canonical_url,abstract=excluded.abstract,provider=excluded.provider,access_status=excluded.access_status,oa_url=excluded.oa_url,match_reasons_json=excluded.match_reasons_json,sources_json=excluded.sources_json,rank_score=excluded.rank_score,raw_json=excluded.raw_json",(item['id'],search_id,project_id,item['title'],item['authors'],item['year'],item['venue'],item['doi'],item['canonical_url'],item['abstract'],item['provider'],item['access_status'],item.get('oa_url',''),json.dumps(item.get('match_reasons',[]),ensure_ascii=False),json.dumps(item.get('sources',[]),ensure_ascii=False),item['dedupe_key'],item['rank_score'],'saved' if item.get('saved') else 'candidate',json.dumps(item,ensure_ascii=False),now()))
            conn.commit()
            _search_session_store.mark_snapshot_saved(key,project_id,search_id,save_stamp)
        return {'ok':True,'search_id':search_id,'saved':True}
    except SearchSessionError as exc:
        conn.rollback();raise HTTPException(409,detail={'code':exc.code,'message':str(exc)}) from exc
    except Exception:
        conn.rollback();raise
    finally:conn.close()


def _promote_session_candidate(conn, project_id: str, search_id: str, candidate_id: str, snapshot: dict[str, Any], *, status: str) -> str:
    """Copy only one selected candidate while caller owns database and session locks."""
    item = next((c for c in snapshot['candidates'] if c['id'] == candidate_id), None)
    if not item or not snapshot['summary']:
        raise HTTPException(404, detail={'code': 'candidate_missing', 'message': '当前会话中没有该候选，请重新搜索。'})
    existing = conn.execute('SELECT * FROM academic_searches WHERE id=?', (search_id,)).fetchone()
    if not existing and snapshot['_durable_version']:
        raise HTTPException(409, detail={'code': 'search_saved_snapshot_changed', 'message': '已保存的检索已删除，请重新搜索。'})
    if existing and (existing['project_id'] != project_id or existing['request_id'] != snapshot['request_id']
                     or existing['query'] != snapshot['query'] or existing['provider'] != snapshot['provider']
                     or existing['rule_version'] != snapshot['rule_version']
                     or json.loads(existing['filters_json'] or '{}') != snapshot['filters']):
        raise HTTPException(409, detail={'code': 'search_identity_conflict', 'message': '检索标识冲突，请重新搜索。'})
    if existing and (not snapshot['_durable_version'] or existing['updated_at'] != snapshot['_durable_version']):
        raise HTTPException(409, detail={'code': 'search_saved_snapshot_changed', 'message': '已保存的检索已变化，请查看历史后重新搜索。'})
    collision = conn.execute('SELECT search_id,dedupe_key FROM academic_candidates WHERE id=?', (candidate_id,)).fetchone()
    deduped = conn.execute('SELECT id FROM academic_candidates WHERE search_id=? AND dedupe_key=?', (search_id,item['dedupe_key'])).fetchone()
    if ((collision and (collision['search_id'] != search_id or collision['dedupe_key'] != item['dedupe_key']))
        or (deduped and deduped['id'] != candidate_id)):
        raise HTTPException(409, detail={'code': 'candidate_identity_conflict', 'message': '候选标识冲突，请重新搜索。'})
    stamp = now()
    if not existing:
        generation = int(conn.execute('SELECT COALESCE(MAX(generation),0)+1 FROM academic_searches WHERE project_id=?', (project_id,)).fetchone()[0])
        conn.execute("INSERT INTO academic_searches(id,project_id,query,filters_json,provider,rule_version,request_id,generation,next_cursor,status,saved,created_at,updated_at,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (search_id,project_id,snapshot['query'],json.dumps(snapshot['filters'],ensure_ascii=False),snapshot['provider'],snapshot['rule_version'],snapshot['request_id'],generation,None,'partial',0,snapshot['created_at'],stamp,'{}'))
    conn.execute("INSERT INTO academic_candidates(id,search_id,project_id,title,authors,year,venue,doi,canonical_url,abstract,provider,access_status,oa_url,match_reasons_json,sources_json,dedupe_key,rank_score,status,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(search_id,dedupe_key) DO UPDATE SET title=excluded.title,authors=excluded.authors,year=excluded.year,venue=excluded.venue,doi=excluded.doi,canonical_url=excluded.canonical_url,abstract=excluded.abstract,provider=excluded.provider,access_status=excluded.access_status,oa_url=excluded.oa_url,match_reasons_json=excluded.match_reasons_json,sources_json=excluded.sources_json,rank_score=excluded.rank_score,raw_json=excluded.raw_json,status=CASE WHEN ?='saved' THEN 'saved' ELSE academic_candidates.status END",
                 (candidate_id,search_id,project_id,item['title'],item['authors'],item['year'],item['venue'],item['doi'],item['canonical_url'],item['abstract'],item['provider'],item['access_status'],item.get('oa_url',''),json.dumps(item.get('match_reasons',[]),ensure_ascii=False),json.dumps(item.get('sources',[]),ensure_ascii=False),item['dedupe_key'],item['rank_score'],status,json.dumps(item,ensure_ascii=False),stamp,status))
    if not existing or existing['status'] == 'partial':
        rows = conn.execute('SELECT * FROM academic_candidates WHERE search_id=? ORDER BY rank_score DESC,year DESC,dedupe_key', (search_id,)).fetchall()
        selected = [_candidate_payload(row) for row in rows]
        summary = academic_search.search_summary(selected,snapshot['query'],snapshot['filters'],snapshot['provider'])
        summary.update(partial_saved_candidates=True,rule_version=snapshot['rule_version'],
                       diagnostics_unavailable=True,pages_fetched=None,raw_count=None,truncated=None,
                       retrieved_at=snapshot['summary'].get('retrieved_at'))
        conn.execute('UPDATE academic_searches SET summary_json=?,updated_at=? WHERE id=?', (json.dumps(summary,ensure_ascii=False),stamp,search_id))
    else:
        conn.execute('UPDATE academic_searches SET updated_at=? WHERE id=?', (stamp,search_id))
    return stamp


def _refresh_saved_session_candidates(conn, project_id: str, search_id: str, snapshot: dict[str, Any]) -> str | None:
    """Refresh only explicitly saved candidates; never promote other page results."""
    selected = [item for item in snapshot['candidates'] if item.get('saved')]
    if not selected:
        return None
    for item in selected:
        existing=conn.execute('SELECT 1 FROM academic_candidates WHERE id=? AND search_id=? AND project_id=?',
                              (item['id'],search_id,project_id)).fetchone()
        if not existing:
            raise HTTPException(409,detail={'code':'candidate_saved_snapshot_changed','message':'已保存候选已变化。'})
        snapshot['_durable_version']=_promote_session_candidate(conn,project_id,search_id,item['id'],snapshot,status='candidate')
    row=conn.execute('SELECT * FROM academic_searches WHERE id=? AND project_id=?',(search_id,project_id)).fetchone()
    if row and row['status']!='partial':
        candidates=[_candidate_payload(item) for item in conn.execute('SELECT * FROM academic_candidates WHERE search_id=? ORDER BY rank_score DESC,year DESC,dedupe_key',(search_id,)).fetchall()]
        summary=_stored_academic_summary(row,candidates)
        summary['top_matches']=[{'title':item.get('title',''),'doi':item.get('doi',''),'match_reasons':item.get('match_reasons',[])} for item in candidates[:3]]
        conn.execute('UPDATE academic_searches SET summary_json=? WHERE id=? AND project_id=?',(json.dumps(summary,ensure_ascii=False),search_id,project_id))
    return snapshot['_durable_version']


@app.post("/api/projects/{project_id}/session-search/{search_id}/candidates/{candidate_id}/save")
def save_session_candidate(project_id: str, search_id: str, candidate_id: str,
                           x_search_session: str = Header(default='', alias='X-Search-Session')) -> dict[str, Any]:
    key = _search_session_key(x_search_session)
    conn = db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _require_search_writable(conn, project_id)
        with _search_session_store.locked_snapshot(key, project_id, search_id) as snapshot:
            stamp = _promote_session_candidate(conn,project_id,search_id,candidate_id,snapshot,status='saved')
            conn.commit()
            _search_session_store.mark_candidate_saved(key,project_id,search_id,candidate_id,stamp)
        return {'ok': True, 'candidate_id': candidate_id, 'saved': True}
    except SearchSessionError as exc:
        conn.rollback()
        raise HTTPException(409, detail={'code':exc.code,'message':str(exc)}) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/session-search/{search_id}")
def delete_session_academic_search(project_id: str, search_id: str,
                                   x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    project_or_404(project_id);key=_search_session_key(x_search_session)
    try:_search_session_store.delete(key,project_id,search_id)
    except SearchSessionError as exc:raise HTTPException(404,detail={'code':exc.code,'message':str(exc)}) from exc
    return {'ok':True,'deleted':True,'search_id':search_id}


@app.delete("/api/session-search")
def close_academic_search_session(x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    _search_session_store.close(_search_session_key(x_search_session))
    return {'ok':True,'closed':True}


@app.post("/api/projects/{project_id}/search")
def academic_search_endpoint(project_id: str, payload: AcademicSearchIn) -> dict[str, Any]:
    project_or_404(project_id)
    query = academic_search.normalize_query(payload.query)
    if not query:
        raise HTTPException(400, "关键词不能为空")
    try:
        filters = academic_search.validate_filters(payload.filters)
    except academic_search.AcademicSearchError as exc:
        raise HTTPException(400, str(exc)) from exc
    provider = academic_search.provider_from_env()
    request_id = payload.request_id or uuid.uuid4().hex
    stamp = now()
    conn = db()
    conn.execute('BEGIN IMMEDIATE')
    try:_require_search_writable(conn,project_id)
    except Exception:conn.close();raise
    search_id = payload.search_id
    if search_id:
        row = conn.execute("SELECT * FROM academic_searches WHERE id=? AND project_id=?", (search_id, project_id)).fetchone()
        if not row:
            conn.close(); raise HTTPException(404, "搜索快照不存在")
        if row['status']=='partial':
            conn.rollback();conn.close()
            raise HTTPException(409,detail={'code':'partial_snapshot','message':'这里只保留了选中候选；请在原会话保存完整检索，或重新搜索。'})
        if not row['saved'] and not payload.save:
            conn.rollback();conn.close()
            raise HTTPException(409,detail={'code':'legacy_search_requires_explicit_save','message':'旧检索尚未保存；请先显式保存历史，或使用会话检索接口重新搜索。'})
        if query != row["query"] or filters != json.loads(row["filters_json"] or "{}"):
            conn.close(); raise HTTPException(409, "继续分页时不能修改查询条件")
        if row['status']=='superseded':
            conn.close();return {"ok":False,"search_id":search_id,"status":"superseded","candidates":[],"next_cursor":None,"rule_version":academic_search.RULE_VERSION}
        if row['rule_version']!=academic_search.RULE_VERSION:
            conn.close();raise HTTPException(409,detail={'code':'search_rule_changed','message':'检索规则已更新；历史结果仍可查看，请重新搜索以继续。'})
        if row['provider']!=provider.name:
            conn.close();raise HTTPException(409,detail={'code':'search_provider_changed','message':'搜索来源配置已变化；历史结果仍可查看，请重新搜索以继续。'})
        if row['status']=='loading':
            conn.close();raise HTTPException(409,detail={'code':'search_in_progress','message':'该搜索仍在进行，请等待结果。'})
        if payload.cursor is not None and payload.cursor!=row['next_cursor']:
            conn.close();raise HTTPException(409,detail={'code':'search_cursor_stale','message':'搜索页已变化，请刷新当前结果后继续。'})
        generation = int(row["generation"])
        conn.execute("UPDATE academic_searches SET status='loading',saved=MAX(saved,?),updated_at=? WHERE id=? AND project_id=?",(int(payload.save),stamp,search_id,project_id))
    else:
        if not payload.save:
            conn.rollback();conn.close()
            raise HTTPException(409,detail={'code':'legacy_search_requires_explicit_save','message':'旧检索接口默认不再保存查询；请升级到会话检索接口，或明确传入 save=true 保存整次检索。'})
        generation = int(conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM academic_searches WHERE project_id=?", (project_id,)).fetchone()[0])
        search_id = uuid.uuid4().hex[:16]
        conn.execute("UPDATE academic_searches SET status='superseded',updated_at=? WHERE project_id=? AND status IN ('loading','succeeded')", (stamp, project_id))
        conn.execute("INSERT INTO academic_searches(id,project_id,query,filters_json,provider,rule_version,request_id,generation,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (search_id, project_id, query, json.dumps(filters, ensure_ascii=False), provider.name, academic_search.RULE_VERSION, request_id, generation, "loading", stamp, stamp))
    conn.commit(); conn.close()
    try:
        result = provider.search(query, filters, payload.cursor, payload.limit)
        normalized = [academic_search.normalize_candidate(item, item.get("__provider") or provider.name) for item in result.get("results", []) if isinstance(item, dict)]
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        latest = conn.execute("SELECT generation,status FROM academic_searches WHERE id=? AND project_id=?", (search_id,project_id)).fetchone()
        project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
        if project and (project['archived'] or project['lifecycle_state']=='published'):
            conn.execute("UPDATE academic_searches SET status='superseded',updated_at=? WHERE id=? AND project_id=? AND generation=? AND status='loading'",(now(),search_id,project_id,generation))
            conn.commit();conn.close()
            return {"ok":False,"search_id":search_id,"status":"superseded","candidates":[],"next_cursor":None,"rule_version":academic_search.RULE_VERSION}
        if not latest or int(latest["generation"]) != generation or latest['status']!='loading':
            conn.rollback();conn.close()
            return {"ok": False, "search_id": search_id, "status": "superseded", "candidates": [], "next_cursor": None, "rule_version": academic_search.RULE_VERSION}
        prior={}
        if payload.search_id and row['summary_json']:
            try:prior=json.loads(row['summary_json'])
            except (ValueError,TypeError):pass
            if not isinstance(prior,dict):prior={}
        prior_rows=conn.execute('SELECT * FROM academic_candidates WHERE search_id=? AND project_id=?',(search_id,project_id)).fetchall()
        fields=('title','authors','year','venue','doi','canonical_url','abstract','provider','access_status','oa_url','dedupe_key')
        prior_candidates=[]
        for previous in prior_rows:
            try:raw=json.loads(previous['raw_json'] or '{}')
            except (ValueError,TypeError):raw={}
            if not isinstance(raw,dict):raw={}
            prior_candidates.append({**{field:previous[field] for field in fields},'external_id':raw.get('external_id',''),'sources':json.loads(previous['sources_json'] or '[]')})
        ranked=academic_search.rank_and_deduplicate(prior_candidates+normalized,query,result.get('expanded_queries') or prior.get('expanded_queries'))
        for item in ranked:
            candidate_id = uuid.uuid4().hex[:16]
            conn.execute("INSERT INTO academic_candidates(id,search_id,project_id,title,authors,year,venue,doi,canonical_url,abstract,provider,access_status,oa_url,match_reasons_json,sources_json,dedupe_key,rank_score,status,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(search_id,dedupe_key) DO UPDATE SET title=excluded.title,authors=excluded.authors,year=excluded.year,venue=excluded.venue,doi=excluded.doi,canonical_url=excluded.canonical_url,abstract=excluded.abstract,provider=excluded.provider,access_status=excluded.access_status,oa_url=excluded.oa_url,match_reasons_json=excluded.match_reasons_json,sources_json=excluded.sources_json,rank_score=excluded.rank_score,raw_json=excluded.raw_json", (candidate_id, search_id, project_id, item["title"], item["authors"], item["year"], item["venue"], item["doi"], item["canonical_url"], item["abstract"], item["provider"], item["access_status"], item.get("oa_url", ""), json.dumps(item.get("match_reasons", []), ensure_ascii=False), json.dumps(item.get("sources", []), ensure_ascii=False), item["dedupe_key"], item["rank_score"], "candidate", json.dumps(item, ensure_ascii=False), now()))
        saved = 1 if payload.save else 0
        has_candidates=bool(conn.execute('SELECT 1 FROM academic_candidates WHERE search_id=? LIMIT 1',(search_id,)).fetchone())
        conn.execute("UPDATE academic_searches SET next_cursor=?,status=?,saved=MAX(saved,?),updated_at=? WHERE id=?", (result.get("next_cursor"), "succeeded" if has_candidates else "empty", saved, now(), search_id))
        rows = conn.execute("SELECT * FROM academic_candidates WHERE search_id=? ORDER BY rank_score DESC, year DESC, dedupe_key", (search_id,)).fetchall()
        search_row = conn.execute("SELECT * FROM academic_searches WHERE id=?", (search_id,)).fetchone()
        candidate_payloads = [_candidate_payload(row) for row in rows]
        diagnostics = result.get("diagnostics") or []
        current_diagnostics=diagnostics or [{"provider":provider.name,"query":query,"pages":1,"raw_count":len(result.get('results') or []),"exhausted":not bool(result.get('next_cursor'))}]
        prior_diagnostics=prior.get('provider_diagnostics') or []
        all_diagnostics=prior_diagnostics+current_diagnostics
        diagnostic_summary = {
            "expanded_queries": result.get("expanded_queries") or prior.get('expanded_queries') or [query],
            "sources": sorted({item.get("provider") for item in all_diagnostics if item.get("provider")}),
            "pages_fetched": sum(int(item.get("pages") or 0) for item in all_diagnostics),
            "raw_count": sum(int(item.get("raw_count") or 0) for item in all_diagnostics),
            "source_failures": [{"provider": item.get("provider"), "error": item.get("error")} for item in all_diagnostics if item.get("error")],
            "truncated": any(item.get("exhausted") is False for item in current_diagnostics if not item.get("error")),
        }
        summary=academic_search.search_summary(candidate_payloads,query,filters,provider.name,diagnostic_summary)
        summary['provider_diagnostics']=all_diagnostics
        summary['has_more']=bool(result.get('next_cursor'))
        summary['retrieved_at']=now()
        conn.execute('UPDATE academic_searches SET summary_json=? WHERE id=? AND project_id=?',(json.dumps(summary,ensure_ascii=False),search_id,project_id))
        conn.commit();conn.close()
        return {"ok": True, "status": search_row["status"], "search": _search_row_payload(search_row), "candidates": candidate_payloads, "next_cursor": result.get("next_cursor"), "provider_status": {"provider": provider.name, "available": True, "diagnostics": diagnostics}, "summary": summary}
    except academic_search.AcademicSearchError as exc:
        conn = db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
                read_only=not project or project['archived'] or project['lifecycle_state']=='published'
                changed=conn.execute("UPDATE academic_searches SET status=?,updated_at=? WHERE id=? AND project_id=? AND generation=? AND status='loading'", ('superseded' if read_only else 'failed',now(),search_id,project_id,generation)).rowcount
                if read_only:changed=0
        finally:conn.close()
        if not changed:return {"ok":False,"search_id":search_id,"status":"superseded","candidates":[],"next_cursor":None,"rule_version":academic_search.RULE_VERSION}
        return {"ok": False, "search_id": search_id, "status": "failed", "candidates": [], "error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}}


@app.get("/api/projects/{project_id}/search/{search_id}")
def get_academic_search(project_id: str, search_id: str) -> dict[str, Any]:
    project_or_404(project_id); conn = db()
    search = conn.execute("SELECT * FROM academic_searches WHERE id=? AND project_id=?", (search_id, project_id)).fetchone()
    if not search:
        conn.close(); raise HTTPException(404, "搜索快照不存在")
    rows = conn.execute("SELECT * FROM academic_candidates WHERE search_id=? ORDER BY rank_score DESC,year DESC,dedupe_key", (search_id,)).fetchall(); conn.close()
    candidates = [_candidate_payload(row) for row in rows]
    summary=_stored_academic_summary(search,candidates)
    return {"ok": True, "search": _search_row_payload(search), "candidates": candidates, "summary": summary}


@app.get("/api/projects/{project_id}/searches")
def list_academic_searches(project_id: str, limit: int = 12) -> dict[str, Any]:
    project_or_404(project_id); limit = max(1, min(int(limit), 50)); conn = db()
    rows = conn.execute("SELECT s.*, COUNT(c.id) AS candidate_count, SUM(CASE WHEN c.access_status='open_access' THEN 1 ELSE 0 END) AS oa_count FROM academic_searches s LEFT JOIN academic_candidates c ON c.search_id=s.id WHERE s.project_id=? GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?", (project_id, limit)).fetchall(); conn.close()
    return {"ok": True, "searches": [{**_search_row_payload(row), "candidate_count": int(row["candidate_count"] or 0), "oa_count": int(row["oa_count"] or 0)} for row in rows]}


@app.post("/api/projects/{project_id}/search/{search_id}/save")
def save_academic_search(project_id: str, search_id: str) -> dict[str, Any]:
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
            row=conn.execute("SELECT status FROM academic_searches WHERE id=? AND project_id=?",(search_id,project_id)).fetchone()
            if row and row['status']=='partial':
                raise HTTPException(409,detail={'code':'partial_snapshot','message':'这里只保存了选中的候选，完整检索需在原会话中保存。'})
            result=conn.execute("UPDATE academic_searches SET saved=1,updated_at=? WHERE id=? AND project_id=?",(now(),search_id,project_id))
            if not result.rowcount:raise HTTPException(404,'搜索快照不存在')
    finally:conn.close()
    return {'ok':True,'search_id':search_id,'saved':True}


@app.delete("/api/projects/{project_id}/search/{search_id}")
def delete_academic_search(project_id: str, search_id: str) -> dict[str, Any]:
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
            result=conn.execute('DELETE FROM academic_searches WHERE id=? AND project_id=?',(search_id,project_id))
            if not result.rowcount:raise HTTPException(404,'搜索快照不存在')
    finally:conn.close()
    return {'ok':True,'search_id':search_id,'deleted':True}


def _set_academic_candidate_saved_in_transaction(conn, project_id: str, candidate_id: str, saved: bool) -> dict[str, Any]:
    candidate=conn.execute("SELECT c.search_id,s.status AS search_status FROM academic_candidates c JOIN academic_searches s ON s.id=c.search_id WHERE c.id=? AND c.project_id=?",(candidate_id,project_id)).fetchone()
    if not candidate:raise HTTPException(404,'候选论文不存在')
    if not saved and candidate['search_status']=='partial':
        referenced=conn.execute("SELECT 1 FROM academic_handoffs WHERE candidate_id=? UNION ALL SELECT 1 FROM oa_imports WHERE candidate_id=? LIMIT 1",(candidate_id,candidate_id)).fetchone()
        if not referenced:
            conn.execute('DELETE FROM academic_candidates WHERE id=? AND project_id=?',(candidate_id,project_id))
            search_id=candidate['search_id']
            rows=conn.execute('SELECT * FROM academic_candidates WHERE search_id=? ORDER BY rank_score DESC,year DESC,dedupe_key',(search_id,)).fetchall()
            if not rows:
                conn.execute("DELETE FROM academic_searches WHERE id=? AND project_id=? AND status='partial'",(search_id,project_id))
            else:
                search=conn.execute('SELECT * FROM academic_searches WHERE id=?',(search_id,)).fetchone()
                summary=academic_search.search_summary([_candidate_payload(row) for row in rows],search['query'],json.loads(search['filters_json'] or '{}'),search['provider'])
                summary.update(partial_saved_candidates=True,rule_version=search['rule_version'],diagnostics_unavailable=True,pages_fetched=None,raw_count=None,truncated=None,retrieved_at=None)
                conn.execute('UPDATE academic_searches SET summary_json=?,updated_at=? WHERE id=?',(json.dumps(summary,ensure_ascii=False),now(),search_id))
            return {'ok':True,'candidate_id':candidate_id,'saved':False}
    result=conn.execute('UPDATE academic_candidates SET status=? WHERE id=? AND project_id=?',('saved' if saved else 'candidate',candidate_id,project_id))
    if not result.rowcount:raise HTTPException(404,'候选论文不存在')
    conn.execute('UPDATE academic_searches SET updated_at=? WHERE id=(SELECT search_id FROM academic_candidates WHERE id=? AND project_id=?)',(now(),candidate_id,project_id))
    return {'ok':True,'candidate_id':candidate_id,'saved':saved}


def _set_academic_candidate_saved(project_id: str, candidate_id: str, saved: bool) -> dict[str, Any]:
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
            return _set_academic_candidate_saved_in_transaction(conn,project_id,candidate_id,saved)
    finally:conn.close()


@app.delete("/api/projects/{project_id}/session-search/{search_id}/candidates/{candidate_id}/save")
def unsave_session_candidate(project_id: str, search_id: str, candidate_id: str,
                             x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    key = _search_session_key(x_search_session);conn=db()
    try:
        conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
        with _search_session_store.locked_snapshot(key,project_id,search_id) as snapshot:
            if not any(c['id']==candidate_id for c in snapshot['candidates']):
                raise HTTPException(404,detail={'code':'candidate_missing','message':'当前会话中没有该候选。'})
            existing=conn.execute('SELECT * FROM academic_searches WHERE id=? AND project_id=?',(search_id,project_id)).fetchone()
            if not existing or not snapshot['_durable_version'] or existing['updated_at']!=snapshot['_durable_version']:
                raise HTTPException(409,detail={'code':'search_saved_snapshot_changed','message':'已保存的检索已变化，请查看历史后重新搜索。'})
            result=_set_academic_candidate_saved_in_transaction(conn,project_id,candidate_id,False)
            row=conn.execute('SELECT updated_at FROM academic_searches WHERE id=? AND project_id=?',(search_id,project_id)).fetchone()
            conn.commit()
            _search_session_store.mark_candidate_unsaved(key,project_id,search_id,candidate_id,row['updated_at'] if row else None)
            return result
    except SearchSessionError as exc:
        conn.rollback();raise HTTPException(409,detail={'code':exc.code,'message':str(exc)}) from exc
    except Exception:
        conn.rollback();raise
    finally:conn.close()


@app.post("/api/projects/{project_id}/search/candidates/{candidate_id}/save")
def save_academic_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
    return _set_academic_candidate_saved(project_id,candidate_id,True)


@app.delete("/api/projects/{project_id}/search/candidates/{candidate_id}/save")
def unsave_academic_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
    return _set_academic_candidate_saved(project_id,candidate_id,False)


def _handoff_academic_candidate(conn, project_id: str, payload: AcademicHandoffIn, *, summary_override=None) -> dict[str, Any]:
    from paperpilot.services.academic_handoff import handoff
    if summary_override is None:
        row = conn.execute("SELECT s.* FROM academic_searches s JOIN academic_candidates c ON c.search_id=s.id WHERE c.id=? AND c.project_id=?", (payload.candidate_id, project_id)).fetchone()
        summary_override = _stored_academic_summary(row, []) if row else {}
    return handoff(conn, project_id, payload, summary_override=summary_override)

@app.post("/api/projects/{project_id}/search/handoff")
def handoff_academic_candidate(project_id: str, payload: AcademicHandoffIn) -> dict[str, Any]:
    conn = db()
    try:
        conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
        result = _handoff_academic_candidate(conn,project_id,payload)
        conn.commit()
        return result
    except Exception:
        conn.rollback();raise
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/session-search/{search_id}/handoff")
def handoff_session_academic_candidate(project_id: str, search_id: str, payload: AcademicHandoffIn,
                                       x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    key = _search_session_key(x_search_session);conn = db()
    try:
        conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
        with _search_session_store.locked_snapshot(key,project_id,search_id) as snapshot:
            _promote_session_candidate(conn,project_id,search_id,payload.candidate_id,snapshot,status='candidate')
            selected = next(c for c in snapshot['candidates'] if c['id']==payload.candidate_id)
            summary = academic_search.search_summary([selected],snapshot['query'],snapshot['filters'],snapshot['provider'])
            summary.update(partial_saved_candidates=True,rule_version=snapshot['rule_version'],diagnostics_unavailable=True,
                           pages_fetched=None,raw_count=None,truncated=None,retrieved_at=snapshot['summary'].get('retrieved_at'))
            result = _handoff_academic_candidate(conn,project_id,payload,summary_override=summary)
            stamp = conn.execute('SELECT updated_at FROM academic_searches WHERE id=?',(search_id,)).fetchone()[0]
            conn.commit()
            _search_session_store.mark_durable_version(key,project_id,search_id,stamp)
            return result
    except SearchSessionError as exc:
        conn.rollback();raise HTTPException(409,detail={'code':exc.code,'message':str(exc)}) from exc
    except Exception:
        conn.rollback();raise
    finally:
        conn.close()


def _oa_queue_cleanup(conn, project_id, import_id, attempt_id, *, owner_pid=0, owner_token='', wait_for_owner=False):
    conn.execute("INSERT OR IGNORE INTO oa_cleanup_queue(project_id,import_id,attempt_id,owner_pid,owner_token,wait_for_owner) VALUES(?,?,?,?,?,?)",(project_id,import_id,attempt_id or '',owner_pid,owner_token,int(wait_for_owner)))


def _oa_cleanup_path(project_id, import_id, attempt_id):
    # IDs and every directory must match exactly; never follow an imported link.
    import re
    if not all(re.fullmatch(pattern,str(value or '')) for pattern,value in [(r'[0-9a-f]{12}',project_id),(r'[0-9a-f]{16}',import_id),(r'[0-9a-f]{32}',attempt_id)]):
        raise ValueError('unknown file identity')
    root=Path(PROJECTS_DIR).resolve();parent=root/project_id/'imports'/import_id
    for directory in (root/project_id,root/project_id/'imports',parent):
        if directory.is_symlink():raise ValueError('linked import directory')
    if parent.resolve()!=parent:raise ValueError('import path escaped project')
    path=parent/('source-'+attempt_id+'.bin')
    if path.is_symlink():raise ValueError('linked import file')
    return path


def _oa_drain_cleanup(conn, project_id=None):
    from paperpilot.services.tex_runtime import owner_state
    sql='SELECT * FROM oa_cleanup_queue' + (' WHERE project_id=?' if project_id else '')
    for item in conn.execute(sql,(project_id,) if project_id else ()).fetchall():
        key=(item['project_id'],item['import_id'],item['attempt_id'])
        if item['wait_for_owner'] and owner_state(dict(item))!='dead':continue
        current=conn.execute('SELECT status,attempt_id,file_path FROM oa_imports WHERE project_id=? AND id=?',key[:2]).fetchone()
        if current and current['status']=='preview_ready' and current['attempt_id']==item['attempt_id']:continue
        try:
            path=_oa_cleanup_path(*key)
            if current and current['status']=='imported' and conn.execute('SELECT 1 FROM external_evidence WHERE project_id=? AND import_id=? AND attachment_path=?',(*key[:2],str(path))).fetchone():continue
            path.unlink(missing_ok=True)
            conn.execute("UPDATE external_evidence SET attachment_path='' WHERE project_id=? AND import_id=? AND attachment_path=?",(*key[:2],str(path)))
            conn.execute('DELETE FROM oa_cleanup_queue WHERE project_id=? AND import_id=? AND attempt_id=?',key)
        except (OSError,ValueError):
            conn.execute("UPDATE oa_cleanup_queue SET last_error='cleanup_unavailable' WHERE project_id=? AND import_id=? AND attempt_id=?",key)


def _oa_cleanup_pending(conn, project_id, import_id):
    return bool(conn.execute('SELECT 1 FROM oa_cleanup_queue WHERE project_id=? AND import_id=? LIMIT 1',(project_id,import_id)).fetchone())


def _oa_cleanup_after_commit(project_id, import_id):
    """Process only committed cleanup intent; an error leaves the intent durable."""
    try:
        conn=db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                _oa_drain_cleanup(conn,project_id)
                return _oa_cleanup_pending(conn,project_id,import_id)
        finally:conn.close()
    except sqlite3.Error:
        return True


def _oa_finish_attempt(project_id, import_id, attempt_id):
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE oa_cleanup_queue SET wait_for_owner=0 WHERE project_id=? AND import_id=? AND attempt_id=?',(project_id,import_id,attempt_id))
    finally:conn.close()
    return _oa_cleanup_after_commit(project_id,import_id)


def _recover_oa_fetches(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    """Only a verified dead owner is recovered; legacy/unknown owners stay cancellable."""
    from paperpilot.services.tex_runtime import owner_state
    sql="SELECT id,project_id,attempt_id,owner_pid,owner_token FROM oa_imports WHERE status='fetching'"
    rows=conn.execute(sql + (' AND project_id=?' if project_id else ''), (project_id,) if project_id else ()).fetchall()
    for row in rows:
        if owner_state(dict(row))=='dead':
            changed=conn.execute("UPDATE oa_imports SET status='failed',error_code='fetch_interrupted',error_message=?,updated_at=? WHERE id=? AND project_id=? AND status='fetching' AND attempt_id=? AND owner_pid=? AND owner_token=?",('抓取进程已中断；未确认的内容不会入库，请重新获取。',now(),row['id'],row['project_id'],row['attempt_id'],row['owner_pid'],row['owner_token'])).rowcount
            if changed:
                _oa_queue_cleanup(conn,row['project_id'],row['id'],row['attempt_id'])


def _oa_import_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for field in ("preview_json", "source_json"):
        target = field.removesuffix("_json")
        try:
            item[target] = json.loads(item.pop(field, "{}") or "{}")
        except (TypeError, ValueError):
            item[target] = {}
    item["preview"].pop("full_fragments", None)
    item.pop("file_path", None)
    item.pop("request_fingerprint", None)
    item.pop("attempt_id", None)
    if item.get('status')=='fetching':
        from paperpilot.services.tex_runtime import owner_state
        item['recovery_state']=owner_state(item)
    item.pop('owner_pid', None)
    item.pop('owner_token', None)
    return item


def _oa_request_fingerprint(project_id: str, payload: ImportIn) -> str:
    identity = {"project_id": project_id, "candidate_id": payload.candidate_id or "", "doi": (payload.doi or "").strip().lower(), "url": (payload.url or "").strip()}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _oa_check_existing(row, project_id: str, payload: ImportIn, fingerprint: str) -> None:
    if not row:return
    legacy_match = not row['request_fingerprint'] and (
        (bool(payload.candidate_id) and not payload.doi and not payload.url and row['candidate_id']==payload.candidate_id)
        or (bool(payload.doi) and not payload.candidate_id and not payload.url and row['doi'].strip().lower()==payload.doi.strip().lower())
        or (bool(payload.url) and not payload.candidate_id and not payload.doi and not row['doi'] and not row['candidate_id'] and row['source_url']==payload.url.strip())
    )
    if row['project_id'] != project_id or (row['request_fingerprint'] != fingerprint and not legacy_match):
        raise HTTPException(409, detail={'code':'import_idempotency_conflict','message':'导入请求编号已用于其他论文、来源或旧记录；请使用新的请求编号。'})


def _oa_write_import_record(conn, project_id: str, payload: ImportIn, metadata: dict[str, Any], key: str, fingerprint: str, existing) -> str:
    """Create or restart a single import inside the caller transaction."""
    import_id=existing['id'] if existing else uuid.uuid4().hex[:16]
    attempt_id=uuid.uuid4().hex;stamp=now();access_status=metadata.get('access_status') or 'metadata_only';oa_url=metadata.get('oa_url') or ''
    status='metadata_only' if access_status!='open_access' or not oa_url else 'created'
    if existing:
        conn.execute("UPDATE oa_imports SET status=?,error_code='',error_message='',preview_json='{}',file_sha256='',file_path='',owner_pid=0,owner_token='',attempt_id=?,request_fingerprint=?,updated_at=? WHERE id=? AND project_id=?",(status,attempt_id,fingerprint,stamp,import_id,project_id))
    else:
        conn.execute("INSERT INTO oa_imports(id,project_id,candidate_id,idempotency_key,doi,title,source_url,oa_url,access_status,status,source_json,created_at,updated_at,request_fingerprint,attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(import_id,project_id,payload.candidate_id,key,metadata.get('doi',''),metadata.get('title',''),metadata.get('canonical_url','') or payload.url or '',oa_url,access_status,status,json.dumps(metadata,ensure_ascii=False),stamp,stamp,fingerprint,attempt_id))
    return import_id


@app.post("/api/projects/{project_id}/imports")
def create_oa_import(project_id: str, payload: ImportIn) -> dict[str, Any]:
    require_project_writable(project_id)
    fingerprint = _oa_request_fingerprint(project_id,payload)

    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            _recover_oa_fetches(conn, project_id)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            _oa_drain_cleanup(conn, project_id)
        if payload.idempotency_key:
            prior = conn.execute('SELECT * FROM oa_imports WHERE idempotency_key=?', (payload.idempotency_key,)).fetchone()
            _oa_check_existing(prior,project_id,payload,fingerprint)
            if prior and prior['status'] not in {'failed','cancelled'}:
                if prior['status']=='created' and not payload.defer_fetch:
                    return _fetch_oa_import(project_id,prior['id'])
                return {'ok':True,'idempotent_replay':True,'import':_oa_import_payload(prior)}
        if payload.candidate_id:
            candidate = conn.execute("SELECT * FROM academic_candidates WHERE id=? AND project_id=?", (payload.candidate_id, project_id)).fetchone()
            if not candidate:
                raise HTTPException(404, "候选论文不存在")
            metadata = {"title":candidate["title"],"authors":candidate["authors"],"year":candidate["year"],"venue":candidate["venue"],"doi":candidate["doi"],"canonical_url":candidate["canonical_url"],"abstract":candidate["abstract"],"provider":candidate["provider"],"access_status":candidate["access_status"],"oa_url":candidate["oa_url"]}
        elif payload.url and not payload.doi and "doi.org/" not in payload.url.lower():
            direct_url = oa_imports.normalize_url(payload.url)
            if not direct_url:raise HTTPException(400, "论文 URL 无效")
            metadata = {"title":"外部 OA 文档","authors":"","year":"","venue":"","doi":"","canonical_url":direct_url,"abstract":"","provider":"direct_url","access_status":"open_access","oa_url":direct_url}
        elif payload.doi or payload.url:
            try:metadata = oa_imports.metadata_from_raw(oa_imports.resolve_openalex(payload.doi or payload.url or ""))
            except oa_imports.ImportError as exc:return {"ok":False,"status":"failed","error":{"code":exc.code,"message":str(exc),"retryable":exc.retryable}}
        else:raise HTTPException(400, "需要 candidate_id、doi 或 url")
        key = payload.idempotency_key or f"{project_id}:{metadata.get('doi') or metadata.get('canonical_url') or payload.candidate_id}"
        conn.execute('BEGIN IMMEDIATE')
        project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
        if not project or project['archived'] or project['lifecycle_state']=='published':
            raise HTTPException(409,detail={'code':'project_read_only','message':'论文已归档、发布或不存在；未开始导入。'})
        existing=conn.execute('SELECT * FROM oa_imports WHERE idempotency_key=?',(key,)).fetchone()
        _oa_check_existing(existing,project_id,payload,fingerprint)
        if existing and existing['status'] not in {'failed','cancelled'}:
            conn.commit()
            if existing['status']=='created' and not payload.defer_fetch:
                return _fetch_oa_import(project_id,existing['id'])
            return {'ok':True,'idempotent_replay':True,'import':_oa_import_payload(existing)}
        import_id=_oa_write_import_record(conn,project_id,payload,metadata,key,fingerprint,existing)
        conn.commit();row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone();status=row['status']
    except Exception:
        conn.rollback();raise
    finally:conn.close()
    if status=='metadata_only':
        return {'ok':True,'status':status,'import':_oa_import_payload(row),'next_action':'非 OA 论文仅保留检索结果和来源链接，不抓取正文'}
    if payload.defer_fetch:
        return {'ok':True,'status':'created','import':_oa_import_payload(row)}
    return _fetch_oa_import(project_id, import_id)


@app.post("/api/projects/{project_id}/session-search/{search_id}/imports")
def create_session_oa_import(project_id: str, search_id: str, payload: ImportIn,
                             x_search_session: str = Header(default='',alias='X-Search-Session')) -> dict[str, Any]:
    if not payload.candidate_id or payload.doi or payload.url:
        raise HTTPException(400,detail={'code':'session_candidate_required','message':'请选择当前会话中的候选，不能同时指定 DOI 或链接。'})
    key_session = _search_session_key(x_search_session)
    require_project_writable(project_id)
    fingerprint = _oa_request_fingerprint(project_id,payload)
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE');_recover_oa_fetches(conn,project_id)
        with conn:
            conn.execute('BEGIN IMMEDIATE');_oa_drain_cleanup(conn,project_id)
        conn.execute('BEGIN IMMEDIATE');_require_search_writable(conn,project_id)
        with _search_session_store.locked_snapshot(key_session,project_id,search_id) as snapshot:
            stamp = _promote_session_candidate(conn,project_id,search_id,payload.candidate_id,snapshot,status='candidate')
            candidate = conn.execute('SELECT * FROM academic_candidates WHERE id=? AND project_id=?',(payload.candidate_id,project_id)).fetchone()
            metadata = {name:candidate[name] for name in ('title','authors','year','venue','doi','canonical_url','abstract','provider','access_status','oa_url')}
            key = payload.idempotency_key or f"{project_id}:{metadata.get('doi') or metadata.get('canonical_url') or payload.candidate_id}"
            existing = conn.execute('SELECT * FROM oa_imports WHERE idempotency_key=?',(key,)).fetchone()
            _oa_check_existing(existing,project_id,payload,fingerprint)
            replay = bool(existing and existing['status'] not in {'failed','cancelled'})
            import_id = existing['id'] if replay else _oa_write_import_record(conn,project_id,payload,metadata,key,fingerprint,existing)
            row = conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
            conn.commit()
            _search_session_store.mark_durable_version(key_session,project_id,search_id,stamp)
    except SearchSessionError as exc:
        conn.rollback();raise HTTPException(409,detail={'code':exc.code,'message':str(exc)}) from exc
    except Exception:
        conn.rollback();raise
    finally:
        conn.close()
    if replay and (row['status']!='created' or payload.defer_fetch):
        return {'ok':True,'idempotent_replay':True,'import':_oa_import_payload(row)}
    if row['status']=='metadata_only':
        return {'ok':True,'status':'metadata_only','import':_oa_import_payload(row),'next_action':'非 OA 论文仅保留检索结果和来源链接，不抓取正文'}
    if payload.defer_fetch:
        return {'ok':True,'status':'created','import':_oa_import_payload(row)}
    return _fetch_oa_import(project_id,import_id)


@app.post("/api/projects/{project_id}/imports/{import_id}/fetch")
def fetch_oa_import(project_id: str, import_id: str) -> dict[str, Any]:
    require_project_writable(project_id)
    return _fetch_oa_import(project_id, import_id)


def _fetch_oa_import(project_id: str, import_id: str) -> dict[str, Any]:
    from paperpilot.services.tex_runtime import current_owner
    try:owner=current_owner()
    except OSError as exc:
        raise HTTPException(503, detail={'code':'worker_identity_unavailable','message':'无法确认抓取进程身份，请稍后重试。'}) from exc
    conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
            if not project or project['archived'] or project['lifecycle_state']=='published':
                raise HTTPException(409,detail={'code':'project_read_only','message':'论文已归档、发布或不存在；未开始抓取。'})
            row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
            if not row:raise HTTPException(404,'导入记录不存在')
            if row['status']!='created':
                return {'ok':False,'status':row['status'],'import':_oa_import_payload(row),'next_action':'该导入已开始、取消或完成；请查看当前状态。'}
            conn.execute("UPDATE oa_imports SET status='fetching',owner_pid=?,owner_token=?,updated_at=? WHERE id=? AND project_id=? AND status='created'",(owner['owner_pid'],owner['owner_token'],now(),import_id,project_id))
            attempt_id=row['attempt_id'];oa_url=row['oa_url'];metadata=json.loads(row['source_json'] or '{}')
            _oa_queue_cleanup(conn,project_id,import_id,attempt_id,owner_pid=owner['owner_pid'],owner_token=owner['owner_token'],wait_for_owner=True)
    finally:conn.close()
    file_path=None
    try:
        content,content_type,file_sha256=oa_imports.fetch_public(oa_url)
        project_dir=PROJECTS_DIR/project_id/'imports'/import_id;project_dir.mkdir(parents=True,exist_ok=True)
        file_path=project_dir/('source-'+attempt_id+'.bin');file_path.write_bytes(content)
        preview=oa_imports.extract_preview(content,content_type,metadata)
        conn=db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                changed=conn.execute("UPDATE oa_imports SET status='preview_ready',preview_json=?,file_sha256=?,file_path=?,updated_at=? WHERE id=? AND project_id=? AND status='fetching' AND attempt_id=?",(json.dumps(preview,ensure_ascii=False),file_sha256,str(file_path),now(),import_id,project_id,attempt_id)).rowcount
                if changed:conn.execute('DELETE FROM oa_cleanup_queue WHERE project_id=? AND import_id=? AND attempt_id=?',(project_id,import_id,attempt_id))
                row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
        finally:conn.close()
        if not changed:
            result={'ok':False,'status':'superseded','next_action':'此抓取已取消或被新尝试替代，迟到结果未写入；请查看当前导入记录。'}
            return result
        result={'ok':True,'status':'preview_ready','import':_oa_import_payload(row)}
        return result
    except Exception as exc:
        code=exc.code if isinstance(exc,oa_imports.ImportError) else 'import_storage_failed' if isinstance(exc,OSError) else 'import_processing_failed'
        message=str(exc) if isinstance(exc,oa_imports.ImportError) else '导入文件无法保存；请检查磁盘后重试。' if isinstance(exc,OSError) else '导入处理未完成；当前内容未入库，请重试或检查来源文件。'
        fallback='metadata_only' if code in {'fetch_denied','not_found','no_oa_location'} else 'failed'
        conn=db()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                changed=conn.execute("UPDATE oa_imports SET status=?,error_code=?,error_message=?,updated_at=? WHERE id=? AND project_id=? AND status='fetching' AND attempt_id=?",(fallback,code,message,now(),import_id,project_id,attempt_id)).rowcount
                row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
        finally:conn.close()
        if not changed:
            result={'ok':False,'status':'superseded','next_action':'此抓取已取消或被新尝试替代，请查看当前导入记录。'}
        else:
            result={'ok':fallback=='metadata_only','status':fallback,'import':_oa_import_payload(row),'error':{'code':code,'message':message,'retryable':getattr(exc,'retryable',True)}}
        return result
    finally:
        try:pending=_oa_finish_attempt(project_id,import_id,attempt_id)
        except sqlite3.Error:pending=True  # Durable record remains for a later retry.
        if 'result' in locals():result['cleanup_pending']=pending


@app.get("/api/projects/{project_id}/imports/{import_id}")
def get_oa_import(project_id: str, import_id: str) -> dict[str, Any]:
    project_or_404(project_id);conn=db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            _recover_oa_fetches(conn,project_id)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            _oa_drain_cleanup(conn,project_id)
            row=conn.execute("SELECT * FROM oa_imports WHERE id=? AND project_id=?", (import_id,project_id)).fetchone()
            pending=_oa_cleanup_pending(conn,project_id,import_id)
    finally:conn.close()
    if not row:
        raise HTTPException(404, "导入记录不存在")
    return {"ok": True, "import": _oa_import_payload(row), "cleanup_pending":pending}


@app.post("/api/projects/{project_id}/imports/{import_id}/confirm")
def confirm_oa_import(project_id: str, import_id: str, payload: ImportConfirmIn) -> dict[str, Any]:
    require_project_writable(project_id);conn=db();cleanup_path=''
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            project=conn.execute('SELECT archived,lifecycle_state FROM projects WHERE id=?',(project_id,)).fetchone()
            if not project or project['archived'] or project['lifecycle_state']=='published':
                raise HTTPException(409,detail={'code':'project_read_only','message':'论文已归档、发布或不存在；未确认导入。'})
            row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
            if not row:raise HTTPException(404,'导入记录不存在')
            if row['status']=='imported':
                evidence=conn.execute('SELECT id FROM external_evidence WHERE import_id=? AND project_id=? ORDER BY confirmed_at DESC LIMIT 1',(import_id,project_id)).fetchone()
                result={'ok':True,'status':'imported','idempotent_replay':True,'evidence_id':evidence['id'] if evidence else None}
            else:
                if row['status']!='preview_ready':raise HTTPException(409,'只有预览完成的 OA 论文才能确认入库')
                preview=json.loads(row['preview_json'] or '{}')
                if payload.selected_fragments:
                    from collections import Counter
                    canonical=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                    available=Counter(canonical(item) for item in preview.get('fragments',[]) if isinstance(item,dict))
                    selected=Counter(canonical(item) for item in payload.selected_fragments)
                    if any(count>available[key] for key,count in selected.items()):
                        raise HTTPException(422,detail={'code':'import_fragment_invalid','message':'所选片段不属于本次预览或重复；请重新查看原文预览后选择。'})
                fragments=payload.selected_fragments or preview.get('full_fragments') or preview.get('fragments') or []
                parser_version = preview.get('parser_version', 'oa-import-v1')
                if parser_version == 'oa-import-v2' and (payload.selected_fragments
                        or preview.get('extraction_quality') == 'partial'
                        or preview.get('content_kind') != 'pdf'):
                    parser_version += '-partial'
                evidence_id=uuid.uuid4().hex[:16]
                content='\n\n'.join(str(item.get('text','')) for item in fragments if isinstance(item,dict)).strip() or str(preview.get('abstract') or preview.get('text_preview') or '').strip()
                if payload.keep_attachment:
                    try:
                        expected=_oa_cleanup_path(project_id,import_id,row['attempt_id'])
                        if not row['file_path'] or Path(row['file_path']).resolve()!=expected or not expected.is_file():raise ValueError('attachment missing')
                    except (OSError,ValueError):
                        raise HTTPException(409,detail={'code':'attachment_unavailable','message':'附件文件不可用；可重新获取，或选择不保留附件后确认。'})
                attachment_path=row['file_path'] if payload.keep_attachment else '';stamp=now()
                conn.execute('INSERT INTO external_evidence(id,project_id,import_id,source_url,content,fragments_json,attachment_path,confirmed_at,source_version) VALUES(?,?,?,?,?,?,?,?,?)',(evidence_id,project_id,import_id,row['oa_url'] or row['source_url'],content,json.dumps(fragments,ensure_ascii=False),attachment_path,stamp,parser_version))
                corpus_id=uuid.uuid4().hex[:12]
                conn.execute('INSERT INTO corpus(id,project_id,title,content,source,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?)',(corpus_id,project_id,row['title'] or 'OA 外部论文',content,row['source_url'] or row['oa_url'],'',evidence_id))
                corpus_lifecycle.ensure_source_version(conn,corpus_id,content,row['source_url'] or row['oa_url'],'oa_import')
                corpus_lifecycle.rebuild_index(conn,project_id,corpus_id)
                conn.execute("UPDATE oa_imports SET status='imported',updated_at=? WHERE id=? AND project_id=?",(stamp,import_id,project_id))
                if not payload.keep_attachment and row['file_path']:
                    _oa_queue_cleanup(conn,project_id,import_id,row['attempt_id'])
                result={'ok':True,'status':'imported','evidence_id':evidence_id,'corpus_id':corpus_id}
    finally:conn.close()
    result['cleanup_pending']=_oa_cleanup_after_commit(project_id,import_id)
    return result


@app.get("/api/projects/{project_id}/imports/{import_id}/pdf")
def read_imported_pdf(project_id: str, import_id: str) -> FileResponse:
    conn = db()
    try:
        row = conn.execute("""SELECT i.attempt_id,i.preview_json,e.attachment_path FROM oa_imports i
            JOIN external_evidence e ON e.import_id=i.id AND e.project_id=i.project_id
            WHERE i.id=? AND i.project_id=? AND i.status='imported' AND e.attachment_path<>''""",
            (import_id, project_id)).fetchone()
    finally:
        conn.close()
    if row and json.loads(row["preview_json"]).get("content_kind") == "pdf":
        try:
            path = _oa_cleanup_path(project_id, import_id, row["attempt_id"])
            if Path(row["attachment_path"]).resolve() == path and path.is_file():
                return FileResponse(path, media_type="application/pdf", headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError):
            pass
    raise HTTPException(404, "此项目未保留该 PDF 原件")


@app.post("/api/projects/{project_id}/imports/{import_id}/cancel")
def cancel_oa_import(project_id: str, import_id: str) -> dict[str, Any]:
    project_or_404(project_id);conn=db();cleanup_path=''
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM oa_imports WHERE id=? AND project_id=?',(import_id,project_id)).fetchone()
            if not row:raise HTTPException(404,'导入记录不存在')
            evidence_ids=[e['id'] for e in conn.execute('SELECT id FROM external_evidence WHERE import_id=? AND project_id=?',(import_id,project_id))]
            if evidence_ids:
                placeholders=','.join('?' for _ in evidence_ids)
                corpus_ids=[c['id'] for c in conn.execute(f'SELECT id FROM corpus WHERE project_id=? AND source_resource_id IN ({placeholders})',(project_id,*evidence_ids))]
                if corpus_ids:
                    corpus_placeholders=','.join('?' for _ in corpus_ids)
                    conn.execute(f"UPDATE corpus_source_versions SET permission_state='revoked',status='superseded' WHERE corpus_id IN ({corpus_placeholders})",corpus_ids)
                    conn.execute(f'DELETE FROM corpus_index_chunks WHERE corpus_id IN ({corpus_placeholders})',corpus_ids)
            conn.execute("UPDATE oa_imports SET status='cancelled',updated_at=? WHERE id=? AND project_id=?",(now(),import_id,project_id))
            if row['file_path'] or row['status']=='fetching':
                _oa_queue_cleanup(conn,project_id,import_id,row['attempt_id'],owner_pid=row['owner_pid'],owner_token=row['owner_token'],wait_for_owner=row['status']=='fetching')
    finally:conn.close()
    pending=_oa_cleanup_after_commit(project_id,import_id)
    return {'ok':True,'status':'cancelled','import_id':import_id,'cleanup_pending':pending}


def call_ai(
    action: str,
    prompt: str,
    content: str,
    selection: str,
    model: str | None,
    base_url: str | None = None,
    context: str = "",
    max_tokens: int | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    return call_chat_completion(
        action, prompt, content, selection, model, base_url, context,
        request_post=requests.post, environ=os.environ, max_tokens=max_tokens,
        messages=messages, tools=tools, tool_choice=tool_choice,
    )


@app.post("/api/projects/{project_id}/ai")
def ai(project_id: str, payload: AIIn) -> dict[str, Any]:
    project_or_404(project_id)
    raise HTTPException(409, detail={"code": "candidate_api_required", "message": "请通过论文工作流预览接收方、范围与预算，再提交持久AI候选；兼容接口不会绕过配置和候选审阅。"})


def _job_targets(project_id: str, payload: AIJobIn) -> tuple[list[dict[str, Any]], dict[str, int]]:
    conn = db()
    if not has_workflow(project_id):
        conn.close()
        raise ProjectServiceError(409, "workflow_missing", "旧论文没有工作流快照；不会自动迁移。")
    projection = workflow_projection(conn, project_id)
    # Candidate generation is not stage-bound; planning gaps only warn.
    raw_targets = payload.scope.get("targets")
    if raw_targets is None:
        raw_targets = [{
            "kind": payload.scope.get("target_kind", "outline" if payload.action == "outline" else "file"),
            "path": payload.scope.get("path", "" if payload.action == "outline" else "main.tex"),
            "start": payload.scope.get("start", 0),
            "end": payload.scope.get("end", 0),
        }]
    if not isinstance(raw_targets, list) or not raw_targets:
        conn.close()
        raise ProjectServiceError(422, "invalid_scope", "至少需要一个候选目标。")
    if len(raw_targets) > 8:
        conn.close()
        raise ProjectServiceError(422, "scope_limit_exceeded", "单次候选最多包含 8 个文件目标。")
    normalized: list[dict[str, Any]] = []
    versions = dict(payload.base_file_versions)
    for raw in raw_targets:
        if not isinstance(raw, dict):
            conn.close()
            raise ProjectServiceError(422, "invalid_scope", "候选目标格式无效。")
        kind = raw.get("kind", "file")
        if kind not in {"file", "outline"}:
            conn.close()
            raise ProjectServiceError(422, "invalid_scope", "候选目标类型必须是 file 或 outline。")
        path = "" if kind == "outline" else clean_path(str(raw.get("path", "")))
        key = "__outline__" if kind == "outline" else path
        actual_version = current_version(conn, project_id, kind, path)
        expected_version = versions.get(key)
        if expected_version is None:
            conn.close()
            raise ProjectServiceError(422, "base_version_required", f"候选目标 {key} 必须携带基础版本。")
        if actual_version < 1:
            conn.close()
            raise ProjectServiceError(404, "version_missing", f"{key} 没有可用版本。")
        if expected_version != actual_version:
            conn.close()
            raise ProjectServiceError(409, "content_conflict", f"{key} 的基础版本已变化。")
        start = raw.get("start", 0)
        end = raw.get("end", start)
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            conn.close()
            raise ProjectServiceError(422, "invalid_range", "候选范围无效。")
        versions[key] = actual_version
        normalized.append({"kind": kind, "path": path, "start": start, "end": end, "base_version": actual_version})
    keys = ["__outline__" if item["kind"] == "outline" else item["path"] for item in normalized]
    if len(keys) != len(set(keys)):
        conn.close()
        raise ProjectServiceError(422, "duplicate_target", "同一候选不能重复包含相同文件目标。")
    if set(versions) != set(keys):
        conn.close()
        raise ProjectServiceError(422, "invalid_base_versions", "base_file_versions 必须与候选目标一一对应。")
    conn.close()
    return normalized, versions


def _job_public(row: sqlite3.Row) -> dict[str, Any]:
    job = {
        key: row[key]
        for key in (
            "id", "project_id", "request_id", "action", "status", "cancel_requested",
            "attempt", "error_code", "context_snapshot_id", "candidate_id", "model", "workflow_version",
            "created_at", "updated_at",
        )
    }
    job["scope"] = json.loads(row["scope_json"] or "{}")
    job["base_file_versions"] = json.loads(row["base_file_versions_json"] or "{}")
    job["usage"] = json.loads(row["usage_json"] or "{}")
    job["cost_status"] = "unknown"
    if row['status'] in {'running','cancel_requested'}:
        from paperpilot.services.tex_runtime import owner_state
        job['recovery_state'] = owner_state(dict(row))
    job["retryable"] = row["status"] in {"failed", "cancelled"}
    return job


def _stored_target_content(conn: sqlite3.Connection, project_id: str, target: dict[str, Any]) -> str:
    row = conn.execute(
        "SELECT content FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?",
        (project_id, target["kind"], target["path"], target["base_version"]),
    ).fetchone()
    if not row:
        raise ProjectServiceError(409, "content_conflict", "AI 任务的基础版本已不存在。")
    return str(row["content"])


def _parse_bundle_result(raw: str, targets: list[dict[str, Any]]) -> dict[str, str]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProjectServiceError(502, "invalid_bundle_response", "AI 多文件候选不是有效 JSON；原稿未修改。") from exc
    if not isinstance(value, dict):
        raise ProjectServiceError(502, "invalid_bundle_response", "AI 多文件候选结构无效；原稿未修改。")
    expected = {item["path"] if item["kind"] == "file" else "__outline__" for item in targets}
    if set(value) != expected or any(not isinstance(item, str) for item in value.values()):
        raise ProjectServiceError(502, "invalid_bundle_response", "AI 候选目标与请求不一致；原稿未修改。")
    return value


def schedule_ai_worker(tasks,project_id,job_id,temporary_key=''):
    if temporary_key:tasks.add_task(process_ai_job,project_id,job_id,temporary_key)
    else:tasks.add_task(process_ai_job,project_id,job_id)


def process_ai_job(project_id: str, job_id: str, temporary_key: str | None = None) -> None:
    """Run one persisted job; failures stay visible and never touch saved content."""
    conn = db()
    row = conn.execute("SELECT * FROM ai_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
    if not row:
        conn.close()
        return
    request_id = row["request_id"]
    if row["status"] == "cancel_requested" or row["cancel_requested"]:
        with conn:
            conn.execute("UPDATE ai_jobs SET status='cancelled',updated_at=? WHERE id=?", (now(), job_id))
            update_event_status(conn, project_id, request_id, "cancelled")
        conn.close()
        return
    if row["status"] != "running":
        conn.close()
        return
    usage_audit: dict[str, Any] = {}
    started=time.perf_counter()
    try:
        input_data = json.loads(row["input_json"] or "{}")
        scope = json.loads(row["scope_json"] or "{}")
        targets = scope.get("targets", [])
        if not targets:
            raise ProjectServiceError(422, "invalid_scope", "AI 任务没有可生成的目标。")
        context = conn.execute("SELECT context_text,source_manifest_json FROM context_snapshots WHERE id=?", (row["context_snapshot_id"],)).fetchone()
        from paperpilot.services.context import style_context_current
        if context and not style_context_current(conn, project_id, json.loads(context['source_manifest_json'])):
            raise ProjectServiceError(409, "style_profile_revoked", "学习来源已过期、撤权或更新，请重新预览后生成。")
        base_versions = json.loads(row["base_file_versions_json"] or "{}")
        from paperpilot.services.context import context_snapshot_current
        if context and not context_snapshot_current(conn,project_id,json.loads(context['source_manifest_json']),base_versions=base_versions,workflow_version=row['workflow_version']):
            raise ProjectServiceError(409,'context_permission_stale','正文、资料授权或工作流已变化，旧预览未发送；请重新预览。')
        source_contents = {
            item["path"] if item["kind"] == "file" else "__outline__": _stored_target_content(
                conn,
                project_id,
                {
                    **item,
                    "base_version": int(base_versions["__outline__" if item["kind"] == "outline" else item["path"]]),
                },
            )
            for item in targets
        }
        project = conn.execute("SELECT p.ai_base_url,p.ai_profile_id,pr.base_url AS bound_base_url,pr.status AS bound_status,pr.current_version AS bound_version,p.ai_config_revision FROM projects p LEFT JOIN ai_provider_profiles pr ON pr.id=p.ai_profile_id WHERE p.id=?", (project_id,)).fetchone()
        admission=scope.get('provider_snapshot')
        if not admission:
            raise ProjectServiceError(409,'provider_snapshot_missing','旧请求没有固定提供方版本，请重新预览并创建新请求。')
        provider_runtime.verify_request(conn,admission)
        runtime=provider_config.resolve_credentials(conn,admission['profile_id'],admission['config_revision'],temporary_key=temporary_key)
        conn.close()
        first = targets[0]
        first_key = first["path"] if first["kind"] == "file" else "__outline__"
        first_content = source_contents[first_key]
        start, end = first["start"], first["end"]
        if start > len(first_content) or end > len(first_content):
            raise ProjectServiceError(422, "invalid_range", "候选范围已超出基础内容。")
        selected = first_content[start:end]
        ai_content = first_content if len(targets) == 1 else json.dumps(source_contents, ensure_ascii=False)
        prompt = input_data.get("prompt", "")
        if len(targets) > 1:
            prompt += "\n请返回 JSON 对象，键必须与输入文件键完全一致，值为每个目标的完整新内容；不要返回代码围栏或额外文本。"
        safe_prompt, prompt_redactions = redact_sensitive_text(prompt)
        safe_content, content_redactions = redact_sensitive_text(ai_content)
        safe_selection, selection_redactions = redact_sensitive_text(selected)
        safe_context, context_redactions = redact_sensitive_text(context["context_text"] if context else "")
        redaction_count = prompt_redactions + content_redactions + selection_redactions + context_redactions
        max_output_tokens = int(scope.get("max_output_tokens") or _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000))
        max_job_tokens = int(scope.get("max_job_tokens") or _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000))
        estimated_input_tokens = estimate_tokens(safe_context + safe_prompt + safe_content + safe_selection)
        if estimated_input_tokens + max_output_tokens > max_job_tokens:
            raise ProjectServiceError(429, "budget_exceeded", "本次上下文加输出上限超过任务 token 硬上限；没有向模型发送请求。")
        conn = db()
        with conn:
            live_job = conn.execute("SELECT status,cancel_requested FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
            if live_job['status'] != 'running' or live_job['cancel_requested'] or (context and not style_context_current(conn, project_id, json.loads(context['source_manifest_json']))):
                raise ProjectServiceError(409, "style_profile_revoked", "任务上下文已失效，请重新预览后生成。")
            if runtime:
                provider_runtime.verify_request(conn,admission)
                provider_config.resolve_credentials(conn,runtime.profile_id,runtime.config_revision,temporary_key=temporary_key)
            if context and not context_snapshot_current(conn,project_id,json.loads(context['source_manifest_json']),base_versions=base_versions,workflow_version=row['workflow_version']):
                raise ProjectServiceError(409,'context_permission_stale','发送前资料授权或内容已变化，请重新预览。')
            current_scope = json.loads(conn.execute("SELECT scope_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()["scope_json"] or "{}")
            current_scope["redacted_secret_count"] = int(current_scope.get("redacted_secret_count", 0)) + redaction_count
            current_scope["budget_status"] = "within_limit"
            current_scope["estimated_input_tokens"] = estimated_input_tokens
            current_scope['egress_state']='sending'
            if current_scope.get('continuation_batch_id'):
                continuation.check_outline(conn, project_id, current_scope['continuation_batch_id'])
                from paperpilot.services import continuation_budget
                continuation_budget.reserve(conn, project_id, job_id, current_scope, estimated_input_tokens+256, max_output_tokens)
            conn.execute("UPDATE ai_jobs SET scope_json=?,updated_at=? WHERE id=? AND status='running'", (json.dumps(current_scope, ensure_ascii=False), now(), job_id))
        conn.close()
        from paperpilot.services.credential_vault import scoped
        with scoped(runtime):
            result = call_ai(
                row["action"], safe_prompt, safe_content, safe_selection, row["model"],
                (runtime.base_url if runtime else project["ai_base_url"] if project else None), safe_context,
                max_tokens=max_output_tokens,
            )
        if not result.get("ok"):
            raise ProjectServiceError(502, result.get('error_code','provider_error'), result.get("error", "AI 服务暂时不可用。"))
        usage_audit = provider_runtime.safe_usage(result.get('usage'))
        generated, generated_redactions = redact_sensitive_text(str(result.get("content", "")))
        estimated_output_tokens = estimate_tokens(generated)
        try:
            provider_input_tokens = int(usage_audit.get("prompt_tokens", usage_audit.get("input_tokens", 0)) or 0)
            provider_output_tokens = int(usage_audit.get("completion_tokens", usage_audit.get("output_tokens", 0)) or 0)
            provider_total_tokens = int(usage_audit.get("total_tokens", provider_input_tokens + provider_output_tokens) or 0)
        except (TypeError, ValueError):
            provider_input_tokens = provider_output_tokens = provider_total_tokens = 0
        if provider_output_tokens > max_output_tokens or estimated_output_tokens > max_output_tokens:
            raise ProjectServiceError(502, "output_limit_exceeded", "AI 返回内容超过请求输出 token 硬上限；候选未保存。")
        if provider_total_tokens and provider_total_tokens > max_job_tokens:
            raise ProjectServiceError(502, "actual_job_limit_exceeded", "AI 服务报告的实际 token 用量超过单任务硬上限；候选未保存。")
        if generated_redactions:
            conn = db()
            with conn:
                job_scope = json.loads(conn.execute("SELECT scope_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()["scope_json"] or "{}")
                job_scope["redacted_secret_count"] = int(job_scope.get("redacted_secret_count", 0)) + generated_redactions
                conn.execute("UPDATE ai_jobs SET scope_json=?,updated_at=? WHERE id=?", (json.dumps(job_scope, ensure_ascii=False), now(), job_id))
            conn.close()
        if not generated.strip():
            raise ProjectServiceError(502, "empty_candidate", "AI 服务返回空候选。")
        if len(targets) == 1:
            generated_by_key = {first_key: generated}
        else:
            generated_by_key = _parse_bundle_result(generated, targets)
        patches = []
        for target in targets:
            key = target["path"] if target["kind"] == "file" else "__outline__"
            base = source_contents[key]
            replacement = generated_by_key[key]
            patched = base[:target["start"]] + replacement + base[target["end"]:]
            patches.append({"kind": target["kind"], "path": target["path"], "content": patched})
        conn = db()
        with conn:
            usage = {
            **usage_audit,
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_output_tokens": estimated_output_tokens,
                "cost_status": "unknown",
            }
            complete_job(conn, job_id, generated_content=generated, patches=patches, usage=usage)
        conn.close()

    except Exception as exc:
        try:
            if not conn.in_transaction:
                conn.close()
        except Exception:
            pass
        error_code = exc.code if isinstance(exc, ProjectServiceError) else "provider_error"
        conn = db()
        with conn:
            current = conn.execute("SELECT status FROM ai_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
            if usage_audit:
                conn.execute("UPDATE ai_jobs SET usage_json=? WHERE id=?", (json.dumps(usage_audit, ensure_ascii=False), job_id))
            if current and current["status"] == "cancel_requested":
                conn.execute("UPDATE ai_jobs SET status='cancelled',updated_at=? WHERE id=?", (now(), job_id))
                update_event_status(conn, project_id, request_id, "cancelled")
            elif current and current["status"] == "running":
                scope_row = conn.execute("SELECT scope_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
                if error_code == "budget_exceeded" and scope_row:
                    failed_scope = json.loads(scope_row["scope_json"] or "{}")
                    failed_scope["budget_status"] = "blocked"
                    failed_scope["budget_reason"] = "estimated_context_plus_output_exceeds_job_limit"
                    conn.execute("UPDATE ai_jobs SET scope_json=? WHERE id=?", (json.dumps(failed_scope, ensure_ascii=False), job_id))
                mark_job_failed(conn, job_id, error_code)
                update_event_status(conn, project_id, request_id, "failed")
                if error_code in {'continuation_budget_exceeded','continuation_outline_stale','budget_exceeded'} and scope_row:
                    failed_scope = json.loads(scope_row['scope_json'] or '{}')
                    if failed_scope.get('continuation_batch_id'):
                        conn.execute("UPDATE continuation_batches SET status='paused',pause_reason=? WHERE id=?", ('budget_exceeded' if 'budget' in error_code else error_code, failed_scope['continuation_batch_id']))
        conn.close()
    finally:
        temporary_key=None
        runtime=None
        audit_conn=db()
        try:
            row=audit_conn.execute('SELECT status,error_code,usage_json FROM ai_jobs WHERE id=?',(job_id,)).fetchone()
            if row:
                with audit_conn:
                    provider_runtime.finish_audit(audit_conn,job_id,row['status'],row['error_code'],json.loads(row['usage_json']),(time.perf_counter()-started)*1000)
                    from paperpilot.services import continuation_budget
                    continuation_budget.settle(audit_conn, job_id, json.loads(row['usage_json']), row['status'])
        finally:audit_conn.close()


@app.get("/api/projects/{project_id}/workflow")
def get_workflow(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        payload = workflow_projection(conn, project_id)
        from paperpilot.services import ai_access
        payload["ai_allowed"] = True
        payload["warnings"] = ai_access.warnings(conn, project_id)
        return payload
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    finally:
        conn.close()


@app.put("/api/projects/{project_id}/workflow/items/{item_id}")
def update_work_item(project_id: str, item_id: str, payload: WorkflowItemIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            item = set_work_item_status(conn, project_id, item_id, payload.status)
        return item
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/extensions/style-profile")
def get_style_profile_extension(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        view = style_learning.read(conn, project_id)
        return {**extension_status(project_id), "status": view["state"], "opt_in": view["opt_in"],
            "profile_version": view["profile_version"], "source_versions": view["effective_source_versions"],
            "learning_backend_available": True, "message": view["next_action"]}
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/ai-context")
def get_ai_context(project_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    bundle = project_payload(project_id)
    conn = db()
    try:
        projection = workflow_projection(conn, project_id)
        snapshot = build_ai_context_snapshot(bundle["project"], bundle, workflow=projection)
        estimated = estimate_tokens(snapshot["text"])
        return {
            "version": snapshot["version"], "sources": snapshot["sources"],
            "omissions": snapshot["omissions"], "redactions": snapshot["redactions"],
            "global_context_complete": snapshot["global_context_complete"], "degradation_mode": snapshot["degradation_mode"],
            "context_policy": snapshot["context_policy"],
            "preview": snapshot["text"], "workflow_stage": projection["current_stage"],
            "estimated_input_tokens": estimated,
            "max_output_tokens": _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000),
            "max_job_tokens": _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000),
            "cost_status": "unknown",
            **provider_runtime.preview_admission(conn,project_id,snapshot['model'],estimated,_positive_env_int('PWA_AI_MAX_OUTPUT_TOKENS',4000)),
        }
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/ai-context")
def preview_ai_context(project_id: str, payload: AIContextPreviewIn) -> dict[str, Any]:
    project_or_404(project_id)
    bundle = project_payload(project_id)
    conn = db()
    try:
        projection = workflow_projection(conn, project_id)
        requested_targets = payload.targets or ([{"kind": payload.target_kind, "path": payload.target_path, "content": payload.content}] if payload.target_kind else [])
        if not requested_targets:
            raise HTTPException(422, detail={"code": "target_required", "message": "上下文预览必须指定文件、大纲或多个目标。"})
        target_contents: dict[str, str] = {}
        target_labels: list[str] = []
        for item in requested_targets:
            kind = item.get("kind")
            path = str(item.get("path") or "") if kind == "file" else ""
            if kind not in {"file", "outline"} or (kind == "file" and not path):
                raise HTTPException(422, detail={"code": "invalid_target", "message": "预览目标必须是论文文件或大纲。"})
            key = path if kind == "file" else "__outline__"
            if kind == "file":
                row = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, path)).fetchone()
            else:
                row = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise HTTPException(404, detail={"code": "target_missing", "message": f"找不到预览目标：{path or '大纲'}。"})
            saved_content = str(row["content"])
            supplied_content = item.get("content")
            if supplied_content is not None and str(supplied_content) != saved_content:
                raise HTTPException(409, detail={"code": "target_unsaved", "message": "目标内容尚未保存；保存后重新预览上下文。"})
            target_contents[key] = saved_content
            target_labels.append(path or "大纲")
        target_content = next(iter(target_contents.values())) if len(target_contents) == 1 else json.dumps(target_contents, ensure_ascii=False)
        model = payload.model or (bundle.get('ai_binding') or {}).get('model_id') or bundle["project"].get("model") or os.getenv("PWA_AI_MODEL", "")
        snapshot = build_ai_context_snapshot(
            bundle["project"], bundle,
            task={
                "action": payload.action, "prompt": payload.prompt,
                "content": target_content, "selection": payload.selection, "target": ",".join(target_labels),
                "context_source_ids": payload.context_source_ids,
            },
            workflow=projection,
            model=model,
        )
        estimated = estimate_tokens(snapshot["text"] + payload.prompt + target_content + payload.selection)
        return {
            "version": snapshot["version"], "sources": snapshot["sources"],
            "omissions": snapshot["omissions"], "redactions": snapshot["redactions"],
            "global_context_complete": snapshot["global_context_complete"], "degradation_mode": snapshot["degradation_mode"],
            "context_policy": snapshot["context_policy"],
            "preview": snapshot["text"], "workflow_stage": projection["current_stage"], "model": snapshot["model"],
            "estimated_input_tokens": estimated,
            "max_output_tokens": _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000),
            "max_job_tokens": _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000),
            "cost_status": "unknown",
            **provider_runtime.preview_admission(conn,project_id,model,estimated,_positive_env_int('PWA_AI_MAX_OUTPUT_TOKENS',4000)),
        }
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/workflow/commands")
def workflow_command(project_id: str, payload: WorkflowCommandIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            expected_drafts = None if payload.expected_drafts is None else sorted((x.model_dump() for x in payload.expected_drafts), key=lambda x: (x['target_kind'],x['target_path'],x['updated_at']))
            snapshot_json = None if expected_drafts is None else hashlib.sha256(json.dumps(expected_drafts, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
            existing = conn.execute(
                "SELECT * FROM workflow_commands WHERE project_id=? AND request_id=?",
                (project_id, payload.request_id),
            ).fetchone()
            if existing:
                same_request = (
                    existing["command"] == payload.command
                    and int(existing["base_version"]) == payload.base_version
                    and existing["target_version"] == payload.target_version
                    and existing["draft_action"] == payload.draft_action
                    and existing["draft_snapshot_json"] == snapshot_json
                )
                if not same_request:
                    raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于另一个工作流命令。")
                return json.loads(existing["result_json"])
            current = current_snapshot(conn, project_id)
            command = payload.command
            if command == "advance":
                command = {"context_ready": "start", "stage_in_progress": "review", "stage_review": "confirm"}.get(current["stage_status"], "")
            leaves_stage = current["current_stage"] == "human_revision" and command in {"confirm", "cancel"}
            if command == "redo" and current["current_stage"] == "human_revision" and payload.target_version is not None:
                target = conn.execute(
                    "SELECT current_stage FROM workflow_snapshots WHERE project_id=? AND version=?",
                    (project_id, payload.target_version),
                ).fetchone()
                leaves_stage = bool(target and target["current_stage"] != "human_revision")
            if leaves_stage:
                if expected_drafts is None:
                    raise ProjectServiceError(428, "revision_client_outdated", "当前页面版本过旧，无法安全处理草稿。请保留输入，重新加载页面后查看草稿并重试。")
                mode = conn.execute("SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'", (project_id,)).fetchone()
                if mode:
                    exit_mode(conn, project_id, payload.draft_action, expected_drafts)
                else:
                    resolve_pending_drafts(conn, project_id, payload.draft_action, expected_drafts=expected_drafts)
            execute_command(conn, project_id, payload.command, payload.base_version, payload.target_version)
            result = workflow_projection(conn, project_id)
            conn.execute(
                "INSERT INTO workflow_commands(project_id,request_id,command,base_version,target_version,draft_action,result_json,created_at,draft_snapshot_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    project_id, payload.request_id, payload.command, payload.base_version,
                    payload.target_version, payload.draft_action,
                    json.dumps(result, ensure_ascii=False), now(), snapshot_json,
                ),
            )
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/human-revision")
def start_human_revision(project_id: str, payload: RevisionModeIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            mode = enter_mode(conn, project_id, payload.workflow_version)
        return {"ok": True, "mode": mode, "workflow": workflow_projection(conn, project_id)}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/human-revision/exit")
def stop_human_revision(project_id: str, payload: RevisionExitIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if payload.expected_drafts is None:
        raise HTTPException(428, detail={"code":"revision_client_outdated","message":"当前页面版本过旧，无法安全处理草稿。请保留输入，重新加载页面后查看草稿并重试。"})
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            expected = None if payload.expected_drafts is None else [x.model_dump() for x in payload.expected_drafts]
            result = exit_mode(conn, project_id, payload.draft_action, expected)
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/human-revision/drafts")
def list_revision_drafts(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    rows = [dict(row) for row in conn.execute("SELECT target_kind,target_path,content,base_version,mode_id,updated_at FROM revision_drafts WHERE project_id=? ORDER BY target_kind,target_path", (project_id,))]
    conn.close()
    return rows


@app.post("/api/projects/{project_id}/human-revision/drafts")
def update_revision_draft(project_id: str, payload: RevisionDraftIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if "expected_draft_updated_at" not in payload.model_fields_set:
        raise HTTPException(428, detail={"code":"revision_client_outdated","message":"当前页面版本过旧，无法安全保存草稿。请先复制当前输入，重新加载页面后重试。"})
    target_path = "" if payload.target_kind == "outline" else clean_path(payload.target_path)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            result = save_draft(conn, project_id, payload.target_kind, target_path, payload.content, payload.base_version,
                conditional="expected_draft_updated_at" in payload.model_fields_set,
                expected_draft_updated_at=payload.expected_draft_updated_at, copy_id=payload.copy_id)
        if result.get("conflict"):
            raise HTTPException(409, detail={"code": "revision_draft_changed", **result})
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/human-revision/draft-copies")
def list_revision_draft_copies(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        return [dict(row) for row in conn.execute("SELECT copy_id,target_kind,target_path,content,base_version,updated_at FROM revision_draft_copies WHERE project_id=? ORDER BY updated_at DESC", (project_id,))]
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/human-revision/draft-copies-page")
def list_revision_draft_copies_page(project_id: str, limit: int = 30, before_updated_at: str | None = None, before_copy_id: str | None = None) -> dict[str, Any]:
    project_or_404(project_id)
    if not 1 <= limit <= 100 or (before_updated_at is None) != (before_copy_id is None):
        raise HTTPException(422, "分页参数无效，请刷新列表。")
    conn = db()
    try:
        condition = "" if before_updated_at is None else "AND (updated_at < ? OR (updated_at = ? AND copy_id < ?))"
        args = (project_id,) if before_updated_at is None else (project_id, before_updated_at, before_updated_at, before_copy_id)
        rows = conn.execute(f"SELECT copy_id,target_kind,target_path,base_version,updated_at,substr(content,1,100) AS preview FROM revision_draft_copies WHERE project_id=? {condition} ORDER BY updated_at DESC,copy_id DESC LIMIT ?", (*args,limit+1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        last = items[-1] if len(rows)>limit else None
        return {"copies":items,"next_cursor":{"updated_at":last['updated_at'],"copy_id":last['copy_id']} if last else None}
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/human-revision/draft-copies/{copy_id}")
def get_revision_draft_copy(project_id: str, copy_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        row=conn.execute("SELECT copy_id,target_kind,target_path,content,base_version,updated_at FROM revision_draft_copies WHERE project_id=? AND copy_id=?",(project_id,copy_id)).fetchone()
        if row is None:raise HTTPException(404,"恢复副本不存在，请刷新列表。")
        return dict(row)
    finally:
        conn.close()


@app.delete("/api/projects/{project_id}/human-revision/draft-copies/{copy_id}")
def delete_revision_draft_copy(project_id: str, copy_id: str, updated_at: str) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("DELETE FROM revision_draft_copies WHERE project_id=? AND copy_id=? AND updated_at=?", (project_id,copy_id,updated_at))
            if not result.rowcount and conn.execute("SELECT 1 FROM revision_draft_copies WHERE project_id=? AND copy_id=?",(project_id,copy_id)).fetchone():
                raise HTTPException(409, detail={"code":"revision_copy_changed","message":"恢复副本已更新，请刷新后重新选择。"})
        return {"ok":True,"deleted":bool(result.rowcount)}
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/human-revision/checkpoint")
def checkpoint_revision_draft(project_id: str, payload: RevisionCheckpointIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if payload.expected_draft_updated_at is None:
        raise HTTPException(428, detail={"code":"revision_client_outdated","message":"当前页面版本过旧，无法安全提交草稿。请先复制当前输入，重新加载页面后重试。"})
    target_path = "" if payload.target_kind == "outline" else clean_path(payload.target_path)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            result = commit_draft(conn, project_id, payload.target_kind, target_path, payload.base_version, payload.expected_draft_updated_at)
        return result
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/human-revision/events")
def list_revision_events(project_id: str) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    rows = []
    for row in conn.execute("SELECT * FROM assist_action_events WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,)):
        item = dict(row)
        item["scope"] = json.loads(item.pop("scope_json") or "{}")
        item["base_file_versions"] = json.loads(item.pop("base_file_versions_json") or "{}")
        rows.append(item)
    conn.close()
    return rows


@app.get("/api/projects/{project_id}/versions")
def list_content_versions(project_id: str, target_kind: str, target_path: str = "") -> list[dict[str, Any]]:
    project_or_404(project_id)
    path = "" if target_kind == "outline" else clean_path(target_path)
    if target_kind not in {"file", "outline"}:
        raise HTTPException(422, "target_kind 必须是 file 或 outline")
    conn = db()
    result = [dict(row) for row in conn.execute(
        "SELECT version,parent_version,restored_from_version,source,created_at FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? ORDER BY version DESC LIMIT 100",
        (project_id, target_kind, path),
    )]
    conn.close()
    return result


@app.post("/api/projects/{project_id}/versions/restore")
def restore_content_version(project_id: str, payload: RevisionRestoreIn) -> dict[str, Any]:
    require_project_writable(project_id)
    path = "" if payload.target_kind == "outline" else clean_path(payload.target_path)
    conn = db()
    try:
        with conn:
            mode = active_mode(conn, project_id)
            result = restore_content(
                conn, project_id, payload.target_kind, path, payload.target_version,
                payload.base_version, payload.action,
            )
            record_event(
                conn, project_id, mode_id=mode["id"], action_type=payload.action,
                scope={"kind": payload.target_kind, "path": path, "target_version": payload.target_version},
                base_file_versions={"__outline__" if payload.target_kind == "outline" else path: payload.base_version},
                status="checkpoint",
            )
        return {"ok": True, **result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/ai-jobs", status_code=202)
def create_ai_job(project_id: str, payload: AIJobIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    project = require_project_writable(project_id)
    if not payload.context_version:
        raise HTTPException(422, detail={"code": "preflight_required", "message": "提交伴写前必须预览并确认本次上下文来源。"})
    targets, base_versions = _job_targets(project_id, payload)
    binding_conn = db()
    try:
        binding = provider_config.project_binding(binding_conn, project_id)
    finally:
        binding_conn.close()
    bundle = project_payload(project_id)
    conn = db()
    try:
        projection = workflow_projection(conn, project_id)
    finally:
        conn.close()
    conn = db()
    try:
        target_contents: dict[str, str] = {}
        for target in targets:
            key = "__outline__" if target["kind"] == "outline" else target["path"]
            base_row = conn.execute(
                "SELECT content FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?",
                (project_id, target["kind"], target["path"], base_versions[key]),
            ).fetchone()
            if base_row is None:
                raise HTTPException(409, detail={"code": "content_conflict", "message": f"{key} 基线版本已不可用，请刷新后重试。"})
            target_contents[key] = str(base_row["content"])
    finally:
        conn.close()
    target_content = next(iter(target_contents.values())) if len(target_contents) == 1 else json.dumps(target_contents, ensure_ascii=False)
    max_input_chars = _positive_env_int("PWA_AI_MAX_INPUT_CHARS", 400000)
    if len(target_content) > max_input_chars:
        raise HTTPException(413, detail={"code": "input_too_large", "message": f"当前写作目标超过输入字符上限 {max_input_chars}；请缩小写作范围后重试。"})
    model = payload.model or (binding["ai_model_id"] if binding else "") or project["model"] or os.getenv("PWA_AI_MODEL", "")
    context_snapshot = build_ai_context_snapshot(
        bundle["project"], bundle,
        task={
            "action": payload.action, "prompt": payload.prompt,
            "content": target_content, "selection": payload.selection,
            "target": ",".join(item["path"] or "大纲" for item in targets),
            "context_source_ids": payload.scope.get("context_source_ids", []),
        },
        workflow=projection,
        model=model,
    )
    if payload.context_version != context_snapshot["version"]:
        raise HTTPException(409, detail={"code": "context_conflict", "message": "论文上下文已变化；请重新查看来源并生成候选。"})
    max_context = _positive_env_int("PWA_AI_MAX_CONTEXT_CHARS", 100000)
    if len(context_snapshot["text"]) > max_context:
        raise HTTPException(413, detail={"code": "context_too_large", "message": f"完整大纲和逻辑上下文超过配置上限 {max_context} 字符；请精简项目材料后重试。"})
    safe_prompt, prompt_redactions = redact_sensitive_text(payload.prompt)
    safe_selection, selection_redactions = redact_sensitive_text(payload.selection)
    max_output_tokens = _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000)
    max_job_tokens = _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000)
    safe_target_content, target_redactions = redact_sensitive_text(target_content)
    estimated_input_tokens = estimate_tokens(context_snapshot["text"] + safe_prompt + safe_selection + safe_target_content)
    scope = {
        "targets": targets, "context_version": context_snapshot["version"],
        "context_source_ids": payload.scope.get("context_source_ids", []),
        "redacted_secret_count": context_snapshot["redactions"],
        "estimated_input_tokens": estimated_input_tokens,
        "max_output_tokens": max_output_tokens,
        "max_job_tokens": max_job_tokens,
        "cost_status": "unknown",
    }
    if estimated_input_tokens + max_output_tokens > max_job_tokens:
        raise HTTPException(413, detail={
            "code": "budget_exceeded",
            "message": "上下文 token 估算加输出硬上限超过单任务 token 限额；没有创建任务或发送数据。",
            "budget": {
                "cost_status": "unknown", "estimated_input_tokens": estimated_input_tokens,
                "max_output_tokens": max_output_tokens, "max_job_tokens": max_job_tokens,
            },
        })
    scope["redacted_secret_count"] += prompt_redactions + selection_redactions + target_redactions
    input_data = {"prompt": safe_prompt, "selection": safe_selection}
    conn = db()
    try:
        with conn:
            latest_project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if not latest_project or int(latest_project["project_revision"]) != int(bundle["project"].get("project_revision", 1)):
                raise ProjectServiceError(409, "context_conflict", "预览后论文上下文已变化；请重新查看来源并生成候选。")
            job, created = create_job(
                conn, project_id, request_id=payload.request_id, action=payload.action,
                input_data=input_data, scope=scope, base_file_versions=base_versions,
                context_version=context_snapshot["version"], context_text=context_snapshot["text"],
                context_sources=context_snapshot["sources"] + [{"kind": "omission", **item} for item in context_snapshot["omissions"]] + [{"kind": "project_revision", "version": context_snapshot["project_revision"]}],
                model=model,
                max_active_jobs=_positive_env_int("PWA_AI_MAX_ACTIVE_JOBS_PER_PROJECT", 3),
                max_jobs_per_minute=_positive_env_int("PWA_AI_MAX_JOBS_PER_MINUTE", 20),
            )
            if job['scope']['provider_snapshot']['auth_mode']=='temporary' and not payload.temporary_key:
                raise ProjectServiceError(409,'temporary_credential_required','此配置使用临时密钥，请为本次明确操作重新输入；不会从磁盘恢复或自动改用环境密钥。')
        if created:
            schedule_ai_worker(background_tasks,project_id,job['id'],payload.temporary_key)
        return {"job": job, "candidate": None, "created": created}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


def _positive_env_int(name: str, fallback: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(fallback))))
    except (TypeError, ValueError):
        return fallback


@app.get("/api/projects/{project_id}/ai-jobs/{job_id}")
def read_ai_job(project_id: str, job_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    row = conn.execute("SELECT * FROM ai_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "AI 任务不存在")
    candidate = None
    if row["candidate_id"]:
        candidate_row = conn.execute("SELECT * FROM draft_candidates WHERE id=? AND project_id=?", (row["candidate_id"], project_id)).fetchone()
        if candidate_row:
            candidate = candidate_payload(conn, candidate_row)
    result = {"job": _job_public(row), "candidate": candidate}
    conn.close()
    return result


@app.post("/api/projects/{project_id}/continuations", status_code=202)
def create_continuation(project_id: str, payload: ContinuationIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    project = require_project_writable(project_id)
    if not payload.context_version:raise HTTPException(422,detail={'code':'preflight_required','message':'续写批次须先预览接收方、完整范围和次数/预算，确认后再提交。'})
    bundle = project_payload(project_id)
    conn = db()
    try:
        projection = workflow_projection(conn, project_id)
        kind = payload.target_kind
        path = "" if kind == "outline" else clean_path(payload.target_path)
        row = conn.execute(
            "SELECT content FROM outline WHERE project_id=?" if kind == "outline" else
            "SELECT content FROM files WHERE project_id=? AND path=?",
            (project_id,) if kind == "outline" else (project_id, path),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail={"code": "target_missing", "message": "续写目标不存在。"})
        content = str(row["content"])
        start, end = payload.anchor_start, payload.anchor_end
        if start < 0 or end < start or end > len(content):
            raise HTTPException(422, detail={"code": "invalid_range", "message": "续写范围超出当前内容。"})
        model = payload.model or (bundle.get('ai_binding') or {}).get('model_id') or project["model"] or os.getenv("PWA_AI_MODEL", "")
        snapshot = build_ai_context_snapshot(
            bundle["project"], bundle,
            task={
                "action": "continue", "prompt": continuation.request_prompt(payload.model_dump(exclude={'temporary_key'})),
                "content": content, "selection": content[start:end], "target": path or "大纲",
                "context_source_ids": [],
            },
            workflow=projection, model=model,
        )
        if payload.context_version != snapshot["version"]:
            raise HTTPException(409, detail={"code": "context_conflict", "message": "论文上下文已变化，请重新预览后开始续写。"})
        max_output = _positive_env_int("PWA_AI_MAX_OUTPUT_TOKENS", 4000)
        hard_limit = payload.hard_budget_tokens or _positive_env_int("PWA_AI_MAX_JOB_TOKENS", 16000)
        snapshot['estimated_input_tokens']=estimate_tokens(snapshot['text']+continuation.request_prompt(payload.model_dump(exclude={'temporary_key'}))+content+content[start:end])+256
        with conn:
            # Serialize the outline check and batch creation against outline writes.
            conn.execute("BEGIN IMMEDIATE")
            result = continuation.create_batch(
                conn, project_id, payload.model_dump(exclude={'temporary_key'}), bundle=bundle, model=model,
                context_snapshot=snapshot, max_output_tokens=max_output, max_job_tokens=hard_limit,
                max_active_jobs=max(payload.segment_count, _positive_env_int("PWA_AI_MAX_ACTIVE_JOBS_PER_PROJECT", 3)),
                schedule_job=lambda pid, jid: schedule_ai_worker(background_tasks,pid,jid,payload.temporary_key),
            )
        return {"ok": True, "batch": result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/continuations")
def list_continuations(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            rows = conn.execute(
                "SELECT id FROM continuation_batches WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, min(max(limit, 1), 100)),
            ).fetchall()
            return [continuation.batch_payload(conn, row["id"], project_id) for row in rows]
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/continuations/{batch_id}")
def read_continuation(project_id: str, batch_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            return continuation.batch_payload(conn, batch_id, project_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/continuations/{batch_id}/{command}")
def command_continuation(project_id: str, batch_id: str, command: str, payload: ContinuationCommandIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    require_project_writable(project_id)
    if command not in {"pause", "resume", "cancel"}:
        raise HTTPException(404, detail={"code": "command_missing", "message": "不支持的续写批次命令。"})
    conn = db()
    try:
        with conn:
            result = continuation.command_batch(
                conn, project_id, batch_id, command, payload.request_id,
                schedule_job=lambda pid, jid: schedule_ai_worker(background_tasks,pid,jid,payload.temporary_key),
                budget_override=payload.budget_override,
                duplicate_cost_confirmed=payload.duplicate_cost_confirmed,
                hard_budget_tokens=payload.hard_budget_tokens, hard_budget_amount=payload.hard_budget_amount,
            )
        return {"ok": True, "batch": result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/continuations/{batch_id}/segments/{segment_id}/decision")
def decide_continuation_segment(project_id: str, batch_id: str, segment_id: str, payload: ContinuationSegmentDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        segment = conn.execute(
            "SELECT candidate_id,job_id FROM continuation_segments WHERE id=? AND batch_id=?",
            (segment_id, batch_id),
        ).fetchone()
        candidate_id = segment["candidate_id"] if segment else None
        if segment is not None and not candidate_id and segment["job_id"]:
            job = conn.execute("SELECT candidate_id FROM ai_jobs WHERE id=?", (segment["job_id"],)).fetchone()
            candidate_id = job["candidate_id"] if job else None
            if candidate_id:
                conn.execute("UPDATE continuation_segments SET candidate_id=?,updated_at=? WHERE id=?", (candidate_id, now(), segment_id))
                conn.commit()
        if segment is None or not candidate_id:
            raise HTTPException(409, detail={"code": "candidate_not_ready", "message": "该分段还没有可审阅候选。"})
    finally:
        conn.close()

    # Continuation segments use the same candidate/apply contract as every
    # other AI-assisted operation.  Resolve the candidate outside the short
    # lookup connection, delegate the actual validation/apply decision to the
    # shared endpoint implementation, then persist the segment event/status.
    result = decide_candidate(
        project_id,
        candidate_id,
        CandidateDecisionIn(
            decision=payload.decision,
            request_id=payload.request_id,
            base_file_versions=payload.base_file_versions,
            selected_paths=payload.selected_paths,
            allow_warnings=payload.allow_warnings,
        ),
    )
    conn = db()
    try:
        with conn:
            existing = conn.execute(
                "SELECT 1 FROM continuation_events WHERE batch_id=? AND request_id=?",
                (batch_id, payload.request_id),
            ).fetchone()
            if existing:
                batch = continuation.batch_payload(conn, batch_id, project_id)
            else:
                batch = continuation.record_decision(
                    conn,
                    project_id,
                    batch_id,
                    segment_id,
                    payload.request_id,
                    payload.decision,
                    result,
                )
        return {"ok": True, "decision": payload.decision, "batch": batch, "candidate_result": result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/modifications", status_code=202)
def create_modification(project_id: str, payload: ModificationIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    project = require_project_writable(project_id)
    bundle = project_payload(project_id)
    conn = db()
    try:
        projection = workflow_projection(conn, project_id)
        kind = payload.target_kind
        path = "" if kind == "outline" else clean_path(payload.target_path)
        row = conn.execute(
            "SELECT content FROM outline WHERE project_id=?" if kind == "outline" else
            "SELECT content FROM files WHERE project_id=? AND path=?",
            (project_id,) if kind == "outline" else (project_id, path),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail={"code": "target_missing", "message": "指定修改目标不存在。"})
        content = str(row["content"])
        if payload.start_offset < 0 or payload.end_offset < payload.start_offset or payload.end_offset > len(content):
            raise HTTPException(422, detail={"code": "invalid_range", "message": "指定修改范围超出当前内容。"})
        model = payload.model or (bundle.get('ai_binding') or {}).get('model_id') or project["model"] or os.getenv("PWA_AI_MODEL", "")
        snapshot = build_ai_context_snapshot(
            bundle["project"], bundle,
            task={"action": payload.action, "prompt": payload.instruction, "content": content, "selection": content[payload.start_offset:payload.end_offset], "target": path or "大纲", "context_source_ids": []},
            workflow=projection, model=model,
        )
        if not payload.context_version:raise ProjectServiceError(422,'preflight_required','指定修改须先预览所选范围、接收方与预算，确认后再提交。')
        if payload.context_version != snapshot["version"]:
            raise HTTPException(409, detail={"code": "context_conflict", "message": "指定修改上下文已变化，请重新预览。"})
        with conn:
            result = modification.create_request(
                conn, project_id, payload.model_dump(exclude={'temporary_key'}), context=snapshot, model=model,
                max_active_jobs=_positive_env_int("PWA_AI_MAX_ACTIVE_JOBS_PER_PROJECT", 3),
                schedule_job=lambda pid, jid: schedule_ai_worker(background_tasks,pid,jid,payload.temporary_key),
            )
        return {"ok": True, "modification": result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/modifications")
def list_modifications(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id); conn = db()
    try:
        with conn:
            rows = conn.execute("SELECT * FROM modification_requests WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, min(max(limit, 1), 100))).fetchall()
            return [modification.payload(conn, row) for row in rows]
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/modifications/cleanup/preview")
def preview_modification_cleanup(project_id: str, payload: ModificationCleanupIn) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        return modification.cleanup_preview(
            conn, project_id, scope=payload.scope, operation_id=payload.operation_id
        )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/modifications/cleanup")
def cleanup_modifications(project_id: str, payload: ModificationCleanupIn) -> dict[str, Any]:
    require_project_writable(project_id)
    if not payload.confirm:
        raise HTTPException(
            422,
            detail={"code": "cleanup_confirmation_required", "message": "请先查看清理预览并明确确认。"},
        )
    conn = db()
    try:
        with conn:
            return modification.cleanup(
                conn, project_id, scope=payload.scope, operation_id=payload.operation_id,
                request_id=payload.request_id, preview_fingerprint=payload.preview_fingerprint,
            )
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/modifications/{request_id}")
def read_modification(project_id: str, request_id: str) -> dict[str, Any]:
    project_or_404(project_id); conn = db()
    try:
        with conn:
            return modification.get(conn, project_id, request_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/modifications/{request_id}/decision")
def decide_modification(project_id: str, request_id: str, payload: ModificationDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id); conn = db()
    try:
        with conn:
            item = modification.get(conn, project_id, request_id)
            if not item.get("candidate"):
                raise HTTPException(409, detail={"code": "candidate_not_ready", "message": "指定修改还没有可审阅候选。"})
            candidate_id = item["candidate"]["id"]
    finally:
        conn.close()
    result = decide_candidate(
        project_id, candidate_id,
        CandidateDecisionIn(
            decision=payload.decision, request_id=payload.request_id,
            base_file_versions=payload.base_file_versions,
            selected_paths=payload.selected_paths, allow_warnings=payload.allow_warnings,
        ),
    )
    conn = db()
    try:
        with conn:
            status = "applied" if payload.decision in {"accept", "partial_apply"} else "rejected"
            conn.execute("UPDATE modification_requests SET status=?,decision_request_id=?,updated_at=? WHERE project_id=? AND (id=? OR request_id=?)", (status, payload.request_id, now(), project_id, request_id, request_id))
            item = modification.get(conn, project_id, request_id)
        return {"ok": True, "decision": payload.decision, "modification": item, "candidate_result": result}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()
    result = decide_candidate(
        project_id, candidate_id,
        CandidateDecisionIn(
            decision=payload.decision, request_id=payload.request_id,
            base_file_versions=payload.base_file_versions,
            selected_paths=payload.selected_paths, allow_warnings=payload.allow_warnings,
        ),
    )
    conn = db()
    try:
        with conn:
            existing = conn.execute("SELECT 1 FROM continuation_events WHERE batch_id=? AND request_id=?", (batch_id, payload.request_id)).fetchone()
            if not existing:
                batch = continuation.record_decision(conn, project_id, batch_id, segment_id, payload.request_id, payload.decision, result)
            else:
                batch = continuation.batch_payload(conn, batch_id, project_id)
        return {"ok": True, "decision": payload.decision, "batch": batch}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/ai-jobs")
def list_ai_jobs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    project_or_404(project_id)
    limit = min(max(limit, 1), 100)
    conn = db()
    result = []
    for row in conn.execute(
        "SELECT * FROM ai_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
        (project_id, limit),
    ):
        candidate = None
        if row["candidate_id"]:
            candidate_row = conn.execute(
                "SELECT * FROM draft_candidates WHERE id=? AND project_id=?",
                (row["candidate_id"], project_id),
            ).fetchone()
            if candidate_row:
                candidate = candidate_payload(conn, candidate_row)
        result.append({"job": _job_public(row), "candidate": candidate})
    conn.close()
    return result


@app.post("/api/projects/{project_id}/ai-jobs/{job_id}/cancel")
def request_ai_job_cancel(project_id: str, job_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        with conn:
            return {"job": cancel_job(conn, project_id, job_id)}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/ai-jobs/{job_id}/recover")
def recover_unverifiable_ai_job(project_id: str, job_id: str, payload: AIJobRecoveryIn) -> dict[str, Any]:
    if not payload.confirmed_stopped:
        raise HTTPException(422, detail={'code':'recovery_confirmation_required','message':'请先核对原进程已停止及提供方请求记录，再明确确认。'})
    from paperpilot.services.tex_runtime import owner_state
    conn = db()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM ai_jobs WHERE id=? AND project_id=?',(job_id,project_id)).fetchone()
            if not row:
                raise ProjectServiceError(404,'job_missing','AI 任务不存在。')
            if row['status'] not in {'running','cancel_requested'}:
                return {'job':_job_public(row),'idempotent_replay':True}
            if owner_state(dict(row)) == 'alive':
                raise ProjectServiceError(409,'worker_still_alive','任务执行进程仍在运行；不会强行中断或重复发送。')
            scope=json.loads(row['scope_json'] or '{}');scope['egress_state']='unknown'
            status='cancelled' if row['cancel_requested'] or row['status']=='cancel_requested' else 'failed'
            conn.execute("UPDATE ai_jobs SET status=?,error_code='interrupted',scope_json=?,updated_at=? WHERE id=? AND project_id=? AND status IN ('running','cancel_requested')",(status,json.dumps(scope,ensure_ascii=False),now(),job_id,project_id))
            update_event_status(conn,project_id,row['request_id'],status)
            conn.execute("UPDATE ai_provider_audit SET status='unknown',error_code='interrupted',updated_at=? WHERE id=? AND status='admitted'",(now(),job_id))
            return {'job':_job_public(conn.execute('SELECT * FROM ai_jobs WHERE id=?',(job_id,)).fetchone()),'idempotent_replay':False}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/ai-jobs/{job_id}/retry", status_code=202)
def retry_ai_job(project_id: str, job_id: str, payload: AIJobRetryIn, background_tasks: BackgroundTasks) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            job, created = retry_job(
                conn, project_id, job_id, payload.request_id,
                max_active_jobs=_positive_env_int("PWA_AI_MAX_ACTIVE_JOBS_PER_PROJECT", 3),
                max_jobs_per_minute=_positive_env_int("PWA_AI_MAX_JOBS_PER_MINUTE", 20),
                duplicate_cost_confirmed=payload.duplicate_cost_confirmed,
            )
            if job['scope']['provider_snapshot']['auth_mode']=='temporary' and not payload.temporary_key:
                raise ProjectServiceError(409,'temporary_credential_required','原临时密钥已释放，请重新输入并预检；没有自动重发。')
        if created:
            schedule_ai_worker(background_tasks,project_id,job['id'],payload.temporary_key)
        return {"job": job, "created": created}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.get("/api/projects/{project_id}/candidates/{candidate_id}")
def read_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
    project_or_404(project_id)
    conn = db()
    try:
        return get_candidate(conn, project_id, candidate_id)
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/candidates/{candidate_id}/decision")
def decide_candidate(project_id: str, candidate_id: str, payload: CandidateDecisionIn) -> dict[str, Any]:
    require_project_writable(project_id)
    conn = db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = get_candidate(conn, project_id, candidate_id)
            decision = {
                "decision": payload.decision,
                "selected_paths": sorted(payload.selected_paths) if payload.selected_paths is not None else None,
                "base_file_versions": payload.base_file_versions,
                "allow_warnings": payload.allow_warnings,
            }
            decision_json = json.dumps(decision, ensure_ascii=False, sort_keys=True)
            if candidate.get("decision_request_id"):
                if candidate["decision_request_id"] != payload.request_id or candidate.get("decision_json") != decision_json:
                    raise ProjectServiceError(409, "candidate_already_decided", "候选已由另一决策处理；请刷新当前状态。")
                versions = {}
                selected = set(decision["selected_paths"] or [])
                for patch in candidate["patches"]:
                    key = "__outline__" if patch["kind"] == "outline" else patch["path"]
                    if payload.decision != "partial_apply" or key in selected:
                        versions[key] = current_version(conn, project_id, patch["kind"], patch["path"])
                return {
                    "ok": True, "decision": payload.decision, "content_versions": versions,
                    "workflow": workflow_projection(conn, project_id), "idempotent_replay": True,
                }
            if payload.decision != "reject" and candidate.get("stale"):
                raise ProjectServiceError(409, "candidate_context_stale", "正文或关键上下文来源已变化；候选不能直接应用，请重新生成或先查看新旧差异。")
            mode = active_mode(conn, project_id)
            projection = workflow_projection(conn, project_id)
            # Applying a candidate is not stage-bound.
            if candidate["status"] != "candidate_ready":
                raise ProjectServiceError(409, "candidate_not_open", "候选已被处理，不能再次决策。")
            used_request = conn.execute(
                "SELECT id FROM draft_candidates WHERE project_id=? AND decision_request_id=?",
                (project_id, payload.request_id),
            ).fetchone()
            if used_request:
                raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于另一个候选决策。")
            used_event = conn.execute(
                "SELECT id FROM assist_action_events WHERE project_id=? AND request_id=?",
                (project_id, payload.request_id),
            ).fetchone()
            if used_event:
                raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于另一个人工修订事件。")
            job_row = conn.execute("SELECT request_id FROM ai_jobs WHERE id=?", (candidate["job_id"],)).fetchone()
            if payload.decision == "reject":
                conn.execute(
                    "UPDATE draft_candidates SET status='rejected',decision_request_id=?,decision_json=?,decided_at=? WHERE id=?",
                    (payload.request_id, decision_json, now(), candidate_id),
                )
                update_event_status(conn, project_id, job_row["request_id"], "rejected", candidate_id)
                record_event(
                    conn, project_id, mode_id=mode["id"], action_type="candidate_decision",
                    scope={"candidate_id": candidate_id, "decision": "reject"},
                    base_file_versions=candidate["base_file_versions"], status="rejected",
                    request_id=payload.request_id, candidate_id=candidate_id,
                )
                return {"ok": True, "decision": "reject", "workflow": workflow_projection(conn, project_id)}
            patches = candidate["patches"]
            available = {item["path"] if item["kind"] == "file" else "__outline__" for item in patches}
            if payload.decision == "partial_apply" and payload.selected_paths is None:
                raise ProjectServiceError(422, "selection_required", "部分应用必须明确选择至少一个目标。")
            if payload.decision == "accept" and payload.selected_paths is not None:
                raise ProjectServiceError(422, "decision_selection_mismatch", "指定部分目标时请使用 partial_apply。")
            if payload.selected_paths is not None:
                selected = set(payload.selected_paths)
                if not selected.issubset(available):
                    raise ProjectServiceError(422, "invalid_selection", "选择中包含不属于该候选的目标。")
                patches = [item for item in patches if (item["path"] if item["kind"] == "file" else "__outline__") in selected]
                if not patches:
                    raise ProjectServiceError(422, "empty_selection", "至少选择一个候选目标。")
            expected = candidate["base_file_versions"]
            if payload.base_file_versions != expected:
                raise ProjectServiceError(409, "candidate_baseline_changed", "候选基线与请求版本不一致。")
            validation = validate_candidate(conn, project_id, patches)
            if validation["hard_errors"]:
                raise HTTPException(422, detail={"code": "candidate_validation_failed", "message": "候选未通过 LaTeX 结构校验。", "validation": validation})
            if validation["warnings"] and not payload.allow_warnings:
                raise HTTPException(409, detail={"code": "candidate_validation_warning", "message": "候选包含引用警告；请明确覆盖后再应用。", "validation": validation})
            new_versions = apply_bundle(conn, project_id, patches, expected)
            new_status = "partially_applied" if payload.decision == "partial_apply" else "applied"
            conn.execute(
                "UPDATE draft_candidates SET status=?,decision_request_id=?,decision_json=?,decided_at=? WHERE id=?",
                (new_status, payload.request_id, decision_json, now(), candidate_id),
            )
            for sibling in conn.execute(
                "SELECT id,base_file_versions_json FROM draft_candidates WHERE project_id=? AND id<>? AND status='candidate_ready'",
                (project_id, candidate_id),
            ).fetchall():
                if json.loads(sibling["base_file_versions_json"] or "{}") == expected:
                    conn.execute("UPDATE draft_candidates SET status='stale',decided_at=? WHERE id=?", (now(), sibling["id"]))
            update_event_status(conn, project_id, job_row["request_id"], new_status, candidate_id)
            record_event(
                conn, project_id, mode_id=mode["id"], action_type="candidate_decision",
                scope={
                    "candidate_id": candidate_id, "decision": payload.decision,
                    "selected_paths": decision["selected_paths"],
                    "allow_warnings": payload.allow_warnings,
                    "validation_warning_codes": [item["code"] for item in validation["warnings"]],
                },
                base_file_versions=expected, status=new_status,
                request_id=payload.request_id, candidate_id=candidate_id,
            )
            workflow = workflow_projection(conn, project_id)
            return {"ok": True, "decision": payload.decision, "content_versions": new_versions, "validation": validation, "workflow": workflow}
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    finally:
        conn.close()


@app.post("/api/projects/{project_id}/copy-resource")
def copy_resource(project_id: str, payload: CopyIn) -> dict[str, Any]:
    require_project_writable(project_id); source = project_or_404(payload.source_project_id); conn = db()
    table = {"term":"terms", "corpus":"corpus", "ref":"refs"}.get(payload.resource_type)
    if not table:
        conn.close()
        raise HTTPException(400, "资源类型必须是 term、corpus 或 ref")
    if source["lifecycle_state"] != "published":
        conn.close()
        raise HTTPException(409, detail={"code": "source_not_published", "message": "只能从已发布论文复制资源。"})
    row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND project_id=?", (payload.resource_id, payload.source_project_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "源资源不存在")
    new_id = uuid.uuid4().hex[:12]
    values = dict(row)
    values.update({
        "id": new_id,
        "project_id": project_id,
        "source_project_id": payload.source_project_id,
        "source_resource_id": payload.resource_id,
        "source_project_state": source["lifecycle_state"],
        "source_project_revision": int(source["project_revision"]),
        "source_project_archived": int(source["archived"]),
    })
    cols = list(values)
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [values[key] for key in cols],
    )
    touch_project(conn, project_id)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "id": new_id,
        "resource_type": payload.resource_type,
        "source": {
            "project_id": payload.source_project_id,
            "lifecycle_state": source["lifecycle_state"],
            "project_revision": int(source["project_revision"]),
            "archived": bool(source["archived"]),
        },
    }


from paperpilot.api.visual import create_router as create_visual_router
app.include_router(create_visual_router(db, project_or_404, require_project_writable, lambda: PROJECTS_DIR, clean_path))
from paperpilot.api.results import create_router as create_results_router
app.include_router(create_results_router(db, project_or_404, require_project_writable, lambda: PROJECTS_DIR))
from paperpilot.api.innovation_points import create_router as create_innovation_router
app.include_router(create_innovation_router(db, project_or_404))
from paperpilot.api.drawing import create_router as create_drawing_router
app.include_router(create_drawing_router(db, project_or_404, require_project_writable, lambda: PROJECTS_DIR))
from paperpilot.api.visual_evidence import create_router as create_visual_evidence_router
app.include_router(create_visual_evidence_router(db, project_or_404, require_project_writable, lambda: PROJECTS_DIR))
from paperpilot.api.startup import create_router as create_startup_router
app.include_router(create_startup_router(db, project_or_404, require_project_writable))
from paperpilot.api.style_learning import create_router as create_style_learning_router
app.include_router(create_style_learning_router(db, project_or_404, require_project_writable))
from paperpilot.api.provider_runtime import create_router as create_provider_runtime_router
app.include_router(create_provider_runtime_router(db, project_or_404, require_project_writable))

from paperpilot.api.resource_workbench import create_router as create_resource_workbench_router
app.include_router(create_resource_workbench_router(db, project_or_404, require_project_writable, clean_path,
    lambda *args, **kwargs: call_ai(*args, **kwargs)))

from paperpilot.api.research_design import create_router as create_research_design_router
app.include_router(create_research_design_router(db, project_or_404, require_project_writable,
    lambda *args, **kwargs: call_ai(*args, **kwargs)))

from paperpilot.api.agent import create_router as create_agent_router
app.include_router(create_agent_router(db, project_or_404, require_project_writable,
    lambda *args, **kwargs: call_ai(*args, **kwargs)))

from paperpilot.api.citation_review import create_router as create_citation_review_router
app.include_router(create_citation_review_router(db, project_or_404, require_project_writable,
    lambda *args, **kwargs: call_ai(*args, **kwargs)))

from paperpilot.api.bibliography import create_router as create_bibliography_router
app.include_router(create_bibliography_router(db, require_project_writable, clean_path))

from paperpilot.api.conformance import create_router as create_conformance_router
app.include_router(create_conformance_router(lambda: ROOT))
from paperpilot.api.manuscript_import import create_router as create_manuscript_import_router
app.include_router(create_manuscript_import_router(db, lambda: PROJECTS_DIR,
    lambda conn,pid,name: seed_project_workspace(conn,pid,ProjectIn(name=name),'',now()),
    lambda *args,**kwargs: call_ai(*args,**kwargs)))

from paperpilot.services.agent_business_integration import configure as configure_agent_business
from paperpilot.api.agent_actions import create_router as create_agent_actions_router
import sys as _sys
_business_adapters, _business_search, _business_search_cards = configure_agent_business(_sys.modules[__name__])
from paperpilot.services.structure_integration import install as install_structure_integration
install_structure_integration(_business_adapters)
app.include_router(create_agent_actions_router(db, project_or_404, _business_adapters, _business_search, _business_search_cards))

from paperpilot.api.structure import create_router as create_structure_router
app.include_router(create_structure_router(db, project_or_404, require_project_writable,
    lambda *args, **kwargs: call_ai(*args, **kwargs), _business_adapters))

from paperpilot.services import scholar_workspace
from paperpilot.services import innovation_points as _innovation_points
from paperpilot.services import agent_tools as _agent_tools
scholar_workspace.configure(lambda: PROJECTS_DIR)
_agent_tools.register_provider(scholar_workspace)
_agent_tools.register_provider(_innovation_points)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



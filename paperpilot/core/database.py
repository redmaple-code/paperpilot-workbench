from __future__ import annotations

import sqlite3


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create the local schema and apply idempotent additive migrations."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',
          style TEXT NOT NULL DEFAULT '', citation_style TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ai_base_url TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
          lifecycle_state TEXT NOT NULL DEFAULT 'draft', project_revision INTEGER NOT NULL DEFAULT 1,
          ai_soft_budget_tokens INTEGER, domain_id TEXT NOT NULL DEFAULT '', ai_profile_id TEXT NOT NULL DEFAULT '',
          ai_model_id TEXT NOT NULL DEFAULT '', ai_config_revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS domains (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS domain_setting_versions (
          domain_id TEXT NOT NULL, version INTEGER NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
          PRIMARY KEY(domain_id, version),
          FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_setting_baselines (
          project_id TEXT PRIMARY KEY, domain_id TEXT NOT NULL DEFAULT '', domain_version INTEGER NOT NULL DEFAULT 0,
          version INTEGER NOT NULL DEFAULT 1, settings_json TEXT NOT NULL DEFAULT '{}', sources_json TEXT NOT NULL DEFAULT '{}',
          updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_setting_overrides (
          project_id TEXT NOT NULL, setting_key TEXT NOT NULL, value_json TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
          PRIMARY KEY(project_id, setting_key),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_setting_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          action TEXT NOT NULL, base_version INTEGER NOT NULL DEFAULT 0,
          result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_domains_name ON domains(name COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS domain_resources (
          id TEXT PRIMARY KEY, domain_id TEXT NOT NULL, resource_type TEXT NOT NULL, title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '', source_project_id TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_domain_resources_domain ON domain_resources(domain_id,resource_type,status,title);
        CREATE TABLE IF NOT EXISTS domain_versions (
          domain_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          batch_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          PRIMARY KEY(domain_id, version),
          FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS domain_terms (
          id TEXT PRIMARY KEY, domain_id TEXT NOT NULL, version INTEGER NOT NULL,
          term TEXT NOT NULL, definition TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active', source_project_id TEXT NOT NULL DEFAULT '',
          source_term_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          FOREIGN KEY(domain_id, version) REFERENCES domain_versions(domain_id, version) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_domain_terms_lookup ON domain_terms(domain_id, version, status, term);
        CREATE TABLE IF NOT EXISTS domain_term_imports (
          request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, domain_term_id TEXT NOT NULL,
          source_domain_version INTEGER NOT NULL, created_term_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS term_promote_batches (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, domain_id TEXT NOT NULL,
          source_project_revision INTEGER NOT NULL, target_domain_version INTEGER NOT NULL,
          status TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
          preview_json TEXT NOT NULL DEFAULT '{}', results_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS terminology_reviews (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, term_id TEXT NOT NULL, domain_id TEXT NOT NULL,
          suggested_term TEXT NOT NULL, suggested_definition TEXT NOT NULL DEFAULT '',
          suggested_aliases TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
          basis TEXT NOT NULL DEFAULT '', classifier_version TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending_manual', decision_json TEXT NOT NULL DEFAULT '',
          request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_terminology_reviews_status ON terminology_reviews(project_id, status, created_at DESC);
        CREATE TABLE IF NOT EXISTS ai_provider_profiles (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, auth_mode TEXT NOT NULL,
          credential_state TEXT NOT NULL DEFAULT 'missing', credential_digest TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
          current_version INTEGER NOT NULL DEFAULT 1, models_json TEXT NOT NULL DEFAULT '[]', capabilities_json TEXT NOT NULL DEFAULT '{}',
          last_test_status TEXT NOT NULL DEFAULT 'not_tested', last_tested_at TEXT, request_id TEXT NOT NULL UNIQUE,
          request_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_provider_profile_versions (
          profile_id TEXT NOT NULL, version INTEGER NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(profile_id,version), FOREIGN KEY(profile_id) REFERENCES ai_provider_profiles(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_status_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          command TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL,
          project_revision INTEGER NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS project_release_checks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_revision INTEGER NOT NULL,
          compile_ok INTEGER NOT NULL, missing_citations_json TEXT NOT NULL DEFAULT '[]',
          result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_release_checks_project ON project_release_checks(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS files (
          project_id TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
          updated_at TEXT NOT NULL, PRIMARY KEY(project_id, path),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline (
          project_id TEXT PRIMARY KEY, content TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_versions (
          project_id TEXT NOT NULL, version INTEGER NOT NULL, source TEXT NOT NULL,
          nodes_json TEXT NOT NULL DEFAULT '[]', links_json TEXT NOT NULL DEFAULT '[]',
          request_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id, version), UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_command_requests (
          project_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
          payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_migration_drafts (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, base_text_version INTEGER NOT NULL,
          nodes_json TEXT NOT NULL DEFAULT '[]', unknown_lines_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'preview', request_id TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_candidates (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, base_version INTEGER NOT NULL,
          operations_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'candidate',
          request_id TEXT NOT NULL UNIQUE, decision_request_id TEXT, decision_json TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, decided_at TEXT,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_local_drafts (
          project_id TEXT NOT NULL, draft_key TEXT NOT NULL, base_version INTEGER NOT NULL,
          content_json TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(project_id, draft_key),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS outline_node_links (
          project_id TEXT NOT NULL, version INTEGER NOT NULL, node_id TEXT NOT NULL,
          resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, source_version INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'active', replacement_resource_id TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(project_id, version, node_id, resource_type, resource_id),
          FOREIGN KEY(project_id, version) REFERENCES outline_versions(project_id, version) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS terms (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, term TEXT NOT NULL,
          definition TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT '',
          version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
          source_domain_id TEXT NOT NULL DEFAULT '', source_domain_term_id TEXT NOT NULL DEFAULT '',
          source_domain_version INTEGER NOT NULL DEFAULT 0,
          source_project_id TEXT NOT NULL DEFAULT '', source_resource_id TEXT NOT NULL DEFAULT '',
          source_project_state TEXT NOT NULL DEFAULT '', source_project_revision INTEGER NOT NULL DEFAULT 0,
          source_project_archived INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS corpus (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
          source_project_id TEXT NOT NULL DEFAULT '', source_resource_id TEXT NOT NULL DEFAULT '',
          source_project_state TEXT NOT NULL DEFAULT '', source_project_revision INTEGER NOT NULL DEFAULT 0,
          source_project_archived INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS corpus_source_versions (
          corpus_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT '', source_kind TEXT NOT NULL DEFAULT 'manual',
          permission_state TEXT NOT NULL DEFAULT 'authorized', status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          PRIMARY KEY(corpus_id, version),
          FOREIGN KEY(corpus_id) REFERENCES corpus(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_corpus_source_versions ON corpus_source_versions(corpus_id, version DESC);
        CREATE TABLE IF NOT EXISTS corpus_curation_versions (
          id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL, source_version INTEGER NOT NULL,
          version INTEGER NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL,
          notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
          UNIQUE(corpus_id, version),
          FOREIGN KEY(corpus_id, source_version) REFERENCES corpus_source_versions(corpus_id, version) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS corpus_refresh_candidates (
          id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL, base_source_version INTEGER NOT NULL,
          proposed_content TEXT NOT NULL, proposed_source TEXT NOT NULL DEFAULT '',
          permission_state TEXT NOT NULL DEFAULT 'authorized', status TEXT NOT NULL DEFAULT 'pending',
          request_id TEXT NOT NULL UNIQUE, decision_request_id TEXT NOT NULL DEFAULT '',
          decision_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, decided_at TEXT,
          FOREIGN KEY(corpus_id) REFERENCES corpus(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_corpus_refresh_candidates ON corpus_refresh_candidates(corpus_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS corpus_index_chunks (
          corpus_id TEXT NOT NULL, source_version INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
          content TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
          location TEXT NOT NULL DEFAULT '',
          token_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
          PRIMARY KEY(corpus_id, source_version, chunk_index),
          FOREIGN KEY(corpus_id, source_version) REFERENCES corpus_source_versions(corpus_id, version) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_corpus_index_chunks_lookup ON corpus_index_chunks(corpus_id, source_version, status);
        CREATE TABLE IF NOT EXISTS modification_requests (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
          action TEXT NOT NULL, target_kind TEXT NOT NULL, target_path TEXT NOT NULL DEFAULT '',
          start_offset INTEGER NOT NULL DEFAULT 0, end_offset INTEGER NOT NULL DEFAULT 0,
          instruction TEXT NOT NULL, workflow_version INTEGER NOT NULL,
          base_file_versions_json TEXT NOT NULL, context_version TEXT NOT NULL,
          job_id TEXT, candidate_id TEXT, status TEXT NOT NULL DEFAULT 'queued',
          snapshot_json TEXT NOT NULL DEFAULT '{}', decision_request_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES ai_jobs(id) ON DELETE SET NULL,
          FOREIGN KEY(candidate_id) REFERENCES draft_candidates(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_modification_requests_project ON modification_requests(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS modification_cleanup_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          scope TEXT NOT NULL, operation_id TEXT NOT NULL DEFAULT '',
          result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS citation_check_runs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          target_path TEXT NOT NULL DEFAULT '', source_project_revision INTEGER NOT NULL,
          source_version INTEGER NOT NULL DEFAULT 0, source_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
          error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_citation_check_runs_project
          ON citation_check_runs(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS format_check_runs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          target_path TEXT NOT NULL DEFAULT '', source_project_revision INTEGER NOT NULL,
          dependencies_json TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_format_check_runs_project
          ON format_check_runs(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS build_snapshots (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, snapshot_version INTEGER NOT NULL,
          project_revision INTEGER NOT NULL, purpose TEXT NOT NULL, manifest_json TEXT NOT NULL,
          files_json TEXT NOT NULL, references_json TEXT NOT NULL DEFAULT '[]', settings_json TEXT NOT NULL DEFAULT '{}',
          request_id TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_build_snapshots_project ON build_snapshots(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS build_jobs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, request_id TEXT NOT NULL,
          purpose TEXT NOT NULL, status TEXT NOT NULL, diagnostics_json TEXT NOT NULL DEFAULT '[]',
          artifact_path TEXT NOT NULL DEFAULT '', artifact_sha256 TEXT NOT NULL DEFAULT '',
          started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(snapshot_id) REFERENCES build_snapshots(id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_build_jobs_project ON build_jobs(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS build_restore_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
          request_id TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          UNIQUE(project_id, job_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES build_jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS refs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, cite_key TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '', authors TEXT NOT NULL DEFAULT '',
          year TEXT NOT NULL DEFAULT '', bibtex TEXT NOT NULL DEFAULT '',
          source_project_id TEXT NOT NULL DEFAULT '', source_resource_id TEXT NOT NULL DEFAULT '',
          source_project_state TEXT NOT NULL DEFAULT '', source_project_revision INTEGER NOT NULL DEFAULT 0,
          source_project_archived INTEGER NOT NULL DEFAULT 0,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reference_records (
          id TEXT PRIMARY KEY, current_version INTEGER NOT NULL DEFAULT 1,
          doi TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
          year TEXT NOT NULL DEFAULT '', venue TEXT NOT NULL DEFAULT '', canonical_url TEXT NOT NULL DEFAULT '',
          abstract TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
          source_trace_json TEXT NOT NULL DEFAULT '{}', full_text_presence TEXT NOT NULL DEFAULT 'metadata_only',
          attachment_status TEXT NOT NULL DEFAULT 'none', confidence TEXT NOT NULL DEFAULT 'lowered',
          status TEXT NOT NULL DEFAULT 'active', merged_into TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(merged_into) REFERENCES reference_records(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reference_records_doi ON reference_records(doi);
        CREATE INDEX IF NOT EXISTS idx_reference_records_title ON reference_records(title,authors,year);
        CREATE TABLE IF NOT EXISTS reference_record_versions (
          reference_id TEXT NOT NULL, version INTEGER NOT NULL, metadata_json TEXT NOT NULL,
          source_trace_json TEXT NOT NULL DEFAULT '{}', full_text_presence TEXT NOT NULL,
          attachment_status TEXT NOT NULL, confidence TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(reference_id,version),
          FOREIGN KEY(reference_id) REFERENCES reference_records(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reference_merge_events (
          id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL,
          target_id TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(source_id) REFERENCES reference_records(id),
          FOREIGN KEY(target_id) REFERENCES reference_records(id)
        );
        CREATE TABLE IF NOT EXISTS project_reference_links (
          project_id TEXT NOT NULL, reference_id TEXT NOT NULL, reference_version INTEGER NOT NULL,
          cite_key TEXT NOT NULL, request_id TEXT NOT NULL, citation_snapshot_json TEXT NOT NULL,
          link_status TEXT NOT NULL DEFAULT 'linked', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(project_id,reference_id), UNIQUE(project_id,cite_key),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(reference_id,reference_version) REFERENCES reference_record_versions(reference_id,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_project_reference_request
          ON project_reference_links(project_id,request_id);
        CREATE TABLE IF NOT EXISTS project_reference_link_history (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, reference_id TEXT NOT NULL,
          reference_version INTEGER NOT NULL, cite_key TEXT NOT NULL, citation_snapshot_json TEXT NOT NULL,
          base_reference_version INTEGER NOT NULL DEFAULT 0,
          request_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(reference_id) REFERENCES reference_records(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reference_drafts (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_id TEXT NOT NULL DEFAULT '',
          handoff_id TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL UNIQUE,
          metadata_json TEXT NOT NULL, source_trace_json TEXT NOT NULL DEFAULT '{}',
          full_text_presence TEXT NOT NULL DEFAULT 'metadata_only', confidence TEXT NOT NULL DEFAULT 'lowered',
          status TEXT NOT NULL DEFAULT 'pending_confirmation', created_at TEXT NOT NULL, confirmed_at TEXT,
          confirmation_request_id TEXT NOT NULL DEFAULT '', confirmation_json TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_reference_drafts_project ON reference_drafts(project_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS logic_nodes (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
          label TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '',
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS logic_graph_versions (
          project_id TEXT NOT NULL, version INTEGER NOT NULL, request_id TEXT NOT NULL,
          nodes_json TEXT NOT NULL, edges_json TEXT NOT NULL, gaps_json TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(project_id,version), UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS logic_suggestions (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          base_version INTEGER NOT NULL, input_hash TEXT NOT NULL DEFAULT '',
          operations_json TEXT NOT NULL, status TEXT NOT NULL,
          decision_request_id TEXT, decision_json TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, decided_at TEXT,
          UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_logic_suggestion_decision_request
          ON logic_suggestions(project_id,decision_request_id) WHERE decision_request_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS academic_searches (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, query TEXT NOT NULL,
          filters_json TEXT NOT NULL DEFAULT '{}', provider TEXT NOT NULL,
          rule_version TEXT NOT NULL, request_id TEXT NOT NULL, generation INTEGER NOT NULL,
          next_cursor TEXT, status TEXT NOT NULL, saved INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_academic_searches_project ON academic_searches(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS academic_candidates (
          id TEXT PRIMARY KEY, search_id TEXT NOT NULL, project_id TEXT NOT NULL,
          title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '', year TEXT NOT NULL DEFAULT '',
          venue TEXT NOT NULL DEFAULT '', doi TEXT NOT NULL DEFAULT '', canonical_url TEXT NOT NULL DEFAULT '',
          abstract TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL, access_status TEXT NOT NULL,
          oa_url TEXT NOT NULL DEFAULT '',
          match_reasons_json TEXT NOT NULL DEFAULT '[]', sources_json TEXT NOT NULL DEFAULT '[]',
          dedupe_key TEXT NOT NULL, rank_score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
          raw_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(search_id) REFERENCES academic_searches(id) ON DELETE CASCADE,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          UNIQUE(search_id, dedupe_key)
        );
        CREATE INDEX IF NOT EXISTS idx_academic_candidates_search ON academic_candidates(search_id, rank_score DESC);
        CREATE TABLE IF NOT EXISTS academic_handoffs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          source_trace_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(candidate_id) REFERENCES academic_candidates(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reference_handoff_drafts (
          handoff_id TEXT PRIMARY KEY, reference_draft_id TEXT NOT NULL,
          FOREIGN KEY(handoff_id) REFERENCES academic_handoffs(id) ON DELETE CASCADE,
          FOREIGN KEY(reference_draft_id) REFERENCES reference_drafts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS oa_imports (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_id TEXT,
          idempotency_key TEXT NOT NULL UNIQUE, doi TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
          oa_url TEXT NOT NULL DEFAULT '', access_status TEXT NOT NULL,
          status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
          error_message TEXT NOT NULL DEFAULT '', preview_json TEXT NOT NULL DEFAULT '{}',
          source_json TEXT NOT NULL DEFAULT '{}', file_sha256 TEXT NOT NULL DEFAULT '',
          file_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(candidate_id) REFERENCES academic_candidates(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_oa_imports_project ON oa_imports(project_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS oa_cleanup_queue (
          project_id TEXT NOT NULL, import_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
          owner_pid INTEGER NOT NULL DEFAULT 0, owner_token TEXT NOT NULL DEFAULT '',
          wait_for_owner INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(project_id, import_id, attempt_id)
        );
        CREATE TABLE IF NOT EXISTS external_evidence (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, import_id TEXT NOT NULL,
          source_url TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
          fragments_json TEXT NOT NULL DEFAULT '[]', attachment_path TEXT NOT NULL DEFAULT '',
          confirmed_at TEXT NOT NULL, source_version TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(import_id) REFERENCES oa_imports(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reference_attachments (
          reference_id TEXT NOT NULL, reference_version INTEGER NOT NULL,
          project_id TEXT NOT NULL, external_evidence_id TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(reference_id,reference_version),
          FOREIGN KEY(reference_id,reference_version) REFERENCES reference_record_versions(reference_id,version),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(external_evidence_id) REFERENCES external_evidence(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS workspace_runs (
          id TEXT PRIMARY KEY, project_id TEXT UNIQUE, request_id TEXT NOT NULL UNIQUE,
          request_fingerprint TEXT NOT NULL DEFAULT '', template_version TEXT NOT NULL,
          status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS workflow_work_items (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, resource_kind TEXT NOT NULL,
          title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id, resource_kind),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_work_items_project ON workflow_work_items(project_id, created_at);
        CREATE TABLE IF NOT EXISTS workflow_snapshots (
          project_id TEXT NOT NULL, version INTEGER NOT NULL, current_stage TEXT NOT NULL,
          stage_status TEXT NOT NULL, stage_outputs_json TEXT NOT NULL DEFAULT '{}',
          confirmation_status TEXT NOT NULL DEFAULT 'pending', history_parent_version INTEGER,
          created_at TEXT NOT NULL,
          PRIMARY KEY(project_id, version),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS workflow_commands (
          project_id TEXT NOT NULL, request_id TEXT NOT NULL, command TEXT NOT NULL,
          base_version INTEGER NOT NULL, target_version INTEGER, draft_action TEXT,
          result_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS content_versions (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, target_kind TEXT NOT NULL,
          target_path TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL, parent_version INTEGER,
          restored_from_version INTEGER, source TEXT NOT NULL, content TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(project_id, target_kind, target_path, version),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS editor_write_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          operation TEXT NOT NULL, target_path TEXT NOT NULL, base_version INTEGER NOT NULL,
          content_sha256 TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS editor_local_drafts (
          project_id TEXT NOT NULL, path TEXT NOT NULL, base_version INTEGER NOT NULL,
          base_content TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(project_id,path),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS editor_conversion_previews (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          source_path TEXT NOT NULL, source_version INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
          direction TEXT NOT NULL, target_path TEXT NOT NULL, target_version INTEGER NOT NULL,
          target_content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'preview',
          apply_request_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id,request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_editor_local_drafts_project ON editor_local_drafts(project_id,updated_at DESC);
        CREATE TABLE IF NOT EXISTS context_snapshots (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, context_version TEXT NOT NULL,
          source_manifest_json TEXT NOT NULL, context_text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS ai_jobs (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          action TEXT NOT NULL, status TEXT NOT NULL, workflow_version INTEGER NOT NULL,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          attempt INTEGER NOT NULL DEFAULT 1, error_code TEXT NOT NULL DEFAULT '',
          input_json TEXT NOT NULL, scope_json TEXT NOT NULL, base_file_versions_json TEXT NOT NULL,
          context_snapshot_id TEXT, model TEXT NOT NULL DEFAULT '', usage_json TEXT NOT NULL DEFAULT '{}', candidate_id TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_jobs_project ON ai_jobs(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS draft_candidates (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE,
          context_version TEXT NOT NULL, context_snapshot_id TEXT,
          base_file_versions_json TEXT NOT NULL,
          scope_json TEXT NOT NULL, content_json TEXT NOT NULL, status TEXT NOT NULL,
          decision_request_id TEXT, decision_json TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, decided_at TEXT,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES ai_jobs(id) ON DELETE CASCADE,
          FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS human_revision_modes (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_version INTEGER NOT NULL,
          state TEXT NOT NULL, entry_source TEXT NOT NULL, exit_target TEXT NOT NULL DEFAULT '',
          mode_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_human_revision_active
          ON human_revision_modes(project_id) WHERE status='active';
        CREATE TABLE IF NOT EXISTS revision_drafts (
          project_id TEXT NOT NULL, target_kind TEXT NOT NULL, target_path TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL, base_version INTEGER NOT NULL, mode_id TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(project_id, target_kind, target_path),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(mode_id) REFERENCES human_revision_modes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS revision_draft_copies (
          project_id TEXT NOT NULL, copy_id TEXT NOT NULL, target_kind TEXT NOT NULL,
          target_path TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, base_version INTEGER NOT NULL,
          updated_at TEXT NOT NULL, PRIMARY KEY(project_id, copy_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS assist_action_events (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, mode_id TEXT NOT NULL,
          action_type TEXT NOT NULL, scope_json TEXT NOT NULL, base_file_versions_json TEXT NOT NULL,
          context_snapshot_id TEXT, candidate_id TEXT, status TEXT NOT NULL,
          request_id TEXT, created_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(mode_id) REFERENCES human_revision_modes(id) ON DELETE CASCADE,
          FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots(id) ON DELETE SET NULL,
          FOREIGN KEY(candidate_id) REFERENCES draft_candidates(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS continuation_batches (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
          target_kind TEXT NOT NULL, target_path TEXT NOT NULL DEFAULT '',
          anchor_start INTEGER NOT NULL DEFAULT 0, anchor_end INTEGER NOT NULL DEFAULT 0,
          goal TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '', segment_count INTEGER NOT NULL DEFAULT 1,
          outline_node_id TEXT NOT NULL DEFAULT '', outline_version INTEGER,
          workflow_version INTEGER NOT NULL, base_file_versions_json TEXT NOT NULL,
          context_version TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
          budget_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
          pause_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id, request_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_batches_project ON continuation_batches(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS continuation_segments (
          id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
          job_id TEXT, candidate_id TEXT, status TEXT NOT NULL DEFAULT 'queued',
          error_code TEXT NOT NULL DEFAULT '', usage_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(batch_id, sequence),
          FOREIGN KEY(batch_id) REFERENCES continuation_batches(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES ai_jobs(id) ON DELETE SET NULL,
          FOREIGN KEY(candidate_id) REFERENCES draft_candidates(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_segments_batch ON continuation_segments(batch_id, sequence);
        CREATE TABLE IF NOT EXISTS continuation_events (
          id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, segment_id TEXT,
          request_id TEXT NOT NULL, event_type TEXT NOT NULL, status TEXT NOT NULL,
          before_version INTEGER, after_version INTEGER, payload_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          UNIQUE(batch_id, request_id),
          FOREIGN KEY(batch_id) REFERENCES continuation_batches(id) ON DELETE CASCADE,
          FOREIGN KEY(segment_id) REFERENCES continuation_segments(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuation_events_batch ON continuation_events(batch_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_snapshots_latest ON workflow_snapshots(project_id, version DESC);
        CREATE INDEX IF NOT EXISTS idx_content_versions_latest ON content_versions(project_id, target_kind, target_path, version DESC);
        CREATE INDEX IF NOT EXISTS idx_revision_events_project ON assist_action_events(project_id, created_at DESC);
        """
    )
    candidate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(draft_candidates)")}
    if "draft_snapshot_json" not in {row["name"] for row in conn.execute("PRAGMA table_info(workflow_commands)")}:
        conn.execute("ALTER TABLE workflow_commands ADD COLUMN draft_snapshot_json TEXT")
    if "decision_request_id" not in candidate_columns:
        conn.execute("ALTER TABLE draft_candidates ADD COLUMN decision_request_id TEXT")
    if "decision_json" not in candidate_columns:
        conn.execute("ALTER TABLE draft_candidates ADD COLUMN decision_json TEXT NOT NULL DEFAULT ''")
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ai_jobs)")}
    if "usage_json" not in job_columns:
        conn.execute("ALTER TABLE ai_jobs ADD COLUMN usage_json TEXT NOT NULL DEFAULT '{}'" )
    for name, declaration in [('owner_pid', 'INTEGER NOT NULL DEFAULT 0'), ('owner_token', "TEXT NOT NULL DEFAULT ''")]:
        if name not in job_columns:
            conn.execute(f'ALTER TABLE ai_jobs ADD COLUMN {name} {declaration}')
    suggestion_columns = {row["name"] for row in conn.execute("PRAGMA table_info(logic_suggestions)")}
    if "input_hash" not in suggestion_columns:
        conn.execute("ALTER TABLE logic_suggestions ADD COLUMN input_hash TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_decision_request ON draft_candidates(project_id,decision_request_id) WHERE decision_request_id IS NOT NULL"
    )
    for table, column in (
        ("projects", "model"),
        ("projects", "ai_base_url"),
        ("projects", "archived"),
        ("terms", "source_project_id"),
        ("terms", "source_resource_id"),
        ("terms", "source_project_state"),
        ("terms", "source_project_revision"),
        ("terms", "source_project_archived"),
        ("terms", "version"),
        ("terms", "status"),
        ("terms", "source_domain_id"),
        ("terms", "source_domain_term_id"),
        ("terms", "source_domain_version"),
        ("corpus", "source_project_id"),
        ("corpus", "source_resource_id"),
        ("corpus", "source_project_state"),
        ("corpus", "source_project_revision"),
        ("corpus", "source_project_archived"),
        ("refs", "source_project_id"),
        ("refs", "source_resource_id"),
        ("refs", "source_project_state"),
        ("refs", "source_project_revision"),
        ("refs", "source_project_archived"),
        ("academic_candidates", "oa_url"),
    ):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            continue
        try:
            integer_columns = {"archived", "source_project_revision", "source_project_archived", "version", "source_domain_version"}
            default = "1" if column == "version" else ("0" if column in integer_columns else ("'active'" if column == "status" else "''"))
            sql_type = "INTEGER" if column in integer_columns else "TEXT"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type} NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    search_columns = {row['name'] for row in conn.execute('PRAGMA table_info(academic_searches)')}
    if 'summary_json' not in search_columns:
        try:conn.execute("ALTER TABLE academic_searches ADD COLUMN summary_json TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower():raise

    baseline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_setting_baselines)")}
    if "sources_json" not in baseline_columns:
        conn.execute("ALTER TABLE project_setting_baselines ADD COLUMN sources_json TEXT NOT NULL DEFAULT '{}'")

    import_columns = {row['name'] for row in conn.execute('PRAGMA table_info(oa_imports)')}
    for name in ('request_fingerprint', 'attempt_id'):
        if name not in import_columns:
            conn.execute(f"ALTER TABLE oa_imports ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    for name, declaration in [('owner_pid', 'INTEGER NOT NULL DEFAULT 0'), ('owner_token', "TEXT NOT NULL DEFAULT ''")]:
        if name not in import_columns:
            conn.execute(f"ALTER TABLE oa_imports ADD COLUMN {name} {declaration}")

    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "lifecycle_state" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'draft'")
    if "project_revision" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN project_revision INTEGER NOT NULL DEFAULT 1")
    if "ai_soft_budget_tokens" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN ai_soft_budget_tokens INTEGER")
    for column, sql_type, default in (
        ("domain_id", "TEXT", "''"), ("ai_profile_id", "TEXT", "''"),
        ("ai_model_id", "TEXT", "''"), ("ai_config_revision", "INTEGER", "0"),
    ):
        if column not in project_columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} {sql_type} NOT NULL DEFAULT {default}")

    link_history_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_reference_link_history)")}
    if "base_reference_version" not in link_history_columns:
        conn.execute("ALTER TABLE project_reference_link_history ADD COLUMN base_reference_version INTEGER NOT NULL DEFAULT 0")

    draft_columns = {row["name"] for row in conn.execute("PRAGMA table_info(reference_drafts)")}
    if "confirmation_request_id" not in draft_columns:
        conn.execute("ALTER TABLE reference_drafts ADD COLUMN confirmation_request_id TEXT NOT NULL DEFAULT ''")
    if "confirmation_json" not in draft_columns:
        conn.execute("ALTER TABLE reference_drafts ADD COLUMN confirmation_json TEXT NOT NULL DEFAULT ''")

    run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(workspace_runs)")}
    if "request_fingerprint" not in run_columns:
        conn.execute("ALTER TABLE workspace_runs ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")

    # Seed the immutable version ledger for domains created by earlier releases.
    stamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO domain_versions(domain_id,version,status,batch_id,created_at) "
        "SELECT id,version,'active','',? FROM domains",
        (stamp,),
    )
    from paperpilot.services.visual_assets import initialize_schema as initialize_visual_schema
    initialize_visual_schema(conn)
    from paperpilot.services.results import initialize_schema as initialize_results_schema
    initialize_results_schema(conn)
    from paperpilot.services.visual_jobs import initialize_schema as initialize_drawing_schema
    initialize_drawing_schema(conn)
    from paperpilot.services.visual_evidence import initialize_schema as initialize_visual_evidence
    initialize_visual_evidence(conn)
    from paperpilot.services.style_learning import initialize_schema as initialize_style_learning
    initialize_style_learning(conn)
    from paperpilot.services.credential_vault import initialize_schema as initialize_credential_vault
    initialize_credential_vault(conn)
    from paperpilot.services.provider_runtime import initialize_schema as initialize_provider_runtime
    initialize_provider_runtime(conn)
    build_columns = {row['name'] for row in conn.execute('PRAGMA table_info(build_jobs)')}
    for name, declaration in [('owner_pid', 'INTEGER NOT NULL DEFAULT 0'), ('owner_token', "TEXT NOT NULL DEFAULT ''"), ('promoted', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in build_columns:
            conn.execute(f'ALTER TABLE build_jobs ADD COLUMN {name} {declaration}')
    from paperpilot.services.build_queue import initialize_schema as initialize_build_queue
    initialize_build_queue(conn)
    from paperpilot.services.build_cache import initialize_schema as initialize_build_cache
    initialize_build_cache(conn)
    from paperpilot.services.domain_resources import initialize_schema as initialize_domain_resource_versions
    initialize_domain_resource_versions(conn)
    from paperpilot.services.corpus_migrations import initialize_schema as initialize_corpus_migrations
    initialize_corpus_migrations(conn)
    if "location" not in {row["name"] for row in conn.execute("PRAGMA table_info(corpus_index_chunks)")}:
        conn.execute("ALTER TABLE corpus_index_chunks ADD COLUMN location TEXT NOT NULL DEFAULT ''")
    review_columns = {row['name'] for row in conn.execute('PRAGMA table_info(terminology_reviews)')}
    if 'input_fingerprint' not in review_columns:
        conn.execute("ALTER TABLE terminology_reviews ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT ''")
    outline_candidate_columns = {row['name'] for row in conn.execute('PRAGMA table_info(outline_candidates)')}
    if 'origin_json' not in outline_candidate_columns:
        conn.execute("ALTER TABLE outline_candidates ADD COLUMN origin_json TEXT NOT NULL DEFAULT '{}'")
    from paperpilot.services.continuation_budget import initialize_schema as initialize_continuation_budget
    initialize_continuation_budget(conn)
    from paperpilot.services.citation_review import initialize_schema as initialize_citation_review
    initialize_citation_review(conn)
    segment_columns = {row['name'] for row in conn.execute('PRAGMA table_info(continuation_segments)')}
    for name in ('goal', 'outline_node_id'):
        if name not in segment_columns:
            conn.execute(f"ALTER TABLE continuation_segments ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    batch_columns = {row['name'] for row in conn.execute('PRAGMA table_info(continuation_batches)')}
    if 'input_fingerprint' not in batch_columns:
        conn.execute("ALTER TABLE continuation_batches ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT ''")
    if 'subgoals_json' not in batch_columns:
        conn.execute("ALTER TABLE continuation_batches ADD COLUMN subgoals_json TEXT NOT NULL DEFAULT '[]'")
    # Legacy domain resources remain available; only explicit domain_terms participate in REQ-002.
    conn.commit()


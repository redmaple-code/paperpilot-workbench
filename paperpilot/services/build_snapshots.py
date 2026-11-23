from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from paperpilot.services import settings
from paperpilot.services import visual_assets
from paperpilot.services.content_versions import version_map
from paperpilot.services.project_errors import ProjectServiceError


PURPOSES = {"draft_preview", "candidate_preview", "submitted", "release"}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _manifest(files: list[dict[str, Any]], refs: list[dict[str, Any]], project_revision: int, purpose: str, setting_view: dict[str, Any]) -> dict[str, Any]:
    items = [{"path": item["path"], "version": int(item.get("version", 0)), "sha256": hashlib.sha256(str(item.get("content", "")).encode("utf-8")).hexdigest()} for item in files]
    ref_items = [{"cite_key": item.get("cite_key", ""), "title": item.get("title", ""), "version": int(item.get("reference_version", 0) or 0)} for item in refs]
    identity = hashlib.sha256(_json({"files": items, "refs": ref_items, "project_revision": project_revision, "purpose": purpose, "settings": setting_view.get("effective", {})}).encode("utf-8")).hexdigest()
    return {"identity": identity, "project_revision": project_revision, "purpose": purpose, "files": items, "references": ref_items, "settings": setting_view.get("effective", {}), "created_at": _now()}


def create(conn: sqlite3.Connection, project_id: str, request_id: str, purpose: str = "draft_preview", expected_project_revision: int | None = None, *, asset_storage_root: Path | None = None, draft_source: str = 'all', candidate_id: str | None = None) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if purpose not in PURPOSES:
        raise ProjectServiceError(422, "invalid_build_purpose", "不支持的构建用途。")
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "项目不存在。")
    existing = conn.execute("SELECT * FROM build_snapshots WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if existing:
        previous = payload(existing)
        expected = {'purpose': purpose, 'draft_source': draft_source, 'candidate_id': candidate_id, 'expected_project_revision': expected_project_revision}
        if previous['manifest'].get('request_params', expected) != expected or previous['purpose'] != purpose:
            raise ProjectServiceError(409, 'build_snapshot_request_conflict', '快照请求编号已用于不同用途、来源或版本，请核对后以新编号提交。')
        return previous | {"idempotent_replay": True}
    if expected_project_revision is not None and int(project["project_revision"]) != int(expected_project_revision):
        raise ProjectServiceError(409, "project_revision_conflict", "项目内容已变化，请重新创建构建快照。")
    files = [dict(row) for row in conn.execute("SELECT path,content FROM files WHERE project_id=? ORDER BY path", (project_id,))]
    versions = version_map(conn, project_id)
    for item in files:
        item["version"] = versions.get(item["path"], 0)
    baseline_files = _manifest(files, [], int(project['project_revision']), purpose, {})['files']
    from paperpilot.services.build_sources import materialize
    files, source = materialize(conn, project_id, files, purpose, draft_source=draft_source, candidate_id=candidate_id)
    refs = [dict(row) for row in conn.execute("SELECT cite_key,title,authors,year,bibtex FROM refs WHERE project_id=? ORDER BY cite_key", (project_id,))]
    for row in conn.execute("SELECT cite_key,reference_version,citation_snapshot_json FROM project_reference_links WHERE project_id=? AND link_status IN ('linked','update_available') ORDER BY cite_key", (project_id,)):
        snap = json.loads(row["citation_snapshot_json"] or "{}")
        refs.append({"cite_key": row["cite_key"], "reference_version": row["reference_version"], **snap})
    setting_view = settings.read(conn, project_id)
    snapshot_id = uuid.uuid4().hex
    stamp = _now()
    manifest = _manifest(files, refs, int(project["project_revision"]), purpose, setting_view)
    asset_manifest = visual_assets.capture_manifest(conn, project_id, files)
    if asset_manifest:
        if asset_storage_root is None:
            raise ProjectServiceError(503, "asset_storage_unavailable", "构建需要可验证的受管图像存储。")
        for item in asset_manifest:
            visual_assets.checked_file(conn, asset_storage_root, project_id, item["version_id"])
    manifest["assets"] = asset_manifest
    manifest['build_profile'] = build_profile(conn, project_id)
    manifest['project_domain_id'] = str(project['domain_id'] or '')
    manifest['source'] = source
    manifest['baseline_files'] = baseline_files
    manifest['request_params'] = {'purpose': purpose, 'draft_source': draft_source, 'candidate_id': candidate_id, 'expected_project_revision': expected_project_revision}
    from paperpilot.services import visual_evidence
    # Read-only advisory projection; snapshot creation never runs a vision model.
    manifest["visual_reports"] = [visual_evidence.current(conn, project_id, item["version_id"], "build") for item in asset_manifest]
    manifest["identity"] = hashlib.sha256(_json({"text_identity": manifest["identity"], "assets": asset_manifest, 'build_profile': manifest['build_profile'], 'source': source, 'project_domain_id': manifest['project_domain_id']}).encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO build_snapshots(id,project_id,snapshot_version,project_revision,purpose,manifest_json,files_json,references_json,settings_json,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, project_id, 1, int(project["project_revision"]), purpose, _json(manifest), _json(files), _json(refs), _json(setting_view), request_id, stamp))
    for item in asset_manifest:
        visual_assets.add_reference(conn, project_id, item["version_id"], "build_snapshot", snapshot_id)
    return payload(conn.execute("SELECT * FROM build_snapshots WHERE id=?", (snapshot_id,)).fetchone())


def build_profile(conn=None, project_id=None):
    import os
    defaults = {key: os.getenv(key, default) for key, default in [('PWA_TEX_SANDBOX_IMAGE', ''),
        ('PWA_LATEX_ENGINE', 'pdflatex'), ('PWA_BIB_ENGINE', 'auto'), ('PWA_DOCKER_EXECUTABLE', 'docker'),
        ('PWA_LATEX_TIMEOUT_SECONDS', '60'), ('PWA_TEX_PIDS_LIMIT', '128'), ('PWA_TEX_MEMORY_MB', '2048'), ('PWA_TEX_CPU_LIMIT', '2'), ('PWA_TEX_DISK_MB','256')]} | {'source_map_format': 'synctex-1'}
    if conn is not None and project_id is not None:
        from paperpilot.services import build_environment
        return build_environment.effective(conn,project_id,defaults)
    return defaults


def snapshot_current(conn, project_id, snapshot):
    """Compare complete dependencies without creating another snapshot."""
    project = conn.execute('SELECT project_revision,domain_id FROM projects WHERE id=?', (project_id,)).fetchone()
    manifest = snapshot['manifest']
    reasons = []
    if not project or int(project['project_revision']) != snapshot['project_revision']:
        reasons.append('论文版本已变化')
    if project and 'project_domain_id' in manifest and str(project['domain_id'] or '') != manifest['project_domain_id']:
        reasons.append('论文领域归属已变化')
    versions = version_map(conn, project_id)
    files = [dict(row) for row in conn.execute('SELECT path,content FROM files WHERE project_id=? ORDER BY path', (project_id,))]
    current_files = [{'path': row['path'], 'version': versions.get(row['path'], 0), 'sha256': hashlib.sha256(row['content'].encode()).hexdigest()} for row in files]
    dependency_files = files
    if current_files != manifest.get('baseline_files', manifest.get('files')):
        reasons.append('工程文件或版本已变化')
    source = manifest.get('source', {'kind': 'saved'})
    if source['kind'] != 'saved':
        try:
            from paperpilot.services.build_sources import materialize
            for row in files:
                row['version'] = versions.get(row['path'], 0)
            materialized, current_source = materialize(conn, project_id, files, snapshot['purpose'], draft_source=source.get('draft_source', 'all'), candidate_id=source.get('candidate_id'))
            dependency_files = materialized
            if current_source != source or _manifest(materialized, [], snapshot['project_revision'], snapshot['purpose'], {})['files'] != manifest['files']:
                reasons.append('草稿或候选来源已变化')
        except ProjectServiceError as exc:
            reasons.append(exc.message)
    if settings.read(conn, project_id).get('effective', {}) != manifest.get('settings', {}):
        reasons.append('论文配置已变化')
    try:
        if visual_assets.capture_manifest(conn, project_id, dependency_files) != manifest.get('assets', []):
            reasons.append('受管图像依赖已变化')
    except ProjectServiceError:
        reasons.append('受管图像依赖不可用')
    if build_profile(conn,project_id) != manifest.get('build_profile'):
        reasons.append('编译工具配置已变化或旧快照未记录环境')
    return {'current': not reasons, 'stale_reasons': reasons}


def described_job(conn, project_id, row):
    result = job_payload(row)
    snapshot = get(conn, project_id, result['snapshot_id'])
    freshness = snapshot_current(conn, project_id, snapshot)
    result.update(freshness)
    result['snapshot_revision'] = snapshot['project_revision']
    result['source'] = snapshot['manifest'].get('source', {'kind': 'saved', 'entries': []})
    if result['status'] == 'queued':
        from paperpilot.services import build_queue
        priority = 0 if result.get('trigger_kind', 'explicit') == 'explicit' else 1
        result['queue_position'] = conn.execute("SELECT COUNT(*) FROM build_jobs WHERE project_id=? AND status='queued' AND (CASE WHEN trigger_kind='explicit' THEN 0 ELSE 1 END < ? OR (CASE WHEN trigger_kind='explicit' THEN 0 ELSE 1 END = ? AND queue_seq<=?))", (project_id, priority, priority, result.get('queue_seq', 0))).fetchone()[0]
        result['global_capacity_wait'] = conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0] >= build_queue.GLOBAL_BUILD_LIMIT
        from paperpilot.services import host_budget
        machine = host_budget.inventory()
        requested = host_budget.limits(snapshot['manifest'].get('build_profile'))
        result['host_resource_budget'] = machine
        result['global_capacity_wait'] = result['global_capacity_wait'] or machine['occupied'] >= machine['slot_limit'] or any(
            sum(c[k] for c in machine['claims'])+requested[k] > machine['limits'][k] for k in requested)
        if result['global_capacity_wait']:
            from paperpilot.services import tex_runtime
            blockers=[]
            for slot in conn.execute('SELECT s.owner_pid,s.owner_token,s.project_id,s.job_id,s.cleanup_pending,p.name,b.diagnostics_json FROM build_global_slots s JOIN projects p ON p.id=s.project_id JOIN build_jobs b ON b.id=s.job_id'):
                state=tex_runtime.owner_state(dict(slot))
                try:
                    pending=bool(slot['cleanup_pending']) or any(item.get('code')=='runtime_cleanup_pending' for item in json.loads(slot['diagnostics_json'] or '[]'))
                except (ValueError, TypeError, AttributeError):
                    pending=True
                if state != 'alive' or pending:
                    blockers.append({'project_id':slot['project_id'],'project_name':slot['name'],'job_id':slot['job_id'],
                                     'reason':'owner_unknown' if state=='unknown' and not slot['cleanup_pending'] else 'resource_cleanup_pending'})
            result['global_capacity_blockers']=blockers
            result['global_capacity_message']=('有旧构建的进程身份无法核实；请检查对应论文的构建历史和旧服务实例，系统不会强制释放资源。' if any(b['reason']=='owner_unknown' for b in blockers) else
                '旧构建资源正在对账；请检查 Docker 服务，对账成功后会自动继续。' if blockers else '本机编译/定位名额或CPU、内存、磁盘预算已满，释放后自动继续。')
    result['artifact_eligibility'] = 'not_available' if result['status'] != 'succeeded' else (
        'stale' if not freshness['current'] else 'current' if result['purpose'] in {'submitted', 'release'} else 'purpose_preview')
    return result


def payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["manifest"] = json.loads(item.pop("manifest_json", "{}") or "{}")
    item["files"] = json.loads(item.pop("files_json", "[]") or "[]")
    item["references"] = json.loads(item.pop("references_json", "[]") or "[]")
    item["settings"] = json.loads(item.pop("settings_json", "{}") or "{}")
    return item


def get(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM build_snapshots WHERE id=? AND project_id=?", (snapshot_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "build_snapshot_missing", "构建快照不存在。")
    return payload(row)


def list_snapshots(conn: sqlite3.Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return [payload(row) for row in conn.execute("SELECT * FROM build_snapshots WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, min(max(int(limit), 1), 100)))]


def job_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["diagnostics"] = json.loads(item.pop("diagnostics_json", "[]") or "[]")
    for internal in ('checks_json', 'request_fingerprint', 'owner_pid', 'owner_token'):
        item.pop(internal, None)
    return item


def create_job(conn: sqlite3.Connection, project_id: str, snapshot_id: str, request_id: str, purpose: str, **options) -> dict[str, Any]:
    from paperpilot.services.build_queue import enqueue
    return enqueue(conn, project_id, snapshot_id, request_id, purpose, **options)


def get_job(conn: sqlite3.Connection, project_id: str, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM build_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "build_job_missing", "构建任务不存在。")
    return described_job(conn, project_id, row)


def list_jobs(conn: sqlite3.Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return [described_job(conn, project_id, row) for row in conn.execute("SELECT * FROM build_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, min(max(int(limit), 1), 100)))]


def reconcile_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    stale_after_seconds: int = 300,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    """Mark jobs left behind by a worker/process restart as interrupted.

    Compilation is currently run in-process, so a process restart can leave a
    queued/running row without a worker to finish it.  Reconciliation is
    explicit and idempotent: it never touches completed artifacts and never
    changes a job that was updated inside the grace period.
    """
    from datetime import datetime, timedelta, timezone

    try:
        grace = max(0, min(int(stale_after_seconds), 7 * 24 * 60 * 60))
    except (TypeError, ValueError):
        grace = 300
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace)
    rows = conn.execute(
        "SELECT * FROM build_jobs WHERE project_id=? AND status IN ('queued','running') ORDER BY created_at",
        (project_id,),
    ).fetchall()
    reconciled: list[dict[str, Any]] = []
    stamp = _now()
    for row in rows:
        from paperpilot.services import tex_runtime
        if tex_runtime.owner_state(dict(row)) != 'dead':
            continue
        updated_at = str(row["updated_at"] or row["created_at"] or "")
        try:
            old_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if old_time > cutoff:
                continue
        except ValueError:
            # An unreadable timestamp is safer to surface than to keep forever
            # in a running state after a restart.
            pass
        diagnostic = {
            "code": "worker_restart",
            "message": "构建任务在进程重启后没有可继续的工作进程；可按原快照重新提交。",
            "previous_status": row["status"],
        }
        if storage_root is not None:
            project_dir = Path(storage_root).resolve() / project_id
            if project_dir.resolve().parent != Path(storage_root).resolve():
                raise ProjectServiceError(503, 'invalid_build_storage', '构建存储身份不正确，未清理其他目录。')
            recovery = tex_runtime.recover(project_dir, job_ids={row['id']})
            diagnostic['runtime_cleanup'] = recovery
            if any(not item.get('cleanup_verified') for item in recovery):
                diagnostic['code'] = 'runtime_cleanup_pending'
                diagnostic['message'] = '构建已中断，容器清理尚未核实。请恢复Docker服务并点击“对账与恢复”；不会自动重跑，旧PDF保留。'
        updated = conn.execute(
            "UPDATE build_jobs SET status='interrupted',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
            (_json([diagnostic]), stamp, stamp, row["id"]),
        )
        if updated.rowcount == 1:
            reconciled.append({"id": row["id"], "previous_status": row["status"], "status": "interrupted", "diagnostics": [diagnostic]})
    return {"project_id": project_id, "reconciled": reconciled, "count": len(reconciled), "stale_after_seconds": grace}


def recover_startup(conn, storage_root):
    """An actual service start reconciles dead owners without a five-minute wait."""
    projects = [r[0] for r in conn.execute("SELECT DISTINCT project_id FROM build_jobs WHERE status IN ('queued','running','interrupted','cancelled')")]
    results = []
    for pid in projects:
        project_dir = Path(storage_root).resolve() / pid
        if project_dir.resolve().parent != Path(storage_root).resolve():
            continue
        # Retry unverified cleanup from an earlier unavailable Docker daemon.
        recover_project_runtime(conn, pid, storage_root)
        results.append(reconcile_jobs(conn, pid, stale_after_seconds=0, storage_root=storage_root))
    return results


def record_runtime_cleanup(conn, project_id, results):
    for result in results:
        job_id = result.get('job_id')
        if not job_id or result['status'] == 'active_owner':
            continue
        row = conn.execute('SELECT diagnostics_json FROM build_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
        if row is None:
            continue
        diagnostics = [item for item in json.loads(row['diagnostics_json']) if item.get('code') != 'runtime_cleanup_pending']
        if not result.get('cleanup_verified'):
            diagnostics.append({'code': 'runtime_cleanup_pending', 'message': '任务已停止接收结果，但编译容器清理尚未核实；请恢复Docker服务后点击对账与恢复。旧PDF保留，不会自动重跑。', 'runtime_cleanup': [result]})
        conn.execute('UPDATE build_jobs SET diagnostics_json=? WHERE id=? AND project_id=?', (_json(diagnostics), job_id, project_id))


def recover_project_runtime(conn, project_id, storage_root, *, job_ids=None, force=False, build_only=False):
    from paperpilot.services.tex_runtime import recover
    project_dir = Path(storage_root).resolve() / project_id
    if project_dir.resolve().parent != Path(storage_root).resolve():
        raise ProjectServiceError(503, 'invalid_build_storage', '构建存储身份不正确，未清理其他目录。')
    results = recover(project_dir, job_ids=job_ids, force=force, build_only=build_only)
    record_runtime_cleanup(conn, project_id, results)
    return results


def cancel_job(conn: sqlite3.Connection, project_id: str, job_id: str, request_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM build_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "build_job_missing", "构建任务不存在。")
    if row["status"] in {"succeeded", "failed", "cancelled", "interrupted", "superseded"}:
        if row["status"] == "cancelled":
            return job_payload(row) | {"idempotent_replay": True}
        raise ProjectServiceError(409, "build_job_closed", "构建任务已经结束，不能取消。")
    stamp = _now()
    conn.execute("UPDATE build_jobs SET status='cancelled',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=?", (_json([{"code": "cancelled", "request_id": request_id}]), stamp, stamp, job_id))
    return job_payload(conn.execute("SELECT * FROM build_jobs WHERE id=?", (job_id,)).fetchone())


def prepare_restore(conn: sqlite3.Connection, project_id: str, job_id: str, request_id: str, include_paths: list[str] | None = None, *, remove_paths=None, setting_keys=None, restore_build_environment=False, preview_fingerprint=None, storage_root=None) -> dict[str, Any]:
    """Load a historical build into the active REQ-001 revision workspace.

    The operation only creates revision drafts.  It never overwrites current
    files; the existing human-revision checkpoint/commit flow remains the sole
    owner of applying content.  Repeating the request returns the same draft
    set and therefore is safe for browser retries.
    """
    from paperpilot.services.build_restores import prepare
    return prepare(conn, project_id, job_id, request_id, include_paths, remove_paths=remove_paths, setting_keys=setting_keys, restore_build_environment=restore_build_environment, preview_fingerprint=preview_fingerprint, storage_root=storage_root)


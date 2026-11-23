from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from paperpilot.services.content_versions import current_version, save_content, delete_content
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import current_snapshot


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_mode(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM human_revision_modes WHERE project_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if not row:
        raise ProjectServiceError(409, "revision_mode_inactive", "请先进入人工修订模式。")
    return dict(row)


def enter_mode(conn: sqlite3.Connection, project_id: str, workflow_version: int) -> dict[str, Any]:
    snapshot = current_snapshot(conn, project_id)
    if snapshot["version"] != workflow_version:
        raise ProjectServiceError(409, "workflow_conflict", "工作流版本已变化；请刷新后再进入人工修订模式。")
    existing = conn.execute(
        "SELECT * FROM human_revision_modes WHERE project_id=? AND status='active'",
        (project_id,),
    ).fetchone()
    if existing:
        return dict(existing)
    # The revision record is kept for traceability only; enter from anywhere.
    stamp = timestamp()
    mode_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO human_revision_modes(id,project_id,workflow_version,state,entry_source,exit_target,mode_version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mode_id, project_id, workflow_version, "human_revision", snapshot["stage_status"], "", 1, "active", stamp, stamp),
    )
    return dict(conn.execute("SELECT * FROM human_revision_modes WHERE id=?", (mode_id,)).fetchone())


def save_draft(
    conn: sqlite3.Connection,
    project_id: str,
    target_kind: str,
    target_path: str,
    content: str,
    base_version: int,
    *, expected_draft_updated_at: str | None = None, conditional: bool = False,
    copy_id: str | None = None,
) -> dict[str, Any]:
    mode = active_mode(conn, project_id)
    path = "" if target_kind == "outline" else target_path
    actual = current_version(conn, project_id, target_kind, path)
    stamp = timestamp()
    old = conn.execute("SELECT updated_at FROM revision_drafts WHERE project_id=? AND target_kind=? AND target_path=?", (project_id, target_kind, path)).fetchone()
    if old and stamp <= old["updated_at"]:
        stamp = (datetime.fromisoformat(old["updated_at"]) + timedelta(microseconds=1)).isoformat()
    if copy_id:
        prior_copy = conn.execute("SELECT updated_at FROM revision_draft_copies WHERE project_id=? AND copy_id=?", (project_id, copy_id)).fetchone()
        if prior_copy and stamp <= prior_copy["updated_at"]:
            stamp = (datetime.fromisoformat(prior_copy["updated_at"]) + timedelta(microseconds=1)).isoformat()
        conn.execute("INSERT INTO revision_draft_copies(project_id,copy_id,target_kind,target_path,content,base_version,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,copy_id) DO UPDATE SET target_kind=excluded.target_kind,target_path=excluded.target_path,content=excluded.content,base_version=excluded.base_version,updated_at=excluded.updated_at", (project_id,copy_id,target_kind,path,content,base_version,stamp))
    if actual != base_version:
        if copy_id:
            return {"ok": False, "conflict": True, "copy_id": copy_id, "message": "正式内容版本已变化；当前输入已保留为恢复副本，请比较后处理。"}
        raise ProjectServiceError(409, "content_conflict", "草稿的基础内容已变化；请刷新并比较后恢复。")
    if conditional and (old["updated_at"] if old else None) != expected_draft_updated_at:
        message = "另一页面已更新草稿；当前输入已保留为恢复副本，请比较后处理。" if copy_id else "另一页面已更新草稿；请保留当前输入并比较后处理。"
        return {"ok": False, "conflict": True, "copy_id": copy_id, "message": message}
    conn.execute(
        "INSERT INTO revision_drafts(project_id,target_kind,target_path,content,base_version,mode_id,updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id,target_kind,target_path) DO UPDATE SET content=excluded.content,base_version=excluded.base_version,mode_id=excluded.mode_id,updated_at=excluded.updated_at",
        (project_id, target_kind, path, content, base_version, mode["id"], stamp),
    )
    record_event(
        conn, project_id, mode_id=mode["id"], action_type="manual_draft",
        scope={"kind": target_kind, "path": path}, base_file_versions={"__outline__" if target_kind == "outline" else path: base_version},
        status="draft_saved",
    )
    return {"ok": True, "base_version": base_version, "saved_at": stamp}


def commit_draft(
    conn: sqlite3.Connection,
    project_id: str,
    target_kind: str,
    target_path: str,
    base_version: int,
    expected_draft_updated_at: str | None = None,
) -> dict[str, Any]:
    mode = active_mode(conn, project_id)
    path = "" if target_kind == "outline" else target_path
    draft = conn.execute(
        "SELECT * FROM revision_drafts WHERE project_id=? AND target_kind=? AND target_path=?",
        (project_id, target_kind, path),
    ).fetchone()
    if draft is None:
        raise ProjectServiceError(404, "draft_missing", "没有可提交的人工修订草稿。")
    if expected_draft_updated_at is not None and draft["updated_at"] != expected_draft_updated_at:
        raise ProjectServiceError(409, "draft_changed", "草稿已被其他页面更新；当前输入仍保留，请比较后再提交。")
    if int(draft["base_version"]) != base_version:
        raise ProjectServiceError(409, "draft_conflict", "草稿基础版本已变化；请恢复草稿并比较后再提交。")
    saved = save_content(
        conn, project_id, target_kind, path, draft["content"],
        expected_version=base_version, source="human_checkpoint",
    )
    conn.execute(
        "DELETE FROM revision_drafts WHERE project_id=? AND target_kind=? AND target_path=?",
        (project_id, target_kind, path),
    )
    record_event(
        conn, project_id, mode_id=mode["id"], action_type="manual_checkpoint",
        scope={"kind": target_kind, "path": path},
        base_file_versions={"__outline__" if target_kind == "outline" else path: base_version},
        status="checkpointed",
    )
    return {"ok": True, **saved}


def commit_build_restore(conn, project_id, restore_id, request_id, candidate_fingerprint, *, storage_root=None):
    """REQ-001 is the sole owner of applying the reviewed, all-file bundle.

    The caller keeps this transaction open through enqueueing its rebuild and
    recording the commit, so a failure cannot leave a partially restored paper.
    """
    from paperpilot.services import build_restores
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    build_restores.writable_mode(conn, project_id)
    row = conn.execute('SELECT * FROM build_restore_events WHERE id=? AND project_id=?', (restore_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, 'build_restore_missing', '此论文内没有该恢复候选，请重新查看历史差异。')
    candidate = json.loads(row['result_json'])
    if candidate.get('candidate_fingerprint') != candidate_fingerprint:
        raise ProjectServiceError(409, 'build_restore_candidate_conflict', '确认内容与恢复候选不一致；请重新预览，不会覆盖现稿。')
    previous = candidate.get('commit')
    if previous:
        if previous['request_id'] != request_id:
            raise ProjectServiceError(409, 'build_restore_already_committed', '此恢复候选已提交；请查看已创建的新版本和编译任务，不会重复恢复。')
        return previous['result'] | {'idempotent_replay': True}
    for existing in conn.execute('SELECT id,result_json FROM build_restore_events WHERE project_id=?', (project_id,)):
        commit = json.loads(existing['result_json']).get('commit')
        if commit and commit['request_id'] == request_id:
            raise ProjectServiceError(409, 'build_restore_request_conflict', '此提交编号已用于另一恢复候选；请核对原记录。')
    if candidate.get('prepared_state_token') != build_restores.state_token(conn, project_id):
        raise ProjectServiceError(409, 'build_restore_candidate_stale', '准备后正文、草稿、配置或工作流已变化；请重新预览并准备候选，所有现稿均保留。')
    snapshot = build_restores.historical_snapshot(conn, project_id, candidate['job_id'])
    if not all(r['available'] for r in build_restores.resource_checks(conn, project_id, snapshot, storage_root)):
        raise ProjectServiceError(409, 'build_restore_resources_missing', '历史资源已缺失或损坏；请补回原资源后重新准备恢复，不会写入不完整工程。')
    environment=candidate.get('environment_to_restore')
    if bool(candidate.get('requested_environment',False)) != bool(environment):
        raise ProjectServiceError(409,'build_restore_candidate_conflict','恢复环境范围不一致；请重新预览。')
    if environment:
        from paperpilot.services import build_environment, build_snapshots
        review=build_environment.inspect_historical(snapshot['manifest'].get('build_profile'),build_snapshots.build_profile(conn,project_id))
        if not review['available'] or review.get('profile')!=environment:
            raise ProjectServiceError(409,'build_restore_environment_changed','本地历史镜像或编译环境已变化或不可用；请重新检查差异，现稿和配置均保留。')
    setting_changes=candidate.get('settings_to_restore', [])
    if setting_changes:
        keys=[item['key'] for item in setting_changes]
        if len(keys)!=len(set(keys)) or sorted(keys)!=candidate.get('requested_settings',[]):
            raise ProjectServiceError(409,'build_restore_candidate_conflict','恢复配置范围不一致；请重新预览。')
        view=build_restores.preview(conn,project_id,candidate['job_id'],candidate.get('requested_paths',[]),
                                   remove_paths=candidate.get('requested_removals',[]),setting_keys=keys,storage_root=storage_root)
        available={item['key']:item for item in view['configuration']['choices']}
        if any(not available[key]['available'] or available[key]['historical']!=item['value'] for key,item in ((item['key'],item) for item in setting_changes)):
            raise ProjectServiceError(409,'build_restore_settings_unavailable','历史配置或领域归属已变化、缺失或不一致；请重新查看差异，所有现稿保留。')
    for item in candidate.get('removals', []):
        path=item['path']
        current=conn.execute('SELECT content FROM files WHERE project_id=? AND path=?',(project_id,path)).fetchone()
        editor=conn.execute('SELECT content,base_content,base_version FROM editor_local_drafts WHERE project_id=? AND path=?',(project_id,path)).fetchone()
        if (path in {f['path'] for f in snapshot['files']} or not current or current_version(conn,project_id,'file',path)!=item['base_version']
                or build_restores.digest(current['content'])!=item['current_sha256']
                or (editor and (editor['content']!=current['content'] or editor['base_content']!=current['content'] or int(editor['base_version'])!=item['base_version']))
                or conn.execute("SELECT 1 FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?",(project_id,path)).fetchone()):
            raise ProjectServiceError(409,'build_restore_candidate_stale','待移除文件、版本或草稿已变化；请重新查看差异，当前内容均保留。')
    drafts = []
    for target in candidate['drafts']:
        draft = conn.execute("SELECT * FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?", (project_id, target['target_path'])).fetchone()
        if not draft or draft['mode_id'] != candidate['mode_id'] or int(draft['base_version']) != target['base_version'] or build_restores.digest(draft['content']) != target['content_sha256']:
            raise ProjectServiceError(409, 'build_restore_candidate_stale', '恢复草稿已变更，请重新比较；不会提交其他内容。')
        if current_version(conn, project_id, 'file', target['target_path']) != target['base_version']:
            raise ProjectServiceError(409, 'content_conflict', '正文版本已变化，请重新预览恢复差异。')
        drafts.append(dict(draft))
    versions = {}
    for draft in drafts:
        saved = save_content(conn, project_id, 'file', draft['target_path'], draft['content'], expected_version=int(draft['base_version']), source='build_restore')
        versions[draft['target_path']] = saved['version']
        editor_draft = conn.execute('SELECT content FROM editor_local_drafts WHERE project_id=? AND path=?', (project_id, draft['target_path'])).fetchone()
        if editor_draft and editor_draft['content'] == draft['content']:
            # Preserve identical visible input while rebasing it to the exact
            # version just accepted. A subsequent preview must not be blocked
            # by the former checkpoint version of those same bytes.
            from paperpilot.services.editor import save_draft as save_editor_draft
            save_editor_draft(conn, project_id, draft['target_path'], saved['version'], draft['content'], draft['content'])
        conn.execute("DELETE FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?", (project_id, draft['target_path']))
    removed={}
    for item in candidate.get('removals', []):
        removed[item['path']]=delete_content(conn,project_id,item['path'],item['base_version'])['version']
        conn.execute('DELETE FROM editor_local_drafts WHERE project_id=? AND path=?',(project_id,item['path']))
    from paperpilot.services import settings
    restored_settings={}
    for item in setting_changes:
        revision=conn.execute('SELECT project_revision FROM projects WHERE id=?',(project_id,)).fetchone()['project_revision']
        result=settings.update_override(conn,project_id,item['key'],item['value'],revision,'build-restore-'+uuid.uuid4().hex)
        restored_settings[item['key']]=result['effective'][item['key']]
    restored_environment=None
    if environment:
        from paperpilot.services import build_environment
        restored_environment=build_environment.apply(conn,project_id,environment,'build-restore-'+uuid.uuid4().hex)
    record_event(conn, project_id, mode_id=candidate['mode_id'], action_type='build_restore', scope={'restore_id': restore_id, 'snapshot_id': candidate['snapshot_id'], 'paths': list(versions),'removed_paths':list(removed),'setting_keys':list(restored_settings),'environment_restored':bool(restored_environment)},
        base_file_versions={**{d['target_path']: d['base_version'] for d in drafts},**{d['path']:d['base_version'] for d in candidate.get('removals',[])}}, status='checkpointed', request_id=request_id)
    return {'ok': True, 'restore_id': restore_id, 'status': 'restored', 'versions': versions, 'removed_versions':removed, 'restored_settings':restored_settings, 'restored_environment':restored_environment, 'configuration_policy': candidate['configuration_policy']}


def resolve_pending_drafts(
    conn: sqlite3.Connection,
    project_id: str,
    draft_action: str | None,
    *,
    mode_id: str | None = None,
    expected_drafts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    query = "SELECT * FROM revision_drafts WHERE project_id=?"
    args: tuple[Any, ...] = (project_id,)
    if mode_id:
        query += " AND mode_id=?"
        args = (project_id, mode_id)
    drafts = conn.execute(query + " ORDER BY target_kind,target_path", args).fetchall()
    if expected_drafts is not None:
        actual = sorted((d["target_kind"], d["target_path"], d["updated_at"], d["base_version"], d["mode_id"], d["content"]) for d in drafts)
        expected = sorted((d["target_kind"], d["target_path"], d["updated_at"], d["base_version"], d["mode_id"], d["content"]) for d in expected_drafts)
        if actual != expected:
            raise ProjectServiceError(409, "draft_set_changed", "草稿已新增、删除或更新；未执行操作，请刷新草稿并重新选择。")
    if drafts and draft_action not in {"commit", "keep", "discard"}:
        raise ProjectServiceError(409, "draft_decision_required", "离开人工修订前请选择提交、保留为草稿或放弃未提交草稿。")
    if draft_action == "commit":
        for draft in drafts:
            if current_version(conn, project_id, draft["target_kind"], draft["target_path"]) != int(draft["base_version"]):
                raise ProjectServiceError(409, "draft_conflict", f"{draft['target_path'] or '大纲'} 的草稿基础版本已过期；请比较后再提交。")
        for draft in drafts:
            version_key = "__outline__" if draft["target_kind"] == "outline" else draft["target_path"]
            save_content(
                conn, project_id, draft["target_kind"], draft["target_path"], draft["content"],
                expected_version=int(draft["base_version"]), source="human_checkpoint",
            )
            record_event(
                conn, project_id, mode_id=draft["mode_id"], action_type="manual_checkpoint",
                scope={"kind": draft["target_kind"], "path": draft["target_path"]},
                base_file_versions={version_key: int(draft["base_version"])}, status="checkpointed",
            )
        conn.execute("DELETE FROM revision_drafts WHERE project_id=?", (project_id,))
    elif draft_action == "discard":
        if mode_id:
            conn.execute("DELETE FROM revision_drafts WHERE project_id=? AND mode_id=?", (project_id, mode_id))
        else:
            conn.execute("DELETE FROM revision_drafts WHERE project_id=?", (project_id,))
    return {"draft_action": draft_action or "none", "draft_count": len(drafts)}


def exit_mode(conn: sqlite3.Connection, project_id: str, draft_action: str | None, expected_drafts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    mode = active_mode(conn, project_id)
    resolved = resolve_pending_drafts(conn, project_id, draft_action, expected_drafts=expected_drafts)
    stamp = timestamp()
    running_jobs = conn.execute(
        "SELECT request_id FROM assist_action_events WHERE project_id=? AND mode_id=? AND status='running'",
        (project_id, mode["id"]),
    ).fetchall()
    for job in running_jobs:
        conn.execute(
            "UPDATE ai_jobs SET status='cancel_requested',cancel_requested=1,updated_at=? WHERE project_id=? AND request_id=? AND status='running'",
            (stamp, project_id, job["request_id"]),
        )
    conn.execute(
        "UPDATE human_revision_modes SET status='closed',exit_target=?,updated_at=? WHERE id=? AND status='active'",
        (draft_action or "no_pending_drafts", stamp, mode["id"]),
    )
    return {"ok": True, "mode_id": mode["id"], "status": "closed", **resolved}


def record_event(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    mode_id: str,
    action_type: str,
    scope: dict[str, Any],
    base_file_versions: dict[str, int],
    status: str,
    request_id: str = "",
    context_snapshot_id: str | None = None,
    candidate_id: str | None = None,
) -> str:
    event_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO assist_action_events(id,project_id,mode_id,action_type,scope_json,base_file_versions_json,context_snapshot_id,candidate_id,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, project_id, mode_id, action_type,
            json.dumps(scope, ensure_ascii=False),
            json.dumps(base_file_versions, ensure_ascii=False),
            context_snapshot_id, candidate_id, status, request_id or None, timestamp(),
        ),
    )
    return event_id


def update_event_status(
    conn: sqlite3.Connection,
    project_id: str,
    request_id: str,
    status: str,
    candidate_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE assist_action_events SET status=?,candidate_id=COALESCE(?,candidate_id) WHERE project_id=? AND request_id=?",
        (status, candidate_id, project_id, request_id),
    )
    if status == "rejected":
        from paperpilot.services.style_learning import best_effort_capture, capture_rejection
        row = conn.execute("SELECT candidate_id FROM assist_action_events WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        best_effort_capture(conn, project_id, capture_rejection, candidate_id or (row["candidate_id"] if row else None))

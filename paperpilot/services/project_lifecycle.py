from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_release_check(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    compile_result: dict[str, Any],
    citation_result: dict[str, Any],
    logic_result: dict[str, Any],
    expected_revision: int | None = None,
) -> dict[str, Any]:
    project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文项目不存在。")
    compile_ok = bool(compile_result.get("ok"))
    missing = list(citation_result.get("missing") or [])
    result = {
        "compile": compile_result,
        "citations": citation_result,
        "logic_gaps": logic_result.get("gaps", []),
        "logic_conflicts": logic_result.get("conflicts", []),
    }
    checked_revision = int(project["project_revision"]) if expected_revision is None else int(expected_revision)
    current = checked_revision == int(project["project_revision"])
    status = "passed" if current and compile_ok and not missing else ("stale" if not current else "blocked")
    check_id = uuid.uuid4().hex
    stamp = _now()
    conn.execute(
        "INSERT INTO project_release_checks(id,project_id,project_revision,compile_ok,missing_citations_json,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (check_id, project_id, checked_revision, int(compile_ok), json.dumps(missing, ensure_ascii=False), json.dumps(result, ensure_ascii=False), stamp),
    )
    return {
        "id": check_id,
        "project_id": project_id,
        "project_revision": checked_revision,
        "current": current,
        "compile_ok": compile_ok,
        "missing_citations": missing,
        "logic_gaps": result["logic_gaps"],
        "logic_conflicts": result["logic_conflicts"],
        "status": status,
        "created_at": stamp,
    }


def latest_release_check(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM project_release_checks WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    result = json.loads(row["result_json"] or "{}")
    missing = json.loads(row["missing_citations_json"] or "[]")
    current_revision = int(project["project_revision"]) if project else -1
    is_current = int(row["project_revision"]) == current_revision
    from paperpilot.services import build_snapshots
    compile_meta = result.get('compile', {})
    build = conn.execute("SELECT * FROM build_jobs WHERE id=? AND project_id=?", (compile_meta.get('build_job_id', ''), project_id)).fetchone()
    verified_compile = False
    if build and build['purpose'] in {'submitted', 'release'}:
        snapshot = build_snapshots.get(conn, project_id, build['snapshot_id'])
        is_current = is_current and build_snapshots.snapshot_current(conn, project_id, snapshot)['current']
        verified_compile = build['status'] == 'succeeded' and bool(build['artifact_sha256']) and bool(build['promoted'])
        if verified_compile:
            try:
                digest = hashlib.sha256()
                with Path(build['artifact_path']).open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                verified_compile = digest.hexdigest() == build['artifact_sha256']
            except OSError:
                verified_compile = False
    else:
        is_current = False
    return {
        "id": row["id"],
        "project_revision": int(row["project_revision"]),
        "current_project_revision": current_revision,
        "current": is_current,
        "compile_ok": bool(row["compile_ok"]) and verified_compile,
        "missing_citations": missing,
        "logic_gaps": result.get("logic_gaps", []),
        "logic_conflicts": result.get("logic_conflicts", []),
        "status": "stale" if not is_current else "passed" if bool(row["compile_ok"]) and verified_compile and not missing else "blocked",
        "created_at": row["created_at"],
        "result": result,
    }


def apply_lifecycle_command(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    command: str,
    request_id: str,
    base_version: int,
    continue_reason: str = "",
) -> dict[str, Any]:
    project = conn.execute("SELECT lifecycle_state,project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文项目不存在。")
    existing = conn.execute(
        "SELECT command,from_state,to_state,project_revision,result_json FROM project_status_events WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if existing:
        if existing["command"] != command or int(existing["project_revision"]) != base_version:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于另一项项目状态操作。")
        previous_result = json.loads(existing["result_json"] or "{}")
        expected_reason = continue_reason.strip() if command == "publish" else ""
        if previous_result.get("continue_reason", "") != expected_reason:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的发布处置理由。")
        return previous_result | {"idempotent_replay": True}

    current = str(project["lifecycle_state"] or "draft")
    expected = int(project["project_revision"])
    if base_version != expected:
        raise ProjectServiceError(409, "project_version_conflict", "论文项目内容已变化；请刷新状态和检查结果后重试。")
    transitions = {
        ("draft", "submit_review"): "in_review",
        ("in_review", "return_to_draft"): "draft",
        ("in_review", "publish"): "published",
        ("published", "withdraw_publication"): "draft",
    }
    target = transitions.get((current, command))
    if target is None:
        raise ProjectServiceError(409, "invalid_lifecycle_transition", f"项目当前状态 {current} 不允许执行 {command}。")

    release = None
    if command == "publish":
        release = latest_release_check(conn, project_id)
        if not release or not release["current"]:
            raise ProjectServiceError(409, "release_check_required", "发布前需要对当前已保存版本重新编译并检查引用。")
        if not release["compile_ok"]:
            raise ProjectServiceError(409, "compile_required", "最近一次编译未通过，不能发布。")
        if release["missing_citations"]:
            raise ProjectServiceError(409, "unresolved_citations", "仍有未解决引用，不能发布。")
        has_logic_warnings = bool(release["logic_gaps"] or release["logic_conflicts"])
        if has_logic_warnings and not continue_reason.strip():
            raise ProjectServiceError(409, "logic_review_required", "存在逻辑缺口或冲突；查看清单并填写继续发布理由后重试。")

    result = {
        "ok": True,
        "project_id": project_id,
        "command": command,
        "from_state": current,
        "lifecycle_state": target,
        "project_revision": expected,
        "release_check_id": release["id"] if release and command == "publish" else None,
        "continue_reason": continue_reason.strip() if command == "publish" else "",
    }
    stamp = _now()
    conn.execute("UPDATE projects SET lifecycle_state=? WHERE id=?", (target, project_id))
    conn.execute(
        "INSERT INTO project_status_events(id,project_id,request_id,command,from_state,to_state,project_revision,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, request_id, command, current, target, expected, json.dumps(result, ensure_ascii=False), stamp),
    )
    return result

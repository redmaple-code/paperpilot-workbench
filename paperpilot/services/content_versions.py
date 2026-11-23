from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlite3

from paperpilot.services.project_errors import ProjectServiceError


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_target(kind: str, path: str) -> tuple[str, str]:
    if kind not in {"file", "outline"}:
        raise ProjectServiceError(422, "invalid_target", "版本目标必须是 file 或 outline。")
    if kind == "outline":
        return kind, ""
    normalized = (path or "").replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith(".") or any(part in {".", ".."} for part in parts):
        raise ProjectServiceError(400, "invalid_path", "非法工程文件路径。")
    return kind, normalized


def seed_initial_versions(conn: sqlite3.Connection, project_id: str, files: list[tuple[str, str]], outline: str, stamp: str) -> None:
    for path, content in files:
        conn.execute(
            "INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, project_id, "file", path, 1, None, None, "workspace_create", content, stamp),
        )
    conn.execute(
        "INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, "outline", "", 1, None, None, "workspace_create", outline, stamp),
    )


def current_version(conn: sqlite3.Connection, project_id: str, kind: str, path: str = "") -> int:
    kind, path = _validate_target(kind, path)
    row = conn.execute(
        "SELECT MAX(version) AS version FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=?",
        (project_id, kind, path),
    ).fetchone()
    if row and row["version"] is not None:
        return int(row["version"])
    return 0


def preserve_legacy_baseline(conn: sqlite3.Connection, project_id: str, kind: str, path: str = "") -> int:
    """Record an existing unversioned value before an explicit versioned edit."""
    kind, path = _validate_target(kind, path)
    version = current_version(conn, project_id, kind, path)
    if version:
        return version
    try:
        content = _current_content(conn, project_id, kind, path)
    except ProjectServiceError as exc:
        if exc.code == "content_missing":
            return 0
        raise
    conn.execute(
        "INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, kind, path, 1, None, None, "legacy_manuscript_baseline", content, timestamp()),
    )
    return 1


def version_map(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT target_kind,target_path,MAX(version) AS version FROM content_versions WHERE project_id=? GROUP BY target_kind,target_path",
        (project_id,),
    ).fetchall()
    result = {row["target_path"]: int(row["version"]) for row in rows if row["target_kind"] == "file"}
    outline = next((int(row["version"]) for row in rows if row["target_kind"] == "outline"), 0)
    result["__outline__"] = outline
    return result


def _current_content(conn: sqlite3.Connection, project_id: str, kind: str, path: str) -> str:
    if kind == "outline":
        row = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    else:
        row = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, path)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "content_missing", "待修改的文件或大纲不存在。")
    return str(row["content"])


def _mark_stale_candidates(conn: sqlite3.Connection, project_id: str) -> None:
    stamp = timestamp()
    candidates = conn.execute(
        "SELECT id,job_id,base_file_versions_json FROM draft_candidates WHERE project_id=? AND status='candidate_ready'",
        (project_id,),
    ).fetchall()
    for candidate in candidates:
        bases = json.loads(candidate["base_file_versions_json"] or "{}")
        job = conn.execute("SELECT request_id FROM ai_jobs WHERE id=?", (candidate["job_id"],)).fetchone()
        if not job:
            continue
        stale = False
        for key, expected in bases.items():
            kind, path = ("outline", "") if key == "__outline__" else ("file", key)
            if current_version(conn, project_id, kind, path) != int(expected):
                stale = True
                break
        if stale:
            conn.execute(
                "UPDATE draft_candidates SET status='stale',decided_at=? WHERE id=? AND status='candidate_ready'",
                (stamp, candidate["id"]),
            )
            conn.execute(
                "UPDATE assist_action_events SET status='stale' WHERE project_id=? AND request_id=?",
                (project_id, job["request_id"]),
            )


def save_content(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    path: str,
    content: str,
    *,
    expected_version: int | None = None,
    source: str = "manual_edit",
    restored_from_version: int | None = None,
) -> dict[str, Any]:
    kind, path = _validate_target(kind, path)
    latest = current_version(conn, project_id, kind, path)
    if expected_version is not None and latest != expected_version:
        raise ProjectServiceError(409, "content_conflict", "内容版本已变化；请刷新并比较后再保存。")
    try:
        previous_content = _current_content(conn, project_id, kind, path)
    except ProjectServiceError:
        prior = conn.execute(
            "SELECT source FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?",
            (project_id, kind, path, latest),
        ).fetchone()
        if latest > 0 and (prior is None or prior["source"] != "file_delete"):
            raise
        previous_content = None
    if previous_content == content and latest > 0:
        return {"version": latest, "unchanged": True}
    next_version = latest + 1
    stamp = timestamp()
    conn.execute(
        "INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, kind, path, next_version, latest or None, restored_from_version, source, content, stamp),
    )
    if kind == "outline":
        conn.execute("INSERT INTO outline(project_id,content) VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET content=excluded.content", (project_id, content))
    else:
        conn.execute("INSERT INTO files(project_id,path,content,updated_at) VALUES(?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at", (project_id, path, content, stamp))
    conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    _mark_stale_candidates(conn, project_id)
    if kind == "file":
        from paperpilot.services.style_learning import best_effort_capture, capture_edit
        best_effort_capture(conn, project_id, capture_edit, path, previous_content, content, next_version, source)
    return {"version": next_version, "unchanged": False}


def delete_content(conn: sqlite3.Connection, project_id: str, path: str, expected_version: int) -> dict[str, Any]:
    kind, path = _validate_target("file", path)
    latest = current_version(conn, project_id, kind, path)
    if latest != expected_version:
        raise ProjectServiceError(409, "content_conflict", "内容版本已变化；请刷新并比较后再删除。")
    current = _current_content(conn, project_id, kind, path)
    stamp = timestamp()
    next_version = latest + 1
    conn.execute(
        "INSERT INTO content_versions(id,project_id,target_kind,target_path,version,parent_version,restored_from_version,source,content,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, kind, path, next_version, latest, None, "file_delete", current, stamp),
    )
    conn.execute("DELETE FROM files WHERE project_id=? AND path=?", (project_id, path))
    conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    _mark_stale_candidates(conn, project_id)
    return {"version": next_version, "deleted": True}


def restore_content(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    path: str,
    target_version: int,
    expected_version: int,
    source: str,
) -> dict[str, Any]:
    kind, path = _validate_target(kind, path)
    row = conn.execute(
        "SELECT content FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?",
        (project_id, kind, path, target_version),
    ).fetchone()
    if not row:
        raise ProjectServiceError(404, "version_missing", "找不到指定内容版本。")
    return save_content(
        conn, project_id, kind, path, row["content"], expected_version=expected_version,
        source=source, restored_from_version=target_version,
    )


def apply_bundle(
    conn: sqlite3.Connection,
    project_id: str,
    patches: list[dict[str, str]],
    expected_versions: dict[str, int],
) -> dict[str, int]:
    normalized: list[tuple[str, str, str]] = []
    for patch in patches:
        kind, path = _validate_target(patch["kind"], patch.get("path", ""))
        normalized.append((kind, path, patch["content"]))
    keys = [(kind, path) for kind, path, _ in normalized]
    if len(keys) != len(set(keys)):
        raise ProjectServiceError(422, "duplicate_target", "候选包含重复文件目标。")
    for kind, path, _ in normalized:
        key = "__outline__" if kind == "outline" else path
        expected = expected_versions.get(key)
        actual = current_version(conn, project_id, kind, path)
        if expected is None or actual != expected:
            raise ProjectServiceError(409, "content_conflict", f"{key} 的基础版本已变化；候选未应用。")
    result: dict[str, int] = {}
    for kind, path, content in normalized:
        key = "__outline__" if kind == "outline" else path
        saved = save_content(
            conn, project_id, kind, path, content,
            expected_version=expected_versions[key], source="candidate_apply",
        )
        result[key] = saved["version"]
    return result

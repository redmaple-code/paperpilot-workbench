from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from paperpilot.services import conversion
from paperpilot.services.content_versions import current_version, save_content
from paperpilot.services.project_errors import ProjectServiceError


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save(
    conn: sqlite3.Connection,
    project_id: str,
    path: str,
    content: str,
    base_version: int | None,
    request_id: str | None,
) -> dict[str, Any]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ProjectServiceError(422, "request_id_required", "编辑器保存必须携带 request_id。")
    digest = _hash(content)
    previous = conn.execute(
        "SELECT operation,target_path,base_version,content_sha256,result_json FROM editor_write_events "
        "WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if previous:
        if (
            previous["operation"] != "save"
            or previous["target_path"] != path
            or int(previous["base_version"]) != int(base_version or 0)
            or previous["content_sha256"] != digest
        ):
            raise ProjectServiceError(409, "editor_request_id_conflict", "该 request_id 已用于不同的编辑器写入。")
        conn.execute(
            "DELETE FROM editor_local_drafts WHERE project_id=? AND path=? AND base_version=? AND content=?",
            (project_id, path, int(base_version or 0), content),
        )
        return json.loads(previous["result_json"]) | {"idempotent_replay": True}

    result = save_content(
        conn, project_id, "file", path, content,
        expected_version=base_version, source="editor_save",
    )
    response = {"ok": True, "path": path, **result}
    conn.execute(
        "INSERT INTO editor_write_events(id,project_id,request_id,operation,target_path,base_version,content_sha256,result_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, request_id, "save", path, int(base_version or 0), digest, _json(response), _now()),
    )
    conn.execute(
        "DELETE FROM editor_local_drafts WHERE project_id=? AND path=? AND base_version=? AND content=?",
        (project_id, path, int(base_version or 0), content),
    )
    return response


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def save_draft(
    conn: sqlite3.Connection, project_id: str, path: str, base_version: int,
    base_content: str, content: str,
) -> dict[str, Any]:
    stamp = _now()
    conn.execute(
        "INSERT INTO editor_local_drafts(project_id,path,base_version,base_content,content,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET "
        "base_version=excluded.base_version,base_content=excluded.base_content,content=excluded.content,updated_at=excluded.updated_at",
        (project_id, path, base_version, base_content, content, stamp),
    )
    return {"path": path, "base_version": base_version, "updated_at": stamp, "content_length": len(content)}


def get_draft(conn: sqlite3.Connection, project_id: str, path: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM editor_local_drafts WHERE project_id=? AND path=?",
        (project_id, path),
    ).fetchone()
    if row is None:
        return None
    current = current_version(conn, project_id, "file", path)
    return {
        "path": path, "base_version": int(row["base_version"]),
        "base_content": row["base_content"], "content": row["content"],
        "updated_at": row["updated_at"], "stale": current != int(row["base_version"]),
        "current_version": current,
    }


def discard_draft(conn: sqlite3.Connection, project_id: str, path: str) -> dict[str, Any]:
    conn.execute("DELETE FROM editor_local_drafts WHERE project_id=? AND path=?", (project_id, path))
    return {"ok": True, "path": path}


def preview_conversion(
    conn: sqlite3.Connection, project_id: str, *, request_id: str, source_path: str,
    source_version: int, target_path: str, direction: str,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM editor_conversion_previews WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if existing:
        if existing["source_path"] != source_path or existing["target_path"] != target_path or existing["direction"] != direction:
            raise ProjectServiceError(409, "editor_request_id_conflict", "该 request_id 已用于不同的转换预览。")
        return conversion_payload(existing) | {"idempotent_replay": True}
    source = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, source_path)).fetchone()
    if source is None:
        raise ProjectServiceError(404, "editor_source_missing", "转换源文件不存在。")
    actual_source_version = current_version(conn, project_id, "file", source_path)
    if actual_source_version != source_version:
        raise ProjectServiceError(409, "editor_conversion_stale", "转换源文件已变化，请刷新后重新预览。")
    target = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, target_path)).fetchone()
    target_content = str(target["content"]) if target else ""
    target_version = current_version(conn, project_id, "file", target_path)
    source_content = str(source["content"])
    converted = (
        conversion.markdown_to_latex(source_content)
        if direction == "markdown_to_latex"
        else conversion.latex_to_markdown(source_content)
    )
    preview_id = uuid.uuid4().hex
    stamp = _now()
    conn.execute(
        "INSERT INTO editor_conversion_previews(id,project_id,request_id,source_path,source_version,source_sha256,direction,target_path,target_version,target_content,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'preview',?,?)",
        (preview_id, project_id, request_id, source_path, source_version, _hash(source_content), direction,
         target_path, target_version, converted, stamp, stamp),
    )
    return {
        "id": preview_id, "request_id": request_id, "source_path": source_path,
        "source_version": source_version, "target_path": target_path,
        "target_version": target_version, "direction": direction,
        "source_content": source_content, "target_before": target_content,
        "target_content": converted, "status": "preview", "idempotent_replay": False,
    }


def conversion_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    source = item["source_path"]
    target = item["target_path"]
    return {
        "id": item["id"], "request_id": item["request_id"],
        "source_path": source, "source_version": int(item["source_version"]),
        "target_path": target, "target_version": int(item["target_version"]),
        "direction": item["direction"], "target_content": item["target_content"],
        "status": item["status"], "result": json.loads(item.get("result_json") or "{}"),
    }


def apply_conversion(
    conn: sqlite3.Connection, project_id: str, preview_id: str, request_id: str,
) -> dict[str, Any]:
    preview = conn.execute(
        "SELECT * FROM editor_conversion_previews WHERE project_id=? AND id=?",
        (project_id, preview_id),
    ).fetchone()
    if preview is None:
        raise ProjectServiceError(404, "editor_conversion_missing", "转换预览不存在。")
    if preview["status"] == "applied":
        if preview["apply_request_id"] != request_id:
            raise ProjectServiceError(409, "editor_conversion_closed", "该转换预览已应用。")
        return json.loads(preview["result_json"]) | {"idempotent_replay": True}
    source = conn.execute(
        "SELECT content FROM files WHERE project_id=? AND path=?",
        (project_id, preview["source_path"]),
    ).fetchone()
    if source is None or current_version(conn, project_id, "file", preview["source_path"]) != int(preview["source_version"]) or _hash(str(source["content"])) != preview["source_sha256"]:
        raise ProjectServiceError(409, "editor_conversion_stale", "转换源文件已变化，请重新预览。")
    if current_version(conn, project_id, "file", preview["target_path"]) != int(preview["target_version"]):
        raise ProjectServiceError(409, "editor_conversion_target_stale", "目标文件已变化，请重新预览并比较。")
    saved = save(
        conn, project_id, preview["target_path"], preview["target_content"],
        int(preview["target_version"]), request_id,
    )
    response = {"ok": True, "preview_id": preview_id, "path": preview["target_path"], **saved}
    conn.execute(
        "UPDATE editor_conversion_previews SET status='applied',apply_request_id=?,result_json=?,updated_at=? WHERE id=? AND status='preview'",
        (request_id, _json(response), _now(), preview_id),
    )
    return response

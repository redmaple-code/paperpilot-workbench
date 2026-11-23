from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_hash(operation: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(_dump({"operation": operation, "payload": payload}).encode("utf-8")).hexdigest()


def _request_begin(conn: sqlite3.Connection, project_id: str, request_id: str, operation: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    digest = _request_hash(operation, payload)
    row = conn.execute("SELECT operation,payload_hash,result_json FROM outline_command_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if row:
        if row["operation"] != operation or row["payload_hash"] != digest:
            raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已用于不同操作，请生成新编号后重试。")
        return digest, json.loads(row["result_json"])
    if (conn.execute("SELECT 1 FROM outline_versions WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        or conn.execute("SELECT 1 FROM outline_migration_drafts WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
        or conn.execute("SELECT 1 FROM outline_candidates WHERE project_id=? AND (request_id=? OR decision_request_id=?)", (project_id, request_id, request_id)).fetchone()):
        raise ProjectServiceError(409, "outline_request_id_conflict", "该历史请求无法安全重放，请生成新编号后重试。")
    return digest, None


def _request_finish(conn: sqlite3.Connection, project_id: str, request_id: str, operation: str, digest: str, result: dict[str, Any]) -> dict[str, Any]:
    conn.execute("INSERT INTO outline_command_requests(project_id,request_id,operation,payload_hash,result_json,created_at) VALUES(?,?,?,?,?,?)", (project_id, request_id, operation, digest, _dump(result), _now()))
    return result


def _reject_managed_request_id(conn: sqlite3.Connection, project_id: str, request_id: str) -> None:
    if conn.execute("SELECT 1 FROM outline_command_requests WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone():
        raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已用于其他操作，请生成新编号后重试。")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _validate_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            raise ProjectServiceError(422, "invalid_outline_node", "大纲节点必须是对象。")
        node_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or raw.get("label") or "").strip()
        if not node_id or not title:
            raise ProjectServiceError(422, "invalid_outline_node", "大纲节点必须包含稳定 ID 和标题。")
        if node_id in ids:
            raise ProjectServiceError(422, "duplicate_outline_node", f"节点 ID 重复：{node_id}")
        ids.add(node_id)
        normalized.append({
            "id": node_id,
            "parent_id": str(raw.get("parent_id") or "") or None,
            "position": int(raw.get("position", 0) or 0),
            "title": title,
            "purpose": str(raw.get("purpose") or ""),
            "scope": str(raw.get("scope") or ""),
            "status": str(raw.get("status") or "draft"),
            "source_anchor": str(raw.get("source_anchor") or ""),
            "source_version": int(raw.get("source_version", 0) or 0),
            "source_status": str(raw.get("source_status") or "active"),
            "links": copy.deepcopy(raw.get("links") or []),
            # Free canvas coordinates; absent means "auto-layout on read".
            **({"canvas_x": int(raw["canvas_x"])} if isinstance(raw.get("canvas_x"), (int, float)) else {}),
            **({"canvas_y": int(raw["canvas_y"])} if isinstance(raw.get("canvas_y"), (int, float)) else {}),
        })
    by_id = {node["id"]: node for node in normalized}
    for node in normalized:
        parent = node["parent_id"]
        if parent and parent not in by_id:
            raise ProjectServiceError(422, "outline_parent_missing", f"父节点不存在：{parent}")
        seen: set[str] = set()
        current = node["id"]
        while current:
            if current in seen:
                raise ProjectServiceError(422, "outline_cycle", "大纲层级不能形成循环。")
            seen.add(current)
            current = by_id[current]["parent_id"] if current in by_id else None
    return normalized


def current_version(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM outline_versions WHERE project_id=?", (project_id,)).fetchone()
    return int(row["version"] or 0)


def _resource_state(
    conn: sqlite3.Connection,
    project_id: str,
    resource_type: str,
    resource_id: str,
) -> dict[str, Any]:
    if resource_type == "file":
        row = conn.execute(
            "SELECT path FROM files WHERE project_id=? AND path=?",
            (project_id, resource_id),
        ).fetchone()
        if row:
            from paperpilot.services.content_versions import current_version as file_version
            return {"label": row["path"], "version": file_version(conn, project_id, "file", row["path"]), "status": "active"}
    elif resource_type == "term":
        row = conn.execute(
            "SELECT term,version,status FROM terms WHERE project_id=? AND id=?",
            (project_id, resource_id),
        ).fetchone()
        if row:
            return {
                "label": row["term"], "version": int(row["version"] or 1),
                "status": "active" if row["status"] == "active" else "inactive",
                "invalid_reason": "" if row["status"] == "active" else "resource_inactive",
            }
    elif resource_type == "corpus":
        row = conn.execute(
            "SELECT c.title,v.version,v.status,v.permission_state FROM corpus c "
            "LEFT JOIN corpus_source_versions v ON v.corpus_id=c.id "
            "WHERE c.project_id=? AND c.id=? ORDER BY v.version DESC LIMIT 1",
            (project_id, resource_id),
        ).fetchone()
        if row:
            active = row["status"] in (None, "active") and row["permission_state"] in (None, "authorized")
            return {
                "label": row["title"], "version": int(row["version"] or 1),
                "status": "active" if active else "inactive",
                "invalid_reason": "" if active else "source_unavailable",
            }
    elif resource_type == "reference":
        row = conn.execute(
            "SELECT l.cite_key,l.reference_version,l.link_status,r.current_version "
            "FROM project_reference_links l LEFT JOIN reference_records r ON r.id=l.reference_id "
            "WHERE l.project_id=? AND (l.reference_id=? OR l.cite_key=?) "
            "ORDER BY l.updated_at DESC LIMIT 1",
            (project_id, resource_id, resource_id),
        ).fetchone()
        if row:
            current = int(row["current_version"] or row["reference_version"] or 1)
            status = "active" if row["link_status"] in {"linked", "update_available"} else "inactive"
            if status == "active" and int(row["reference_version"] or 0) != current:
                status = "stale"
            return {
                "label": row["cite_key"], "version": int(row["reference_version"] or 0),
                "status": status,
                "invalid_reason": "" if status == "active" else (
                    "reference_version_outdated" if status == "stale" else "reference_unlinked"
                ),
            }
    elif resource_type == "logic_node":
        row = conn.execute(
            "SELECT label FROM logic_nodes WHERE project_id=? AND id=?",
            (project_id, resource_id),
        ).fetchone()
        if row:
            version_row = conn.execute(
                "SELECT MAX(version) AS version FROM logic_graph_versions WHERE project_id=?",
                (project_id,),
            ).fetchone()
            return {
                "label": row["label"], "version": int(version_row["version"] or 1),
                "status": "active",
            }
    else:
        raise ProjectServiceError(422, "outline_resource_type_invalid", "不支持的大纲资源类型。")
    return {
        "label": resource_id,
        "version": 0,
        "status": "missing",
        "invalid_reason": "resource_missing_or_outside_project",
    }


def list_linkable_resources(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for row in conn.execute("SELECT path FROM files WHERE project_id=? ORDER BY path", (project_id,)):
        state = _resource_state(conn, project_id, "file", row["path"])
        resources.append({"resource_type": "file", "resource_id": row["path"], **state})
    for row in conn.execute("SELECT id FROM terms WHERE project_id=? AND status='active' ORDER BY term", (project_id,)):
        state = _resource_state(conn, project_id, "term", row["id"])
        resources.append({"resource_type": "term", "resource_id": row["id"], **state})
    for row in conn.execute("SELECT id FROM corpus WHERE project_id=? ORDER BY title", (project_id,)):
        state = _resource_state(conn, project_id, "corpus", row["id"])
        if state["status"] == "active":
            resources.append({"resource_type": "corpus", "resource_id": row["id"], **state})
    for row in conn.execute(
        "SELECT reference_id FROM project_reference_links WHERE project_id=? "
        "AND link_status IN ('linked','update_available') ORDER BY cite_key",
        (project_id,),
    ):
        state = _resource_state(conn, project_id, "reference", row["reference_id"])
        if state["status"] == "active":
            resources.append({"resource_type": "reference", "resource_id": row["reference_id"], **state})
    for row in conn.execute("SELECT id FROM logic_nodes WHERE project_id=? ORDER BY label", (project_id,)):
        state = _resource_state(conn, project_id, "logic_node", row["id"])
        resources.append({"resource_type": "logic_node", "resource_id": row["id"], **state})
    return resources


def _project_links(conn: sqlite3.Connection, project_id: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for link in links:
        try:
            state = _resource_state(
                conn, project_id, str(link.get("resource_type") or ""),
                str(link.get("resource_id") or ""),
            )
        except ProjectServiceError:
            state = {
                "label": str(link.get("resource_id") or ""),
                "version": 0,
                "status": "missing",
                "invalid_reason": "resource_type_unsupported",
            }
        source_version = int(link.get("source_version") or 0)
        status = state["status"]
        reason = state.get("invalid_reason", "")
        if status == "active" and source_version and source_version != int(state["version"]):
            status = "stale"
            reason = "resource_version_changed"
        projected.append({**link, "label": state["label"], "current_version": state["version"], "status": status, "invalid_reason": reason})
    return projected


def read_outline(conn: sqlite3.Connection, project_id: str, version: int | None = None) -> dict[str, Any]:
    selected = current_version(conn, project_id) if version is None else int(version)
    row = conn.execute("SELECT * FROM outline_versions WHERE project_id=? AND version=?", (project_id, selected)).fetchone()
    if row is None:
        if version not in (None, 0):
            raise ProjectServiceError(404, "outline_version_missing", "找不到指定大纲版本。")
        legacy = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
        return {"version": 0, "source": "legacy_text", "nodes": [], "links": [], "legacy_text": legacy["content"] if legacy else ""}
    nodes = json.loads(row["nodes_json"] or "[]")
    links = _project_links(conn, project_id, json.loads(row["links_json"] or "[]"))
    for node in nodes:
        node["links"] = [link for link in links if link.get("node_id") == node["id"]]
    return {"version": int(row["version"]), "source": row["source"], "nodes": nodes, "links": links, "legacy_text": ""}


def list_versions(conn: sqlite3.Connection, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version,source,created_at FROM outline_versions "
        "WHERE project_id=? ORDER BY version DESC LIMIT ?",
        (project_id, min(max(int(limit), 1), 20)),
    ).fetchall()
    return [dict(row) for row in rows]


def restore_version(
    conn: sqlite3.Connection,
    project_id: str,
    source_version: int,
    base_version: int,
    request_id: str,
) -> dict[str, Any]:
    payload={"source_version":source_version,"base_version":base_version}
    digest,replay=_request_begin(conn,project_id,request_id,"history_restore",payload)
    if replay is not None:return replay
    source = conn.execute(
        "SELECT nodes_json,links_json FROM outline_versions WHERE project_id=? AND version=?",
        (project_id, source_version),
    ).fetchone()
    if source is None:
        raise ProjectServiceError(404, "outline_version_missing", "找不到指定大纲版本。")
    result = _write_version(
        conn,
        project_id,
        base_version,
        request_id,
        json.loads(source["nodes_json"] or "[]"),
        json.loads(source["links_json"] or "[]"),
        source="history_restore",
    )
    return _request_finish(conn,project_id,request_id,"history_restore",digest,result)


def _legacy_parse(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    unknown: list[str] = []
    stack: list[tuple[int, str]] = []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^(\d+(?:\.\d+)*)[.)]?\s+(.+)$", line)
        if not match:
            unknown.append(f"{line_no}: {line}")
            continue
        number, title = match.groups()
        depth = number.count(".")
        while len(stack) > depth:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        node_id = f"legacy-{number.replace('.', '-')}-{line_no}"
        node = {
            "id": node_id, "parent_id": parent_id, "position": len([n for n in nodes if n.get("parent_id") == parent_id]),
            "title": title.strip(), "purpose": "", "scope": "", "status": "draft",
            "source_anchor": f"line:{line_no}", "source_version": 0, "source_status": "active", "links": [],
        }
        nodes.append(node)
        stack.append((depth, node_id))
    return _validate_nodes(nodes), unknown


def preview_migration(conn: sqlite3.Connection, project_id: str, request_id: str) -> dict[str, Any]:
    _reject_managed_request_id(conn,project_id,request_id)
    replay = conn.execute("SELECT * FROM outline_migration_drafts WHERE request_id=?", (request_id,)).fetchone()
    if replay:
        if replay["project_id"] != project_id:
            raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已被使用，请生成新编号后重试。")
        return _draft_payload(replay, idempotent_replay=True)
    legacy = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    if legacy is None:
        raise ProjectServiceError(404, "outline_missing", "项目没有可迁移的大纲。")
    base_row = conn.execute("SELECT MAX(version) AS version FROM content_versions WHERE project_id=? AND target_kind='outline'", (project_id,)).fetchone()
    base_version = int(base_row["version"] or 0)
    nodes, unknown = _legacy_parse(legacy["content"])
    draft_id = uuid.uuid4().hex
    stamp = _now()
    conn.execute("INSERT INTO outline_migration_drafts(id,project_id,base_text_version,nodes_json,unknown_lines_json,status,request_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (draft_id, project_id, base_version, _dump(nodes), _dump(unknown), "preview", request_id, stamp, stamp))
    return {"id": draft_id, "status": "preview", "base_text_version": base_version, "nodes": nodes, "unknown_lines": unknown, "original_text": legacy["content"], "idempotent_replay": False}


def _draft_payload(row: sqlite3.Row, idempotent_replay: bool = False) -> dict[str, Any]:
    return {"id": row["id"], "status": row["status"], "base_text_version": int(row["base_text_version"]), "nodes": json.loads(row["nodes_json"] or "[]"), "unknown_lines": json.loads(row["unknown_lines_json"] or "[]"), "idempotent_replay": idempotent_replay}


def commit_migration(conn: sqlite3.Connection, project_id: str, draft_id: str, request_id: str) -> dict[str, Any]:
    _reject_managed_request_id(conn,project_id,request_id)
    draft = conn.execute("SELECT * FROM outline_migration_drafts WHERE id=? AND project_id=?", (draft_id, project_id)).fetchone()
    if draft is None:
        raise ProjectServiceError(404, "outline_migration_missing", "大纲迁移预览不存在。")
    if draft["status"] == "committed":
        return _draft_payload(draft, idempotent_replay=True)
    if conn.execute("SELECT 1 FROM outline_versions WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone():
        raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已用于其他操作，请生成新编号后重试。")
    current_text = conn.execute("SELECT content FROM outline WHERE project_id=?", (project_id,)).fetchone()
    if current_text is None:
        raise ProjectServiceError(404, "outline_missing", "项目没有大纲。")
    base_row = conn.execute("SELECT MAX(version) AS version FROM content_versions WHERE project_id=? AND target_kind='outline'", (project_id,)).fetchone()
    if int(base_row["version"] or 0) != int(draft["base_text_version"]):
        raise ProjectServiceError(409, "outline_migration_stale", "原文已变化，请重新生成迁移预览。")
    version = current_version(conn, project_id) + 1
    stamp = _now()
    nodes = json.loads(draft["nodes_json"] or "[]")
    conn.execute("INSERT INTO outline_versions(project_id,version,source,nodes_json,links_json,request_id,created_at) VALUES(?,?,?,?,?,?,?)", (project_id, version, "legacy_migration", _dump(nodes), "[]", request_id, stamp))
    conn.execute("UPDATE outline_migration_drafts SET status='committed',updated_at=? WHERE id=?", (stamp, draft_id))
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp, project_id))
    return {"id": draft_id, "status": "committed", "version": version, "nodes": nodes, "unknown_lines": json.loads(draft["unknown_lines_json"] or "[]"), "idempotent_replay": False}


def _write_version(conn: sqlite3.Connection, project_id: str, base_version: int, request_id: str, nodes: list[dict[str, Any]], links: list[dict[str, Any]], source: str = "node_command") -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM outline_versions WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if existing:
        raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已用于其他操作，请生成新编号后重试。")
    current = current_version(conn, project_id)
    if current != int(base_version):
        raise ProjectServiceError(409, "outline_conflict", "大纲版本已变化；当前操作已保留为本地草稿，请刷新后比较。")
    normalized = _validate_nodes(nodes)
    version = current + 1
    stamp = _now()
    conn.execute("INSERT INTO outline_versions(project_id,version,source,nodes_json,links_json,request_id,created_at) VALUES(?,?,?,?,?,?,?)", (project_id, version, source, _dump(normalized), _dump(links), request_id, stamp))
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp, project_id))
    return {"version": version, "source": source, "nodes": normalized, "links": links, "legacy_text": ""}


def node_command(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    digest,replay=_request_begin(conn,project_id,payload["request_id"],"node_command",payload)
    if replay is not None:return replay
    graph = read_outline(conn, project_id)
    nodes = copy.deepcopy(graph["nodes"])
    links = copy.deepcopy(graph.get("links") or [])
    command = payload["command"]
    node_id = str(payload.get("node_id") or payload.get("node", {}).get("id") or "").strip()
    if command == "create":
        node = copy.deepcopy(payload.get("node") or {})
        node.setdefault("id", node_id or uuid.uuid4().hex)
        node.setdefault("position", len(nodes))
        nodes.append(node)
    elif command in {"update", "move", "delete"}:
        target = next((item for item in nodes if item["id"] == node_id), None)
        if target is None:
            raise ProjectServiceError(404, "outline_node_missing", "大纲节点不存在。")
        if command == "delete":
            descendants = {node_id}
            changed = True
            while changed:
                changed = False
                for item in nodes:
                    if item.get("parent_id") in descendants and item["id"] not in descendants:
                        descendants.add(item["id"]); changed = True
            nodes = [item for item in nodes if item["id"] not in descendants]
            links = [link for link in links if link.get("node_id") not in descendants]
        elif command == "move":
            target["parent_id"] = payload.get("parent_id")
            target["position"] = int(payload.get("position", target.get("position", 0)) or 0)
        else:
            for key, value in (payload.get("node") or {}).items():
                if key not in {"id", "parent_id", "position", "title", "purpose", "scope", "status", "source_anchor", "source_version", "source_status", "links", "canvas_x", "canvas_y"}:
                    continue
                target[key] = value
    elif command in {"link", "unlink"}:
        link = payload.get("link") or {}
        if not node_id or not any(item["id"] == node_id for item in nodes):
            raise ProjectServiceError(404, "outline_node_missing", "大纲节点不存在。")
        key = {"node_id": node_id, "resource_type": str(link.get("resource_type") or ""), "resource_id": str(link.get("resource_id") or ""), "source_version": int(link.get("source_version", 0) or 0), "status": str(link.get("status") or "active")}
        if not key["resource_type"] or not key["resource_id"]:
            raise ProjectServiceError(422, "outline_link_invalid", "资源链接必须包含类型和 ID。")
        if command == "link":
            resource = _resource_state(conn, project_id, key["resource_type"], key["resource_id"])
            if resource["status"] != "active":
                raise ProjectServiceError(409, "outline_resource_unavailable", "资源已失效或不可用于当前论文，不能建立新链接。")
            requested_version = key["source_version"]
            if requested_version not in (0, int(resource["version"])):
                raise ProjectServiceError(409, "outline_resource_version_conflict", "资源版本已变化，请刷新后重新选择。")
            key["source_version"] = int(resource["version"])
            key["status"] = "active"
            links = [item for item in links if not (item.get("node_id") == node_id and item.get("resource_type") == key["resource_type"] and item.get("resource_id") == key["resource_id"])]
            links.append(key)
        else:
            links = [item for item in links if not (item.get("node_id") == node_id and item.get("resource_type") == key["resource_type"] and item.get("resource_id") == key["resource_id"])]
    result=_write_version(conn, project_id, int(payload["base_version"]), payload["request_id"], nodes, links)
    return _request_finish(conn,project_id,payload["request_id"],"node_command",digest,result)


def create_candidate(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _reject_managed_request_id(conn,project_id,payload["request_id"])
    current = current_version(conn, project_id)
    if int(payload["base_version"]) != current:
        raise ProjectServiceError(409, "outline_candidate_stale", "AI 候选基线已过期，请重新生成。")
    existing = conn.execute("SELECT * FROM outline_candidates WHERE request_id=?", (payload["request_id"],)).fetchone()
    if existing:
        if existing["project_id"] != project_id or int(existing["base_version"]) != int(payload["base_version"]) or json.loads(existing["operations_json"] or "[]") != (payload.get("operations") or []):
            raise ProjectServiceError(409, "outline_request_id_conflict", "请求编号已被使用，请生成新编号后重试。")
        return dict(existing) | {"operations": json.loads(existing["operations_json"] or "[]"), "idempotent_replay": True}
    candidate_id = uuid.uuid4().hex; stamp = _now()
    conn.execute("INSERT INTO outline_candidates(id,project_id,base_version,operations_json,status,request_id,created_at) VALUES(?,?,?,?,?,?,?)", (candidate_id, project_id, current, _dump(payload.get("operations") or []), "candidate", payload["request_id"], stamp))
    return {"id": candidate_id, "project_id": project_id, "base_version": current, "operations": payload.get("operations") or [], "status": "candidate", "idempotent_replay": False}


def decide_candidate(conn: sqlite3.Connection, project_id: str, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from paperpilot.services import innovation_points
    _reject_managed_request_id(conn,project_id,payload["request_id"])
    row = conn.execute("SELECT * FROM outline_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "outline_candidate_missing", "大纲候选不存在。")
    if row["status"] != "candidate":
        return dict(row) | {"operations": json.loads(row["operations_json"] or "[]"), "idempotent_replay": True}
    if payload["decision"] == "reject":
        status = "rejected"; result = {"status": status, "version": current_version(conn, project_id)}
    else:
        innovation_refs = innovation_points.usage_references(conn, project_id, "outline_candidate", candidate_id)
        if not innovation_points.references_current(conn, project_id, innovation_refs):
            raise ProjectServiceError(409, "innovation_context_stale", "论文创新点已更新，请重新预览大纲建议。")
        if current_version(conn, project_id) != int(row["base_version"]) or int(payload["base_version"]) != int(row["base_version"]):
            raise ProjectServiceError(409, "outline_candidate_stale", "AI 候选基线已过期，不能直接应用。")
        operations = json.loads(row["operations_json"] or "[]")
        if payload['decision'] == 'partial_apply':
            selected = set(payload.get('selected_indices') or [])
            if not selected or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(operations) for i in selected):
                raise ProjectServiceError(422, 'outline_selection_invalid', '请至少选择一个有效节点修改；空选择不会应用全部候选。')
        else:
            selected = set(range(len(operations)))
        graph = read_outline(conn, project_id)
        nodes = copy.deepcopy(graph["nodes"]); links = copy.deepcopy(graph.get("links") or [])
        for idx, operation in enumerate(operations):
            if idx not in selected:
                continue
            op = operation.get("op") if isinstance(operation, dict) else None
            if op == "add_node": nodes.append(copy.deepcopy(operation.get("node") or {}))
            elif op == "update_node":
                target = next((item for item in nodes if item["id"] == operation.get("id")), None)
                if target is None: raise ProjectServiceError(422, "outline_candidate_invalid", "候选引用了不存在的节点。")
                target.update({k: v for k, v in (operation.get("changes") or {}).items() if k != "id"})
            elif op == "delete_node":
                nodes = [item for item in nodes if item["id"] != operation.get("id")]
            else:
                raise ProjectServiceError(422, "outline_candidate_invalid", "不支持的大纲候选操作。")
        result = _write_version(conn, project_id, int(row["base_version"]), payload["request_id"], nodes, links, source="ai_candidate")
        innovation_points.record_usage(conn, project_id, innovation_refs, "outline", str(result["version"]),
                                       result["version"], "已采纳大纲")
        status = "partially_applied" if payload["decision"] == "partial_apply" else "applied"
        result = {"status": status, **result}
    decision_json = _dump(payload)
    conn.execute("UPDATE outline_candidates SET status=?,decision_request_id=?,decision_json=?,decided_at=? WHERE id=?", (status, payload["request_id"], decision_json, _now(), candidate_id))
    return {"id": candidate_id, "project_id": project_id, "operations": json.loads(row["operations_json"] or "[]"), "idempotent_replay": False, **result}


def save_local_draft(conn: sqlite3.Connection, project_id: str, draft_key: str, base_version: int, content: dict[str, Any]) -> dict[str, Any]:
    stamp = _now()
    conn.execute("INSERT INTO outline_local_drafts(project_id,draft_key,base_version,content_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,draft_key) DO UPDATE SET base_version=excluded.base_version,content_json=excluded.content_json,updated_at=excluded.updated_at", (project_id, draft_key, base_version, _dump(content), stamp))
    return {"project_id": project_id, "draft_key": draft_key, "base_version": base_version, "content": content, "updated_at": stamp}


def get_local_draft(conn: sqlite3.Connection, project_id: str, draft_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM outline_local_drafts WHERE project_id=? AND draft_key=?", (project_id, draft_key)).fetchone()
    if row is None:
        return None
    return {"project_id": project_id, "draft_key": draft_key, "base_version": int(row["base_version"]), "content": json.loads(row["content_json"] or "{}"), "updated_at": row["updated_at"]}

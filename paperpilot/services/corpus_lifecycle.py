from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def ensure_source_version(conn: sqlite3.Connection, corpus_id: str, content: str, source: str = "", source_kind: str = "manual") -> dict[str, Any]:
    row = conn.execute("SELECT version FROM corpus_source_versions WHERE corpus_id=? ORDER BY version DESC LIMIT 1", (corpus_id,)).fetchone()
    if row:
        return {"corpus_id": corpus_id, "version": int(row["version"]), "created": False}
    stamp = _now()
    conn.execute(
        "INSERT INTO corpus_source_versions(corpus_id,version,content,source,source_kind,permission_state,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (corpus_id, 1, content, source, source_kind, "authorized", "active", stamp),
    )
    return {"corpus_id": corpus_id, "version": 1, "created": True}


def _corpus(conn: sqlite3.Connection, project_id: str, corpus_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM corpus WHERE id=? AND project_id=?", (corpus_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "corpus_missing", "语料资源不存在。")
    return row


def _current_source(conn: sqlite3.Connection, corpus_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM corpus_source_versions WHERE corpus_id=? ORDER BY version DESC LIMIT 1", (corpus_id,)).fetchone()
    if row is None:
        raise ProjectServiceError(409, "source_version_missing", "语料没有可用原文版本。")
    return row


def lineage(conn: sqlite3.Connection, project_id: str, corpus_id: str) -> dict[str, Any]:
    resource = _corpus(conn, project_id, corpus_id)
    source_versions = [dict(row) for row in conn.execute("SELECT * FROM corpus_source_versions WHERE corpus_id=? ORDER BY version DESC", (corpus_id,))]
    curation = [dict(row) for row in conn.execute("SELECT * FROM corpus_curation_versions WHERE corpus_id=? ORDER BY version DESC", (corpus_id,))]
    candidates = []
    for row in conn.execute("SELECT * FROM corpus_refresh_candidates WHERE corpus_id=? ORDER BY created_at DESC LIMIT 30", (corpus_id,)):
        item = dict(row)
        item["decision"] = json.loads(item.pop("decision_json") or "{}")
        candidates.append(item)
    current = source_versions[0] if source_versions else None
    indexed = conn.execute("SELECT COUNT(*) AS count FROM corpus_index_chunks WHERE corpus_id=? AND source_version=? AND status='active'", (corpus_id, current["version"] if current else 0)).fetchone()
    result = {"resource": dict(resource), "current_source_version": current, "source_versions": source_versions, "curation_versions": curation, "refresh_candidates": candidates, "index": {"source_version": current["version"] if current else None, "chunk_count": int(indexed["count"] if indexed else 0)}}
    from paperpilot.services.corpus_migrations import augment_lineage
    return augment_lineage(conn, project_id, corpus_id, result)


def rebuild_index(conn: sqlite3.Connection, project_id: str, corpus_id: str, source_version: int | None = None, chunk_size: int = 1200) -> dict[str, Any]:
    resource = _corpus(conn, project_id, corpus_id)
    source = conn.execute(
        "SELECT * FROM corpus_source_versions WHERE corpus_id=? AND version=?" if source_version is not None else
        "SELECT * FROM corpus_source_versions WHERE corpus_id=? ORDER BY version DESC LIMIT 1",
        (corpus_id, source_version) if source_version is not None else (corpus_id,),
    ).fetchone()
    if source is None:
        raise ProjectServiceError(409, "source_version_missing", "语料没有可索引的原文版本。")
    if source["permission_state"] != "authorized":
        raise ProjectServiceError(409, "permission_unavailable", "当前原文权限不可用，不能建立可检索索引。")
    chunk_size = max(200, min(int(chunk_size), 10000))
    content = str(source["content"])
    conn.execute("DELETE FROM corpus_index_chunks WHERE corpus_id=? AND source_version=?", (corpus_id, source["version"]))
    stamp = _now()
    fragments = [{"text": content, "location": ""}]
    if source["source_kind"] == "oa_import" and source["version"] == 1:
        evidence = conn.execute("SELECT fragments_json FROM external_evidence WHERE id=? AND project_id=?",
                                (resource["source_resource_id"], project_id)).fetchone()
        if evidence:
            parsed = json.loads(evidence["fragments_json"])
            if parsed and "\n\n".join(piece.get("text", "") for piece in parsed) == content:
                fragments = parsed

    chunks = []
    offset = 0
    for fragment in fragments:
        text = fragment["text"]
        location = fragment.get("location", "")
        cursor = 0
        while cursor < len(text):
            end = min(len(text), cursor + chunk_size)
            if end < len(text):
                boundary = text.rfind("\n", cursor, end)
                if location and boundary <= cursor + chunk_size // 3:
                    boundary = text.rfind(" ", cursor, end)
                if boundary > cursor + chunk_size // 3:
                    end = boundary
            segment = text[cursor:end]
            piece = segment.strip()
            if piece:
                start_offset = offset + cursor + len(segment) - len(segment.lstrip())
                chunks.append((corpus_id, source["version"], len(chunks), piece,
                               start_offset, start_offset + len(piece), location, len(piece), "active", stamp))
            cursor = end
        offset += len(text) + 2
    conn.executemany("INSERT INTO corpus_index_chunks(corpus_id,source_version,chunk_index,content,start_offset,end_offset,location,token_count,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", chunks)
    return {"corpus_id": corpus_id, "source_version": int(source["version"]), "chunk_count": len(chunks), "status": "indexed"}


def search_index(conn: sqlite3.Connection, project_id: str, query: str, corpus_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    query = " ".join(str(query or "").split())
    if not query:
        raise ProjectServiceError(422, "query_required", "语料检索需要关键词。")
    terms = [term.lower() for term in query.split() if term]
    limit = max(1, min(int(limit), 100))
    params: list[Any] = [project_id]
    where = "c.project_id=? AND s.permission_state='authorized' AND s.status='active' AND i.status='active'"
    if corpus_id:
        where += " AND c.id=?"
        params.append(corpus_id)
    rows = conn.execute(
        "SELECT c.id AS corpus_id,c.title,c.source,s.version AS source_version,i.chunk_index,i.content,i.start_offset,i.end_offset,i.location, "
        "CASE WHEN e.attachment_path<>'' AND oi.status='imported' AND json_extract(oi.preview_json,'$.content_kind')='pdf' "
        "THEN oi.id ELSE NULL END AS pdf_import_id "
        "FROM corpus c JOIN corpus_source_versions s ON s.corpus_id=c.id "
        "JOIN corpus_index_chunks i ON i.corpus_id=s.corpus_id AND i.source_version=s.version "
        "LEFT JOIN external_evidence e ON e.id=c.source_resource_id AND e.project_id=c.project_id AND s.source_kind='oa_import' AND s.version=1 "
        "LEFT JOIN oa_imports oi ON oi.id=e.import_id AND oi.project_id=e.project_id "
        "WHERE " + where + " AND s.version=(SELECT MAX(s2.version) FROM corpus_source_versions s2 WHERE s2.corpus_id=s.corpus_id) "
        "ORDER BY c.id,i.chunk_index",
        params,
    ).fetchall()
    matches = []
    for row in rows:
        haystack = str(row["content"]).lower()
        hit_terms = [term for term in terms if term in haystack]
        if not hit_terms:
            continue
        matches.append({**dict(row), "score": len(hit_terms), "match_reasons": [f"命中：{term}" for term in hit_terms]})
    matches.sort(key=lambda item: (-item["score"], item["corpus_id"], item["chunk_index"]))
    return {"query": query, "results": matches[:limit], "omitted": max(0, len(matches) - limit), "authorized_only": True}


def create_curation(conn: sqlite3.Connection, project_id: str, corpus_id: str, base_source_version: int, content: str, notes: str, kind: str) -> dict[str, Any]:
    _corpus(conn, project_id, corpus_id)
    source = conn.execute("SELECT 1 FROM corpus_source_versions WHERE corpus_id=? AND version=?", (corpus_id, base_source_version)).fetchone()
    if source is None:
        raise ProjectServiceError(409, "source_version_conflict", "人工修订绑定的原文版本不存在。")
    latest = conn.execute("SELECT COALESCE(MAX(version),0) AS version FROM corpus_curation_versions WHERE corpus_id=?", (corpus_id,)).fetchone()
    version = int(latest["version"]) + 1
    item_id = uuid.uuid4().hex
    conn.execute("INSERT INTO corpus_curation_versions(id,corpus_id,source_version,version,content,kind,notes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (item_id, corpus_id, base_source_version, version, content, kind, notes, "active", _now()))
    return {"id": item_id, "corpus_id": corpus_id, "source_version": base_source_version, "version": version, "content": content, "kind": kind, "notes": notes, "status": "active"}


def create_refresh_candidate(conn: sqlite3.Connection, project_id: str, corpus_id: str, base_source_version: int, content: str, source: str, permission_state: str, request_id: str) -> dict[str, Any]:
    _corpus(conn, project_id, corpus_id)
    existing = conn.execute("SELECT * FROM corpus_refresh_candidates WHERE request_id=?", (request_id,)).fetchone()
    if existing:
        if existing['corpus_id'] != corpus_id or existing['base_source_version'] != base_source_version or existing['proposed_content'] != content or existing['proposed_source'] != source or existing['permission_state'] != permission_state:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于其他语料、原文版本或刷新内容。')
        return {**dict(existing), "idempotent_replay": True}
    current = _current_source(conn, corpus_id)
    if int(current["version"]) != base_source_version:
        raise ProjectServiceError(409, "source_version_conflict", "刷新基线已变化，请重新读取当前原文版本。")
    if permission_state not in {"authorized", "unknown", "revoked"}:
        raise ProjectServiceError(422, "invalid_permission_state", "权限状态无效。")
    candidate_id = uuid.uuid4().hex
    conn.execute("INSERT INTO corpus_refresh_candidates(id,corpus_id,base_source_version,proposed_content,proposed_source,permission_state,status,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (candidate_id, corpus_id, base_source_version, content, source, permission_state, "pending", request_id, _now()))
    return {"id": candidate_id, "corpus_id": corpus_id, "base_source_version": base_source_version, "proposed_content": content, "proposed_source": source, "permission_state": permission_state, "status": "pending", "request_id": request_id, "idempotent_replay": False}


def decide_refresh(conn: sqlite3.Connection, project_id: str, candidate_id: str, decision: str, request_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT c.*,p.project_id FROM corpus_refresh_candidates c JOIN corpus p ON p.id=c.corpus_id WHERE c.id=? AND p.project_id=?", (candidate_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "refresh_candidate_missing", "刷新候选不存在。")
    if row["decision_request_id"]:
        if row["decision_request_id"] != request_id:
            raise ProjectServiceError(409, "candidate_already_decided", "刷新候选已由另一决策处理。")
        return {"id": candidate_id, "status": row["status"], "idempotent_replay": True}
    if row["status"] != "pending":
        raise ProjectServiceError(409, "candidate_not_open", "刷新候选已处理。")
    current = _current_source(conn, row["corpus_id"])
    if int(current["version"]) != int(row["base_source_version"]):
        raise ProjectServiceError(409, "source_version_conflict", "原文版本已变化，旧刷新候选不能直接应用。")
    if decision == "accept" and row["permission_state"] == "revoked":
        raise ProjectServiceError(409, "permission_revoked", "外部来源权限已撤销，不能接受刷新候选。")
    status = "accepted" if decision == "accept" else "rejected"
    stamp = _now()
    conn.execute("UPDATE corpus_refresh_candidates SET status=?,decision_request_id=?,decision_json=?,decided_at=? WHERE id=?", (status, request_id, json.dumps({"decision": decision}, ensure_ascii=False), stamp, candidate_id))
    result: dict[str, Any] = {"id": candidate_id, "status": status, "idempotent_replay": False}
    if decision == "accept":
        version = int(current["version"]) + 1
        conn.execute("UPDATE corpus_source_versions SET status='superseded' WHERE corpus_id=? AND version=?", (row["corpus_id"], current["version"]))
        conn.execute("INSERT INTO corpus_source_versions(corpus_id,version,content,source,source_kind,permission_state,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (row["corpus_id"], version, row["proposed_content"], row["proposed_source"], "external_refresh", row["permission_state"], "active", stamp))
        conn.execute("UPDATE corpus SET content=?,source=? WHERE id=?", (row["proposed_content"], row["proposed_source"], row["corpus_id"]))
        rebuild_index(conn, project_id, row["corpus_id"], version)
        result["source_version"] = version
    return result

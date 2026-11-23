from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


REFERENCE_FIELDS = {"title", "authors", "year", "venue", "doi", "canonical_url", "abstract", "notes", "tags"}
REFERENCE_VERSION_FIELDS = REFERENCE_FIELDS | {"full_text_presence", "attachment_status", "confidence"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi.removeprefix("doi:").strip().rstrip(".")


def normalize_title(value: Any) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(value or "").lower(), flags=re.UNICODE).split())


def _metadata(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": row["title"], "authors": row["authors"], "year": row["year"],
        "venue": row["venue"], "doi": row["doi"], "canonical_url": row["canonical_url"],
        "abstract": row["abstract"], "notes": row["notes"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "full_text_presence": row["full_text_presence"],
        "attachment_status": row["attachment_status"], "confidence": row["confidence"],
    }


def _possible_duplicates(conn: sqlite3.Connection, metadata: dict[str, Any], *, exclude_id: str | None = None) -> list[dict[str, Any]]:
    doi = normalize_doi(metadata.get("doi"))
    title = normalize_title(metadata.get("title"))
    authors = str(metadata.get("authors") or "").strip().lower()
    year = str(metadata.get("year") or "").strip()
    if doi:
        rows = conn.execute(
            "SELECT id,title,authors,year,doi,current_version,status FROM reference_records WHERE lower(doi)=? AND status='active'",
            (doi,),
        ).fetchall()
        if rows:
            return [dict(row) for row in rows if row["id"] != exclude_id]
    if not title:
        return []
    rows = conn.execute(
        "SELECT id,title,authors,year,doi,current_version,status FROM reference_records WHERE status='active'"
    ).fetchall()
    matches = []
    for row in rows:
        if row["id"] == exclude_id or normalize_title(row["title"]) != title:
            continue
        if year and str(row["year"] or "").strip() != year:
            continue
        row_authors = str(row["authors"] or "").strip().lower()
        if authors and row_authors and row_authors != authors:
            continue
        matches.append(dict(row))
    return matches


def find_possible_duplicates(conn: sqlite3.Connection, metadata: dict[str, Any], *, exclude_id: str | None = None) -> list[dict[str, Any]]:
    return _possible_duplicates(conn, metadata, exclude_id=exclude_id)


def create_reference(
    conn: sqlite3.Connection,
    metadata: dict[str, Any],
    *,
    source_trace: dict[str, Any] | None = None,
    full_text_presence: str = "metadata_only",
    attachment_status: str = "none",
    confidence: str = "lowered",
) -> dict[str, Any]:
    title = str(metadata.get("title") or "").strip()
    if not title:
        raise ProjectServiceError(422, "reference_title_required", "参考文献标题不能为空。")
    record_id = uuid.uuid4().hex
    stamp = _now()
    clean = {key: metadata.get(key, "") for key in REFERENCE_FIELDS}
    clean["title"] = title
    clean["doi"] = normalize_doi(clean.get("doi"))
    clean["tags"] = sorted({str(tag).strip() for tag in (clean.get("tags") or []) if str(tag).strip()})
    trace = source_trace or {"kind": "manual", "created_by": "user"}
    conn.execute(
        "INSERT INTO reference_records(id,current_version,doi,title,authors,year,venue,canonical_url,abstract,notes,tags_json,source_trace_json,full_text_presence,attachment_status,confidence,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record_id, 1, clean["doi"], clean["title"], clean["authors"], str(clean["year"]), clean["venue"], clean["canonical_url"], clean["abstract"], clean["notes"], json.dumps(clean["tags"], ensure_ascii=False), json.dumps(trace, ensure_ascii=False), full_text_presence, attachment_status, confidence, "active", stamp, stamp),
    )
    version_metadata = {**clean, "full_text_presence": full_text_presence, "attachment_status": attachment_status, "confidence": confidence}
    conn.execute(
        "INSERT INTO reference_record_versions(reference_id,version,metadata_json,source_trace_json,full_text_presence,attachment_status,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (record_id, 1, json.dumps(version_metadata, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), full_text_presence, attachment_status, confidence, stamp),
    )
    return {"id": record_id, "version": 1, **version_metadata, "source_trace": trace, "created_at": stamp}


def create_reference_draft(
    conn: sqlite3.Connection,
    project_id: str,
    metadata: dict[str, Any],
    source_trace: dict[str, Any],
    idempotency_key: str,
    *,
    full_text_presence: str = "metadata_only",
    confidence: str = "lowered",
    candidate_id: str = "",
    handoff_id: str = "",
) -> tuple[dict[str, Any], bool]:
    existing = conn.execute("SELECT * FROM reference_drafts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing:
        existing = dict(existing)
        same = (
            json.loads(existing["metadata_json"] or "{}") == metadata
            and json.loads(existing["source_trace_json"] or "{}") == source_trace
            and existing["project_id"] == project_id
            and existing["candidate_id"] == candidate_id
            and existing["handoff_id"] == handoff_id
            and existing["full_text_presence"] == full_text_presence
            and existing["confidence"] == confidence
        )
        if not same:
            raise ProjectServiceError(409, "idempotency_key_reused", "该交接键已用于不同的参考文献资料。")
        result = dict(existing)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["source_trace"] = json.loads(result.pop("source_trace_json") or "{}")
        return result, False
    draft_id = uuid.uuid4().hex
    stamp = _now()
    conn.execute(
        "INSERT INTO reference_drafts(id,project_id,candidate_id,handoff_id,idempotency_key,metadata_json,source_trace_json,full_text_presence,confidence,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (draft_id, project_id, candidate_id, handoff_id, idempotency_key, json.dumps(metadata, ensure_ascii=False), json.dumps(source_trace, ensure_ascii=False), full_text_presence, confidence, "pending_confirmation", stamp),
    )
    return {
        "id": draft_id, "project_id": project_id, "candidate_id": candidate_id,
        "handoff_id": handoff_id, "metadata": metadata, "source_trace": source_trace,
        "full_text_presence": full_text_presence, "confidence": confidence,
        "status": "pending_confirmation", "created_at": stamp,
    }, True


def list_references(conn: sqlite3.Connection, query: str = "", *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    text = str(query or "").strip().lower()
    if text:
        like = f"%{text}%"
        rows = conn.execute(
            "SELECT * FROM reference_records WHERE status='active' AND (lower(title) LIKE ? OR lower(authors) LIKE ? OR lower(doi) LIKE ? OR lower(notes) LIKE ?) ORDER BY title COLLATE NOCASE,id LIMIT ? OFFSET ?",
            (like, like, like, like, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM reference_records WHERE status='active' AND (lower(title) LIKE ? OR lower(authors) LIKE ? OR lower(doi) LIKE ? OR lower(notes) LIKE ?)",
            (like, like, like, like),
        ).fetchone()[0]
    else:
        rows = conn.execute(
            "SELECT * FROM reference_records WHERE status='active' ORDER BY title COLLATE NOCASE,id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM reference_records WHERE status='active'").fetchone()[0]
    items = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        item["source_trace"] = json.loads(item.pop("source_trace_json") or "{}")
        item["possible_duplicates"] = []
        items.append(item)
    return {"items": items, "total": int(total), "limit": limit, "offset": offset}


def read_reference(conn: sqlite3.Connection, reference_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM reference_records WHERE id=?", (reference_id,)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "reference_missing", "参考文献不存在。")
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["source_trace"] = json.loads(item.pop("source_trace_json") or "{}")
    versions = []
    for version in conn.execute("SELECT * FROM reference_record_versions WHERE reference_id=? ORDER BY version DESC", (reference_id,)):
        versions.append({
            "version": int(version["version"]), "metadata": json.loads(version["metadata_json"] or "{}"),
            "source_trace": json.loads(version["source_trace_json"] or "{}"), "created_at": version["created_at"],
        })
    links = [dict(link) for link in conn.execute("SELECT project_id,reference_version,cite_key,link_status,created_at,updated_at FROM project_reference_links WHERE reference_id=? ORDER BY project_id", (reference_id,))]
    attachments = [dict(attachment) for attachment in conn.execute("SELECT project_id,reference_version,external_evidence_id,created_at FROM reference_attachments WHERE reference_id=? ORDER BY reference_version DESC", (reference_id,))]
    item["versions"] = versions
    item["project_links"] = links
    item["attachments"] = attachments
    item["possible_duplicates"] = _possible_duplicates(conn, item, exclude_id=reference_id)
    return item


def update_reference(conn: sqlite3.Connection, reference_id: str, base_version: int, changes: dict[str, Any]) -> dict[str, Any]:
    record = conn.execute("SELECT * FROM reference_records WHERE id=?", (reference_id,)).fetchone()
    if record is None:
        raise ProjectServiceError(404, "reference_missing", "参考文献不存在。")
    current_version = int(record["current_version"])
    if current_version != base_version:
        raise ProjectServiceError(409, "reference_version_conflict", "参考文献已更新；请重新加载后比较字段差异。")
    unknown = set(changes) - REFERENCE_VERSION_FIELDS
    if unknown:
        raise ProjectServiceError(422, "invalid_reference_field", "参考文献更新包含不支持的字段。")
    metadata = _metadata(record)
    metadata.update(changes)
    metadata["doi"] = normalize_doi(metadata.get("doi"))
    metadata["tags"] = sorted({str(tag).strip() for tag in (metadata.get("tags") or []) if str(tag).strip()})
    if not str(metadata.get("title") or "").strip():
        raise ProjectServiceError(422, "reference_title_required", "参考文献标题不能为空。")
    if metadata.get("full_text_presence") not in {"metadata_only", "full_text_available", "parse_failed", "ocr_required", "parse_review"}:
        raise ProjectServiceError(422, "invalid_full_text_state", "参考文献全文状态无效。")
    if metadata.get("attachment_status") not in {"none", "retained", "not_retained", "pending"}:
        raise ProjectServiceError(422, "invalid_attachment_state", "参考文献附件状态无效。")
    if metadata.get("confidence") not in {"normal", "lowered", "not_assessed"}:
        raise ProjectServiceError(422, "invalid_confidence", "参考文献置信状态无效。")
    next_version = current_version + 1
    stamp = _now()
    trace = json.loads(record["source_trace_json"] or "{}")
    for key in ("doi", "title", "authors", "year", "venue", "canonical_url", "abstract", "notes"):
        metadata.setdefault(key, "")
    conn.execute(
        "INSERT INTO reference_record_versions(reference_id,version,metadata_json,source_trace_json,full_text_presence,attachment_status,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (reference_id, next_version, json.dumps(metadata, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), metadata["full_text_presence"], metadata["attachment_status"], metadata["confidence"], stamp),
    )
    conn.execute(
        "UPDATE reference_records SET current_version=?,doi=?,title=?,authors=?,year=?,venue=?,canonical_url=?,abstract=?,notes=?,tags_json=?,full_text_presence=?,attachment_status=?,confidence=?,updated_at=? WHERE id=?",
        (next_version, metadata["doi"], metadata["title"], metadata["authors"], str(metadata["year"]), metadata["venue"], metadata["canonical_url"], metadata["abstract"], metadata["notes"], json.dumps(metadata["tags"], ensure_ascii=False), metadata["full_text_presence"], metadata["attachment_status"], metadata["confidence"], stamp, reference_id),
    )
    conn.execute(
        "UPDATE project_reference_links SET link_status='update_available',updated_at=? WHERE reference_id=? AND reference_version<?",
        (stamp, reference_id, next_version),
    )
    return {"id": reference_id, "version": next_version, "metadata": metadata, "project_links_unchanged": True}


def attach_reference_evidence(
    conn: sqlite3.Connection,
    project_id: str,
    reference_id: str,
    external_evidence_id: str,
    base_version: int,
) -> dict[str, Any]:
    record = conn.execute("SELECT * FROM reference_records WHERE id=? AND status='active'", (reference_id,)).fetchone()
    if record is None:
        raise ProjectServiceError(404, "reference_missing", "参考文献不存在。")
    link = conn.execute("SELECT reference_version FROM project_reference_links WHERE project_id=? AND reference_id=? AND link_status IN ('linked','update_available')", (project_id, reference_id)).fetchone()
    if link is None:
        raise ProjectServiceError(409, "reference_link_required", "先将参考文献关联到该论文，再附加该项目中已确认的解析证据。")
    if int(record["current_version"]) != base_version:
        raise ProjectServiceError(409, "reference_version_conflict", "参考文献已更新；请重新加载后再附加全文。")
    evidence = conn.execute("""SELECT e.id,e.source_url,e.source_version,e.attachment_path,i.doi
        FROM external_evidence e JOIN oa_imports i ON i.id=e.import_id AND i.project_id=e.project_id
        WHERE e.id=? AND e.project_id=? AND i.status='imported'""", (external_evidence_id, project_id)).fetchone()
    if evidence is None:
        raise ProjectServiceError(404, "evidence_missing", "该论文项目中找不到已确认的导入证据。")
    record_doi = normalize_doi(record["doi"])
    evidence_doi = normalize_doi(evidence["doi"])
    if record_doi and evidence_doi and record_doi != evidence_doi:
        raise ProjectServiceError(422, "reference_evidence_mismatch", "全文来源的 DOI 与此参考文献不一致。")
    partial = evidence["source_version"] in {"oa-import-v1", "oa-import-v2-partial"}
    revised = update_reference(
        conn, reference_id, base_version,
        {"full_text_presence": "parse_review" if partial else "full_text_available",
         "attachment_status": "retained" if evidence["attachment_path"] else "not_retained",
         "confidence": "lowered" if partial else "normal"},
    )
    stamp = _now()
    conn.execute(
        "INSERT INTO reference_attachments(reference_id,reference_version,project_id,external_evidence_id,created_at) VALUES(?,?,?,?,?)",
        (reference_id, revised["version"], project_id, external_evidence_id, stamp),
    )
    return {
        "reference_id": reference_id,
        "reference_version": revised["version"],
        "external_evidence_id": external_evidence_id,
        "source_url": evidence["source_url"],
        "previous_link_version": int(link["reference_version"]),
        "link_refresh_required": True,
    }


def link_reference(
    conn: sqlite3.Connection,
    project_id: str,
    reference_id: str,
    reference_version: int,
    cite_key: str,
    request_id: str,
) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM project_reference_links WHERE project_id=? AND reference_id=?", (project_id, reference_id)).fetchone()
    if existing:
        if existing["request_id"] == request_id:
            if int(existing["reference_version"]) != reference_version or existing["cite_key"] != cite_key.strip():
                raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的参考文献关联。")
            return dict(existing) | {"idempotent_replay": True}
        if existing["link_status"] != "unlinked":
            raise ProjectServiceError(409, "reference_already_linked", "该参考文献已关联；如需更新请显式刷新引用版本。")
    version_row = conn.execute(
        "SELECT * FROM reference_record_versions WHERE reference_id=? AND version=?", (reference_id, reference_version)
    ).fetchone()
    if version_row is None:
        raise ProjectServiceError(404, "reference_version_missing", "找不到指定参考文献版本。")
    metadata = json.loads(version_row["metadata_json"] or "{}")
    citation_snapshot = {key: metadata.get(key, "") for key in ("title", "authors", "year", "doi", "canonical_url", "full_text_presence", "confidence")}
    citation_snapshot["cite_key"] = cite_key.strip()
    stamp = _now()
    base_reference_version = int(existing["reference_version"]) if existing else 0
    if existing:
        conn.execute(
            "UPDATE project_reference_links SET reference_version=?,cite_key=?,request_id=?,citation_snapshot_json=?,link_status='linked',updated_at=? WHERE project_id=? AND reference_id=?",
            (reference_version, cite_key.strip(), request_id, json.dumps(citation_snapshot, ensure_ascii=False), stamp, project_id, reference_id),
        )
        action = "relinked"
    else:
        conn.execute(
            "INSERT INTO project_reference_links(project_id,reference_id,reference_version,cite_key,request_id,citation_snapshot_json,link_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, reference_id, reference_version, cite_key.strip(), request_id, json.dumps(citation_snapshot, ensure_ascii=False), "linked", stamp, stamp),
        )
        action = "linked"
    conn.execute(
        "INSERT INTO project_reference_link_history(id,project_id,reference_id,reference_version,cite_key,citation_snapshot_json,base_reference_version,request_id,action,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, reference_id, reference_version, cite_key.strip(), json.dumps(citation_snapshot, ensure_ascii=False), base_reference_version, request_id, action, stamp),
    )
    conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    return {"project_id": project_id, "reference_id": reference_id, "reference_version": reference_version, "cite_key": cite_key.strip(), "link_status": "linked", "citation_snapshot": citation_snapshot}


def refresh_reference_link(conn: sqlite3.Connection, project_id: str, reference_id: str, reference_version: int, base_version: int, request_id: str) -> dict[str, Any]:
    replay = conn.execute("SELECT * FROM project_reference_link_history WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if replay:
        if (
            replay["reference_id"] != reference_id
            or int(replay["reference_version"]) != reference_version
            or int(replay["base_reference_version"] or 0) != base_version
        ):
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的参考文献版本刷新。")
        return {"project_id": project_id, "reference_id": replay["reference_id"], "reference_version": replay["reference_version"], "cite_key": replay["cite_key"], "link_status": "linked", "idempotent_replay": True}
    link = conn.execute("SELECT * FROM project_reference_links WHERE project_id=? AND reference_id=?", (project_id, reference_id)).fetchone()
    if link is None:
        raise ProjectServiceError(404, "reference_link_missing", "论文尚未关联该参考文献。")
    if int(link["reference_version"]) != base_version:
        raise ProjectServiceError(409, "reference_link_conflict", "论文中的引用版本已变化；请重新加载后重试。")
    version_row = conn.execute("SELECT * FROM reference_record_versions WHERE reference_id=? AND version=?", (reference_id, reference_version)).fetchone()
    if version_row is None:
        raise ProjectServiceError(404, "reference_version_missing", "找不到指定参考文献版本。")
    metadata = json.loads(version_row["metadata_json"] or "{}")
    snapshot = {key: metadata.get(key, "") for key in ("title", "authors", "year", "doi", "canonical_url", "full_text_presence", "confidence")}
    snapshot["cite_key"] = link["cite_key"]
    stamp = _now()
    conn.execute("UPDATE project_reference_links SET reference_version=?,citation_snapshot_json=?,link_status='linked',updated_at=? WHERE project_id=? AND reference_id=?", (reference_version, json.dumps(snapshot, ensure_ascii=False), stamp, project_id, reference_id))
    conn.execute("INSERT INTO project_reference_link_history(id,project_id,reference_id,reference_version,cite_key,citation_snapshot_json,base_reference_version,request_id,action,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, reference_id, reference_version, link["cite_key"], json.dumps(snapshot, ensure_ascii=False), base_version, request_id, "explicit_refresh", stamp))
    conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    return {"project_id": project_id, "reference_id": reference_id, "reference_version": reference_version, "cite_key": link["cite_key"], "link_status": "linked", "citation_snapshot": snapshot}


def merge_references(conn: sqlite3.Connection, source_id: str, target_id: str, base_version: int, request_id: str) -> dict[str, Any]:
    replay = conn.execute("SELECT source_id,target_id,result_json FROM reference_merge_events WHERE request_id=?", (request_id,)).fetchone()
    if replay:
        if replay["source_id"] != source_id or replay["target_id"] != target_id:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于另一组合并操作。")
        result = json.loads(replay["result_json"])
        if int(result["target_version"]) - 1 != base_version:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的合并基线版本。")
        return result | {"idempotent_replay": True}
    if source_id == target_id:
        raise ProjectServiceError(422, "invalid_merge", "不能把参考文献合并到自身。")
    source = conn.execute("SELECT * FROM reference_records WHERE id=? AND status='active'", (source_id,)).fetchone()
    target = conn.execute("SELECT * FROM reference_records WHERE id=? AND status='active'", (target_id,)).fetchone()
    if source is None or target is None:
        raise ProjectServiceError(404, "reference_missing", "合并源或目标参考文献不存在。")
    if int(target["current_version"]) != base_version:
        raise ProjectServiceError(409, "reference_version_conflict", "合并目标已更新；请重新加载后比较。")
    source_metadata = _metadata(source)
    target_metadata = _metadata(target)
    notes = [str(value).strip() for value in (target_metadata.get("notes"), source_metadata.get("notes")) if str(value or "").strip()]
    target_metadata["notes"] = "\n\n".join(dict.fromkeys(notes))
    target_metadata["tags"] = sorted(set(target_metadata.get("tags", [])) | set(source_metadata.get("tags", [])))
    next_version = int(target["current_version"]) + 1
    stamp = _now()
    trace = json.loads(target["source_trace_json"] or "{}")
    conn.execute("INSERT INTO reference_record_versions(reference_id,version,metadata_json,source_trace_json,full_text_presence,attachment_status,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)", (target_id, next_version, json.dumps(target_metadata, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), target_metadata["full_text_presence"], target_metadata["attachment_status"], target_metadata["confidence"], stamp))
    conn.execute("UPDATE reference_records SET current_version=?,notes=?,tags_json=?,updated_at=? WHERE id=?", (next_version, target_metadata["notes"], json.dumps(target_metadata["tags"], ensure_ascii=False), stamp, target_id))
    conn.execute("UPDATE reference_records SET status='merged',merged_into=?,updated_at=? WHERE id=?", (target_id, stamp, source_id))
    conn.execute("UPDATE project_reference_links SET link_status='merged_source',updated_at=? WHERE reference_id=?", (stamp, source_id))
    conn.execute(
        "UPDATE project_reference_links SET link_status='update_available',updated_at=? WHERE reference_id=? AND reference_version<?",
        (stamp, target_id, next_version),
    )
    result = {"source_id": source_id, "target_id": target_id, "target_version": next_version, "source_history_preserved": True, "project_links_rewritten": False}
    conn.execute("INSERT INTO reference_merge_events(id,request_id,source_id,target_id,result_json,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, request_id, source_id, target_id, json.dumps(result, ensure_ascii=False), stamp))
    return result


def confirm_draft(
    conn: sqlite3.Connection,
    draft_id: str,
    request_id: str,
    decision: str,
    merge_into_id: str | None = None,
) -> dict[str, Any]:
    draft = conn.execute("SELECT * FROM reference_drafts WHERE id=?", (draft_id,)).fetchone()
    if draft is None:
        raise ProjectServiceError(404, "reference_draft_missing", "待确认参考文献不存在。")
    decision_json = json.dumps({"decision": decision, "merge_into_id": merge_into_id}, ensure_ascii=False, sort_keys=True)
    if draft["status"] == "confirmed" and draft["confirmed_at"]:
        if draft["confirmation_request_id"] != request_id or draft["confirmation_json"] != decision_json:
            raise ProjectServiceError(409, "reference_draft_already_confirmed", "该导入记录已用另一项决定确认。")
        return {"id": draft_id, "status": "confirmed", "idempotent_replay": True}
    metadata = json.loads(draft["metadata_json"] or "{}")
    duplicates = _possible_duplicates(conn, metadata)
    if decision == "merge":
        if not merge_into_id:
            raise ProjectServiceError(422, "merge_target_required", "合并时必须指定目标参考文献。")
        target = conn.execute("SELECT current_version FROM reference_records WHERE id=? AND status='active'", (merge_into_id,)).fetchone()
        if target is None:
            raise ProjectServiceError(404, "merge_target_missing", "合并目标不存在。")
        created = create_reference(conn, metadata, source_trace=json.loads(draft["source_trace_json"] or "{}"), full_text_presence=draft["full_text_presence"], confidence=draft["confidence"])
        merged = merge_references(conn, created["id"], merge_into_id, int(target["current_version"]), f"draft-confirm:{draft_id}")
        conn.execute("UPDATE reference_drafts SET status='confirmed',confirmed_at=?,confirmation_request_id=?,confirmation_json=? WHERE id=?", (_now(), request_id, decision_json, draft_id))
        return {"draft_id": draft_id, "status": "confirmed", "decision": "merge", "duplicates": duplicates, "merge": merged}
    if decision != "create":
        raise ProjectServiceError(422, "invalid_reference_decision", "操作必须为 create 或 merge。")
    created = create_reference(
        conn, metadata, source_trace=json.loads(draft["source_trace_json"] or "{}"),
        full_text_presence=draft["full_text_presence"], confidence=draft["confidence"],
    )
    conn.execute("UPDATE reference_drafts SET status='confirmed',confirmed_at=?,confirmation_request_id=?,confirmation_json=? WHERE id=?", (_now(), request_id, decision_json, draft_id))
    return {"draft_id": draft_id, "status": "confirmed", "decision": "create", "reference": created, "possible_duplicates": duplicates}

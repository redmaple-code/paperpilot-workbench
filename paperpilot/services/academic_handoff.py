"""Shared academic-candidate handoff; caller owns the transaction."""
import json
import uuid
from typing import Any
from datetime import datetime, timezone
from fastapi import HTTPException
from paperpilot.domain.schemas import AcademicHandoffIn
from paperpilot.services.reference_library import create_reference_draft
from paperpilot.services.project_errors import ProjectServiceError

def now():
    return datetime.now(timezone.utc).isoformat()


def _reference_draft_payload(row):
    item = dict(row)
    item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
    item["source_trace"] = json.loads(item.pop("source_trace_json", "{}") or "{}")
    return item

def handoff(conn, project_id: str, payload: AcademicHandoffIn, *, summary_override=None) -> dict[str, Any]:
    """Create or replay a handoff inside the caller's transaction."""
    candidate = conn.execute("SELECT * FROM academic_candidates WHERE id=? AND project_id=?", (payload.candidate_id, project_id)).fetchone()
    if not candidate:
        raise HTTPException(404, "候选论文不存在")
    key = payload.idempotency_key or f"{project_id}:{payload.candidate_id}"
    existing = conn.execute("SELECT * FROM academic_handoffs WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        if existing["project_id"] != project_id or existing["candidate_id"] != candidate["id"]:
            raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": "交接键已用于另一篇论文或另一个搜索候选。"})
        draft_link = conn.execute("SELECT reference_draft_id FROM reference_handoff_drafts WHERE handoff_id=?", (existing["id"],)).fetchone()
        draft = conn.execute("SELECT * FROM reference_drafts WHERE id=?", (draft_link["reference_draft_id"],)).fetchone() if draft_link else None
        return {"ok": True, "handoff": dict(existing), "reference_draft": _reference_draft_payload(draft) if draft else None, "idempotent_replay": True, "metadata_only": candidate["access_status"] != "open_access", "next_action": "确认参考文献记录；如需原文解析，再交给 REQ-018 预览和用户确认"}
    search_row=conn.execute("SELECT * FROM academic_searches WHERE id=? AND project_id=?",(candidate['search_id'],project_id)).fetchone()
    if not search_row:
        raise HTTPException(409,detail={'code':'search_provenance_missing','message':'检索来源不可用，请重新搜索后交接。'})
    summary=summary_override or {}
    handoff_id = uuid.uuid4().hex[:16]; stamp = now()
    trace = {"search_id": candidate["search_id"], "candidate_id": candidate["id"], "provider": candidate["provider"], "doi": candidate["doi"], "canonical_url": candidate["canonical_url"], "access_status": candidate["access_status"], "interface_version": "v3", "candidate_sources":json.loads(candidate['sources_json'] or '[]'), "query_snapshot":{"query":search_row['query'],"filters":json.loads(search_row['filters_json'] or '{}'),"rule_version":search_row['rule_version'],"provider":search_row['provider'],"retrieved_at":summary.get('retrieved_at'),"summary":summary}}
    metadata = {"title": candidate["title"], "authors": candidate["authors"], "year": candidate["year"], "venue": candidate["venue"], "doi": candidate["doi"], "canonical_url": candidate["canonical_url"], "abstract": candidate["abstract"], "notes": "", "tags": []}
    try:
        conn.execute("INSERT INTO academic_handoffs(id,project_id,candidate_id,idempotency_key,status,source_trace_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (handoff_id, project_id, candidate["id"], key, "handed_off", json.dumps(trace, ensure_ascii=False), stamp, stamp))
        draft, _ = create_reference_draft(conn, project_id, metadata, trace, key, full_text_presence="metadata_only", confidence="lowered", candidate_id=candidate["id"], handoff_id=handoff_id)
        conn.execute("INSERT INTO reference_handoff_drafts(handoff_id,reference_draft_id) VALUES(?,?)", (handoff_id, draft["id"]))
        conn.execute("UPDATE academic_candidates SET status='handed_off' WHERE id=?", (candidate["id"],))
        conn.execute("UPDATE academic_searches SET updated_at=? WHERE id=? AND project_id=?", (now(),candidate["search_id"],project_id))
    except ProjectServiceError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    return {"ok": True, "handoff": {"id": handoff_id, "project_id": project_id, "candidate_id": candidate["id"], "status": "handed_off"}, "reference_draft": draft, "source_trace": trace, "metadata_only": candidate["access_status"] != "open_access", "next_action": "确认参考文献记录；如需原文解析，再交给 REQ-018 预览和用户确认"}


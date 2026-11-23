from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from typing import Any

from paperpilot.services.ai_jobs import candidate_payload, create_job
from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import workflow_projection


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _key(kind: str, path: str) -> str:
    return "__outline__" if kind == "outline" else path


def _content(conn: sqlite3.Connection, project_id: str, kind: str, path: str) -> str:
    row = conn.execute(
        "SELECT content FROM outline WHERE project_id=?" if kind == "outline" else
        "SELECT content FROM files WHERE project_id=? AND path=?",
        (project_id,) if kind == "outline" else (project_id, path),
    ).fetchone()
    if row is None:
        raise ProjectServiceError(404, "target_missing", "指定修改目标不存在。")
    return str(row["content"])


def _sync_status(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if not row["job_id"]:
        return
    job = conn.execute("SELECT status,candidate_id,error_code FROM ai_jobs WHERE id=?", (row["job_id"],)).fetchone()
    if not job:
        return
    candidate_id = job["candidate_id"] or row["candidate_id"]
    status = {
        "running": "running", "succeeded": "candidate_ready", "failed": "failed", "cancelled": "cancelled",
    }.get(job["status"], row["status"])
    if candidate_id:
        candidate = conn.execute("SELECT status FROM draft_candidates WHERE id=?", (candidate_id,)).fetchone()
        if candidate:
            status = {"candidate_ready": "candidate_ready", "stale": "stale", "applied": "applied", "partially_applied": "partially_applied", "rejected": "rejected"}.get(candidate["status"], status)
    if candidate_id != row["candidate_id"] or status != row["status"]:
        conn.execute("UPDATE modification_requests SET candidate_id=?,status=?,updated_at=? WHERE id=?", (candidate_id, status, _now(), row["id"]))


def payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    _sync_status(conn, row)
    row = conn.execute("SELECT * FROM modification_requests WHERE id=?", (row["id"],)).fetchone()
    item = dict(row)
    item["base_file_versions"] = json.loads(item.pop("base_file_versions_json") or "{}")
    item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
    item["job"] = None
    item["candidate"] = None
    if item.get("job_id"):
        job = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (item["job_id"],)).fetchone()
        if job:
            item["job"] = {"id": job["id"], "request_id": job["request_id"], "status": job["status"], "error_code": job["error_code"], "attempt": job["attempt"], "candidate_id": job["candidate_id"]}
    if item.get("candidate_id"):
        candidate = conn.execute("SELECT * FROM draft_candidates WHERE id=?", (item["candidate_id"],)).fetchone()
        if candidate:
            item["candidate"] = candidate_payload(conn, candidate)
    return item


def create_request(conn: sqlite3.Connection, project_id: str, data: dict[str, Any], *, context: dict[str, Any], model: str, max_active_jobs: int, schedule_job) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM modification_requests WHERE project_id=? AND request_id=?", (project_id, data["request_id"])).fetchone()
    if existing:
        comparisons={'action':data.get('action','rewrite'),'target_kind':data['target_kind'],'target_path':'' if data['target_kind']=='outline' else data.get('target_path',''),
            'start_offset':data.get('start_offset',0),'end_offset':data.get('end_offset',0),'instruction':data['instruction'],'context_version':context['version']}
        key=_key(data['target_kind'],comparisons['target_path'])
        old_job=conn.execute('SELECT model FROM ai_jobs WHERE id=?',(existing['job_id'],)).fetchone()
        if not old_job or old_job['model']!=model or any(existing[field]!=value for field,value in comparisons.items()) or json.loads(existing['base_file_versions_json']).get(key)!=data['base_version']:
            raise ProjectServiceError(409,'idempotency_key_reused','修改请求编号已用于不同目标、指令、模型或上下文；请重新预览并创建新请求。')
        result = payload(conn, existing)
        result["idempotent_replay"] = True
        return result
    projection = workflow_projection(conn, project_id)
    # Targeted modification is available at any stage.
    kind = data["target_kind"]
    path = "" if kind == "outline" else data.get("target_path", "")
    version = current_version(conn, project_id, kind, path)
    if int(data["base_version"]) != version:
        raise ProjectServiceError(409, "content_conflict", "指定修改目标版本已变化，请刷新后重试。")
    content = _content(conn, project_id, kind, path)
    start, end = int(data.get("start_offset", 0)), int(data.get("end_offset", 0))
    if start < 0 or end < start or end > len(content):
        raise ProjectServiceError(422, "invalid_range", "指定修改范围超出当前内容。")
    key = _key(kind, path)
    request_db_id = uuid.uuid4().hex
    stamp = _now()
    snapshot = {
        "base_version": version,
        "target_ref": {"kind": kind, "path": path, "version": version},
        "range": {"start": start, "end": end},
        "target_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "selection_sha256": hashlib.sha256(content[start:end].encode("utf-8")).hexdigest(),
        "context_version": context["version"],
    }
    conn.execute(
        "INSERT INTO modification_requests(id,project_id,request_id,action,target_kind,target_path,start_offset,end_offset,instruction,workflow_version,base_file_versions_json,context_version,status,snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (request_db_id, project_id, data["request_id"], data.get("action", "rewrite"), kind, path, start, end, data["instruction"], projection["version"], json.dumps({key: version}), context["version"], "queued", json.dumps(snapshot, ensure_ascii=False), stamp, stamp),
    )
    job, created = create_job(
        conn, project_id, request_id=f"modification:{data['request_id']}", action=data.get("action", "rewrite"),
        input_data={"prompt": data["instruction"], "selection": content[start:end], "modification_request_id": request_db_id},
        scope={"targets": [{"kind": kind, "path": path, "start": start, "end": end}], "modification_request_id": request_db_id, "context_source_ids": []},
        base_file_versions={key: version}, context_version=context["version"], context_text=context["text"],
        context_sources=context["sources"] + [{"kind": "omission", **item} for item in context["omissions"]] + [{"kind": "project_revision", "version": context["project_revision"]}],
        model=model, max_active_jobs=max_active_jobs, max_jobs_per_minute=100,
    )
    conn.execute("UPDATE modification_requests SET job_id=?,status='running',updated_at=? WHERE id=?", (job["id"], _now(), request_db_id))
    if created:
        schedule_job(project_id, job["id"])
    return payload(conn, conn.execute("SELECT * FROM modification_requests WHERE id=?", (request_db_id,)).fetchone())


def get(conn: sqlite3.Connection, project_id: str, request_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM modification_requests WHERE (id=? OR request_id=?) AND project_id=?", (request_id, request_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "modification_missing", "指定修改请求不存在。")
    return payload(conn, row)


def cleanup_preview(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    scope: str,
    operation_id: str = "",
) -> dict[str, Any]:
    if scope not in {"operation", "project"}:
        raise ProjectServiceError(422, "invalid_cleanup_scope", "清理范围必须是 operation 或 project。")
    if scope == "operation" and not operation_id:
        raise ProjectServiceError(422, "operation_id_required", "按操作清理时必须提供 operation_id。")
    if scope == "operation":
        rows = conn.execute(
            "SELECT * FROM modification_requests WHERE project_id=? AND (id=? OR request_id=?)",
            (project_id, operation_id, operation_id),
        ).fetchall()
        if not rows:
            raise ProjectServiceError(404, "modification_missing", "指定修改请求不存在。")
    else:
        rows = conn.execute(
            "SELECT * FROM modification_requests WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()

    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    retained_applied = 0
    for row in rows:
        item = payload(conn, row)
        status = item["status"]
        if status in {"applied", "partially_applied"}:
            retained_applied += 1
            continue
        if status == "purged":
            continue
        if status in {"running", "queued"} or (item.get("job") or {}).get("status") in {"running", "cancel_requested"}:
            blocked.append({"id": item["id"], "request_id": item["request_id"], "status": status})
            continue
        eligible.append({
            "id": item["id"],
            "request_id": item["request_id"],
            "status": status,
            "target_path": item["target_path"],
            "has_candidate": bool(item.get("candidate_id")),
            "has_job": bool(item.get("job_id")),
        })
    result = {
        "project_id": project_id,
        "scope": scope,
        "operation_id": operation_id,
        "eligible": eligible,
        "blocked_active": blocked,
        "retained_applied_count": retained_applied,
        "counts": {
            "operations": len(eligible),
            "candidates": sum(item["has_candidate"] for item in eligible),
            "jobs_and_prompts": sum(item["has_job"] for item in eligible),
        },
        "warning": "清理会删除未采纳候选、其专属提示和未共享上下文；这些操作将不能恢复或重试。已采纳修订、正文版本、撤销/重做记录及被其他操作引用的快照会保留。",
    }
    fingerprint_payload = {
        "project_id": project_id,
        "scope": scope,
        "operation_id": operation_id,
        "eligible": eligible,
        "blocked_active": blocked,
        "retained_applied_count": retained_applied,
    }
    result["preview_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return result


def cleanup(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    scope: str,
    operation_id: str,
    request_id: str,
    preview_fingerprint: str,
) -> dict[str, Any]:
    replay = conn.execute(
        "SELECT scope,operation_id,result_json FROM modification_cleanup_events WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if replay:
        if replay["scope"] != scope or replay["operation_id"] != operation_id:
            raise ProjectServiceError(409, "cleanup_idempotency_conflict", "该 request_id 已用于不同的清理范围。")
        return json.loads(replay["result_json"]) | {"idempotent_replay": True}
    preview = cleanup_preview(conn, project_id, scope=scope, operation_id=operation_id)
    if not preview_fingerprint or preview["preview_fingerprint"] != preview_fingerprint:
        raise ProjectServiceError(409, "cleanup_preview_stale", "清理目标或状态已变化，请重新预览后确认。")
    purged: list[str] = []
    preserved_shared_jobs = 0
    for item in preview["eligible"]:
        row = conn.execute(
            "SELECT * FROM modification_requests WHERE id=? AND project_id=?",
            (item["id"], project_id),
        ).fetchone()
        if row is None:
            continue
        job_id = row["job_id"]
        if job_id:
            shared = conn.execute(
                "SELECT COUNT(*) AS count FROM modification_requests WHERE project_id=? AND job_id=? AND id<>?",
                (project_id, job_id, row["id"]),
            ).fetchone()["count"]
            if int(shared):
                preserved_shared_jobs += 1
            else:
                job = conn.execute(
                    "SELECT context_snapshot_id FROM ai_jobs WHERE id=? AND project_id=?",
                    (job_id, project_id),
                ).fetchone()
                snapshot_id = job["context_snapshot_id"] if job else None
                conn.execute("UPDATE modification_requests SET job_id=NULL,candidate_id=NULL WHERE id=?", (row["id"],))
                conn.execute("DELETE FROM ai_jobs WHERE id=? AND project_id=?", (job_id, project_id))
                if snapshot_id:
                    other_users = conn.execute(
                        "SELECT COUNT(*) AS count FROM ai_jobs WHERE context_snapshot_id=?",
                        (snapshot_id,),
                    ).fetchone()["count"]
                    if not int(other_users):
                        conn.execute(
                            "DELETE FROM context_snapshots WHERE id=? AND project_id=?",
                            (snapshot_id, project_id),
                        )
        conn.execute(
            "UPDATE modification_requests SET candidate_id=NULL,instruction='',snapshot_json='{}',status='purged',updated_at=? WHERE id=? AND project_id=?",
            (_now(), row["id"], project_id),
        )
        purged.append(row["request_id"])
    result = {
        "project_id": project_id,
        "scope": scope,
        "operation_id": operation_id,
        "purged_request_ids": purged,
        "purged_count": len(purged),
        "retained_applied_count": preview["retained_applied_count"],
        "preserved_shared_jobs": preserved_shared_jobs,
        "preview_fingerprint": preview["preview_fingerprint"],
        "idempotent_replay": False,
    }
    conn.execute(
        "INSERT INTO modification_cleanup_events(id,project_id,request_id,scope,operation_id,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, project_id, request_id, scope, operation_id, json.dumps(result, ensure_ascii=False), _now()),
    )
    return result

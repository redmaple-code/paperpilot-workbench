from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from paperpilot.services.ai_jobs import candidate_payload, create_job
from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import workflow_projection
from paperpilot.services import outline as outline_service
from paperpilot.services import continuation_budget
import hashlib
import os


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def request_prompt(payload):
    prompt = payload.get('prompt') or payload['goal']
    if payload.get('subgoals'):
        prompt += '\n本批逐段目标（只生成当前指定子目标，不自动改动其他章节）：' + _json(payload['subgoals'])
    return prompt


def stale_outline_reasons(conn, project_id, batch_id):
    batch = conn.execute('SELECT outline_version,outline_node_id FROM continuation_batches WHERE id=? AND project_id=?', (batch_id, project_id)).fetchone()
    if not batch:
        return ['续写批次不可用']
    graph = outline_service.read_outline(conn, project_id)
    if batch['outline_version'] != graph['version']:
        return [f"结构化大纲已从v{batch['outline_version']}变为v{graph['version']}，请重新预览并生成"]
    return []


def check_outline(conn, project_id, batch_id):
    reasons = stale_outline_reasons(conn, project_id, batch_id)
    if reasons:
        raise ProjectServiceError(409, 'continuation_outline_stale', reasons[0])


def _target_key(kind: str, path: str) -> str:
    return "__outline__" if kind == "outline" else path


def _read_content(conn: sqlite3.Connection, project_id: str, kind: str, path: str) -> str:
    row = conn.execute(
        "SELECT content FROM outline WHERE project_id=?" if kind == "outline" else
        "SELECT content FROM files WHERE project_id=? AND path=?",
        (project_id,) if kind == "outline" else (project_id, path),
    ).fetchone()
    if row is None:
        raise ProjectServiceError(404, "target_missing", "续写目标不存在。")
    return str(row["content"])


def _segment_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["usage"] = json.loads(item.pop("usage_json") or "{}")
    job = None
    candidate = None
    if item.get("job_id"):
        job_row = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (item["job_id"],)).fetchone()
        if job_row:
            if job_row["candidate_id"] and not item.get("candidate_id"):
                item["candidate_id"] = job_row["candidate_id"]
                conn.execute("UPDATE continuation_segments SET candidate_id=?,updated_at=? WHERE id=?", (job_row["candidate_id"], _now(), item["id"]))
            job = {
                "id": job_row["id"], "request_id": job_row["request_id"], "status": job_row["status"],
                "action": job_row["action"], "error_code": job_row["error_code"], "attempt": job_row["attempt"],
                "candidate_id": job_row["candidate_id"], "workflow_version": job_row["workflow_version"],
                "updated_at": job_row["updated_at"],
                'provider_snapshot':json.loads(job_row['scope_json']).get('provider_snapshot'),
                'egress_state':json.loads(job_row['scope_json']).get('egress_state','unknown'),
            }
    if item.get("candidate_id"):
        candidate_row = conn.execute("SELECT * FROM draft_candidates WHERE id=?", (item["candidate_id"],)).fetchone()
        if candidate_row:
            candidate = candidate_payload(conn, candidate_row)
    if candidate:
        item["status"] = candidate["status"]
    elif job:
        item["status"] = {
            "running": "running", "cancel_requested": "cancelling", "succeeded": "candidate_ready",
            "failed": "failed", "cancelled": "cancelled",
        }.get(job["status"], item["status"])
    item["job"] = job
    item["candidate"] = candidate
    return item


def _sync_batch_status(conn: sqlite3.Connection, batch_id: str) -> str:
    batch = conn.execute("SELECT status FROM continuation_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise ProjectServiceError(404, "continuation_missing", "续写批次不存在。")
    if batch["status"] in {"cancelled", "paused"}:
        return batch["status"]
    rows = conn.execute("SELECT status,job_id,candidate_id FROM continuation_segments WHERE batch_id=?", (batch_id,)).fetchall()
    if not rows:
        return batch["status"]
    statuses = []
    for row in rows:
        status = row["status"]
        if row["candidate_id"]:
            candidate = conn.execute("SELECT status FROM draft_candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if candidate:
                status = candidate["status"]
        elif row["job_id"]:
            job = conn.execute("SELECT status FROM ai_jobs WHERE id=?", (row["job_id"],)).fetchone()
            if job:
                status = {"running": "running", "succeeded": "candidate_ready", "failed": "failed", "cancelled": "cancelled"}.get(job["status"], status)
        statuses.append(status)
    if all(status in {"applied", "rejected", "stale", "cancelled"} for status in statuses):
        new_status = "completed"
    elif any(status == "running" for status in statuses):
        new_status = "running"
    elif any(status == "failed" for status in statuses):
        # A retained candidate must not hide an interrupted sibling or strand
        # the batch outside the explicit, duplicate-cost-aware resume path.
        new_status = "paused"
        conn.execute("UPDATE continuation_batches SET pause_reason='segment_failed' WHERE id=?", (batch_id,))
    elif any(status == "candidate_ready" for status in statuses):
        new_status = "review"
    else:
        new_status = "queued"
    conn.execute("UPDATE continuation_batches SET status=?,updated_at=? WHERE id=?", (new_status, _now(), batch_id))
    return new_status


def batch_payload(conn: sqlite3.Connection, batch_id: str, project_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM continuation_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "continuation_missing", "续写批次不存在。")
    _sync_batch_status(conn, batch_id)
    row = conn.execute("SELECT * FROM continuation_batches WHERE id=?", (batch_id,)).fetchone()
    item = dict(row)
    item["base_file_versions"] = json.loads(item.pop("base_file_versions_json") or "{}")
    item["budget"] = json.loads(item.pop("budget_json") or "{}")
    item["segments"] = [
        _segment_payload(conn, segment)
        for segment in conn.execute("SELECT * FROM continuation_segments WHERE batch_id=? ORDER BY sequence", (batch_id,))
    ]
    counts: dict[str, int] = {}
    for segment in item["segments"]:
        counts[segment["status"]] = counts.get(segment["status"], 0) + 1
    item["progress"] = {"total": len(item["segments"]), "counts": counts}
    item['budget']['usage'] = continuation_budget.usage(conn, batch_id)
    item['outline_stale_reasons'] = stale_outline_reasons(conn, project_id, batch_id)
    return item


def _insert_event(conn: sqlite3.Connection, batch_id: str, request_id: str, event_type: str, status: str, *, segment_id: str | None = None, payload: dict[str, Any] | None = None, before_version: int | None = None, after_version: int | None = None) -> None:
    conn.execute(
        "INSERT INTO continuation_events(id,batch_id,segment_id,request_id,event_type,status,before_version,after_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, batch_id, segment_id, request_id, event_type, status, before_version, after_version, _json(payload or {}), _now()),
    )


def create_batch(
    conn: sqlite3.Connection,
    project_id: str,
    payload: dict[str, Any],
    *,
    bundle: dict[str, Any],
    model: str,
    context_snapshot: dict[str, Any],
    max_output_tokens: int,
    max_job_tokens: int,
    max_active_jobs: int,
    schedule_job,
) -> dict[str, Any]:
    input_fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
    replay = conn.execute("SELECT * FROM continuation_batches WHERE project_id=? AND request_id=?", (project_id, payload["request_id"])).fetchone()
    if replay:
        if replay['input_fingerprint'] and replay['input_fingerprint'] != input_fingerprint:
            raise ProjectServiceError(409, 'idempotency_key_reused', '批次目标、分段或硬预算已变化，请使用新请求。')
        path='' if payload['target_kind']=='outline' else payload.get('target_path','')
        fields={'target_kind':payload['target_kind'],'target_path':path,'anchor_start':payload.get('anchor_start',0),'anchor_end':payload.get('anchor_end',0),
            'goal':payload['goal'],'prompt':payload.get('prompt',''),'segment_count':payload.get('segment_count',1),'outline_node_id':payload.get('outline_node_id',''),
            'outline_version':payload.get('outline_version'),'model':model,'context_version':context_snapshot['version']}
        if any(replay[field]!=value for field,value in fields.items()) or json.loads(replay['base_file_versions_json']).get(_target_key(payload['target_kind'],path))!=payload['base_version']:
            raise ProjectServiceError(409,'idempotency_key_reused','续写编号已用于不同目标、模型、上下文或批次范围，请重新预览并创建新请求。')
        return batch_payload(conn, replay["id"], project_id) | {"idempotent_replay": True}
    node_id = str(payload.get("outline_node_id") or "").strip()
    version = payload.get("outline_version")
    if not node_id or version is None:
        raise ProjectServiceError(422, "outline_selection_required", "请先选择当前论文的结构化大纲节点；如无节点，请到大纲页创建或迁移。")
    outline = outline_service.read_outline(conn, project_id)
    if int(version) != outline["version"]:
        raise ProjectServiceError(409, "outline_version_conflict", "大纲已更新，请刷新节点列表并重新预览。")
    if not any(node["id"] == node_id for node in outline["nodes"]):
        raise ProjectServiceError(422, "outline_node_missing", "所选节点不存在或不属于当前论文，请刷新节点列表。")
    projection = workflow_projection(conn, project_id)
    # Continuation is available at any stage.
    kind = payload["target_kind"]
    path = "" if kind == "outline" else payload.get("target_path", "")
    key = _target_key(kind, path)
    current = current_version(conn, project_id, kind, path)
    if int(payload["base_version"]) != current:
        raise ProjectServiceError(409, "content_conflict", f"{key} 的基础版本已变化，请刷新后重试。")
    content = _read_content(conn, project_id, kind, path)
    start, end = int(payload.get("anchor_start", 0)), int(payload.get("anchor_end", 0))
    if start < 0 or end < start or end > len(content):
        raise ProjectServiceError(422, "invalid_range", "续写范围超出当前内容。")
    count = int(payload.get("segment_count", 1))
    if count < 1 or count > 20:
        raise ProjectServiceError(422, "invalid_segment_count", "续写分段数量必须在 1 到 20 之间。")
    subgoals = [dict(g) for g in payload.get('subgoals', [])] or [{'goal': payload['goal'], 'outline_node_id': node_id, 'anchor_start': start, 'anchor_end': end} for _ in range(count)]
    if len(subgoals) != count:
        raise ProjectServiceError(422, 'subgoal_count_mismatch', '每个续写分段须对应一个子目标。')
    nodes = {n['id'] for n in outline['nodes']}
    for goal in subgoals:
        if goal['outline_node_id'] not in nodes:
            raise ProjectServiceError(422, 'outline_node_missing', '子目标节点不属于当前论文的大纲。')
        goal['anchor_start'] = start if goal.get('anchor_start') is None else goal['anchor_start']
        goal['anchor_end'] = end if goal.get('anchor_end') is None else goal['anchor_end']
        if not 0 <= goal['anchor_start'] <= goal['anchor_end'] <= len(content):
            raise ProjectServiceError(422, 'invalid_range', '子目标锚点超出当前文件范围。')
    estimate = int(context_snapshot.get("estimated_input_tokens", 0) or 0)
    total_estimate = (estimate + max_output_tokens) * count
    from paperpilot.services import provider_runtime
    provider = provider_runtime.request_provider(conn, project_id, model)
    pricing = provider.get('model_limits', {}).get(model, {}).get('pricing')
    estimated_cost = continuation_budget.price(pricing, estimate*count, max_output_tokens*count)
    amount = payload.get('hard_budget_amount')
    budget = {
        "estimated_input_tokens_per_segment": estimate, "estimated_total_tokens": total_estimate,
        "max_output_tokens": max_output_tokens, "max_job_tokens": max_job_tokens,
        "cost_status": "unknown", "status": "within_limit" if total_estimate <= max_job_tokens else "blocked",
        'hard_budget_amount': amount, 'pricing': pricing, 'estimated_cost': estimated_cost,
        'currency': pricing.get('currency') if pricing else None,
        'pricing_source': pricing.get('source') if pricing else None,
        'pricing_verification': 'user_provided_unverified' if pricing else 'unknown',
    }
    if pricing:
        budget['cost_status'] = 'estimated'
    money_allowed = amount is None or (estimated_cost is not None and estimated_cost <= amount)
    status = "running" if total_estimate <= max_job_tokens and money_allowed else "paused"
    budget['status'] = 'within_limit' if status == 'running' else 'blocked'
    pause_reason = "" if status == "running" else "budget_exceeded"
    batch_id = uuid.uuid4().hex
    stamp = _now()
    context_id = uuid.uuid4().hex
    conn.execute(
        "INSERT OR IGNORE INTO context_snapshots(id,project_id,context_version,source_manifest_json,context_text,created_at) VALUES(?,?,?,?,?,?)",
        (context_id, project_id, context_snapshot["version"], _json(context_snapshot["sources"] + [{"kind": "omission", **item} for item in context_snapshot["omissions"]] + [{"kind": "project_revision", "version": context_snapshot["project_revision"]}]), context_snapshot["text"], stamp),
    )
    conn.execute(
        "INSERT INTO continuation_batches(id,project_id,request_id,target_kind,target_path,anchor_start,anchor_end,goal,prompt,segment_count,outline_node_id,outline_version,workflow_version,base_file_versions_json,context_version,model,budget_json,status,pause_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, project_id, payload["request_id"], kind, path, start, end, payload["goal"], payload.get("prompt", ""), count, payload.get("outline_node_id", ""), payload.get("outline_version"), projection["version"], _json({key: current}), context_snapshot["version"], model or "", _json(budget), status, pause_reason, stamp, stamp),
    )
    conn.execute('UPDATE continuation_batches SET input_fingerprint=?,subgoals_json=? WHERE id=?', (input_fingerprint, _json(payload.get('subgoals',[])), batch_id))
    for sequence, goal in enumerate(subgoals):
        segment_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO continuation_segments(id,batch_id,sequence,start_offset,end_offset,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (segment_id, batch_id, sequence, goal['anchor_start'], goal['anchor_end'], "queued" if status == "running" else "paused", stamp, stamp),
        )
        conn.execute('UPDATE continuation_segments SET goal=?,outline_node_id=? WHERE id=?', (goal['goal'], goal['outline_node_id'], segment_id))
    _insert_event(conn, batch_id, payload["request_id"], "start", status, payload={"segment_count": count, "budget": budget})
    if status == "running":
        for segment in conn.execute("SELECT * FROM continuation_segments WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall():
            segment_request = f"{payload['request_id']}:segment:{segment['sequence']}"
            selection = content[segment['start_offset']:segment['end_offset']]
            scope = {"targets": [{"kind": kind, "path": path, "start": segment['start_offset'], "end": segment['end_offset']}], "continuation_batch_id": batch_id, "continuation_segment_id": segment["id"], "context_source_ids": [], 'max_output_tokens': max_output_tokens}
            job, created = create_job(
                conn, project_id, request_id=segment_request, action="continue",
                input_data={"prompt": request_prompt(payload) + ('\n本段目标：'+segment['goal'] if payload.get('subgoals') else ''), "selection": selection, "continuation_batch_id": batch_id, "continuation_segment_id": segment["id"]},
                scope=scope, base_file_versions={key: current}, context_version=context_snapshot["version"], context_text=context_snapshot["text"],
                context_sources=context_snapshot["sources"] + [{"kind": "omission", **item} for item in context_snapshot["omissions"]] + [{"kind": "project_revision", "version": context_snapshot["project_revision"]}],
                model=model, max_active_jobs=max_active_jobs, max_jobs_per_minute=100,
            )
            conn.execute("UPDATE continuation_segments SET job_id=?,status='running',updated_at=? WHERE id=?", (job["id"], _now(), segment["id"]))
            if created:
                schedule_job(project_id, job["id"])
    return batch_payload(conn, batch_id, project_id)


def command_batch(conn: sqlite3.Connection, project_id: str, batch_id: str, command: str, request_id: str, *, schedule_job, budget_override: bool = False, duplicate_cost_confirmed: bool = False, hard_budget_tokens=None, hard_budget_amount=None) -> dict[str, Any]:
    command_identity = {'budget_override': budget_override, 'duplicate_cost_confirmed': duplicate_cost_confirmed,
                        'hard_budget_tokens': hard_budget_tokens, 'hard_budget_amount': hard_budget_amount}
    existing = conn.execute("SELECT * FROM continuation_events WHERE batch_id=? AND request_id=?", (batch_id, request_id)).fetchone()
    if existing:
        if existing['event_type']!=command or (json.loads(existing['payload_json'] or '{}').get('command_identity') not in (None,command_identity)):raise ProjectServiceError(409,'idempotency_key_reused','此编号已经用于不同批次操作或预算设置，请核对后使用新编号。')
        return batch_payload(conn, batch_id, project_id) | {"idempotent_replay": True}
    row = conn.execute("SELECT * FROM continuation_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "continuation_missing", "续写批次不存在。")
    status = row["status"]
    if command == "pause":
        if status not in {"running", "review"}:
            raise ProjectServiceError(409, "invalid_transition", "只有运行中或待审阅批次可以暂停。")
        conn.execute("UPDATE continuation_batches SET status='paused',pause_reason='user_paused',updated_at=? WHERE id=?", (_now(), batch_id))
        for segment in conn.execute("SELECT job_id FROM continuation_segments WHERE batch_id=? AND job_id IS NOT NULL", (batch_id,)):
            conn.execute("UPDATE ai_jobs SET cancel_requested=1,status='cancel_requested',updated_at=? WHERE id=? AND status='running'", (_now(), segment["job_id"]))
        _insert_event(conn, batch_id, request_id, "pause", "paused")
    elif command == "cancel":
        if status in {"completed", "cancelled"}:
            raise ProjectServiceError(409, "invalid_transition", "批次已经结束。")
        conn.execute("UPDATE continuation_batches SET status='cancelled',pause_reason='user_cancelled',updated_at=? WHERE id=?", (_now(), batch_id))
        for segment in conn.execute("SELECT job_id FROM continuation_segments WHERE batch_id=? AND job_id IS NOT NULL", (batch_id,)):
            conn.execute("UPDATE ai_jobs SET cancel_requested=1,status='cancel_requested',updated_at=? WHERE id=? AND status='running'", (_now(), segment["job_id"]))
        _insert_event(conn, batch_id, request_id, "cancel", "cancelled")
    elif command == "resume":
        if status != "paused":
            raise ProjectServiceError(409, "invalid_transition", "只有暂停批次可以恢复。")
        budget = json.loads(row["budget_json"] or "{}")
        check_outline(conn, project_id, batch_id)
        if budget_override:
            try: default_limit = max(1,int(os.getenv('PWA_AI_MAX_JOB_TOKENS','16000')))
            except ValueError: default_limit = 16000
            budget['max_job_tokens'] = hard_budget_tokens if hard_budget_tokens is not None else max(budget['max_job_tokens'], default_limit*row['segment_count'])
            if hard_budget_amount is not None:
                budget['hard_budget_amount'] = hard_budget_amount
            if budget.get('hard_budget_amount') is not None and not budget.get('pricing'):
                raise ProjectServiceError(409, 'budget_unverifiable', '金额硬限制需要有来源的固定费率；请配置费率并重新预览新批次。')
            budget['status'] = 'within_limit'
            conn.execute('UPDATE continuation_batches SET budget_json=? WHERE id=?', (_json(budget), batch_id))
        elif hard_budget_tokens is not None or hard_budget_amount is not None:
            raise ProjectServiceError(422, 'budget_change_confirmation_required', '调整整批上限须明确确认。')
        if budget.get("status") == "blocked" and not budget_override:
            raise ProjectServiceError(409, "budget_exceeded", "当前批次超过硬预算上限；请缩小批次或明确覆盖后恢复。")
        projection = workflow_projection(conn, project_id)
        # Resuming continuation is not stage-bound.
        bases = json.loads(row["base_file_versions_json"] or "{}")
        for key, expected in bases.items():
            kind, path = ("outline", "") if key == "__outline__" else ("file", key)
            if current_version(conn, project_id, kind, path) != int(expected):
                raise ProjectServiceError(409, "content_conflict", "批次基础版本已变化；请重新创建续写批次。")
        context = conn.execute("SELECT * FROM context_snapshots WHERE context_version=? AND project_id=? ORDER BY created_at DESC LIMIT 1", (row["context_version"], project_id)).fetchone()
        if context is None:
            raise ProjectServiceError(409, "context_missing", "原上下文快照不可用，不能恢复。")
        for segment in conn.execute("SELECT * FROM continuation_segments WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall():
            job = conn.execute("SELECT status,error_code,scope_json FROM ai_jobs WHERE id=?", (segment["job_id"],)).fetchone() if segment["job_id"] else None
            if job and (job['error_code']=='provider_result_unknown' or (job['error_code']=='interrupted' and json.loads(job['scope_json']).get('egress_state')=='unknown')) and not duplicate_cost_confirmed:
                raise ProjectServiceError(409,'duplicate_cost_confirmation_required','此批次有远端结果/费用不明的请求，请核对后明确接受可能重复费用，再重新预检恢复。')
            if segment["candidate_id"] or (job and job["status"] in {"running", "succeeded"}):
                continue
            request = f"{row['request_id']}:resume:{segment['sequence']}:{uuid.uuid4().hex[:8]}"
            target = {"kind": row["target_kind"], "path": row["target_path"], "start": segment['start_offset'], "end": segment['end_offset']}
            selection = _read_content(conn, project_id, row["target_kind"], row["target_path"])[segment['start_offset']:segment['end_offset']]
            created, is_new = create_job(
                conn, project_id, request_id=request, action="continue",
                input_data={"prompt": request_prompt({'prompt': row['prompt'], 'goal': row['goal'], 'subgoals': json.loads(row['subgoals_json'])})+'\n本段目标：'+(segment['goal'] or row['goal']), "selection": selection, "continuation_batch_id": batch_id, "continuation_segment_id": segment["id"]},
                scope={"targets": [target], "continuation_batch_id": batch_id, "continuation_segment_id": segment["id"], "context_source_ids": [], 'max_output_tokens': budget['max_output_tokens']},
                base_file_versions=bases, context_version=context["context_version"], context_text=context["context_text"], context_sources=json.loads(context["source_manifest_json"] or "[]"),
                model=row["model"], max_active_jobs=int(row["segment_count"]), max_jobs_per_minute=100,
            )
            conn.execute("UPDATE continuation_segments SET job_id=?,status='running',error_code='',updated_at=? WHERE id=?", (created["id"], _now(), segment["id"]))
            if is_new:
                schedule_job(project_id, created["id"])
        conn.execute("UPDATE continuation_batches SET status='running',pause_reason='',updated_at=? WHERE id=?", (_now(), batch_id))
        _insert_event(conn, batch_id, request_id, "resume", "running", payload={'command_identity':command_identity})
    else:
        raise ProjectServiceError(422, "invalid_command", "不支持的续写批次命令。")
    return batch_payload(conn, batch_id, project_id)


def record_decision(conn: sqlite3.Connection, project_id: str, batch_id: str, segment_id: str, request_id: str, decision: str, result: dict[str, Any]) -> dict[str, Any]:
    segment = conn.execute("SELECT * FROM continuation_segments WHERE id=? AND batch_id=?", (segment_id, batch_id)).fetchone()
    if segment is None:
        raise ProjectServiceError(404, "segment_missing", "续写分段不存在。")
    if decision == "accept" or decision == "partial_apply":
        status = "applied"
    elif decision == "reject":
        status = "rejected"
    else:
        status = "failed"
    conn.execute("UPDATE continuation_segments SET status=?,updated_at=? WHERE id=?", (status, _now(), segment_id))
    _insert_event(conn, batch_id, request_id, "segment_decision", status, segment_id=segment_id, payload={"decision": decision, "result": result})
    return batch_payload(conn, batch_id, project_id)

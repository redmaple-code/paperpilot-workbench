from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from paperpilot.services.content_versions import current_version
from paperpilot.services.candidate_validation import validate_candidate
from paperpilot.services.human_revision import active_mode, record_event, update_event_status
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.workflow import append_snapshot, current_snapshot
from paperpilot.services.context import style_context_current
from paperpilot.services.context import context_snapshot_current


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    """Conservative display estimate; providers may report exact usage separately."""
    value = text or ""
    non_ascii = sum(ord(character) > 127 for character in value)
    ascii_count = len(value) - non_ascii
    return max(1, math.ceil(ascii_count / 3 + non_ascii * 1.5))


def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    if job.get('status') in {'running','cancel_requested'}:
        from paperpilot.services.tex_runtime import owner_state
        job['recovery_state'] = owner_state(job)
    job.pop('owner_pid', None)
    job.pop('owner_token', None)
    for field, output in (
        ("input_json", "input"),
        ("scope_json", "scope"),
        ("base_file_versions_json", "base_file_versions"),
        ("usage_json", "usage"),
    ):
        job[output] = json.loads(job.pop(field) or "{}")
    return job


def candidate_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    candidate = dict(row)
    candidate["scope"] = json.loads(candidate.pop("scope_json") or "{}")
    candidate["base_file_versions"] = json.loads(candidate.pop("base_file_versions_json") or "{}")
    candidate["patches"] = json.loads(candidate.pop("content_json") or "[]")
    stale = candidate["status"] == "stale"
    for patch in candidate["patches"]:
        key = "__outline__" if patch["kind"] == "outline" else patch["path"]
        base_version = int(candidate["base_file_versions"].get(key, 0))
        base_row = conn.execute(
            "SELECT content FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?",
            (candidate["project_id"], patch["kind"], patch["path"], base_version),
        ).fetchone()
        if patch["kind"] == "outline":
            current_row = conn.execute("SELECT content FROM outline WHERE project_id=?", (candidate["project_id"],)).fetchone()
        else:
            current_row = conn.execute(
                "SELECT content FROM files WHERE project_id=? AND path=?",
                (candidate["project_id"], patch["path"]),
            ).fetchone()
        actual_version = current_version(conn, candidate["project_id"], patch["kind"], patch["path"])
        patch["base_version"] = base_version
        patch["base_content"] = str(base_row["content"]) if base_row else ""
        patch["current_version"] = actual_version
        patch["current_content"] = str(current_row["content"]) if current_row else None
        if actual_version != base_version:
            stale = True
    candidate["validation"] = validate_candidate(conn, candidate["project_id"], candidate["patches"])
    job = conn.execute("SELECT model,scope_json,usage_json FROM ai_jobs WHERE id=?", (candidate["job_id"],)).fetchone()
    job_scope = json.loads(job["scope_json"] or "{}") if job else {}
    candidate["model"] = job["model"] if job else ""
    candidate["usage"] = json.loads(job["usage_json"] or "{}") if job else {}
    candidate["cost"] = {
        "status": "unknown",
        "reason": "未配置可信模型费率；显示可得用量和字符/4 的 token 估算。",
        "estimated_input_tokens": job_scope.get("estimated_input_tokens"),
        "max_output_tokens": job_scope.get("max_output_tokens"),
        "max_job_tokens": job_scope.get("max_job_tokens"),
    }
    snapshot = conn.execute(
        "SELECT source_manifest_json FROM context_snapshots WHERE id=?",
        (candidate.get("context_snapshot_id"),),
    ).fetchone()
    manifest_items = json.loads(snapshot["source_manifest_json"]) if snapshot else []
    from paperpilot.services import innovation_points
    if not innovation_points.references_current(conn, candidate['project_id'], innovation_points.manifest_references(manifest_items)):
        stale = True
    if not style_context_current(conn, candidate['project_id'], manifest_items):
        stale = True
    revision_item = next((item for item in manifest_items if item.get("kind") == "project_revision"), {})
    candidate["context_sources"] = [item for item in manifest_items if item.get("kind") != "project_revision"]
    candidate["context_revision"] = revision_item.get("version")
    current_project = conn.execute(
        "SELECT project_revision FROM projects WHERE id=?", (candidate["project_id"],)
    ).fetchone()
    if current_project and int(revision_item.get("version", current_project["project_revision"])) != int(current_project["project_revision"]):
        stale = True
    candidate["stale"] = stale
    if job_scope.get('continuation_batch_id'):
        batch = conn.execute('SELECT budget_json FROM continuation_batches WHERE id=? AND project_id=?',(job_scope['continuation_batch_id'],candidate['project_id'])).fetchone()
        budget = json.loads(batch['budget_json']) if batch else {}
        pricing = budget.get('pricing')
        if pricing:
            from paperpilot.services.continuation_budget import price
            usage = candidate['usage']
            actual_input = usage.get('prompt_tokens',usage.get('input_tokens'))
            actual_output = usage.get('completion_tokens',usage.get('output_tokens'))
            actual = price(pricing,actual_input,actual_output) if all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (actual_input,actual_output)) else None
            candidate['cost'].update({'status':'actual_usage_estimate' if actual is not None else 'estimated','currency':pricing['currency'],'pricing_source':pricing['source'],
                'amount':actual if actual is not None else price(pricing,job_scope.get('estimated_input_tokens') or 0,job_scope.get('max_output_tokens') or budget['max_output_tokens']),
                'reason':'按本批次固定的用户登记费率估算；实际账单以服务方为准。'})
        from paperpilot.services.continuation import stale_outline_reasons
        reasons = stale_outline_reasons(conn, candidate['project_id'], job_scope['continuation_batch_id'])
        candidate['stale_reasons'] = reasons
        candidate['stale'] = stale or bool(reasons)
    return candidate


def create_job(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    request_id: str,
    action: str,
    input_data: dict[str, Any],
    scope: dict[str, Any],
    base_file_versions: dict[str, int],
    context_version: str,
    context_text: str,
    context_sources: list[dict[str, Any]],
    model: str,
    max_active_jobs: int = 3,
    max_jobs_per_minute: int = 20,
) -> tuple[dict[str, Any], bool]:
    from paperpilot.services import provider_runtime
    scope=dict(scope)
    scope.setdefault('egress_state','not_sent')
    if not context_snapshot_current(conn,project_id,context_sources,base_versions=base_file_versions):
        raise ProjectServiceError(409,'context_permission_stale','上下文素材或授权已变化，请重新预览；旧快照不能继续发送。')
    if scope.get('provider_snapshot'):
        admission=scope['provider_snapshot']
        config=provider_runtime.verify_request(conn,admission)
    else:
        source=next((s for s in context_sources if s.get('kind')=='provider'),None)
        if source:
            if source['model']!=model:raise ProjectServiceError(409,'provider_context_conflict','模型与已预览接收方不一致，请重新预览。')
            admission=provider_runtime.snapshot_for(conn,source['id'],source['version'],model)
        else:admission=provider_runtime.request_provider(conn,project_id,model)
        config=provider_runtime.verify_request(conn,admission)
        scope['provider_snapshot']=admission
    contents={}
    for target in scope.get('targets',[]):
        key='__outline__' if target['kind']=='outline' else target['path']
        original=conn.execute('SELECT content FROM content_versions WHERE project_id=? AND target_kind=? AND target_path=? AND version=?',
            (project_id,target['kind'],target['path'],base_file_versions.get(key))).fetchone()
        contents[key]=original['content'] if original else ''
    # Include target text, selection and protocol framing; actual transport is
    # checked again immediately before sending, using its assembled messages.
    input_estimate=estimate_tokens(context_text+str(input_data.get('prompt',''))+str(input_data.get('selection',''))+json.dumps(contents,ensure_ascii=False))+256
    scope['capacity_admission']=provider_runtime.admit_tokens(config,model,input_estimate,int(scope.get('max_output_tokens') or 4000))
    existing = conn.execute(
        "SELECT * FROM ai_jobs WHERE project_id=? AND request_id=?",
        (project_id, request_id),
    ).fetchone()
    if existing:
        snapshot_row = conn.execute(
            "SELECT context_version FROM context_snapshots WHERE id=?",
            (existing["context_snapshot_id"],),
        ).fetchone()
        stored_scope = json.loads(existing["scope_json"] or "{}")
        request_scope_keys = {
            "targets", "context_version", "context_source_ids",
            "max_output_tokens", "max_job_tokens", "cost_status",
            'provider_snapshot',
        }
        comparable_stored_scope = {key: stored_scope.get(key) for key in request_scope_keys if key in stored_scope}
        comparable_request_scope = {key: scope.get(key) for key in request_scope_keys if key in scope}
        same_request = (
            existing["action"] == action
            and json.loads(existing["input_json"] or "{}") == input_data
            and comparable_stored_scope == comparable_request_scope
            and json.loads(existing["base_file_versions_json"] or "{}") == base_file_versions
            and existing["model"] == (model or "")
            and bool(snapshot_row and snapshot_row["context_version"] == context_version)
        )
        if not same_request:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的 AI 请求；请使用新的 request_id。")
        return _decode_job(existing), False
    # AI is not gated by the workflow stage: the author may generate at any
    # point. Incomplete planning surfaces as a reminder, never as a refusal.
    mode = active_mode(conn, project_id)
    snapshot = current_snapshot(conn, project_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    active_count = conn.execute(
        "SELECT COUNT(*) AS count FROM ai_jobs WHERE project_id=? AND status IN ('running','cancel_requested')",
        (project_id,),
    ).fetchone()["count"]
    recent_count = conn.execute(
        "SELECT COUNT(*) AS count FROM ai_jobs WHERE project_id=? AND created_at>=?",
        (project_id, cutoff),
    ).fetchone()["count"]
    if int(active_count) >= max_active_jobs or int(recent_count) >= max_jobs_per_minute:
        raise ProjectServiceError(429, "job_rate_limited", "AI 任务达到本地并发或频率上限，请等待后重试。")
    context_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex
    stamp = timestamp()
    from paperpilot.services.tex_runtime import current_owner
    try:
        owner = current_owner()
    except OSError as exc:
        raise ProjectServiceError(503, 'worker_identity_unavailable', '无法确认当前执行进程身份，任务未发送；请检查系统权限后重试。') from exc
    provider_runtime.begin_audit(conn,job_id,project_id,request_id,admission,action,context_version,scope)
    conn.execute(
        "INSERT INTO context_snapshots(id,project_id,context_version,source_manifest_json,context_text,created_at) VALUES(?,?,?,?,?,?)",
        (context_id, project_id, context_version, json.dumps(context_sources, ensure_ascii=False), context_text, stamp),
    )
    conn.execute(
        "INSERT INTO ai_jobs(id,project_id,request_id,action,status,workflow_version,cancel_requested,attempt,error_code,input_json,scope_json,base_file_versions_json,context_snapshot_id,model,candidate_id,created_at,updated_at,owner_pid,owner_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id, project_id, request_id, action, "running", snapshot["version"], 0, 1, "",
            json.dumps(input_data, ensure_ascii=False), json.dumps(scope, ensure_ascii=False),
            json.dumps(base_file_versions, ensure_ascii=False), context_id, model or "", None, stamp, stamp, owner['owner_pid'], owner['owner_token'],
        ),
    )
    record_event(
        conn, project_id, mode_id=mode["id"], action_type=action, scope=scope,
        base_file_versions=base_file_versions, status="running", request_id=request_id,
        context_snapshot_id=context_id,
    )
    return _decode_job(conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()), True


def mark_job_failed(conn: sqlite3.Connection, job_id: str, error_code: str) -> dict[str, Any]:
    conn.execute(
        "UPDATE ai_jobs SET status='failed',error_code=?,updated_at=? WHERE id=? AND status='running'",
        (error_code, timestamp(), job_id),
    )
    row = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ProjectServiceError(404, "job_missing", "AI 任务不存在。")
    job = _decode_job(row)
    return job


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    generated_content: str,
    patches: list[dict[str, str]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ProjectServiceError(404, "job_missing", "AI 任务不存在。")
    job = _decode_job(row)
    if row["status"] == "succeeded" and row["candidate_id"]:
        candidate_row = conn.execute("SELECT * FROM draft_candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        return {"job": job, "candidate": candidate_payload(conn, candidate_row) if candidate_row else None}
    if row["status"] not in {"running", "cancel_requested"}:
        return {"job": job, "candidate": None}
    if row["cancel_requested"] or row["status"] == "cancel_requested":
        conn.execute("UPDATE ai_jobs SET status='cancelled',updated_at=? WHERE id=?", (timestamp(), job_id))
        update_event_status(conn, row["project_id"], row["request_id"], "cancelled")
        return {"job": _decode_job(conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()), "candidate": None}
    snapshot = current_snapshot(conn, row["project_id"])
    mode_row = conn.execute(
        "SELECT id FROM human_revision_modes WHERE project_id=? AND status='active'",
        (row["project_id"],),
    ).fetchone()
    if snapshot["version"] != row["workflow_version"]:
        failed = mark_job_failed(conn, job_id, "workflow_changed")
        update_event_status(conn, row["project_id"], row["request_id"], "failed")
        return {"job": failed, "candidate": None}
    candidate_id = uuid.uuid4().hex
    context_row = conn.execute("SELECT context_version,source_manifest_json FROM context_snapshots WHERE id=?", (row["context_snapshot_id"],)).fetchone()
    stamp = timestamp()
    manifest_items = json.loads(context_row["source_manifest_json"] or "[]") if context_row else []
    from paperpilot.services import innovation_points
    innovation_refs = innovation_points.manifest_references(manifest_items)
    revision_item = next((item for item in manifest_items if item.get("kind") == "project_revision"), {})
    project_revision = conn.execute(
        "SELECT project_revision FROM projects WHERE id=?", (row["project_id"],)
    ).fetchone()
    stale = any(
        current_version(conn, row["project_id"], item["kind"], item["path"]) != int(
            json.loads(row["base_file_versions_json"] or "{}").get("__outline__" if item["kind"] == "outline" else item["path"], 0)
        )
        for item in patches
    )
    if project_revision and int(revision_item.get("version", project_revision["project_revision"])) != int(project_revision["project_revision"]):
        stale = True
    if not style_context_current(conn, row['project_id'], manifest_items):
        stale = True
    if not innovation_points.references_current(conn, row['project_id'], innovation_refs):
        stale = True
    candidate_status = "stale" if stale else "candidate_ready"
    conn.execute(
        "INSERT INTO draft_candidates(id,project_id,job_id,context_version,context_snapshot_id,base_file_versions_json,scope_json,content_json,status,created_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            candidate_id, row["project_id"], job_id, context_row["context_version"], row["context_snapshot_id"],
            row["base_file_versions_json"], row["scope_json"],
            json.dumps(patches, ensure_ascii=False), candidate_status, stamp, stamp if stale else None,
        ),
    )
    conn.execute(
        "UPDATE ai_jobs SET status='succeeded',candidate_id=?,usage_json=?,error_code='',updated_at=? WHERE id=?",
        (candidate_id, json.dumps(usage or {}, ensure_ascii=False), stamp, job_id),
    )
    update_event_status(conn, row["project_id"], row["request_id"], candidate_status, candidate_id)
    innovation_points.record_usage(conn, row["project_id"], innovation_refs, "writing_candidate", candidate_id,
                                   context_row["context_version"], "正文或章节写作候选")
    candidate = conn.execute("SELECT * FROM draft_candidates WHERE id=?", (candidate_id,)).fetchone()
    return {
        "job": _decode_job(conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()),
        "candidate": candidate_payload(conn, candidate),
        "generated_content": generated_content,
    }


def cancel_job(conn: sqlite3.Connection, project_id: str, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ai_jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
    if not row:
        raise ProjectServiceError(404, "job_missing", "AI 任务不存在。")
    if row["status"] in {"succeeded", "failed", "cancelled"}:
        raise ProjectServiceError(409, "job_finished", "任务已结束，不能取消。")
    conn.execute(
        "UPDATE ai_jobs SET status='cancel_requested',cancel_requested=1,updated_at=? WHERE id=?",
        (timestamp(), job_id),
    )
    return _decode_job(conn.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone())


def retry_job(
    conn: sqlite3.Connection,
    project_id: str,
    job_id: str,
    request_id: str,
    *,
    max_active_jobs: int = 3,
    max_jobs_per_minute: int = 20,
    duplicate_cost_confirmed: bool = False,
) -> tuple[dict[str, Any], bool]:
    previous = conn.execute(
        "SELECT * FROM ai_jobs WHERE id=? AND project_id=?",
        (job_id, project_id),
    ).fetchone()
    if previous is None:
        raise ProjectServiceError(404, "job_missing", "AI 任务不存在。")
    if previous["status"] not in {"failed", "cancelled"}:
        raise ProjectServiceError(409, "job_not_retryable", "只有失败或已取消的任务可以重试。")
    previous_scope=json.loads(previous['scope_json'] or '{}')
    if (previous['error_code']=='provider_result_unknown' or (previous['error_code']=='interrupted' and previous_scope.get('egress_state')=='unknown')) and not duplicate_cost_confirmed:
        raise ProjectServiceError(409,'duplicate_cost_confirmation_required','原请求远端结果与费用不明；请先核对提供方记录。再次发送可能重复执行/收费，需明确确认并重新检查预算。')
    mode = active_mode(conn, project_id)
    snapshot = current_snapshot(conn, project_id)
    # Retry is likewise not stage-bound.
    bases = json.loads(previous["base_file_versions_json"] or "{}")
    for key, version in bases.items():
        kind, path = ("outline", "") if key == "__outline__" else ("file", key)
        if current_version(conn, project_id, kind, path) != int(version):
            raise ProjectServiceError(409, "content_conflict", f"{key} 的基线已变化，请基于新版本重新生成。")
    context = conn.execute(
        "SELECT * FROM context_snapshots WHERE id=?",
        (previous["context_snapshot_id"],),
    ).fetchone()
    if context is None:
        raise ProjectServiceError(409, "context_missing", "原上下文快照已不可用，不能安全重试。")
    if not style_context_current(conn, project_id, json.loads(context['source_manifest_json'])):
        raise ProjectServiceError(409, "style_profile_revoked", "学习来源已过期、撤权或更新，请重新预览上下文并生成。")
    if not context_snapshot_current(conn,project_id,json.loads(context['source_manifest_json']),base_versions=bases,workflow_version=previous['workflow_version']):
        raise ProjectServiceError(409,'context_permission_stale','原上下文资料、授权或工作流已变化，请创建新的预览。')
    input_data = json.loads(previous["input_json"] or "{}")
    input_data["retry_of_job_id"] = job_id
    retry_scope=json.loads(previous['scope_json'] or '{}')
    retry_scope['egress_state']='not_sent'
    return create_job(
        conn, project_id, request_id=request_id, action=previous["action"],
        input_data=input_data, scope=retry_scope,
        base_file_versions=bases, context_version=context["context_version"],
        context_text=context["context_text"],
        context_sources=json.loads(context["source_manifest_json"] or "[]"), model=previous["model"],
        max_active_jobs=max_active_jobs, max_jobs_per_minute=max_jobs_per_minute,
    )


def get_candidate(conn: sqlite3.Connection, project_id: str, candidate_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM draft_candidates WHERE id=? AND project_id=?",
        (candidate_id, project_id),
    ).fetchone()
    if not row:
        raise ProjectServiceError(404, "candidate_missing", "候选稿不存在。")
    return candidate_payload(conn, row)

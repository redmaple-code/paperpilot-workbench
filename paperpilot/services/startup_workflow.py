"""REQ-021 planning embedded in REQ-001's sole immutable workflow and command ledgers."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.plan_state import (
    STAGES, confirmed_context, fresh_plan, missing_fields, plan_from_snapshot, projection_from_snapshot,
)
from paperpilot.services.workflow import append_snapshot, current_snapshot, parse_snapshot, timestamp

__all__ = ["STAGES", "confirmed_context", "fresh_plan", "missing_fields", "plan_from_snapshot", "projection_from_snapshot"]


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def projection(conn, project_id):
    result = projection_from_snapshot(current_snapshot(conn, project_id))
    result["history"] = [{"version": r["version"], "created_at": r["created_at"], "history_parent_version": r["history_parent_version"],
        "branch_id": plan_from_snapshot(parse_snapshot(r))["branch_id"]} for r in conn.execute(
            "SELECT * FROM workflow_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 50", (project_id,))]
    return result


def history(conn, project_id, version):
    row = conn.execute("SELECT * FROM workflow_snapshots WHERE project_id=? AND version=?", (project_id, version)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "history_missing", "当前论文中没有所选规划历史版本。")
    return projection_from_snapshot(parse_snapshot(row))


def begin(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def replay(conn, project_id, payload, operation):
    fingerprint = hashlib.sha256(compact({"operation": operation, **payload}).encode()).hexdigest()
    old = conn.execute("SELECT * FROM workflow_commands WHERE project_id=? AND request_id=?", (project_id, payload["request_id"])).fetchone()
    if old:
        if old["command"] != "startup."+operation or old["draft_action"] != fingerprint:
            raise ProjectServiceError(409, "idempotency_key_reused", "这个请求编号已用于不同工作流操作，请保留输入并重新提交。")
        return fingerprint, json.loads(old["result_json"]) | {"idempotent_replay": True}
    return fingerprint, None


def ensure_version(snapshot, expected):
    if snapshot["version"] != expected:
        raise ProjectServiceError(409, "workflow_conflict", "论文工作流已更新。请刷新后核对差异；本次输入不会覆盖新版本。")


def commit(conn, project_id, snapshot, plan, payload, operation, fp, *, message=""):
    from paperpilot.services.plan_state import derived_position
    position = derived_position(plan)
    # The planning plan owns its own position inside the single ledger, so the
    # snapshot's current_stage/stage_status move with it. Browsing and AI access
    # are independent of this confirmation position.
    kwargs = {"stage_outputs": {"startup_plan": plan}} if position is None else {
        "current_stage": position[0], "stage_status": position[1], "stage_outputs": {"startup_plan": plan}}
    new = append_snapshot(conn, project_id, expected_version=payload["expected_workflow_version"], **kwargs)
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (timestamp(), project_id))
    result = projection(conn, project_id) | {"message": message or "规划已保存为新版本。",
        "project_revision": conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()[0]}
    conn.execute("INSERT INTO workflow_commands(project_id,request_id,command,base_version,target_version,draft_action,result_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (project_id, payload["request_id"], "startup."+operation,
        payload["expected_workflow_version"], None, fp, compact(result), timestamp()))
    return result


def command(conn, project_id, payload):
    begin(conn)
    from paperpilot.services.agent_actions import require_writable
    require_writable(conn, project_id)
    action = payload["command"]
    fp, prior = replay(conn, project_id, payload, action)
    if prior:
        return prior
    snapshot = current_snapshot(conn, project_id)
    ensure_version(snapshot, payload["expected_workflow_version"])
    plan = plan_from_snapshot(snapshot)
    view = projection_from_snapshot(snapshot)
    target_id = payload.get("stage_id")
    target_index = next((i for i, s in enumerate(plan["stages"]) if s["id"] == target_id), None)
    independent_save = (action == "save" and plan["status"] == "active" and target_index is not None
                        and plan["stages"][target_index]["status"] not in {"confirmed", "skipped"})
    if action not in view["allowed_actions"] and not independent_save:
        raise ProjectServiceError(409, "invalid_transition", "当前规划状态不允许此操作；请按页面显示的下一步继续。")
    if action not in {"save", "review"} and (payload.get("fields") is not None or payload.get("source_refs") is not None):
        raise ProjectServiceError(422, "unreviewed_changes", "此操作不能同时修改字段；请先保存并审阅新的草稿。")
    index = target_index if independent_save else plan["current_index"]
    stage = plan["stages"][index]
    if action == "save" and stage["status"] in {"confirmed", "skipped"}:
        raise ProjectServiceError(409, "revision_required", "已确认或跳过的阶段需先显式修订；旧版本保留在历史中。")
    if action in {"save", "review", "confirm", "skip"} and target_id != stage["id"]:
        raise ProjectServiceError(409, "stage_locked", "只能处理当前阶段；其他阶段请先使用回退操作。")
    if plan["branch_id"] is None:
        plan["branch_id"] = uuid.uuid4().hex
    message = ""
    if action == "start":
        plan["status"] = "active"
        if plan["branch_id"] is None:
            plan["branch_id"] = uuid.uuid4().hex
        message = "已开始启动规划；请从当前阶段填写并提交审阅。"
    elif action in {"save", "review"}:
        fields = payload.get("fields")
        if fields is not None:
            allowed = STAGES[index]["fields"]
            if set(fields) - set(allowed) or any(not isinstance(v, str) or len(v) > 16000 for v in fields.values()):
                raise ProjectServiceError(422, "invalid_stage_fields", "字段不属于当前阶段或单项超过 16000 字，请核对后保存。")
            stage["fields"].update(fields)
        if payload.get("source_refs") is not None:
            stage["source_refs"] = payload["source_refs"]
        stage["status"] = "draft"
        stage["invalidated_reason"] = ""
        plan["status"] = "active"
        missing = missing_fields(stage)
        if action == "review" and not missing:
            stage["status"] = "review"
            message = "已提交审阅；请核对产出与来源后确认当前阶段。"
        elif action == "review":
            message = "草稿已保存。请补充：" + "、".join(m["label"] for m in missing) + "，再提交审阅。"
        else:
            message = "草稿已保存，尚未进入已确认上下文。"
    elif action in {"confirm", "skip"}:
        if action == "skip":
            if not stage["optional"]:
                raise ProjectServiceError(409, "stage_required", "必选阶段不能跳过，请完善草稿后审阅确认。")
            if not payload["reason"].strip():
                raise ProjectServiceError(422, "skip_reason_required", "请填写跳过工作量与资源计划的原因。")
            stage.update(status="skipped", skip_reason=payload["reason"])
        else:
            if stage["status"] != "review" or missing_fields(stage):
                raise ProjectServiceError(409, "review_required", "请先完善草稿并提交审阅，不能直接确认。")
            stage.update(status="confirmed", confirmed_at=timestamp(), confirmed_workflow_version=snapshot["version"]+1)
        if index + 1 < len(STAGES):
            plan["current_index"] += 1
            plan["status"] = "active"
        else:
            plan["status"] = "complete"
            # A terminal skip is an explicit decision, not a silent default: it
            # completes planning but stays visibly unanswered for later readers.
            plan["skipped_terminal"] = action == "skip"
        message = "当前阶段已确认，进入下一阶段。" if plan["status"] != "complete" else "启动规划已完成；可以进入人工修订写作。"
    elif action == "back":
        ids = [s["id"] for s in STAGES]
        target = ids.index(target_id) if target_id in ids else max(0, index-1) if target_id is None else -1
        if not 0 <= target <= index:
            raise ProjectServiceError(409, "stage_locked", "只能回退至当前阶段或之前阶段。")
        for item in plan["stages"][target:]:
            item.update(status="draft", confirmed_at=None, confirmed_workflow_version=None,
                        invalidated_reason="上游阶段已重新打开，需重新审阅确认。")
            item.pop("skip_reason", None)
        plan.update(current_index=target, status="active")
        message = "已在新版本中回退；后续产出保留为草稿，旧确认历史仍可查看。"
    elif action == "restart":
        plan = fresh_plan(project_id, branch_id=uuid.uuid4().hex, parent={"branch_id": plan["branch_id"], "workflow_version": snapshot["version"]})
        plan["status"] = "active"
        message = "已开始新的规划分支，旧分支与产出保留在历史中。"
    elif action == "cancel":
        plan.update(resume_status=plan["status"], status="cancelled")
        message = "规划已暂停，已保存产出保留；可恢复继续。"
    elif action == "resume":
        plan["status"] = plan.pop("resume_status", "active")
        message = "已恢复原规划与草稿。"
    return commit(conn, project_id, snapshot, plan, payload, action, fp, message=message)


def copy_as_new(conn, project_id, payload):
    begin(conn)
    from paperpilot.services.agent_actions import require_writable
    require_writable(conn, project_id)
    fp, prior = replay(conn, project_id, payload, "copy")
    if prior:
        return prior
    target = current_snapshot(conn, project_id)
    ensure_version(target, payload["expected_workflow_version"])
    source = history(conn, payload["source_project_id"], payload["source_version"])
    if not source["plan"]["branch_id"]:
        raise ProjectServiceError(409, "source_plan_missing", "来源版本还没有已保存的规划，请选择有效版本。")
    plan = copy.deepcopy(source["plan"])
    plan.update(project_id=project_id, branch_id=uuid.uuid4().hex, current_index=0, status="active",
        parent_branch={"branch_id": plan_from_snapshot(target)["branch_id"], "workflow_version": target["version"]},
        lineage={"source_project_id": payload["source_project_id"], "source_version": payload["source_version"], "source_branch": source["plan"]["branch_id"], "copied_at": timestamp()})
    plan.pop("resume_status", None)
    for stage in plan["stages"]:
        stage.update(status="draft", confirmed_at=None, confirmed_workflow_version=None, invalidated_reason="复制到新规划后需在当前论文重新核验。")
        stage.pop("skip_reason", None)
    return commit(conn, project_id, target, plan, payload, "copy", fp,
        message="已复制为独立新规划。请从第一阶段重新核验；不会同步修改来源论文。")

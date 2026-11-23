"""Single source of truth for the five-planning-stage state, shared by the
workflow ledger and the startup facade.

This module is intentionally dependency-free (no imports from workflow or
startup_workflow) so the ledger can project planning state without importing
the domain service and creating a cycle.
"""
from __future__ import annotations

import copy
import json

STAGES = (
    {"id": "STAGE-01", "title": "研究问题与创新点", "fields": {"research_question": "研究问题", "contribution": "拟解决缺口与创新", "scope": "研究范围"}, "optional": False},
    {"id": "STAGE-02", "title": "写作大纲", "fields": {"outline": "章节大纲", "objectives": "章节目标"}, "optional": False},
    {"id": "STAGE-03", "title": "文章结构", "fields": {"structure": "结构与论证顺序", "transitions": "章节衔接"}, "optional": False},
    {"id": "STAGE-04", "title": "术语与证据准备", "fields": {"terms": "关键术语", "evidence": "证据与来源计划", "gaps": "缺口与补充动作"}, "optional": False},
    {"id": "STAGE-05", "title": "工作量与资源计划", "fields": {"tasks": "工作任务", "resources": "资源与工作量估计"}, "optional": True},
)
STAGE_IDS = tuple(s["id"] for s in STAGES)

# Confirmation position is independent from browsing and AI capability.
WRITING_STAGE = "human_revision"
CONFIRMED_STAGE = "stage_confirmed"


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fresh_plan(project_id, *, branch_id=None, parent=None, status="active", start_index=0):
    return {"schema_version": 2, "project_id": project_id, "branch_id": branch_id, "parent_branch": parent,
        "status": status, "current_index": start_index, "lineage": None,
        "stages": [{"id": s["id"], "title": s["title"], "optional": s["optional"], "status": "start",
                    "fields": {key: "" for key in s["fields"]}, "source_refs": [], "confirmed_at": None,
                    "confirmed_workflow_version": None, "invalidated_reason": ""} for s in STAGES]}


def legacy_plan(project_id):
    """Read-time placeholder for snapshots written before planning was embedded.

    It never drives the project position: older papers keep whatever
    `current_stage` their snapshot already recorded.
    """
    return fresh_plan(project_id, status="not_started")


def has_plan(snapshot):
    return isinstance((snapshot.get("stage_outputs") or {}).get("startup_plan"), dict)


def plan_from_snapshot(snapshot):
    plan = (snapshot.get("stage_outputs") or {}).get("startup_plan")
    return copy.deepcopy(plan) if isinstance(plan, dict) else legacy_plan(snapshot["project_id"])


def missing_fields(stage):
    definition = next(s for s in STAGES if s["id"] == stage["id"])
    return [{"key": key, "label": label, "action": "填写“" + label + "”后提交审阅"}
            for key, label in definition["fields"].items()
            if key != "contribution" and not str(stage["fields"].get(key, "")).strip()]


def confirmed_context(plan, *, include_innovation_candidate=True):
    """Only a continuous confirmed prefix from the active branch is effective."""
    if not plan or plan.get("status") in {"cancelled", "not_started"}:
        return []
    result = []
    for stage in plan.get("stages", []):
        if stage.get("status") != "confirmed":
            break
        value = {key: copy.deepcopy(stage[key]) for key in ("id", "title", "fields", "source_refs", "confirmed_workflow_version")}
        if not include_innovation_candidate:
            value["fields"].pop("contribution", None)
        result.append(value)
    return result


def derived_position(plan):
    """Map the planning state onto the ledger's (current_stage, stage_status).

    Returns None when the plan is a legacy placeholder, so old papers keep the
    position already stored in their snapshot.
    """
    if not plan or plan.get("status") == "not_started":
        return None
    if plan.get("status") == "complete":
        return WRITING_STAGE, "context_ready"
    index = min(int(plan.get("current_index") or 0), len(STAGES) - 1)
    stage = plan["stages"][index]
    prefix = "planning_cancelled" if plan.get("status") == "cancelled" else "planning_" + str(stage.get("status") or "start")
    return stage["id"], prefix


def planning_complete(plan):
    return bool(plan) and plan.get("status") == "complete"


def projection_from_snapshot(snapshot):
    plan = plan_from_snapshot(snapshot)
    index = min(int(plan.get("current_index") or 0), len(STAGES) - 1)
    current = plan["stages"][index]
    if plan["status"] == "not_started":
        actions = ["start"]
    elif plan["status"] == "cancelled":
        actions = ["resume", "restart"]
    elif plan["status"] == "complete":
        actions = ["back", "restart"]
    else:
        actions = ["save", "review", "cancel", "back", "restart"] if current["status"] in {"start", "draft"} else ["save", "confirm", "back", "cancel", "restart"]
        if current["optional"] and current["status"] in {"start", "draft", "review"}:
            actions.append("skip")
    stage_actions = {}
    for stage in plan["stages"]:
        allowed = []
        if plan["status"] == "active" and stage["status"] not in {"confirmed", "skipped"}:
            allowed.append("save")
            if stage["id"] == current["id"]:
                allowed.extend(a for a in actions if a in {"review", "confirm", "skip"})
        stage_actions[stage["id"]] = allowed
    summary = {"label": current["title"], "confirmed_count": sum(s["status"] == "confirmed" for s in plan["stages"]),
               "total": len(STAGES), "skipped_count": sum(s["status"] == "skipped" for s in plan["stages"]),
               "next_action": "核对正文与来源" if plan["status"] == "complete" else "核对并确认“" + current["title"] + "”"}
    return {"project_id": snapshot["project_id"], "workflow_version": snapshot["version"], "history_parent_version": snapshot.get("history_parent_version"),
        "plan": plan, "definitions": list(STAGES), "current_stage": current["id"],
        "next_stage": plan["stages"][index+1]["title"] if index+1 < len(STAGES) else "进入人工修订写作",
        "missing_fields": missing_fields(current) if current["status"] not in {"confirmed", "skipped"} else [],
        "allowed_actions": actions, "stage_actions": stage_actions, "summary": summary, "confirmed_context": confirmed_context(plan),
        "planning_complete": plan.get("status") == "complete",
        "source_policy": "来源说明仅用于作者归因，不是自动证据验证或读取授权。", "body_written": False}

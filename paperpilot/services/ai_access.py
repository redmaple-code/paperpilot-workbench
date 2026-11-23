"""AI access policy.

The workflow never blocks AI. It only produces reminders so the author knows
when the upstream material is thin. Human actions are never gated either: the
author may switch stages, edit, compile or insert images at any time.
"""
from __future__ import annotations

from paperpilot.services.plan_state import plan_from_snapshot
from paperpilot.services.workflow import current_snapshot


def warnings(conn, project_id: str) -> list[dict]:
    """Non-blocking reminders about incomplete upstream material."""
    from paperpilot.services.project_errors import ProjectServiceError

    try:
        snapshot = current_snapshot(conn, project_id)
    except ProjectServiceError:
        return []
    plan = plan_from_snapshot(snapshot)
    items = []
    if plan.get("status") != "complete":
        index = min(int(plan.get("current_index") or 0), len(plan.get("stages") or [{}]) - 1)
        stage = (plan.get("stages") or [{}])[index]
        items.append({
            "code": "planning_incomplete",
            "message": "启动规划尚未完成（当前在第 %d 阶段：%s）。AI 仍可使用，但产出可能缺少研究问题与结构依据。"
                       % (index + 1, stage.get("title", "")),
            "stage": stage.get("id", ""),
        })
    fields = (plan.get("stages") or [{}])[0].get("fields") or {}
    if not str(fields.get("research_question") or "").strip():
        items.append({"code": "research_question_empty",
                      "message": "研究问题为空：AI 生成的候选可能偏离你的研究定位。"})
    return items


def allowed(conn, project_id: str) -> bool:
    """Always true: AI availability is not a workflow switch."""
    return True


def payload_warnings(conn, project_id: str) -> dict:
    items = warnings(conn, project_id)
    return {"ai_allowed": True, "warnings": items}
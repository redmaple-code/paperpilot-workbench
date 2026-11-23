from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlite3

from paperpilot.services import plan_state
from paperpilot.services.project_errors import ProjectServiceError


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["stage_outputs"] = json.loads(item.pop("stage_outputs_json") or "{}")
    return item


def current_snapshot(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workflow_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if not row:
        raise ProjectServiceError(409, "workflow_missing", "该论文没有 REQ-001 工作流快照；旧论文不会自动迁移。")
    return parse_snapshot(row)


def create_initial_workflow(conn: sqlite3.Connection, project_id: str, outline: str, stamp: str) -> None:
    # A new paper opens directly inside planning (STAGE-01, active), so the
    # workflow position and the plan position start out as the same thing.
    plan = plan_state.fresh_plan(project_id, branch_id=uuid.uuid4().hex)
    outputs = {"outline": outline, "initialized": True, "startup_plan": plan}
    conn.execute(
        "INSERT INTO workflow_snapshots(project_id,version,current_stage,stage_status,stage_outputs_json,confirmation_status,history_parent_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (project_id, 1, "STAGE-01", "planning_start", json.dumps(outputs, ensure_ascii=False), "pending", None, stamp),
    )
    for resource_kind, title in (
        ("terms", "补充论文核心术语"),
        ("corpus", "整理研究语料"),
        ("references", "补充关键参考文献"),
    ):
        conn.execute(
            "INSERT INTO workflow_work_items(id,project_id,resource_kind,title,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, project_id, resource_kind, title, "pending", stamp, stamp),
        )


def append_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    current_stage: str | None = None,
    stage_status: str,
    confirmation_status: str | None = None,
    stage_outputs: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    current = current_snapshot(conn, project_id)
    if expected_version is not None and current["version"] != expected_version:
        raise ProjectServiceError(409, "workflow_conflict", "工作流版本已更新；请刷新状态后重试。")
    version = current["version"] + 1
    stage = current_stage or current["current_stage"]
    outputs = dict(current["stage_outputs"])
    if stage_outputs:
        outputs.update(stage_outputs)
    stamp = timestamp()
    conn.execute(
        "INSERT INTO workflow_snapshots(project_id,version,current_stage,stage_status,stage_outputs_json,confirmation_status,history_parent_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            project_id, version, stage, stage_status,
            json.dumps(outputs, ensure_ascii=False),
            confirmation_status or current["confirmation_status"],
            current["version"], stamp,
        ),
    )
    return current_snapshot(conn, project_id)


def workflow_projection(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    current = current_snapshot(conn, project_id)
    history = [
        parse_snapshot(row)
        for row in conn.execute(
            "SELECT * FROM workflow_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 30",
            (project_id,),
        )
    ]
    mode = conn.execute(
        "SELECT id,state,workflow_version,mode_version,status,created_at FROM human_revision_modes WHERE project_id=? AND status='active'",
        (project_id,),
    ).fetchone()
    work_items = [
        dict(row)
        for row in conn.execute(
            "SELECT id,resource_kind,title,status,created_at,updated_at FROM workflow_work_items WHERE project_id=? ORDER BY created_at,resource_kind",
            (project_id,),
        )
    ]
    status = current["stage_status"]
    startup = plan_state.projection_from_snapshot(current)
    allowed = {
        "context_ready": ["start", "redo"],
        "stage_in_progress": ["review", "cancel", "redo"],
        "stage_review": ["confirm", "back", "cancel", "redo"],
        "stage_confirmed": ["start", "back", "redo"],
        "failed": ["retry", "back", "redo"],
        "cancelled": ["retry", "back", "redo"],
    }.get(status, [])
    return {
        **current,
        # One derived phase for the whole product: planning → writing → confirming.
        # Consumers read this instead of inferring from two separate state machines.
        "phase": phase_of(current, startup),
        "allowed_actions": allowed,
        "human_revision_mode": dict(mode) if mode else None,
        "work_items": work_items,
        "history": history,
        "startup": startup,
    }


def phase_of(snapshot, startup_projection):
    plan = startup_projection["plan"]
    if plan.get("status") == "not_started":
        # Legacy snapshots written before planning was embedded.
        return "writing" if snapshot["current_stage"] == plan_state.WRITING_STAGE else "legacy"
    if not plan_state.planning_complete(plan):
        return "planning_cancelled" if plan.get("status") == "cancelled" else "planning"
    if snapshot["current_stage"] == plan_state.WRITING_STAGE:
        return "writing"
    return "confirming"


def set_work_item_status(conn: sqlite3.Connection, project_id: str, item_id: str, status: str) -> dict[str, Any]:
    if status not in {"pending", "done"}:
        raise ProjectServiceError(422, "invalid_work_item_status", "待办状态必须是 pending 或 done。")
    row = conn.execute(
        "SELECT * FROM workflow_work_items WHERE id=? AND project_id=?",
        (item_id, project_id),
    ).fetchone()
    if row is None:
        raise ProjectServiceError(404, "work_item_missing", "工作项不存在。")
    conn.execute(
        "UPDATE workflow_work_items SET status=?,updated_at=? WHERE id=? AND project_id=?",
        (status, timestamp(), item_id, project_id),
    )
    return dict(conn.execute("SELECT * FROM workflow_work_items WHERE id=?", (item_id,)).fetchone())


def execute_command(
    conn: sqlite3.Connection,
    project_id: str,
    command: str,
    base_version: int,
    target_version: int | None = None,
) -> dict[str, Any]:
    current = current_snapshot(conn, project_id)
    if current["version"] != base_version:
        raise ProjectServiceError(409, "workflow_conflict", "工作流版本已更新；请刷新状态后重试。")
    status = current["stage_status"]
    # Planning never blocks writing. Incomplete planning is surfaced as a
    # reminder by the projection, not as a refusal here.
    transitions = {
        "start": {"context_ready": ("human_revision", "stage_in_progress", "pending"),
                  "stage_confirmed": ("human_revision", "stage_in_progress", "pending"),
                  "planning_start": ("human_revision", "stage_in_progress", "pending"),
                  "planning_draft": ("human_revision", "stage_in_progress", "pending"),
                  "planning_review": ("human_revision", "stage_in_progress", "pending"),
                  "planning_cancelled": ("human_revision", "stage_in_progress", "pending")},
        "review": {"stage_in_progress": ("human_revision", "stage_review", "pending")},
        "confirm": {"stage_review": ("stage_confirmed", "stage_confirmed", "confirmed")},
        "back": {
            "stage_review": ("human_revision", "stage_in_progress", "pending"),
            "stage_confirmed": ("human_revision", "stage_review", "pending"),
            "failed": ("human_revision", "stage_in_progress", "pending"),
            "cancelled": ("human_revision", "stage_in_progress", "pending"),
        },
        "retry": {"failed": ("human_revision", "stage_in_progress", "pending"), "cancelled": ("human_revision", "stage_in_progress", "pending")},
        "cancel": {"stage_in_progress": ("cancelled", "cancelled", "pending"), "stage_review": ("cancelled", "cancelled", "pending")},
    }
    if command == "redo":
        if target_version is None:
            raise ProjectServiceError(422, "target_required", "重做需要 target_version。")
        row = conn.execute(
            "SELECT * FROM workflow_snapshots WHERE project_id=? AND version=?",
            (project_id, target_version),
        ).fetchone()
        if not row:
            raise ProjectServiceError(404, "history_missing", "找不到指定的工作流历史版本。")
        target = parse_snapshot(row)
        if current["current_stage"] == "human_revision" and target["current_stage"] != "human_revision":
            invalidate_human_revision(conn, project_id)
        return append_snapshot(
            conn, project_id, current_stage=target["current_stage"],
            stage_status=target["stage_status"], confirmation_status=target["confirmation_status"],
            stage_outputs=target["stage_outputs"], expected_version=base_version,
        )
    if command == "advance":
        command = {"context_ready": "start", "stage_in_progress": "review", "stage_review": "confirm"}.get(status, "")
    target = transitions.get(command, {}).get(status)
    if not target:
        raise ProjectServiceError(409, "invalid_transition", f"不允许从 {status} 执行 {command or '未知命令'}。")
    new_stage, new_status, confirmation_status = target
    if current["current_stage"] == "human_revision" and new_stage != "human_revision":
        invalidate_human_revision(conn, project_id)
    return append_snapshot(
        conn, project_id, current_stage=new_stage, stage_status=new_status,
        confirmation_status=confirmation_status,
        stage_outputs={"last_command": command}, expected_version=base_version,
    )


def invalidate_human_revision(conn: sqlite3.Connection, project_id: str) -> None:
    stamp = timestamp()
    conn.execute(
        "UPDATE draft_candidates SET status='stale',decided_at=? WHERE project_id=? AND status='candidate_ready'",
        (stamp, project_id),
    )
    conn.execute(
        "UPDATE ai_jobs SET status='failed',error_code='workflow_changed',updated_at=? WHERE project_id=? AND status IN ('running','cancel_requested')",
        (stamp, project_id),
    )
    conn.execute(
        "UPDATE assist_action_events SET status='failed' WHERE project_id=? AND status IN ('running','candidate_ready')",
        (project_id,),
    )
    conn.execute(
        "UPDATE human_revision_modes SET status='closed',exit_target='workflow_changed',updated_at=? WHERE project_id=? AND status='active'",
        (stamp, project_id),
    )

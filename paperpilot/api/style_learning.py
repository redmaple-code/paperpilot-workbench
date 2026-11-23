from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from fastapi import APIRouter, BackgroundTasks, HTTPException
from paperpilot.domain.style_learning_schemas import (
    LearningCommandIn, LearningConsentIn, LearningSignalIn, LearningApprovalIn,
    LearningRuleEditIn, LearningRollbackIn, LearningDeleteIn,
)
from paperpilot.services import style_learning as learning
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write):
    router = APIRouter(prefix="/api/projects/{project_id}/style-learning", tags=["style learning"])

    @contextmanager
    def connection(project_id, writable=False):
        (project_write if writable else project_read)(project_id)
        conn = database()
        try:
            with conn:
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, detail={"code": "learning_storage_failed", "message": "学习数据暂时无法保存；当前输入保留，正文保存独立。请稍后刷新重试。"}) from exc
        finally:
            conn.close()

    @router.get("")
    def read(project_id: str):
        with connection(project_id) as conn:
            return learning.read(conn, project_id)

    @router.post("/consent")
    def consent(project_id: str, payload: LearningConsentIn):
        with connection(project_id, True) as conn:
            return learning.opt_in(conn, project_id, payload.model_dump())

    @router.post("/signals")
    def signal(project_id: str, payload: LearningSignalIn):
        with connection(project_id, True) as conn:
            return learning.collect_explicit(conn, project_id, payload.model_dump())

    @router.post("/distill")
    def distill(project_id: str, payload: LearningCommandIn, background_tasks: BackgroundTasks):
        with connection(project_id, True) as conn:
            result = learning.request_distillation(conn, project_id, payload.model_dump())
        background_tasks.add_task(learning.process_run, database, project_id, result["run_id"])
        return result

    @router.post("/approve")
    def approve(project_id: str, payload: LearningApprovalIn):
        with connection(project_id, True) as conn:
            return learning.approve(conn, project_id, payload.model_dump())

    @router.post("/rules/{rule_id}/edit")
    def edit(project_id: str, rule_id: str, payload: LearningRuleEditIn):
        with connection(project_id, True) as conn:
            return learning.edit_rule(conn, project_id, rule_id, payload.model_dump())

    @router.post("/rules/{rule_id}/remove")
    def remove(project_id: str, rule_id: str, payload: LearningCommandIn):
        with connection(project_id, True) as conn:
            return learning.govern(conn, project_id, "remove:"+rule_id, payload.model_dump())

    @router.post("/rollback")
    def rollback(project_id: str, payload: LearningRollbackIn):
        with connection(project_id, True) as conn:
            return learning.govern(conn, project_id, "rollback", payload.model_dump())

    @router.post("/delete")
    def delete(project_id: str, payload: LearningDeleteIn):
        with connection(project_id, True) as conn:
            return learning.delete_learning(conn, project_id, payload.model_dump())

    @router.post("/{action}")
    def command(project_id: str, action: str, payload: LearningCommandIn):
        if action not in {"pause", "resume", "disable"}:
            raise HTTPException(404, "不支持的学习操作。")
        with connection(project_id, True) as conn:
            return learning.govern(conn, project_id, action, payload.model_dump())

    return router

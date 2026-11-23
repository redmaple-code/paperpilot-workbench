from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from fastapi import APIRouter, HTTPException

from paperpilot.domain.startup_schemas import StartupCommandIn, StartupCopyIn
from paperpilot.services import startup_workflow as startup
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write):
    router = APIRouter(prefix="/api/projects/{project_id}/workflow/startup", tags=["paper startup planning"])

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
            raise HTTPException(503, detail={"code": "workflow_storage_failed", "message": "规划暂时无法保存；原版本与当前输入保留。请检查可用空间或稍后重试，不要重复确认阶段。"}) from exc
        finally:
            conn.close()

    @router.get("")
    def read(project_id: str):
        with connection(project_id) as conn:
            return startup.projection(conn, project_id)

    @router.get("/history/{version}")
    def history(project_id: str, version: int):
        with connection(project_id) as conn:
            return startup.history(conn, project_id, version)

    @router.post("/commands")
    def command(project_id: str, payload: StartupCommandIn):
        with connection(project_id, True) as conn:
            return startup.command(conn, project_id, payload.model_dump())

    @router.post("/copy")
    def copy(project_id: str, payload: StartupCopyIn):
        project_read(payload.source_project_id)
        with connection(project_id, True) as conn:
            return startup.copy_as_new(conn, project_id, payload.model_dump())

    return router

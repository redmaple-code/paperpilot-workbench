"""Project-scoped author actions for the innovation workspace."""
from contextlib import contextmanager
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from paperpilot.services import innovation_points as points, innovation_sources as sources
from paperpilot.services.project_errors import ProjectServiceError


class SupportRef(BaseModel):
    kind: Literal["work", "logic", "outline", "result", "asset"]
    id: str = Field(min_length=1, max_length=240)
    version: str = Field(min_length=1, max_length=120)


class SupportIn(BaseModel):
    description: str = Field(min_length=1, max_length=1200)
    expected_output: str = Field(default="", max_length=1200)
    refs: list[SupportRef] = Field(default_factory=list, max_length=20)


class BodyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(default="", max_length=4000)
    difference: str = Field(default="", max_length=4000)
    scope: str = Field(default="", max_length=4000)
    support: list[SupportIn] = Field(default_factory=list, max_length=25)


class CommandIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    action: Literal["create_draft", "save_draft", "confirm", "set_primary"]
    point_id: str | None = Field(default=None, max_length=120)
    draft_revision: int | None = Field(default=None, ge=0)
    active_revision: int | None = Field(default=None, ge=0)
    body: BodyIn | None = None
    detach_source: bool = False


class SourceIn(BaseModel):
    kind: Literal["file", "import", "planning", "message"]
    id: str = Field(min_length=1, max_length=1000)


class ExtractIn(SourceIn):
    request_id: str = Field(min_length=1, max_length=120)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    keys: list[str] = Field(min_length=1, max_length=12)
    point_id: str | None = Field(default=None, max_length=120)
    draft_revision: int | None = Field(default=None, ge=0)


def create_router(database, project_read):
    router = APIRouter(prefix="/api/projects/{project_id}/innovations", tags=["Innovations"])

    @contextmanager
    def connection(project_id, write=False):
        project_read(project_id)
        conn = database()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn
        except ProjectServiceError as error:
            raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message}) from error
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            raise HTTPException(503, detail={"code": "innovation_storage_busy", "message": "存储繁忙，请保留输入并重试。"}) from error
        finally:
            conn.close()

    @router.get("")
    def list_points(project_id: str):
        with connection(project_id) as conn:
            return points.list_points(conn, project_id)

    @router.get("/options")
    def options(project_id: str):
        project = project_read(project_id)
        with connection(project_id) as conn:
            return {"sources": sources.sources(conn, project_id), "support": sources.support_options(conn, project_id),
                    "read_only": project["lifecycle_state"] == "published", "project_name": project["name"]}

    @router.get("/{point_id}")
    def read_point(project_id: str, point_id: str, version: int | None = None):
        with connection(project_id) as conn:
            return points.read_point(conn, project_id, point_id, version)

    @router.post("/commands")
    def command(project_id: str, payload: CommandIn):
        with connection(project_id, True) as conn:
            return points.command(conn, project_id, payload.model_dump())

    @router.post("/extractions/preview")
    def preview(project_id: str, payload: SourceIn):
        with connection(project_id) as conn:
            return sources.preview(conn, project_id, payload.kind, payload.id)

    @router.post("/extractions")
    def extract(project_id: str, payload: ExtractIn):
        with connection(project_id, True) as conn:
            return sources.adopt(conn, project_id, payload.model_dump())

    return router

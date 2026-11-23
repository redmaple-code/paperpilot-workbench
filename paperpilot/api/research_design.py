"""Research-design guidance routes (scholar-apprentice E stage), never auto-applied."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3

from fastapi import APIRouter, HTTPException

from paperpilot.domain.research_design_schemas import (
    ResearchDesignApplyIn, ResearchDesignGenerateIn, ResearchDesignPreviewIn, ResearchDedupIn,
)
from paperpilot.services import research_design
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write, call_ai):
    router = APIRouter(prefix="/api/projects/{project_id}/workflow/research-design",
                       tags=["research design guidance"])

    @contextmanager
    def connection(project_id, writable=False):
        (project_write if writable else project_read)(project_id)
        conn = database()
        try:
            with conn:
                if writable:
                    from paperpilot.services.agent_actions import require_writable
                    require_writable(conn, project_id)
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, detail={"code": "research_design_storage_failed",
                "message": "研究设计建议暂时无法保存；已完成的输入不会写入，请稍后重试。"}) from exc
        finally:
            conn.close()

    @router.get("")
    def list_all(project_id: str, stage_id: str = ""):
        with connection(project_id) as conn:
            return {"candidates": research_design.list_candidates(conn, project_id, stage_id or None),
                    "operators": research_design.operator_definitions(),
                    "red_team": research_design.red_team_definitions()}

    @router.post("/preview")
    def preview(project_id: str, payload: ResearchDesignPreviewIn):
        with connection(project_id) as conn:
            result = research_design.preview(conn, project_id, payload.model_dump())
            return {key: value for key, value in result.items() if not key.startswith("_")}

    @router.get("/resources")
    def resources(project_id: str):
        with connection(project_id) as conn:
            return {"resources": [dict(r) for r in conn.execute("SELECT c.id,c.title,s.version,length(s.content) AS characters FROM corpus c JOIN corpus_source_versions s ON s.corpus_id=c.id WHERE c.project_id=? AND s.permission_state='authorized' AND s.status='active' AND s.version=(SELECT max(t.version) FROM corpus_source_versions t WHERE t.corpus_id=c.id) ORDER BY c.title LIMIT 100", (project_id,))]}

    @router.post("")
    def generate(project_id: str, payload: ResearchDesignGenerateIn):
        with connection(project_id, True) as conn:
            return research_design.generate(conn, project_id, payload.model_dump(), call_ai)

    @router.post("/apply")
    def apply(project_id: str, payload: ResearchDesignApplyIn):
        with connection(project_id, True) as conn:
            return research_design.apply_card(conn, project_id, payload.model_dump())

    @router.post("/{candidate_id}/cards/{card_id}/dedup")
    def dedup(project_id: str, candidate_id: str, card_id: str, payload: ResearchDedupIn):
        from paperpilot.services.agent_business_tools import keyword_search
        with connection(project_id, True) as conn:
            return research_design.check_keywords(conn, project_id, candidate_id, card_id, payload.model_dump(), keyword_search)

    @router.post("/{candidate_id}/cards/{card_id}/reject")
    def reject(project_id: str, candidate_id: str, card_id: str):
        with connection(project_id, True) as conn:
            return research_design.reject_card(conn, project_id, candidate_id, card_id)

    return router

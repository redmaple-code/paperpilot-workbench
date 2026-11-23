from contextlib import contextmanager
import sqlite3
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from paperpilot.domain.agent_action_schemas import ActionCreateIn, ActionPreviewIn, ActionApplyIn, BusinessSearchIn
from paperpilot.services import agent_actions
from paperpilot.services.project_errors import ProjectServiceError


class DisplayedIn(BaseModel):
    conversation_id: str = Field(min_length=1,max_length=120)
    action_fingerprint: str = Field(min_length=64,max_length=64)


class AuthorDecisionIn(BaseModel):
    conversation_id: str = Field(min_length=1,max_length=120)
    request_id: str = Field(min_length=1,max_length=120)
    instruction: str = Field(min_length=1,max_length=4000)
    action_id: str = Field(default='',max_length=120)


def create_router(database, project_read, adapters, search, search_cards):
    router = APIRouter(prefix="/api/projects/{project_id}/agent", tags=["reviewable business actions"])

    @contextmanager
    def connection(pid):
        project_read(pid)
        conn = database()
        try:
            with conn:
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message, "retryable": exc.status_code in {409, 503}}) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, detail={"code": "storage_or_provider_unavailable", "message": "候选暂时无法保存；原内容保留，请稍后重试。", "retryable": True}) from exc
        finally:
            conn.close()

    @router.get("/actions")
    def list_actions(project_id: str, conversation_id: str = ""):
        with connection(project_id) as conn:
            rows = conn.execute("SELECT * FROM agent_actions WHERE project_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 60", (project_id, conversation_id)).fetchall()
            return {"cards": [agent_actions.card(dict(r)) for r in reversed(rows)]}

    @router.post('/actions/{action_id}/displayed')
    def displayed(project_id: str, action_id: str, payload: DisplayedIn):
        from paperpilot.services import agent_author_decisions
        with connection(project_id) as conn:
            return agent_author_decisions.displayed(conn,project_id,payload.conversation_id,action_id,payload.action_fingerprint)

    @router.post('/author-decisions')
    def author_decision(project_id: str, payload: AuthorDecisionIn):
        from paperpilot.services import agent_author_decisions
        with connection(project_id) as conn:
            conn.execute('BEGIN IMMEDIATE')
            agent_actions.require_writable(conn,project_id)
            response = agent_author_decisions.resolve(conn,project_id,payload.model_dump(),adapters)
        if response.get('event'):
            hook = adapters[response['event']['kind']].get('after_commit')
            if hook:
                hook(response)
        return response

    @router.post("/actions")
    def create(project_id: str, payload: ActionCreateIn):
        with connection(project_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return agent_actions.create(conn, project_id, payload.model_dump(), adapters)

    @router.post("/actions/{action_id}/preview")
    def preview(project_id: str, action_id: str, payload: ActionPreviewIn):
        with connection(project_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return agent_actions.preview(conn, project_id, action_id, payload.model_dump(), adapters)

    @router.post("/actions/{action_id}/apply")
    def apply(project_id: str, action_id: str, payload: ActionApplyIn):
        with connection(project_id) as conn:
            response = agent_actions.apply(conn, project_id, action_id, payload.model_dump(), adapters)
        hook = adapters[response["event"]["kind"]].get("after_commit")
        if hook:
            hook(response)
        return response

    @router.post("/actions/{action_id}/reject")
    def reject(project_id: str, action_id: str):
        with connection(project_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return agent_actions.reject(conn, project_id, action_id)

    @router.post("/business-search")
    def business_search(project_id: str, payload: BusinessSearchIn):
        if not payload.consent_to_search:
            raise HTTPException(422, detail={"code": "consent_required", "message": "请确认发送检索式。"})
        project_read(project_id)
        return search(project_id, payload.query, payload.conversation_id)

    @router.get("/business-search")
    def list_search(project_id: str, conversation_id: str = ""):
        project_read(project_id)
        return {"cards": search_cards(project_id, conversation_id=conversation_id)}

    return router

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response

from paperpilot.domain.drawing_schemas import CandidateSaveIn, DecisionIn, DispatchIn, RunCommand, RunIn, StyleRuleIn, VisualProviderBindingIn
from paperpilot.services import visual_generation as generation, visual_jobs as jobs, visual_provider
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write, storage_root):
    router = APIRouter(prefix="/api/projects/{project_id}/drawing", tags=["drawing candidates"])

    @contextmanager
    def connection(project_id, writable=False):
        (project_write if writable else project_read)(project_id)
        conn = database()
        try:
            with conn:
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except OSError as exc:
            raise HTTPException(503, detail={"code": "drawing_storage_failed", "message": "图像存储不可用；请检查可用空间后重试，原资产保持不变。"}) from exc
        finally:
            conn.close()

    @router.get("/capabilities")
    def capabilities(project_id: str, purpose: str = "drawing"):
        with connection(project_id) as conn:
            return visual_provider.available(conn, project_id, purpose)

    @router.put("/provider-binding")
    def bind_provider(project_id: str, payload: VisualProviderBindingIn):
        with connection(project_id, True) as conn:
            return visual_provider.bind(conn, project_id, payload.model_dump())

    @router.get("/runs")
    def list_runs(project_id: str):
        with connection(project_id) as conn:
            return {"runs": [jobs.read(conn, project_id, r["id"]) for r in conn.execute("SELECT id FROM drawing_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,))]}

    @router.post("/runs")
    def create_run(project_id: str, payload: RunIn):
        with connection(project_id, True) as conn:
            return jobs.create(conn, project_id, payload.model_dump())

    @router.get("/runs/{run_id}")
    def read_run(project_id: str, run_id: str):
        with connection(project_id) as conn:
            return jobs.read(conn, project_id, run_id)

    @router.post("/runs/{run_id}/decision")
    def decide(project_id: str, run_id: str, payload: DecisionIn):
        with connection(project_id, True) as conn:
            return jobs.decide(conn, project_id, run_id, payload.model_dump())

    @router.post("/runs/{run_id}/undo")
    def undo(project_id: str, run_id: str, payload: RunCommand):
        with connection(project_id, True) as conn:
            return jobs.history_command(conn, project_id, run_id, payload.model_dump(), "undo")

    @router.post("/runs/{run_id}/redo")
    def redo(project_id: str, run_id: str, payload: RunCommand):
        with connection(project_id, True) as conn:
            return jobs.history_command(conn, project_id, run_id, payload.model_dump(), "redo")

    @router.post("/runs/{run_id}/dispatch")
    def dispatch(project_id: str, run_id: str, payload: DispatchIn, background_tasks: BackgroundTasks):
        with connection(project_id, True) as conn:
            result = jobs.reserve(conn, project_id, run_id, payload.model_dump(exclude={'temporary_key'}),temporary_key=payload.temporary_key or None)
        if not result.get("idempotent_replay"):
            background_tasks.add_task(jobs.execute, database, storage_root(), project_write, project_id, run_id, result["attempt_id"],temporary_key=payload.temporary_key or None)
        return result

    @router.post("/runs/{run_id}/cancel")
    def cancel(project_id: str, run_id: str, payload: RunCommand):
        with connection(project_id, True) as conn:
            return jobs.command(conn, project_id, run_id, payload.model_dump(), "cancel")

    @router.post("/runs/{run_id}/reconcile")
    def reconcile(project_id: str, run_id: str, payload: RunCommand):
        with connection(project_id, True) as conn:
            return jobs.command(conn, project_id, run_id, payload.model_dump(), "reconcile")

    @router.post("/runs/{run_id}/copy")
    def copy(project_id: str, run_id: str, payload: RunCommand):
        with connection(project_id, True) as conn:
            jobs.begin(conn)
            fp, prior = jobs.operation(conn, project_id, payload.request_id, {"action": "copy", "run_id": run_id, **payload.model_dump()})
            if prior:
                return prior
            old = jobs.read(conn, project_id, run_id)
            jobs.check_revision(jobs.row(conn, project_id, run_id), payload.expected_revision)
            result = jobs.create(conn, project_id, {"request_id": "copy-create:" + payload.request_id, "title": old["title"], "intent": old["intent"]})
            # Always returns to type confirmation. Re-execution requires fresh per-attempt consent.
            return jobs.remember(conn, project_id, payload.request_id, fp, result | {"copied_spec": old["spec"], "copied_type": old["selected_type"], "message": "新任务需重新确认输入；原任务结果不明时，请先核对提供方账单。"})

    @router.get("/candidates/{candidate_id}")
    def read_candidate(project_id: str, candidate_id: str):
        with connection(project_id) as conn:
            return jobs.candidate(conn, project_id, candidate_id)

    @router.get("/candidates/{candidate_id}/preview")
    def preview(project_id: str, candidate_id: str):
        with connection(project_id) as conn:
            jobs.candidate(conn, project_id, candidate_id)
            c = conn.execute("SELECT content,filename FROM drawing_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
            mime = "image/svg+xml" if c["filename"].endswith(".svg") else "image/jpeg" if c["filename"].endswith(".jpg") else "image/webp" if c["filename"].endswith(".webp") else "image/png"
            return Response(content=c["content"], media_type=mime, headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    @router.post("/candidates/{candidate_id}/save")
    def save(project_id: str, candidate_id: str, payload: CandidateSaveIn):
        with connection(project_id, True) as conn:
            return jobs.save_candidate(conn, storage_root(), project_id, candidate_id, payload.model_dump())

    @router.get("/style-rules")
    def rules(project_id: str):
        with connection(project_id) as conn:
            return {"rules": [dict(r) | {"values": json.loads(r["values_json"])} for r in conn.execute("SELECT * FROM visual_style_rules WHERE scope='project' AND scope_id=? ORDER BY version", (project_id,))]}

    @router.post("/style-rules/{kind}")
    def create_rule(project_id: str, kind: str, payload: StyleRuleIn):
        with connection(project_id, True) as conn:
            if kind not in generation.TYPES:
                raise ProjectServiceError(422, "invalid_tool", "请选择合法绘图类型。")
            values = generation.validate_style(kind, payload.values)
            jobs.begin(conn)
            fp, prior = jobs.operation(conn, project_id, payload.request_id, {"action": "rule", "kind": kind, "values": values})
            if prior:
                return prior
            rid = uuid.uuid4().hex
            version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM visual_style_rules WHERE scope='project' AND scope_id=? AND kind=?", (project_id, kind)).fetchone()[0]
            conn.execute("INSERT INTO visual_style_rules VALUES(?,?,?,?,?,?,?,?)", (rid, "project", project_id, kind, version, "candidate", jobs.compact(values), jobs.stamp()))
            return jobs.remember(conn, project_id, payload.request_id, fp, {"id": rid, "version": version, "status": "candidate", "values": values})

    @router.post("/style-rules/{rule_id}/approve")
    def approve_rule(project_id: str, rule_id: str):
        with connection(project_id, True) as conn:
            jobs.begin(conn)
            rule = conn.execute("SELECT * FROM visual_style_rules WHERE id=? AND scope='project' AND scope_id=?", (rule_id, project_id)).fetchone()
            if not rule:
                raise ProjectServiceError(404, "resource_missing", "风格候选不属于当前论文。")
            provenance = conn.execute("SELECT report_id FROM visual_style_candidates WHERE rule_id=?", (rule_id,)).fetchone()
            if provenance:
                from paperpilot.services import visual_evidence
                if visual_evidence.report(conn, project_id, provenance["report_id"])["stale"]:
                    raise ProjectServiceError(409, "source_unavailable", "风格候选的来源或规则已变化，请基于当前参考图重新提取。")
            conn.execute("UPDATE visual_style_rules SET status='approved' WHERE id=?", (rule_id,))
            return {"id": rule_id, "status": "approved", "version": rule["version"]}

    @router.post("/style-plans/compile")
    def compile_plan(project_id: str, payload: DecisionIn):
        with connection(project_id) as conn:
            generation.validate_spec(payload.selected_type, payload.spec)
            return generation.compile_style(conn, project_id, payload.selected_type, payload.constraints)

    return router

from __future__ import annotations

from contextlib import contextmanager
from fastapi import APIRouter, BackgroundTasks, HTTPException

from paperpilot.domain.visual_evidence_schemas import VisualCheckIn, VisualReviewIn
from paperpilot.services import visual_evidence as evidence, visual_jobs as jobs
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write, storage_root):
    router = APIRouter(prefix="/api/projects/{project_id}", tags=["visual evidence"])

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
            raise HTTPException(503, detail={"code": "source_unavailable", "message": "图像或证据文件不可用，请检查后重试；原报告与复核记录保留。"}) from exc
        finally:
            conn.close()

    @router.post("/visual-checks")
    def create_check(project_id: str, payload: VisualCheckIn, background_tasks: BackgroundTasks):
        with connection(project_id, True) as conn:
            result = evidence.create(conn, project_id, payload.model_dump(exclude={'temporary_key'}),temporary_key=payload.temporary_key or None,storage_root=storage_root())
        if not result.get("idempotent_replay"):
            background_tasks.add_task(evidence.execute, database, storage_root(), project_write, project_id, result["id"],temporary_key=payload.temporary_key or None)
        return result

    @router.post("/visual-checks/preview")
    def preview_check(project_id: str, payload: VisualCheckIn):
        with connection(project_id) as conn:
            # A single SQLite read snapshot binds image and configuration facts.
            conn.execute('BEGIN')
            return evidence.capacity_preview(conn, storage_root(), project_id, payload.model_dump(exclude={'temporary_key'}))

    @router.get("/visual-checks/{run_id}")
    def read_check(project_id: str, run_id: str):
        with connection(project_id) as conn:
            return evidence.read_run(conn, project_id, run_id)

    @router.post("/visual-checks/{run_id}/reconcile")
    def reconcile(project_id: str, run_id: str):
        with connection(project_id, True) as conn:
            jobs.begin(conn)
            run = evidence.get_run(conn, project_id, run_id)
            if run["status"] in {"queued", "running"}:
                conn.execute("UPDATE visual_check_runs SET status='unknown',error_code='provider_unknown',updated_at=? WHERE id=?", (jobs.stamp(), run_id))
            return evidence.read_run(conn, project_id, run_id) | {"message": "已核对本地任务记录。结果不明时请核对提供方账单，不会自动重发请求。"}

    @router.get("/visual-reports")
    def list_reports(project_id: str):
        with connection(project_id) as conn:
            return {"reports": [evidence.report(conn, project_id, r["id"]) for r in conn.execute("SELECT id FROM visual_reports WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,))]}

    @router.get("/visual-reports/current")
    def current(project_id: str, asset_version_id: str, purpose: str = "review"):
        with connection(project_id) as conn:
            return evidence.current(conn, project_id, asset_version_id, purpose)

    @router.get("/visual-reports/{report_id}")
    def read_report(project_id: str, report_id: str):
        with connection(project_id) as conn:
            return evidence.report(conn, project_id, report_id)

    @router.post("/visual-reports/{report_id}/reviews")
    def review(project_id: str, report_id: str, payload: VisualReviewIn):
        with connection(project_id, True) as conn:
            return evidence.review(conn, project_id, report_id, payload.model_dump())

    @router.post("/visual-reports/{report_id}/style-candidates")
    def style_candidates(project_id: str, report_id: str):
        with connection(project_id, True) as conn:
            return evidence.extract_style(conn, storage_root(), project_id, report_id)

    return router

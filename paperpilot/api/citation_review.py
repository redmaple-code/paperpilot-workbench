"""Explicit, bounded citation review actions on the current project."""
from fastapi import APIRouter, HTTPException
from paperpilot.domain.stage3_schemas import CitationReviewIn, CitationCommandIn, CitationDispositionIn, CitationSourceProfileIn
from paperpilot.services import citation_review as review, citation_rules
from paperpilot.services import citation_support
from paperpilot.services.project_errors import ProjectServiceError


def create_router(db, project_or_404, writable, call_ai):
    router=APIRouter()

    def run(pid, work, *, mutate=False):
        (writable if mutate else project_or_404)(pid)
        conn=db()
        try:
            return work(conn)
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        finally:
            conn.close()

    @router.get('/api/projects/{pid}/citation-review/source-profiles')
    def profiles(pid:str):
        return run(pid,lambda c:{'profiles':citation_rules.profiles(c,pid),'sources':citation_support.analyze(c,pid,content='')['source_pool']})

    @router.put('/api/projects/{pid}/citation-review/source-profiles/{source_id}')
    def profile(pid:str,source_id:str,payload:CitationSourceProfileIn):
        def work(conn):
            with conn:return citation_rules.update_profile(conn,pid,source_id,payload.expected_version,payload.model_dump(exclude={'expected_version'}))
        return run(pid,work,mutate=True)

    @router.post('/api/projects/{pid}/citation-review/preview')
    def preview(pid:str,payload:CitationReviewIn):
        return run(pid,lambda c:{k:v for k,v in review.preview(c,pid,payload.model_dump()).items() if not k.startswith('_')})

    @router.post('/api/projects/{pid}/citation-review/tasks')
    def create(pid:str,payload:CitationReviewIn):
        return run(pid,lambda c:review.create(c,pid,payload.model_dump()),mutate=True)

    @router.get('/api/projects/{pid}/citation-review/tasks')
    def tasks(pid:str):
        return run(pid,lambda c:[dict(r) for r in c.execute('SELECT id,status,fingerprint,created_at,updated_at,cleanup_state FROM citation_review_tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 50',(pid,))])

    @router.get('/api/projects/{pid}/citation-review/tasks/{task_id}')
    def read(pid:str,task_id:str):
        return run(pid,lambda c:review.read(c,pid,task_id))

    @router.post('/api/projects/{pid}/citation-review/tasks/{task_id}/step')
    def step(pid:str,task_id:str,payload:CitationCommandIn):
        return run(pid,lambda c:review.step(c,pid,task_id,payload.model_dump(),call_ai),mutate=True)

    @router.post('/api/projects/{pid}/citation-review/tasks/{task_id}/{action}')
    def command(pid:str,task_id:str,action:str,payload:CitationCommandIn):
        if action=='delete-preview':
            return run(pid,lambda c:{'id':review.row_for(c,pid,task_id)['id'],'scope':'此报告的输入快照、结果、译释、人工处置理由；保留最小任务墓碑、hash及无正文发送审计，原论文/来源保留','confirmation_required':True})
        return run(pid,lambda c:review.command(c,pid,task_id,action,payload.model_dump()),mutate=True)

    @router.post('/api/projects/{pid}/citation-review/tasks/{task_id}/claims/{claim_id}/disposition')
    def disposition(pid:str,task_id:str,claim_id:str,payload:CitationDispositionIn):
        return run(pid,lambda c:review.disposition(c,pid,task_id,claim_id,payload.model_dump()),mutate=True)

    return router

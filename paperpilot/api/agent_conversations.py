"""Project-owned APIs; committed turns are dispatched after the request transaction."""
from contextlib import contextmanager
import sqlite3
from fastapi import APIRouter,BackgroundTasks,HTTPException,Query
from pydantic import BaseModel, Field
from paperpilot.domain.startup_schemas import AgentMemoryDecisionIn
from paperpilot.domain.agent_conversation_schemas import ConversationCreateIn,ConversationPreviewIn,ConversationRunIn,ResumeIn,CardDecisionIn
from paperpilot.services import agent_memory,agent_runtime
from paperpilot.services import agent_conversations as store
from paperpilot.services.project_errors import ProjectServiceError


class SteeringIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class AttachmentIn(BaseModel):
    request_id: str = Field(min_length=1,max_length=120)
    filename: str = Field(min_length=1,max_length=240)
    content_base64: str = Field(min_length=1,max_length=28000000)
    pages: list[int] = Field(default_factory=list,max_length=40)


def create_router(database,project_read,project_write,call_ai):
    router=APIRouter(prefix='/api/projects/{project_id}/agent',tags=['paper agent'])
    @contextmanager
    def connection(pid,writable=False):
        (project_write if writable else project_read)(pid)
        conn=database()
        try:yield conn
        except ProjectServiceError as exc:
            conn.rollback()
            raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        except sqlite3.Error as exc:
            conn.rollback()
            raise HTTPException(503,detail={'code':'agent_storage_failed','message':'Agent存储暂时不可用；输入保留，请核对后重试。'}) from exc
        finally:conn.close()
    def dispatch(conn,tasks,result):
        if result['status']=='queued':tasks.add_task(agent_runtime.schedule,conn.execute('PRAGMA database_list').fetchone()['file'],result['id'],call_ai)
        return result
    @router.get('/conversations')
    def conversations(project_id:str):
        with connection(project_id) as c:return {'conversations':store.list_all(c,project_id)}
    @router.get('/tools')
    def tools(project_id: str, query: str = ''):
        from paperpilot.services.workspace_catalog import catalog
        with connection(project_id):
            return {'items': catalog(query)}
    @router.get('/conversations/{conversation_id}/attachments')
    def attachments(project_id:str,conversation_id:str):
        from paperpilot.services import agent_attachments
        with connection(project_id) as c:
            return {'files':agent_attachments.list_files(c,project_id,conversation_id)}
    @router.post('/conversations/{conversation_id}/attachments')
    def attach(project_id:str,conversation_id:str,payload:AttachmentIn):
        from paperpilot.services import agent_attachments
        with connection(project_id,True) as c:
            with c:
                c.execute('BEGIN IMMEDIATE')
                return agent_attachments.create(c,project_id,conversation_id,payload.model_dump())
    @router.get('/conversations/{conversation_id}/attachments/{attachment_id}')
    def read_attachment(project_id:str,conversation_id:str,attachment_id:str):
        with connection(project_id) as c:
            store.get(c,project_id,conversation_id)
            row=c.execute("SELECT id,filename,sha256,content,size,pages_json,result_bytes FROM agent_attachments WHERE id=? AND project_id=? AND conversation_id=? AND status='active'",(attachment_id,project_id,conversation_id)).fetchone()
            if row is None:
                raise ProjectServiceError(404,'attachment_not_found','当前对话附件不存在或已移除。')
            item = dict(row)
            raw = item.pop("result_bytes")
            if raw is not None:
                from paperpilot.services import results
                columns, rows = results.parse(raw, item["filename"])
                item.update(columns=columns, row_count=len(rows),
                            rows=[[value[:250] for value in values] for values in rows[:results.PREVIEW_ROWS]])
            return item
    @router.delete('/conversations/{conversation_id}/attachments/{attachment_id}')
    def remove_attachment(project_id:str,conversation_id:str,attachment_id:str):
        with connection(project_id,True) as c:
            store.get(c,project_id,conversation_id)
            with c:
                changed=c.execute("UPDATE agent_attachments SET status='removed' WHERE id=? AND project_id=? AND conversation_id=?",(attachment_id,project_id,conversation_id)).rowcount
                if not changed:
                    raise ProjectServiceError(404,'attachment_not_found','当前对话附件不存在。')
            return {'ok':True}
    @router.get('/conversations/{conversation_id}/attachments/{attachment_id}/download')
    def download_result_attachment(project_id: str, conversation_id: str, attachment_id: str):
        from paperpilot.api.results import attachment
        from paperpilot.services import visual_assets
        with connection(project_id) as c:
            store.get(c, project_id, conversation_id)
            row = c.execute("""SELECT filename,sha256,result_bytes FROM agent_attachments WHERE project_id=?
                AND conversation_id=? AND id=? AND status='active' AND result_bytes IS NOT NULL""",
                (project_id, conversation_id, attachment_id)).fetchone()
            if row is None:
                raise ProjectServiceError(404, 'attachment_not_found', '表格原件已移除，请重新添加附件。')
            if visual_assets.digest(row["result_bytes"]) != row["sha256"]:
                raise ProjectServiceError(409, 'attachment_changed', '附件校验值不符，请重新添加。')
            return attachment(row["filename"], row["result_bytes"])
    @router.get('/subtasks')
    def subtasks(project_id: str, run_id: str = ''):
        import json
        with connection(project_id) as c:
            rows = c.execute('SELECT id,run_id,conversation_id,module_key,instruction,status,result_json,created_at,updated_at FROM agent_subtasks WHERE project_id=? AND (?=\'\' OR run_id=?) ORDER BY created_at DESC LIMIT 40', (project_id,run_id,run_id)).fetchall()
            return {'tasks': [dict(row) | {'result': json.loads(row['result_json'])} for row in rows]}
    @router.post('/runs/{run_id}/steering')
    def steer(project_id: str, run_id: str, payload: SteeringIn):
        import uuid
        with connection(project_id, True) as c:
            with c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM agent_runs WHERE id=? AND project_id=?', (run_id,project_id)).fetchone()
                if row is None:
                    raise ProjectServiceError(404,'agent_run_not_found','运行记录不存在。')
                old = c.execute('SELECT * FROM agent_steering WHERE run_id=? AND request_id=?', (run_id,payload.request_id)).fetchone()
                if old:
                    if old['text'] != payload.text:
                        raise ProjectServiceError(409,'idempotency_key_reused','请求编号已用于其他补充要求。')
                    return {'id':old['id'],'status':old['status']}
                if row['status'] not in ('queued','running'):
                    raise ProjectServiceError(409,'run_finished','此轮已结束，请将要求作为新消息发送。')
                sid=uuid.uuid4().hex
                c.execute('INSERT INTO agent_steering(id,project_id,run_id,request_id,text,created_at) VALUES(?,?,?,?,?,?)', (sid,project_id,run_id,payload.request_id,payload.text,store.stamp()))
                store.append_message(c,project_id,row['conversation_id'],'user',payload.text,context={'steering_id':sid},run_id=run_id)
            return {'id':sid,'status':'queued'}
    @router.get('/workspace')
    def agent_workspace(project_id:str):
        with connection(project_id) as c:return store.workspace(c,project_id)
    @router.post('/workspace')
    def ensure_agent_workspace(project_id:str):
        from paperpilot.services import redmaple_runtime
        with connection(project_id,True) as c:
            result=store.workspace(c,project_id,ensure=True)
        redmaple_runtime.warm_workers()
        return result
    @router.get('/research')
    def research_workspace(project_id: str):
        from paperpilot.services import scholar_workspace
        with connection(project_id):
            return scholar_workspace.snapshot(project_id)
    @router.post('/conversations')
    def create(project_id:str,payload:ConversationCreateIn):
        with connection(project_id,True) as c:return store.create(c,project_id,payload.title,payload.parent_id,payload.module_key)
    @router.get('/conversations/{conversation_id}/messages')
    def messages(project_id:str,conversation_id:str,cursor:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),tail:bool=False,before_seq:int|None=Query(None,ge=1)):
        with connection(project_id) as c:return store.messages(c,project_id,conversation_id,cursor,limit,tail,before_seq)
    @router.get('/conversations/{conversation_id}/requests/{request_id}')
    def request_status(project_id:str,conversation_id:str,request_id:str):
        with connection(project_id) as c:
            store.get(c,project_id,conversation_id)
            row=c.execute('SELECT r.* FROM agent_turns t JOIN agent_runs r ON r.id=t.run_id WHERE t.project_id=? AND t.conversation_id=? AND t.request_id=?',(project_id,conversation_id,request_id)).fetchone()
            return {'run':agent_runtime._public_run(row) if row else None}
    @router.post('/conversations/{conversation_id}/cards/{card_id}/decision')
    def decide(project_id:str,conversation_id:str,card_id:str,payload:CardDecisionIn):
        with connection(project_id,True) as c:return store.decide_card(c,project_id,conversation_id,card_id,payload.model_dump())
    @router.post('/preview')
    def preview(project_id:str,payload:ConversationPreviewIn):
        with connection(project_id) as c:return {k:v for k,v in agent_runtime.preview(c,project_id,payload.model_dump()).items() if not k.startswith('_')}
    @router.post('/runs')
    def start(project_id:str,payload:ConversationRunIn,tasks:BackgroundTasks):
        with connection(project_id,True) as c:return dispatch(c,tasks,agent_runtime.start(c,project_id,payload.model_dump()))
    @router.get('/runs')
    def runs(project_id:str):
        with connection(project_id) as c:return [agent_runtime._public_run(r) for r in c.execute('SELECT * FROM agent_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 20',(project_id,))]
    @router.get('/runs/{run_id}')
    def run(project_id:str,run_id:str,after_seq:int=Query(0,ge=0)):
        with connection(project_id) as c:return agent_runtime.get_run(c,project_id,run_id,after_seq)
    @router.post('/runs/{run_id}/cancel')
    def cancel(project_id:str,run_id:str):
        with connection(project_id,True) as c:return agent_runtime.cancel(c,project_id,run_id)
    @router.post('/runs/{run_id}/resume/preview')
    def resume_preview(project_id:str,run_id:str):
        with connection(project_id) as c:return agent_runtime.resume_preview(c,project_id,run_id)
    @router.post('/runs/{run_id}/resume')
    def resume(project_id:str,run_id:str,payload:ResumeIn,tasks:BackgroundTasks):
        with connection(project_id,True) as c:return dispatch(c,tasks,agent_runtime.resume(c,project_id,run_id,payload.model_dump()))
    @router.get('/memories')
    def memories(project_id:str,kind:str=''):
        with connection(project_id) as c:return {'memories':agent_memory.list_memories(c,project_id,kind)}
    @router.post('/memories/{memory_id}/decision')
    def memory_decision(project_id:str,memory_id:str,payload:AgentMemoryDecisionIn):
        with connection(project_id,True) as c:
            with c:result=agent_memory.decide(c,project_id,memory_id,payload.action)
            if not result.get('ok'):raise HTTPException(409,detail={'code':result.get('error'),'message':'来源已失效或决定无法完成。'})
            return result
    return router

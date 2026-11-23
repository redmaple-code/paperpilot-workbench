"""Fixed-reference bibliography copy with review and guarded commit."""
from typing import Literal
import sqlite3
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from paperpilot.services import bibliography_import
from paperpilot.services.project_errors import ProjectServiceError


class PreviewIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference_id:str=Field(min_length=1,max_length=120)
    target_path:str=Field(default='references.bib',min_length=1,max_length=240)


class CommitIn(PreviewIn):
    request_id:str=Field(min_length=1,max_length=120)
    preview_fingerprint:str=Field(min_length=64,max_length=64)
    confirm:bool=False
    conflict_resolution:Literal['none','replace']='none'


def create_router(database,project_write,clean_path):
    router=APIRouter(tags=['reviewed bibliography import'])
    def execute(pid,payload,write):
        project_write(pid);conn=database()
        try:
            data=payload.model_dump();data['target_path']=clean_path(data['target_path'])
            with conn:
                if write:return bibliography_import.commit(conn,pid,data)
                return bibliography_import.preview(conn,pid,data['reference_id'],data['target_path'])
        except ProjectServiceError as exc:raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        except sqlite3.Error as exc:raise HTTPException(503,detail={'code':'bib_storage_failed','message':'工程bib暂时无法保存，事务已回滚；请保留输入并重试。'}) from exc
        finally:conn.close()
    @router.post('/api/projects/{project_id}/bibliography/preview')
    def preview(project_id:str,payload:PreviewIn):return execute(project_id,payload,False)
    @router.post('/api/projects/{project_id}/bibliography/commit')
    def commit(project_id:str,payload:CommitIn):return execute(project_id,payload,True)
    return router

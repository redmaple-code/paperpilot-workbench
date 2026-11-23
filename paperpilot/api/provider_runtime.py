"""Guided provider lifecycle, with credentials confined to the server or one operation."""
from contextlib import contextmanager
import sqlite3
import requests
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field,ConfigDict
from typing import Literal
from paperpilot.services import provider_runtime as runtime,provider_config as providers
from paperpilot.services.project_errors import ProjectServiceError


class TargetIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    profile_id: str = Field(min_length=1,max_length=120)
    config_revision: int = Field(ge=1)
    model_id: str = Field(min_length=1,max_length=200)


class DefaultIn(TargetIn):
    expected_version: int = Field(ge=0)


class ApplyIn(TargetIn):
    project_revisions: dict[str,int] = Field(min_length=1,max_length=200)
    purpose: Literal['writing','drawing','vision'] = 'writing'


class DiscoverIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    expected_version: int = Field(ge=1)
    confirm_network: bool = False
    temporary_key: str = Field(default='',max_length=500)


def create_router(database,project_read,project_write):
    router=APIRouter(tags=['provider lifecycle'])
    @contextmanager
    def connection():
        conn=database()
        try:
            with conn:yield conn
        except ProjectServiceError as exc:raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        except sqlite3.Error as exc:raise HTTPException(503,detail={'code':'provider_storage_failed','message':'模型配置暂时无法保存，当前输入保留；请稍后刷新核对。'}) from exc
        finally:conn.close()

    @router.get('/api/ai/default')
    def default():
        with connection() as conn:
            row=conn.execute("SELECT * FROM ai_provider_defaults WHERE id='default'").fetchone()
            return dict(row) if row else {'version':0,'profile_id':None,'applies_to':'new_papers_only'}

    @router.put('/api/ai/default')
    def set_default(payload:DefaultIn):
        with connection() as conn:return runtime.set_default(conn,payload.profile_id,payload.config_revision,payload.model_id,payload.expected_version)

    @router.get('/api/ai/profiles/{profile_id}/versions')
    def versions(profile_id:str):
        with connection() as conn:
            if not conn.execute('SELECT 1 FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone():raise HTTPException(404,'配置不存在。')
            output=[]
            for row in conn.execute('SELECT version FROM ai_provider_profile_versions WHERE profile_id=? ORDER BY version DESC',(profile_id,)):
                config=runtime.configuration(conn,profile_id,row['version'])
                output.append({k:config[k] for k in ('profile_id','config_revision','name','base_url','auth_mode','models','capabilities','model_limits')}|
                    {'status':config['global_status'] if config['global_status'] in {'disabled','revoked'} else config['status'],
                    'ready_modes':{model:[mode for mode in sorted(runtime.MODES) if runtime.tested(conn,config,model,mode)] for model in config['models']}})
            return output

    @router.get('/api/ai/profiles/{profile_id}/impact')
    def impact(profile_id:str,purpose:Literal['writing','drawing','vision']='writing'):
        with connection() as conn:
            config=runtime.configuration(conn,profile_id)
            return {'profile_id':profile_id,'latest_version':config['config_revision'],'latest_status':config['status'],
                'papers':[dict(r) for r in conn.execute('SELECT id,name,ai_config_revision AS bound_version,ai_model_id AS model_id,project_revision,lifecycle_state FROM projects WHERE ai_profile_id=? ORDER BY name',(profile_id,))] if purpose=='writing' else [dict(r) for r in conn.execute('SELECT p.id,p.name,b.profile_version AS bound_version,b.model_id,p.project_revision,p.lifecycle_state FROM projects p JOIN project_visual_provider_bindings b ON b.project_id=p.id WHERE b.profile_id=? AND b.purpose=? ORDER BY p.name',(profile_id,purpose))],
                'purpose':purpose,
                'next_action':'仅勾选的论文会切换版本；未选论文与已排队请求保留各自快照。'}

    @router.post('/api/ai/profiles/{profile_id}/apply')
    def apply(profile_id:str,payload:ApplyIn):
        if profile_id!=payload.profile_id:raise HTTPException(422,'提供方身份不一致。')
        for pid in payload.project_revisions:project_write(pid)
        with connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for pid,revision in payload.project_revisions.items():
                paper=conn.execute('SELECT ai_profile_id,lifecycle_state FROM projects WHERE id=?',(pid,)).fetchone()
                binding=conn.execute('SELECT profile_id FROM project_visual_provider_bindings WHERE project_id=? AND purpose=?',(pid,payload.purpose)).fetchone() if payload.purpose!='writing' else None
                belongs=paper and paper['ai_profile_id']==profile_id if payload.purpose=='writing' else binding and binding['profile_id']==profile_id
                if not paper or not belongs or paper['lifecycle_state']=='published':
                    raise ProjectServiceError(409,'paper_not_applicable','选择的论文不再属于此配置或已发布；没有批量覆盖，请刷新影响清单。')
                if payload.purpose=='writing':providers.bind_project(conn,pid,profile_id,payload.model_id,revision,payload.config_revision)
                else:
                    from paperpilot.services import visual_provider
                    visual_provider.bind(conn,pid,{'profile_id':profile_id,'model_id':payload.model_id,'config_revision':payload.config_revision,'expected_project_revision':revision,'purpose':payload.purpose})
            return {'applied_project_ids':list(payload.project_revisions),'config_revision':payload.config_revision,'queued_requests':'unchanged'}

    @router.post('/api/ai/profiles/{profile_id}/discover')
    def discover(profile_id:str,payload:DiscoverIn):
        if not payload.confirm_network:raise HTTPException(422,detail={'code':'network_confirmation_required','message':'发现模型会向此配置服务发送列表请求；请明确发起，不含论文内容。'})
        with connection() as conn:
            config=runtime.configuration(conn,profile_id)
            if config['config_revision']!=payload.expected_version:raise ProjectServiceError(409,'provider_version_conflict','配置版本已变化，请刷新后重新发现。')
            credential=providers.resolve_credentials(conn,profile_id,config['config_revision'],temporary_key=payload.temporary_key or None)
        try:
            response=requests.get(config['base_url'].rstrip('/')+'/models',headers={'Authorization':'Bearer '+credential.key} if credential.key else {},timeout=(5,15),allow_redirects=False)
            try:
                if not 200<=response.status_code<300:raise ProjectServiceError(422,'model_discovery_failed','列表发现未通过；请核对地址/鉴权，或手工登记模型再做生成测试。')
                data=response.json();items=data.get('data')
                if not isinstance(items,list):raise ValueError('schema')
                candidates=[item['id'] for item in items[:100] if isinstance(item,dict) and isinstance(item.get('id'),str) and 0<len(item['id'])<=200 and not (credential.key and credential.key in item['id'])]
            finally:response.close()
        except ProjectServiceError as exc:raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc
        except (requests.RequestException,ValueError,TypeError,AttributeError) as exc:raise HTTPException(502,detail={'code':'model_discovery_failed','message':'无法读取兼容模型列表；可以手工登记，随后测试；不会自动启用。'}) from exc
        return {'candidates':candidates,'registered_automatically':False,'generation_test_passed':False,'message':'列表仅供选择；请明确登记并完成相关生成测试。'}

    @router.get('/api/projects/{project_id}/ai-audit')
    def audit(project_id:str):
        project_read(project_id)
        with connection() as conn:return [dict(r) for r in conn.execute('SELECT * FROM ai_provider_audit WHERE project_id=? ORDER BY created_at DESC LIMIT 100',(project_id,))]

    @router.delete('/api/projects/{project_id}/ai-audit')
    def delete_audit(project_id:str):
        project_write(project_id)
        with connection() as conn:
            conn.execute('DELETE FROM ai_provider_audit WHERE project_id=?',(project_id,))
            return {'deleted':True,'request_and_manuscript_unchanged':True}

    return router

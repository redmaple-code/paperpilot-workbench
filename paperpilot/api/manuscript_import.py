"""Author manuscript imports. Public-paper OA imports retain their own routes."""
from __future__ import annotations

import base64
from contextlib import closing
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from paperpilot.services import manuscript_apply as adoption, manuscript_import as store, manuscript_sources as parser
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services import term_comparison
from paperpilot.domain.term_comparison_schemas import ComparisonIn, ComparisonApplyIn


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Request(Body):
    request_id: str = Field(min_length=1,max_length=120)


class BatchIn(Request):
    target_mode: Literal['new','existing']
    name: str = Field(default='',max_length=160)
    project_id: str | None = Field(default=None,max_length=40)


class UploadIn(Request):
    filename: str = Field(min_length=1,max_length=150)
    content_base64: str = Field(min_length=1,max_length=((parser.MAX_FILE_BYTES+2)//3)*4)


class PrimaryIn(Body):
    source_id: str = Field(min_length=1,max_length=40)


class FragmentIn(Body):
    revision: int = Field(ge=1)
    text: str = Field(min_length=1,max_length=16000)


class RetryIn(Body):
    revision: int = Field(ge=1)
    page: int = Field(ge=1,le=10000)


class FingerprintIn(Body):
    fingerprint: str = Field(pattern=r'^[0-9a-f]{24}$')


class ModelPreviewIn(Body):
    chunks: list[int] | None = Field(default=None,max_length=6)


class ModelRunIn(Request,FingerprintIn):
    chunks: list[int] = Field(min_length=1,max_length=6)
    authorize: Literal[True]


class ApplyIn(Request,FingerprintIn):
    decisions: dict[str,Literal['add','keep','replace','skip','enable_planning']] = Field(min_length=1,max_length=3000)


def create_router(database, storage_root, seed_workspace, model_call):
    router=APIRouter(prefix='/api/manuscript-imports',tags=['manuscript imports'])

    def using(service,*args,write=False):
        try:
            with closing(database()) as conn, conn:
                if not write:
                    conn.execute('BEGIN')
                return service(conn,*args)
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc

    @router.get('')
    def list_imports():
        return {'batches':using(store.list_batches)}

    @router.post('')
    def create_import(payload:BatchIn):
        return using(store.create,payload.request_id,payload.target_mode,payload.name,payload.project_id,write=True)

    @router.get('/{batch_id}')
    def get_import(batch_id:str):
        return using(store.view,batch_id)

    @router.post('/{batch_id}/sources')
    def upload(batch_id:str,payload:UploadIn):
        try:
            raw=base64.b64decode(payload.content_base64,validate=True)
        except ValueError as exc:
            raise HTTPException(422,detail={'code':'invalid_encoding','message':'文件编码无效，请重新选择原件。'}) from exc
        return using(store.upload,batch_id,payload.request_id,payload.filename,raw,write=True)

    @router.get('/{batch_id}/sources/{source_id}/original')
    def download(batch_id:str,source_id:str,inline:bool=False):
        def original(conn,bid,sid):
            return store.source_row(conn,bid,sid,include_raw=True)
        source=using(original,batch_id,source_id)
        filename=source['name'].split('/')[-1]
        preview_pdf=inline and source['kind']=='pdf'
        disposition='inline' if preview_pdf else 'attachment'
        return Response(source['raw'],media_type='application/pdf' if preview_pdf else 'application/octet-stream',headers={
            'Content-Disposition':disposition+"; filename*=UTF-8''"+quote(filename,safe=''),
            'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})

    @router.put('/{batch_id}/primary')
    def choose_primary(batch_id:str,payload:PrimaryIn):
        return using(store.select_primary,batch_id,payload.source_id,write=True)

    @router.put('/{batch_id}/sources/{source_id}/fragments/{fragment_id}')
    def correct(batch_id:str,source_id:str,fragment_id:str,payload:FragmentIn):
        return using(store.revise_fragment,batch_id,source_id,fragment_id,payload.text,payload.revision,write=True)

    @router.post('/{batch_id}/sources/{source_id}/retry-page')
    def retry_page(batch_id:str,source_id:str,payload:RetryIn):
        return using(store.retry_page,batch_id,source_id,payload.page,payload.revision,write=True)

    @router.get('/{batch_id}/removal-preview')
    def remove_preview(batch_id:str,source_id:str|None=None):
        return using(store.removal_preview,batch_id,source_id)

    @router.post('/{batch_id}/cancel')
    def cancel(batch_id:str,payload:FingerprintIn):
        return using(store.remove,batch_id,payload.fingerprint,write=True)

    @router.post('/{batch_id}/sources/{source_id}/remove')
    def remove(batch_id:str,source_id:str,payload:FingerprintIn):
        return using(store.remove,batch_id,payload.fingerprint,source_id,write=True)

    @router.post('/{batch_id}/model-preview')
    def model_preview(batch_id:str,payload:ModelPreviewIn):
        return using(store.model_preview,batch_id,payload.chunks)

    @router.post('/{batch_id}/model-runs')
    def run_model(batch_id:str,payload:ModelRunIn):
        try:
            return store.model_run(database,batch_id,payload.request_id,payload.fingerprint,payload.chunks,call=model_call)
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code,detail={'code':exc.code,'message':exc.message}) from exc

    @router.get('/{batch_id}/preview')
    def split_preview(batch_id:str):
        return using(adoption.preview,batch_id)

    @router.post('/{batch_id}/terminology/preview')
    def preview_terms(batch_id: str, payload: ComparisonIn):
        return using(term_comparison.preview, None, batch_id, payload.reference_ids)

    @router.post('/{batch_id}/terminology/apply')
    def apply_terms(batch_id: str, payload: ComparisonApplyIn):
        return using(term_comparison.apply, payload.model_dump(), None, batch_id, seed_workspace, write=True)

    @router.post('/{batch_id}/apply')
    def apply(batch_id:str,payload:ApplyIn):
        return using(adoption.apply,storage_root(),seed_workspace,batch_id,payload.request_id,
                     payload.fingerprint,payload.decisions,write=True)

    return router

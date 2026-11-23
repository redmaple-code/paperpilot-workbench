from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from paperpilot.domain.visual_schemas import AssetCopyIn, AssetImportIn, AssetInsertIn, AssetReplaceIn, AssetSelectIn, AssetStateIn
from paperpilot.services import visual_assets as assets, visual_insertion
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database: Callable, project_read: Callable, project_write: Callable, storage_root: Callable[[], Path], clean_path: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/drawing", tags=["visual assets"])

    @contextmanager
    def connection(project_id: str, writable: bool = False):
        (project_write if writable else project_read)(project_id)
        conn = database()
        try:
            with conn:
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except OSError as exc:
            raise HTTPException(503, detail={"code": "asset_storage_failed", "message": "资产文件无法保存或读取；当前版本保持不变，请检查可用空间后重试。"}) from exc
        finally:
            conn.close()

    @router.get("/assets")
    def list_assets(project_id: str, include_archived: bool = False):
        with connection(project_id) as conn:
            return {"assets": assets.list_assets(conn, project_id, include_archived)}

    @router.post("/assets/import")
    def import_asset(project_id: str, payload: AssetImportIn):
        with connection(project_id, True) as conn:
            return assets.save_asset(conn, storage_root(), project_id, content=assets.decode_upload(payload.content_base64),
                                     filename=payload.filename, title=payload.title, request_id=payload.request_id,
                                     page_no=payload.page_no, origin_ref=payload.origin_ref)

    @router.post("/assets/copy")
    def copy_asset(project_id: str, payload: AssetCopyIn):
        project_read(payload.source_project_id)
        with connection(project_id, True) as conn:
            return assets.copy_asset(conn, storage_root(), project_id, payload.source_project_id, payload.source_version_id, payload.title, payload.request_id)

    @router.get("/storage-check")
    def storage_check(project_id: str):
        with connection(project_id) as conn:
            return assets.reconcile_storage(conn, storage_root(), project_id)

    @router.post("/storage-recovery")
    def storage_recovery(project_id: str):
        with connection(project_id, True) as conn:
            return assets.reconcile_storage(conn, storage_root(), project_id, cleanup=True)

    @router.get("/assets/{asset_id}")
    def read_asset(project_id: str, asset_id: str):
        with connection(project_id) as conn:
            return assets.read(conn, project_id, asset_id)

    @router.post("/assets/{asset_id}/versions")
    def replace_asset(project_id: str, asset_id: str, payload: AssetReplaceIn):
        with connection(project_id, True) as conn:
            return assets.save_asset(conn, storage_root(), project_id, content=assets.decode_upload(payload.content_base64),
                                     filename=payload.filename, title=payload.title, request_id=payload.request_id, asset_id=asset_id,
                                     expected_version=payload.expected_version, page_no=payload.page_no, origin_ref=payload.origin_ref)

    @router.post("/assets/{asset_id}/archive")
    def archive_asset(project_id: str, asset_id: str, payload: AssetStateIn):
        with connection(project_id, True) as conn:
            return assets.set_archived(conn, project_id, asset_id, True, payload.expected_revision, payload.request_id)

    @router.post("/assets/{asset_id}/restore")
    def restore_asset(project_id: str, asset_id: str, payload: AssetStateIn):
        with connection(project_id, True) as conn:
            return assets.set_archived(conn, project_id, asset_id, False, payload.expected_revision, payload.request_id)

    @router.get("/assets/{asset_id}/versions/{version_id}/preview")
    def preview_asset(project_id: str, asset_id: str, version_id: str):
        with connection(project_id) as conn:
            row = assets.version(conn, project_id, version_id)
            if row["asset_id"] != asset_id:
                raise ProjectServiceError(404, "resource_missing", "该版本不属于所选资产。")
            path = assets.checked_file(conn, storage_root(), project_id, version_id, "preview")
            return FileResponse(path, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})

    @router.get("/assets/{asset_id}/versions/{version_id}/download")
    def download_asset(project_id: str, asset_id: str, version_id: str):
        with connection(project_id) as conn:
            row = assets.version(conn, project_id, version_id)
            if row["asset_id"] != asset_id:
                raise ProjectServiceError(404, "resource_missing", "该版本不属于所选资产。")
            path = assets.checked_file(conn, storage_root(), project_id, version_id, "original")
            return FileResponse(path, media_type="application/octet-stream", filename=row["filename"], headers={"X-Content-Type-Options": "nosniff"})

    @router.post("/assets/{asset_id}/select-for-editor")
    def select_asset(project_id: str, asset_id: str, payload: AssetSelectIn):
        with connection(project_id) as conn:
            row = assets.version(conn, project_id, payload.version_id)
            if row["asset_id"] != asset_id or assets.asset(conn, project_id, asset_id)["status"] != "active":
                raise ProjectServiceError(409, "asset_unavailable", "请选择当前论文中有效的资产版本。")
            assets.checked_file(conn, storage_root(), project_id, payload.version_id)
            return {"asset_id": asset_id, "version_id": row["id"], "sha256": row["build_sha256"], "path": row["build_name"],
                    "latex": "\\includegraphics[width=\\linewidth]{" + row["build_name"] + "}", "body_written": False}

    @router.post("/assets/{asset_id}/insert-into-editor")
    def insert_asset(project_id: str, asset_id: str, payload: AssetInsertIn):
        with connection(project_id, True) as conn:
            checked = payload.model_copy(update={"target_path": clean_path(payload.target_path)})
            return visual_insertion.insert(conn, storage_root(), project_id, asset_id, checked)

    @router.delete("/assets/{asset_id}/versions/{version_id}")
    def delete_version(project_id: str, asset_id: str, version_id: str):
        with connection(project_id, True) as conn:
            row = assets.version(conn, project_id, version_id)
            if row["asset_id"] != asset_id:
                raise ProjectServiceError(404, "resource_missing", "该版本不属于所选资产。")
            used = conn.execute("SELECT 1 FROM asset_references WHERE project_id=? AND version_id=?", (project_id, version_id)).fetchone()
            if used:
                raise ProjectServiceError(409, "asset_referenced", "该版本被正文或历史构建引用，不能硬删除；可以归档资产。")
            raise ProjectServiceError(409, "immutable_version", "历史版本不可直接覆盖或删除；请使用归档保留可恢复历史。")

    return router

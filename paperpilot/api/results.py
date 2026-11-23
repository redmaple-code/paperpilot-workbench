"""Research data APIs scoped by the current paper."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints

from paperpilot.services import result_charts, result_collections, result_conversations, results, visual_assets
from paperpilot.services.project_errors import ProjectServiceError

Column = Annotated[str, StringConstraints(min_length=1, max_length=120)]
RequestID = Annotated[str, StringConstraints(min_length=1, max_length=120)]
AxisLabel = Annotated[str, StringConstraints(max_length=120, pattern=r"^[^\x00-\x1f]*$")]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    title: str = Field(default="", max_length=160)
    content_base64: str
    note: str = Field(default="", max_length=1000)
    request_id: RequestID


class SelectColumns(BaseModel):
    kind: Literal["select"]
    columns: list[Column] = Field(min_length=1, max_length=100)


class DropBlank(BaseModel):
    kind: Literal["drop_blank"]


class Deduplicate(BaseModel):
    kind: Literal["deduplicate"]


class FilterRange(BaseModel):
    kind: Literal["filter"]
    column: Column
    minimum: str | None = Field(default=None, max_length=64)
    maximum: str | None = Field(default=None, max_length=64)


class Summarize(BaseModel):
    kind: Literal["summarize"]
    group_by: Column
    column: Column


class DeriveIn(BaseModel):
    operation: Annotated[SelectColumns | DropBlank | Deduplicate | FilterRange | Summarize, Field(discriminator="kind")]
    note: str = Field(default="", max_length=1000)
    request_id: RequestID


class ChartIn(BaseModel):
    kind: Literal["line", "scatter"]
    x_column: Column
    y_column: Column
    title: Annotated[str, StringConstraints(max_length=72, pattern=r"^[^\x00-\x1f]*$")] = ""
    x_label: AxisLabel = ""
    y_label: AxisLabel = ""


class SaveChartIn(BaseModel):
    chart: ChartIn
    preview_sha256: Hash
    request_id: RequestID


class ResultTurnIn(BaseModel):
    request_id: RequestID
    instruction: str = Field(min_length=1, max_length=40000)
    reference_id: Column | None = None
    dataset_id: Column | None = None
    asset_id: Column | None = None
    version_id: Column | None = None
    file_path: str = Field(default="", max_length=500)


class ResultDecisionIn(BaseModel):
    request_id: RequestID
    action: Literal["choose", "confirm", "cancel"]
    option: int | None = None


class LibraryLabelIn(BaseModel):
    title: str = Field(max_length=160)
    group: str = Field(max_length=80)


def attachment(filename: str, content: bytes) -> Response:
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })


def create_router(database, project_read, project_write, storage_root) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/results", tags=["research results"])

    @contextmanager
    def connection(project_id: str, writable: bool = False):
        (project_write if writable else project_read)(project_id)
        conn = database()
        try:
            with conn:
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except (OSError, sqlite3.OperationalError) as exc:
            raise HTTPException(503, detail={"code": "result_storage_failed", "message": "结果或图像存储暂时不可用，请检查磁盘后重试。"}) from exc
        finally:
            conn.close()

    def upload_file(project_id: str, dataset_id: str | None, payload: UploadIn):
        with connection(project_id, True) as conn:
            return results.upload(conn, project_id, dataset_id=dataset_id, filename=payload.filename, title=payload.title,
                                  content=results.decode_upload(payload.content_base64), note=payload.note, request_id=payload.request_id)

    @router.get("")
    def datasets(project_id: str):
        with connection(project_id) as conn:
            return {"datasets": results.list_datasets(conn, project_id)}

    @router.post("")
    def create_dataset(project_id: str, payload: UploadIn):
        return upload_file(project_id, None, payload)

    @router.get("/collections")
    def collections(project_id: str):
        with connection(project_id) as conn:
            return {"collections": result_collections.list_collections(conn, project_id)}

    @router.get("/collections/{collection_id}/sources/{source_id}")
    def download_source(project_id: str, collection_id: str, source_id: str):
        with connection(project_id) as conn:
            filename, content = result_collections.read_source(conn, project_id, collection_id, source_id)
            return attachment(filename, content)

    @router.get("/library")
    def result_library(project_id: str, q: str = ""):
        with connection(project_id) as conn:
            return result_conversations.library(conn, project_id, q[:250])

    @router.put("/library/items/{kind}/{item_id}")
    def update_library_item(project_id: str, kind: Literal["table", "figure"], item_id: str, payload: LibraryLabelIn):
        with connection(project_id, True) as conn:
            return result_conversations.label_library_item(conn, project_id, kind, item_id, payload.title, payload.group)

    @router.post("/conversations/{conversation_id}/turn")
    def result_turn(project_id: str, conversation_id: str, payload: ResultTurnIn):
        with connection(project_id) as conn:
            return result_conversations.turn(conn, storage_root(), project_id, conversation_id, payload.model_dump())

    @router.get("/conversations/{conversation_id}/cards/{card_id}")
    def result_card(project_id: str, conversation_id: str, card_id: str):
        with connection(project_id) as conn:
            return result_conversations.read_card(conn, project_id, conversation_id, card_id)

    @router.get("/conversations/{conversation_id}/cards/{card_id}/preview")
    def result_preview(project_id: str, conversation_id: str, card_id: str):
        with connection(project_id) as conn:
            card = result_conversations.card_row(conn, project_id, conversation_id, card_id)
            if not card["svg"]:
                raise ProjectServiceError(404, "result_preview_missing", "此结果没有图像预览。")
            return Response(card["svg"], media_type="image/svg+xml", headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            })

    @router.post("/conversations/{conversation_id}/cards/{card_id}/decision")
    def result_decision(project_id: str, conversation_id: str, card_id: str, payload: ResultDecisionIn):
        with connection(project_id, True) as conn:
            return result_conversations.decide(conn, storage_root(), project_id, conversation_id, card_id, payload.model_dump())

    @router.get("/{dataset_id}")
    def read_dataset(project_id: str, dataset_id: str):
        with connection(project_id) as conn:
            return results.read_dataset(conn, project_id, dataset_id)

    @router.post("/{dataset_id}/versions")
    def import_version(project_id: str, dataset_id: str, payload: UploadIn):
        return upload_file(project_id, dataset_id, payload)

    @router.get("/{dataset_id}/versions/{version_id}")
    def read_version(project_id: str, dataset_id: str, version_id: str):
        with connection(project_id) as conn:
            return results.read_version(conn, project_id, dataset_id, version_id)

    @router.get("/{dataset_id}/versions/{version_id}/download")
    def download(project_id: str, dataset_id: str, version_id: str):
        with connection(project_id) as conn:
            row = results.version(conn, project_id, dataset_id, version_id, with_content=True)
            return attachment(row["filename"], results.checked_content(row))

    @router.post("/{dataset_id}/versions/{version_id}/derive")
    def derive(project_id: str, dataset_id: str, version_id: str, payload: DeriveIn):
        with connection(project_id, True) as conn:
            return results.derive(conn, project_id, dataset_id, version_id, operation=payload.operation.model_dump(),
                                  note=payload.note, request_id=payload.request_id)

    @router.post("/{dataset_id}/versions/{version_id}/chart-preview")
    def chart_preview(project_id: str, dataset_id: str, version_id: str, payload: ChartIn):
        with connection(project_id) as conn:
            svg, provenance = result_charts.render_version(conn, project_id, dataset_id, version_id, payload.model_dump())
            return {"svg": svg.decode("utf-8"), "sha256": visual_assets.digest(svg), "source": provenance}

    @router.post("/{dataset_id}/versions/{version_id}/chart")
    def save_chart(project_id: str, dataset_id: str, version_id: str, payload: SaveChartIn):
        with connection(project_id, True) as conn:
            svg, provenance = result_charts.render_version(conn, project_id, dataset_id, version_id, payload.chart.model_dump())
            if visual_assets.digest(svg) != payload.preview_sha256:
                raise ProjectServiceError(409, "chart_preview_changed", "图表与预览已不同，请重新生成预览。")
            return visual_assets.save_asset(
                conn, storage_root(), project_id, content=svg, filename="result-chart.svg",
                title=provenance["chart"]["title"], request_id=f"result-chart:{payload.request_id}",
                kind="result_chart", origin_ref=f"result:{version_id}", editable_source=visual_assets.compact(provenance),
                _before_write=lambda *_: results.require_writable(conn, project_id),
            )

    return router

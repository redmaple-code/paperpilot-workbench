from __future__ import annotations

from pydantic import BaseModel, Field


class AssetImportIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=28_000_000)
    title: str = Field(default="", max_length=240)
    request_id: str = Field(min_length=1, max_length=120)
    page_no: int | None = Field(default=None, ge=1, le=200)
    origin_ref: str = Field(default="", max_length=1000)


class AssetReplaceIn(AssetImportIn):
    expected_version: int = Field(ge=1)


class AssetStateIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(ge=1)


class AssetCopyIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    source_project_id: str = Field(min_length=1, max_length=100)
    source_version_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=240)


class AssetSelectIn(BaseModel):
    version_id: str = Field(min_length=1, max_length=100)


class AssetInsertIn(AssetSelectIn):
    request_id: str = Field(min_length=1, max_length=120)
    target_path: str = Field(min_length=1, max_length=240)
    base_file_version: int = Field(ge=1)
    caption: str = Field(default="", max_length=1000)

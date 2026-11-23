from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from paperpilot.domain.drawing_schemas import StrictModel


class SourceRef(StrictModel):
    corpus_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)


class Region(StrictModel):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_page(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("region must be within image")
        return self


class VisualCheckIn(StrictModel):
    asset_version_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=120)
    source: SourceRef | None = None
    source_asset_version: str | None = Field(default=None, max_length=100)
    region: Region | None = None
    check_types: list[Literal["file_integrity", "readability", "provenance", "source_consistency", "semantic", "style"]] = Field(default_factory=lambda: ["file_integrity", "readability", "provenance", "source_consistency", "semantic"], min_length=1, max_length=6)
    cloud: bool = False
    external_consent: bool = False
    unknown_price_confirmed: bool = False
    hard_limit: float | None = Field(default=None, ge=0)
    temporary_key: str = Field(default='',max_length=500)
    capacity_preview_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class VisualReviewIn(StrictModel):
    report_version: int = Field(ge=1)
    expected_review_count: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=120)
    decision: Literal["accept", "reject", "needs_review"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    reviewer: str = Field(min_length=1, max_length=120)

from __future__ import annotations

from typing import Literal
from pydantic import Field

from paperpilot.domain.drawing_schemas import StrictModel


class StartupSource(StrictModel):
    label: str = Field(min_length=1, max_length=300)
    version: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=1000)


class StartupCommandIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_workflow_version: int = Field(ge=1)
    command: Literal["start", "save", "review", "confirm", "back", "restart", "cancel", "resume", "skip"]
    stage_id: str | None = Field(default=None, max_length=30)
    fields: dict[str, str] | None = None
    source_refs: list[StartupSource] | None = Field(default=None, max_length=30)
    reason: str = Field(default="", max_length=2000)


class StartupCopyIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_workflow_version: int = Field(ge=1)
    source_project_id: str = Field(min_length=1, max_length=100)
    source_version: int = Field(ge=1)


class ResearchDesignPreviewIn(StrictModel):
    """Preview the exact outbound payload before any research-design request."""
    stage_id: str = Field(min_length=1, max_length=30)
    operators: list[str] | None = Field(default=None, max_length=9)
    instruction: str = Field(default="", max_length=4000)


class ResearchDesignGenerateIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    stage_id: str = Field(min_length=1, max_length=30)
    operators: list[str] | None = Field(default=None, max_length=9)
    instruction: str = Field(default="", max_length=4000)
    preview_fingerprint: str = Field(min_length=16, max_length=128)
    consent_to_send: bool = False
    possible_cost_confirmed: bool = False
    temporary_key: str = Field(default="", max_length=500)


class ResearchDesignApplyIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    candidate_id: str = Field(min_length=1, max_length=64)
    card_id: str = Field(min_length=1, max_length=64)
    expected_workflow_version: int = Field(ge=1)
    field: str = Field(default="", max_length=40)



class AgentPreviewIn(StrictModel):
    stage_id: str = Field(default="", max_length=30)
    operators: list[str] | None = Field(default=None, max_length=9)
    instruction: str = Field(default="", max_length=4000)


class AgentRunIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    stage_id: str = Field(default="", max_length=30)
    operators: list[str] | None = Field(default=None, max_length=9)
    instruction: str = Field(default="", max_length=4000)
    preview_fingerprint: str = Field(min_length=16, max_length=128)
    consent_to_send: bool = False
    possible_cost_confirmed: bool = False
    temporary_key: str = Field(default="", max_length=500)


class AgentMemoryDecisionIn(StrictModel):
    action: str = Field(pattern="^(approve|reject|supersede|delete)$")

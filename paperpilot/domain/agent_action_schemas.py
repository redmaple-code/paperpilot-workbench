from typing import Literal
from pydantic import Field, StrictInt
from paperpilot.domain.drawing_schemas import StrictModel


class ActionCreateIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    conversation_id: str = Field(default="", max_length=120)
    run_id: str = Field(default="", max_length=120)
    operation: Literal["outline", "logic", "term", "corpus", "reference", "oa", "research", "planning", "structure_outline"]
    base_version: int = Field(ge=0)
    payload: dict
    source_refs: list[dict] = Field(default_factory=list, max_length=30)


class ActionPreviewIn(StrictModel):
    selected_indices: list[StrictInt] | None = Field(default=None, max_length=500)
    edited_text: str | None = Field(default=None, max_length=16000)
    review_reason: str = Field(default="", max_length=2000)
    acknowledge_revision: bool = False


class ActionApplyIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=0)
    action_fingerprint: str = Field(min_length=64, max_length=64)
    accepted: bool = False


class BusinessSearchIn(StrictModel):
    query: str = Field(min_length=1, max_length=240)
    consent_to_search: bool = False
    conversation_id: str = Field(default="", max_length=120)


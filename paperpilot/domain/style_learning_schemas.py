from __future__ import annotations

from typing import Literal
from pydantic import Field

from paperpilot.domain.drawing_schemas import StrictModel


class LearningCommandIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=0)


class LearningConsentIn(LearningCommandIn):
    authorized: bool
    scopes: list[Literal["accepted_edit", "rejected_edit", "example", "feedback"]] = Field(min_length=1, max_length=4)
    retention_days: int = Field(default=30, ge=1, le=365)


class LearningSignalIn(LearningCommandIn):
    kind: Literal["example", "feedback"]
    content: str = Field(min_length=1, max_length=16000)
    source_version: str = Field(min_length=1, max_length=240)


class LearningApprovalIn(LearningCommandIn):
    rule_ids: list[str] = Field(min_length=1, max_length=20)


class LearningRuleEditIn(LearningCommandIn):
    instruction: str = Field(min_length=1, max_length=2000)


class LearningRollbackIn(LearningCommandIn):
    target_profile_id: str = Field(min_length=1, max_length=100)


class LearningDeleteIn(LearningCommandIn):
    confirmation: Literal["删除学习数据"]
    signal_id: str | None = Field(default=None, max_length=100)

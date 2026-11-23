from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ComparisonIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reference_ids: list[Identifier] | None = Field(default=None, max_length=500)


class TermResolution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'merge', 'separate']
    target_id: Identifier | None = None
    canonical: Alias | None = None
    aliases: list[Alias] | None = Field(default=None, max_length=60)
    definition_id: Identifier | None = None
    source_ids: list[Identifier] | None = Field(default=None, max_length=500)


class ComparisonApplyIn(ComparisonIn):
    request_id: Identifier
    fingerprint: str = Field(pattern=r'^[0-9a-f]{24}$')
    decisions: dict[Identifier, TermResolution] = Field(min_length=1, max_length=500)


class DomainReferenceTermDecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: Identifier
    expected_version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r'^[0-9a-f]{24}$')
    decision: Literal['publish', 'reject']
    definition: str | None = Field(default=None, max_length=12000)


class DomainReferenceSourceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


class DomainReferenceTermBatchIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: Identifier
    expected_version: int = Field(ge=1)
    candidates: dict[Identifier, Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{24}$')]] = Field(min_length=1)

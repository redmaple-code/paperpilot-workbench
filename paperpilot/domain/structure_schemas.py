from typing import Literal
from pydantic import Field
from paperpilot.domain.drawing_schemas import StrictModel


class WorkspaceCreateIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    entry_mode: Literal["idea_from_stage01", "paper_import"] = "idea_from_stage01"
    content: str = Field(default="", max_length=1000000)
    title: str = Field(default="", max_length=180)
    locator: str = Field(default="", max_length=500)
    pages: list[dict] = Field(default_factory=list, max_length=1000)
    source_status: Literal["complete", "partial", "failed"] = "complete"
    source_warnings: list[str] = Field(default_factory=list, max_length=1000)


class WorkspaceSaveIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_workspace_revision: int = Field(ge=0)
    source_hash: str = Field(min_length=1, max_length=128)
    commands: list[dict] = Field(default_factory=list, max_length=500)


class LayoutIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_layout_revision: int = Field(ge=0)
    positions: dict
    graph_kind: Literal["structure", "outline", "logic"] = "structure"
    graph_branch: str = Field(default="main", max_length=100)
    draft_id: str = Field(default="", max_length=100)


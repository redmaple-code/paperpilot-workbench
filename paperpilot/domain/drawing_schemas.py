from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GraphNode(StrictModel):
    id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(max_length=300)


class GraphEdge(StrictModel):
    source: str = Field(min_length=1, max_length=60)
    target: str = Field(min_length=1, max_length=60)


class StructuredSpec(StrictModel):
    nodes: list[GraphNode] = Field(min_length=1, max_length=30)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=100)


class SceneNode(GraphNode):
    x: float = Field(ge=0, le=4000)
    y: float = Field(ge=0, le=4000)
    width: float = Field(default=140, ge=10, le=2000)
    height: float = Field(default=60, ge=10, le=2000)
    shape: Literal["rect", "ellipse", "text"] = "rect"


class SceneSpec(StrictModel):
    schema_version: Literal[1] = 1
    width: int = Field(default=900, ge=100, le=4000)
    height: int = Field(default=500, ge=100, le=4000)
    nodes: list[SceneNode] = Field(min_length=1, max_length=100)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=200)


class ImageSpec(StrictModel):
    prompt: str = Field(min_length=1, max_length=12000)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024"
    input_asset_version: str | None = Field(default=None, max_length=100)
    mask_version: str | None = Field(default=None, max_length=100)


class RunIn(StrictModel):
    intent: str = Field(min_length=1, max_length=12000)
    title: str = Field(default="论文图像", max_length=240)
    request_id: str = Field(min_length=1, max_length=120)
    input_asset_version: str | None = Field(default=None, max_length=100)


class DecisionIn(StrictModel):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)
    selected_type: Literal["structured_svg", "scene_graph", "ai_image"]
    spec: dict
    constraints: dict = Field(default_factory=dict)


class RunCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class DispatchIn(RunCommand):
    external_consent: bool = False
    unknown_price_confirmed: bool = False
    hard_limit: float | None = Field(default=None, ge=0)
    temporary_key: str = Field(default='',max_length=500)


class CandidateSaveIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_run_revision: int = Field(ge=1)


class StyleRuleIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    values: dict


class VisualProviderBindingIn(StrictModel):
    profile_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    expected_project_revision: int = Field(ge=1)
    purpose: Literal["drawing", "vision"] = "drawing"
    config_revision: int | None = Field(default=None,ge=1)

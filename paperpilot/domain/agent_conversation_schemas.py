"""Additive Agent API; exact outbound context and authority are explicit."""
from typing import Literal
from pydantic import Field
from paperpilot.domain.drawing_schemas import StrictModel


class ContextSnapshot(StrictModel):
    project_id: str = Field(default='', max_length=100)
    file_path: str = Field(default='', max_length=500)
    file_hash: str = Field(default='', max_length=128)
    selection_start: int = Field(default=0, ge=0)
    selection_end: int = Field(default=0, ge=0)
    selection_hash: str = Field(default='', max_length=128)
    selected_text: str = Field(default='', max_length=8000)
    excerpt: str = Field(default='', max_length=32000)
    scope: Literal['excerpt', 'full'] = 'excerpt'


class RunLimits(StrictModel):
    model_calls: int = Field(default=8, ge=1, le=8)
    tool_calls: int = Field(default=20, ge=1, le=20)
    tokens: int = Field(default=40000, ge=512, le=40000)
    seconds: int = Field(default=300, ge=1, le=300)


class ConversationCreateIn(StrictModel):
    title: str = Field(default='新会话', min_length=1, max_length=120)
    parent_id: str = Field(default='', max_length=64)
    module_key: str = Field(default='', max_length=30)


class ConversationPreviewIn(StrictModel):
    structure_workspace_id: str = Field(default='', max_length=100)
    structure_draft_id: str = Field(default='', max_length=100)
    structure_target: dict[str, str] | None = None
    conversation_id: str = Field(default='', max_length=64)
    expected_conversation_revision: int | None = Field(default=None, ge=0)
    stage_id: str = Field(default='', max_length=30)
    research_profile: Literal['', 'scholar_apprentice'] = ''
    operators: list[str] | None = Field(default=None, max_length=9)
    instruction: str = Field(default='', max_length=4000)
    context_snapshot: ContextSnapshot = Field(default_factory=ContextSnapshot)
    limits: RunLimits = Field(default_factory=RunLimits)


class ConversationRunIn(ConversationPreviewIn):
    request_id: str = Field(min_length=1, max_length=120)
    preview_fingerprint: str = Field(min_length=16, max_length=128)
    consent_to_send: bool = False
    possible_cost_confirmed: bool = False
    temporary_key: str = Field(default='', max_length=500)


class ResumeIn(StrictModel):
    request_id: str = Field(min_length=1, max_length=120)
    preview_fingerprint: str = Field(min_length=16, max_length=128)
    consent_to_send: bool = False
    possible_cost_confirmed: bool = False


class CardDecisionIn(StrictModel):
    action: Literal['accepted', 'applied', 'rejected']
    file_path: str = Field(default='', max_length=500)
    target_hash: str = Field(default='', max_length=128)

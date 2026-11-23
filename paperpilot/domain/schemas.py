from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    domain: str = ""
    style: str = ""
    citation_style: str = ""
    model: str = ""
    ai_base_url: str = ""
    ai_soft_budget_tokens: int | None = Field(default=None, ge=1)
    domain_id: str = ""
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class SettingOverrideIn(BaseModel):
    value: Any
    expected_project_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class AIJobRecoveryIn(BaseModel):
    confirmed_stopped: bool = False


class SettingRestoreIn(BaseModel):
    expected_project_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class SettingSyncIn(BaseModel):
    expected_baseline_version: int = Field(ge=1)
    expected_domain_version: int = Field(ge=1)
    selected_keys: list[str] = Field(default_factory=list, max_length=20)
    request_id: str = Field(min_length=1, max_length=120)


class SettingBindingRepairIn(BaseModel):
    expected_project_revision: int = Field(ge=1)
    expected_baseline_version: int = Field(ge=1)
    expected_project_domain_id: str = Field(max_length=120)
    expected_baseline_domain_id: str = Field(max_length=120)
    request_id: str = Field(min_length=1, max_length=120)


class DomainSettingsIn(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class BuildSnapshotIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="draft_preview", pattern="^(draft_preview|candidate_preview|submitted|release)$")
    expected_project_revision: int | None = Field(default=None, ge=1)
    draft_source: Literal['all', 'editor', 'revision'] = 'all'
    candidate_id: str | None = Field(default=None, min_length=1, max_length=120)


class BuildRestoreIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=120)
    include_paths: list[str] = Field(default_factory=list, max_length=500)
    remove_paths: list[str] = Field(default_factory=list, max_length=500)
    setting_keys: list[str] = Field(default_factory=list, max_length=20)
    restore_build_environment: bool = False
    preview_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class BuildRestorePreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include_paths: list[str] = Field(default_factory=list, max_length=500)
    remove_paths: list[str] = Field(default_factory=list, max_length=500)
    setting_keys: list[str] = Field(default_factory=list, max_length=20)
    restore_build_environment: bool = False


class BuildRestoreCommitIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=120)
    candidate_fingerprint: str = Field(min_length=64, max_length=64)


class BuildSourceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=500)
    line: int = Field(default=1, ge=1, le=1000000)


class BuildSourceForwardIn(BuildSourceIn):
    column: int = Field(default=0, ge=0, le=1000000)
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_version: int = Field(ge=0)
    source_context: Literal['editor', 'snapshot']


class BuildSourceInverseIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: int = Field(ge=1, le=100000)
    x: float = Field(ge=0, le=1000000, allow_inf_nan=False)
    y: float = Field(ge=0, le=1000000, allow_inf_nan=False)


class CompileIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str | None = Field(default=None, max_length=120)
    purpose: str = Field(default="draft_preview", pattern="^(draft_preview|candidate_preview|submitted|release)$")
    snapshot_id: str | None = Field(default=None, max_length=120)
    draft_source: Literal['all', 'editor', 'revision'] | None = None
    candidate_id: str | None = Field(default=None, min_length=1, max_length=120)
    trigger: Literal['explicit', 'automatic'] = 'explicit'
    force_rebuild: bool = False


class CitationCheckIn(BaseModel):
    target_path: str | None = Field(default=None, max_length=240)
    content: str | None = Field(default=None, max_length=200000)
    request_id: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=160)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    consent_to_send: bool = False
    preview_fingerprint: str | None = Field(default=None, max_length=128)
    temporary_key: str = Field(default='',max_length=500)


class CitationSuggestionHandoffIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=1)
    confirm_handoff: bool = False


class FormatCheckIn(BaseModel):
    target_path: str | None = Field(default=None, max_length=240)
    content: str | None = Field(default=None, max_length=200000)
    request_id: str | None = Field(default=None, max_length=120)


class FileIn(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = ""
    base_version: int | None = Field(default=None, ge=0)
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class EditorDraftIn(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    base_version: int = Field(ge=0)
    base_content: str = Field(default="", max_length=200000)
    content: str = Field(default="", max_length=200000)


class EditorConversionPreviewIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    source_path: str = Field(min_length=1, max_length=240)
    source_version: int = Field(ge=0)
    target_path: str = Field(min_length=1, max_length=240)
    direction: str = Field(pattern="^(markdown_to_latex|latex_to_markdown)$")


class EditorConversionApplyIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)


class TextIn(BaseModel):
    content: str = ""
    base_version: int | None = Field(default=None, ge=0)


class OutlineMigrationIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)


class OutlineMigrationCommitIn(BaseModel):
    draft_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)


class OutlineNodeCommandIn(BaseModel):
    command: str = Field(pattern="^(create|update|move|delete|link|unlink)$")
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=0)
    node: dict[str, Any] = Field(default_factory=dict)
    node_id: str = ""
    parent_id: str | None = None
    position: int | None = Field(default=None, ge=0)
    link: dict[str, Any] = Field(default_factory=dict)


class OutlineCandidateIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=0)
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class OutlineCandidateDecisionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(accept|partial_apply|reject)$")
    selected_indices: list[int] | None = None
    base_version: int = Field(ge=0)


class OutlineDraftIn(BaseModel):
    draft_key: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=0)
    content: dict[str, Any] = Field(default_factory=dict)


class OutlineRestoreIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=0)


class TermIn(BaseModel):
    term: str
    definition: str = ""
    aliases: str = ""


class CorpusIn(BaseModel):
    title: str
    content: str = ""
    source: str = ""


class CorpusCurationIn(BaseModel):
    base_source_version: int = Field(ge=1)
    content: str = Field(max_length=200000)
    notes: str = Field(default="", max_length=12000)
    kind: str = Field(default="manual", pattern="^(manual|ai_assisted|annotation)$")


class CorpusRefreshIn(BaseModel):
    base_source_version: int = Field(ge=1)
    content: str = Field(max_length=200000)
    source: str = Field(default="", max_length=2000)
    permission_state: str = Field(default="authorized", pattern="^(authorized|unknown|revoked)$")
    request_id: str = Field(min_length=1, max_length=120)


class CorpusRefreshDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject)$")
    request_id: str = Field(min_length=1, max_length=120)


class RefIn(BaseModel):
    cite_key: str
    title: str = ""
    authors: str = ""
    year: str = ""
    bibtex: str = ""


class AIIn(BaseModel):
    action: str
    prompt: str = ""
    content: str = ""
    selection: str = ""
    model: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=120)
    scope: dict[str, Any] = Field(default_factory=dict)
    base_file_versions: dict[str, int] = Field(default_factory=dict)
    context_version: str | None = None


class ProviderProfileIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=1000)
    auth_mode: str = "environment"
    models: list[str] = Field(default_factory=list, max_length=100)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    model_limits: dict[str, Any] = Field(default_factory=dict)
    api_key: str = Field(default="", max_length=500)
    request_id: str = Field(min_length=1, max_length=120)


class ProviderProfileUpdateIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    base_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(default_factory=dict)


class ProviderTestIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    request_id: str = Field(min_length=1,max_length=120)
    expected_version: int = Field(ge=1)
    model_id: str = Field(min_length=1,max_length=200)
    mode: str = Field(default='chat',pattern='^(chat|stream|image_generation|image_edit|vision_analysis)$')
    consent_to_send: bool = False
    possible_cost_confirmed: bool = False
    temporary_key: str = Field(default='',max_length=500)


class ProviderBindingIn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    profile_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    base_project_revision: int = Field(ge=1)
    config_revision: int | None = Field(default=None,ge=1)


class DomainIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)


class DomainAssignIn(BaseModel):
    domain_id: str = Field(min_length=1, max_length=120)
    base_project_revision: int = Field(ge=1)


class DomainResourceIn(BaseModel):
    resource_type: str = Field(pattern="^(term|corpus|reference)$")
    title: str = Field(min_length=1, max_length=600)
    content: str = Field(default="", max_length=200000)
    source_project_id: str = ""


class DomainResourceCopyIn(BaseModel):
    resource_id: str = Field(min_length=1, max_length=120)
    base_project_revision: int = Field(ge=1)


class DomainTermImportIn(BaseModel):
    domain_term_id: str = Field(min_length=1, max_length=120)
    source_domain_version: int = Field(ge=1)
    base_project_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class TermPromotePreviewIn(BaseModel):
    domain_id: str = Field(min_length=1, max_length=120)
    term_ids: list[str] = Field(default_factory=list, max_length=500)
    source_project_revision: int = Field(ge=1)
    target_domain_version: int | None = Field(default=None, ge=1)
    request_id: str = Field(min_length=1, max_length=120)


class TermPromoteCommitIn(BaseModel):
    preview_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)
    selected_term_ids: list[str] | None = Field(default=None, max_length=500)
    conflict_decisions: dict[str, str] = Field(default_factory=dict)


class TermClassificationReviewIn(BaseModel):
    term_id: str = Field(min_length=1, max_length=120)
    domain_id: str = Field(min_length=1, max_length=120)
    suggested_term: str = Field(min_length=1, max_length=600)
    suggested_definition: str = Field(default="", max_length=12000)
    suggested_aliases: str = Field(default="", max_length=4000)
    confidence: float = Field(ge=0, le=1)
    basis: str = Field(default="", max_length=4000)
    classifier_version: str = Field(default="manual-or-local", max_length=120)
    request_id: str = Field(min_length=1, max_length=120)


class TermClassificationDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|edit|reject)$")
    request_id: str = Field(min_length=1, max_length=120)
    term: str | None = Field(default=None, max_length=600)
    definition: str | None = Field(default=None, max_length=12000)
    aliases: str | None = Field(default=None, max_length=4000)
    replace_conflict: bool = False
    expected_domain_version: int | None = Field(default=None, ge=1)


class RevisionDraftSnapshotItem(BaseModel):
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    updated_at: str
    base_version: int = Field(ge=1)
    mode_id: str
    content: str


class WorkflowCommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=40)
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=1)
    target_version: int | None = Field(default=None, ge=1)
    draft_action: str | None = Field(default=None, pattern="^(commit|keep|discard)$")
    expected_drafts: list[RevisionDraftSnapshotItem] | None = None


class WorkflowItemIn(BaseModel):
    status: str = Field(pattern="^(pending|done)$")


class AIJobRetryIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    temporary_key: str = Field(default='',max_length=500)
    duplicate_cost_confirmed: bool = False


class AIContextPreviewIn(BaseModel):
    action: str = Field(pattern="^(write|companion|follow|follow_write|continue|edit|rewrite|expand_or_condense|style_or_format|term_logic_check|citation_compare|explain_or_suggest|outline)$")
    target_kind: str | None = Field(default=None, pattern="^(file|outline)$")
    target_path: str = ""
    targets: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    prompt: str = Field(default="", max_length=12000)
    content: str | None = Field(default=None, max_length=200000)
    selection: str = Field(default="", max_length=100000)
    context_source_ids: list[str] = Field(default_factory=list, max_length=100)
    model: str | None = Field(default=None, max_length=160)


class RevisionModeIn(BaseModel):
    workflow_version: int = Field(ge=1)


class RevisionExitIn(BaseModel):
    draft_action: str | None = Field(default=None, pattern="^(commit|keep|discard)$")
    expected_drafts: list[RevisionDraftSnapshotItem] | None = None


class RevisionDraftIn(BaseModel):
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    content: str
    base_version: int = Field(ge=1)
    expected_draft_updated_at: str | None = None
    copy_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,120}$")


class RevisionCheckpointIn(BaseModel):
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    base_version: int = Field(ge=1)
    expected_draft_updated_at: str | None = None


class AIJobIn(BaseModel):
    action: str = Field(pattern="^(write|companion|follow|follow_write|continue|edit|rewrite|expand_or_condense|style_or_format|term_logic_check|citation_compare|explain_or_suggest|outline)$")
    request_id: str = Field(min_length=1, max_length=120)
    prompt: str = Field(default="", max_length=12000)
    content: str = Field(default="", max_length=200000)
    selection: str = Field(default="", max_length=100000)
    model: str | None = Field(default=None, max_length=160)
    scope: dict[str, Any] = Field(default_factory=dict)
    base_file_versions: dict[str, int] = Field(default_factory=dict)
    context_version: str | None = None
    budget_override: bool = False
    temporary_key: str = Field(default='',max_length=500)


class CandidateDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject|partial_apply)$")
    request_id: str = Field(min_length=1, max_length=120)
    base_file_versions: dict[str, int] = Field(default_factory=dict)
    selected_paths: list[str] | None = None
    allow_warnings: bool = False


class ContinuationSubgoal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: str = Field(min_length=1, max_length=12000)
    outline_node_id: str = Field(min_length=1, max_length=120)
    anchor_start: int | None = Field(default=None, ge=0)
    anchor_end: int | None = Field(default=None, ge=0)


class ContinuationIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    base_version: int = Field(ge=1)
    anchor_start: int = Field(default=0, ge=0)
    anchor_end: int = Field(default=0, ge=0)
    goal: str = Field(min_length=1, max_length=12000)
    prompt: str = Field(default="", max_length=12000)
    segment_count: int = Field(default=1, ge=1, le=20)
    subgoals: list[ContinuationSubgoal] = Field(default_factory=list, max_length=20)
    outline_node_id: str = Field(default="", max_length=120)
    outline_version: int | None = Field(default=None, ge=1)
    context_version: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=160)
    hard_budget_tokens: int | None = Field(default=None, ge=1)
    hard_budget_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_override: bool = False
    temporary_key: str = Field(default='',max_length=500)


class ContinuationCommandIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    budget_override: bool = False
    temporary_key: str = Field(default='',max_length=500)
    duplicate_cost_confirmed: bool = False
    hard_budget_tokens: int | None = Field(default=None, ge=1)
    hard_budget_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ContinuationSegmentDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject|partial_apply)$")
    request_id: str = Field(min_length=1, max_length=120)
    base_file_versions: dict[str, int] = Field(default_factory=dict)
    selected_paths: list[str] | None = None
    allow_warnings: bool = False


class ModificationIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    action: str = Field(default="rewrite", pattern="^(edit|rewrite|expand_or_condense|style_or_format|term_logic_check|citation_compare)$")
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    base_version: int = Field(ge=1)
    start_offset: int = Field(default=0, ge=0)
    end_offset: int = Field(default=0, ge=0)
    instruction: str = Field(min_length=1, max_length=12000)
    context_version: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=160)
    temporary_key: str = Field(default='',max_length=500)


class ModificationDecisionIn(BaseModel):
    decision: str = Field(pattern="^(accept|reject|partial_apply)$")
    request_id: str = Field(min_length=1, max_length=120)
    base_file_versions: dict[str, int] = Field(default_factory=dict)
    selected_paths: list[str] | None = None
    allow_warnings: bool = False


class ModificationCleanupIn(BaseModel):
    scope: str = Field(pattern="^(operation|project)$")
    operation_id: str = ""
    request_id: str = Field(min_length=1, max_length=120)
    preview_fingerprint: str = Field(default="", max_length=128)
    confirm: bool = False


class RevisionRestoreIn(BaseModel):
    target_kind: str = Field(pattern="^(file|outline)$")
    target_path: str = ""
    target_version: int = Field(ge=1)
    base_version: int = Field(ge=1)
    action: str = Field(pattern="^(undo|redo)$")


class CopyIn(BaseModel):
    source_project_id: str
    resource_type: str
    resource_id: str


class LogicIn(BaseModel):
    kind: str = "claim"
    label: str
    details: str = ""
    links: str = ""


class LogicGraphIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    base_version: int = Field(ge=0)
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    edges: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)


class LogicSuggestionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    prompt: str = Field(default="", max_length=12000)
    preview_fingerprint: str | None = Field(default=None,max_length=128)
    consent_to_send: bool = False
    temporary_key: str = Field(default='',max_length=500)


class LogicSuggestionDecisionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(accept|reject|partial_apply)$")
    selected_indices: list[int] | None = None
    base_version: int = Field(ge=0)


class ProjectLifecycleCommandIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    command: str = Field(pattern="^(submit_review|return_to_draft|publish|withdraw_publication)$")
    base_version: int = Field(ge=1)
    continue_reason: str = Field(default="", max_length=1000)


class ProjectLibrarySearchIn(BaseModel):
    query: str = Field(default="", max_length=240)
    scope: str = Field(default="current", pattern="^(current|global|archived)$")
    current_project_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ReferenceRecordIn(BaseModel):
    title: str = Field(min_length=1, max_length=600)
    authors: str = Field(default="", max_length=2000)
    year: str = Field(default="", max_length=20)
    venue: str = Field(default="", max_length=400)
    doi: str = Field(default="", max_length=240)
    canonical_url: str = Field(default="", max_length=1000)
    abstract: str = Field(default="", max_length=12000)
    notes: str = Field(default="", max_length=12000)
    tags: list[str] = Field(default_factory=list, max_length=50)


class ReferenceVersionIn(BaseModel):
    base_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(min_length=1)


class ReferenceSearchIn(BaseModel):
    query: str = Field(default="", max_length=240)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ReferenceLinkIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    reference_version: int = Field(ge=1)
    cite_key: str = Field(min_length=1, max_length=120)


class ReferenceRefreshIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    reference_version: int = Field(ge=1)
    base_version: int = Field(ge=1)


class ReferenceAttachmentIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=80)
    external_evidence_id: str = Field(min_length=1, max_length=80)
    base_version: int = Field(ge=1)


class ReferenceDraftConfirmIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(create|merge)$")
    merge_into_id: str | None = None


class ReferenceMergeIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    target_reference_id: str = Field(min_length=1, max_length=80)
    base_version: int = Field(ge=1)


class DrawIn(BaseModel):
    title: str = "论文示意图"
    labels: list[str] = Field(default_factory=list)

class AcademicSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=240)
    filters: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None
    search_id: str | None = None
    request_id: str | None = None
    save: bool = False
    limit: int = Field(default=10, ge=1, le=50)


class AcademicHandoffIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=160)


class ImportIn(BaseModel):
    candidate_id: str | None = Field(default=None, max_length=80)
    doi: str | None = Field(default=None, max_length=240)
    url: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=160)
    defer_fetch: bool = False


class ImportConfirmIn(BaseModel):
    selected_fragments: list[dict[str, Any]] = Field(default_factory=list)
    keep_attachment: bool = False



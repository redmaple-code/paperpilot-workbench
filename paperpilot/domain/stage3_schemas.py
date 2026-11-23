"""Incremental contracts for the existing resource and review workbenches."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class ClassificationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=120)
    term_id: str = Field(min_length=1, max_length=120)
    preview_fingerprint: str = ''
    consent_to_send: bool = False
    temporary_key: str = Field(default='', max_length=500)


class DomainResourceEditIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=600)
    content: str = Field(max_length=200000)


class CorpusMigrationIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    curation_id: str = Field(min_length=1, max_length=120)
    target_source_version: int = Field(ge=1)
    content: str = Field(max_length=200000)
    notes: str = Field(default='', max_length=12000)


class CorpusMigrationDecisionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern='^(accept|reject)$')


class DraftCopyIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    source_path: str = Field(min_length=1, max_length=500)
    target_path: str = Field(min_length=1, max_length=500)
    draft_updated_at: str


class CorpusSourceLinkIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    source_version: int = Field(ge=1)
    reference_id: str = Field(min_length=1, max_length=120)
    reference_version: int = Field(ge=1)


class StructuredOutlineSuggestionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=12000)
    preview_fingerprint: str = ''
    consent_to_send: bool = False
    temporary_key: str = Field(default='', max_length=500)


class CitationReviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=120)
    input_kind: Literal['draft', 'saved', 'candidate'] = 'draft'
    target_paths: list[str] = Field(default_factory=lambda: ['main.tex'], min_length=1, max_length=100)
    draft_source: Literal['all', 'editor', 'revision'] = 'all'
    candidate_id: str | None = None
    source_ids: list[str] | None = None
    paper_language: str = Field(default='auto', max_length=80)
    claim_types: dict[str, Literal['descriptive','quantitative','causal','comparative','definition','original_result']] = Field(default_factory=dict)
    preview_fingerprint: str = ''
    consent_to_send: bool = False
    temporary_key: str = Field(default='', max_length=500)


class CitationCommandIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    preview_fingerprint: str = ''
    consent_to_send: bool = False
    duplicate_cost_confirmed: bool = False
    confirm: bool = False
    temporary_key: str = Field(default='', max_length=500)


class CitationDispositionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    action: Literal['retain','dismiss','needs_evidence','correct_type','handoff']
    reason: str = Field(min_length=1, max_length=4000)
    claim_type: Literal['descriptive','quantitative','causal','comparative','definition','original_result'] | None = None
    expected_draft_updated_at: str | None = None


class CitationSourceProfileIn(BaseModel):
    expected_version: int = Field(ge=0)
    parse_reliability: Literal['verified','unverified','unreliable'] = 'unverified'
    source_role: Literal['primary','secondary','note','unknown'] = 'unknown'
    origin_source_id: str = Field(default='', max_length=120)
    derived_from_current: bool = False
    evidence_kind: Literal['text','table','figure','mixed'] = 'text'
    visual_asset_version_id: str = Field(default='', max_length=120)
    study_group: str = Field(default='', max_length=120)

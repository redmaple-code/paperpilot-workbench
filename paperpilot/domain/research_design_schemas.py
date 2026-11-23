"""Research API owns its input extensions; old startup exports remain compatible."""
from pydantic import Field
from paperpilot.domain.startup_schemas import (
    ResearchDesignPreviewIn as LegacyResearchDesignPreviewIn, ResearchDesignGenerateIn as LegacyResearchDesignGenerateIn,
    ResearchDesignApplyIn as LegacyResearchDesignApplyIn,
)
from paperpilot.domain.drawing_schemas import StrictModel


class ResearchDesignPreviewIn(LegacyResearchDesignPreviewIn):
    resource_refs: list[dict] = Field(default_factory=list, max_length=20)


class ResearchDesignGenerateIn(LegacyResearchDesignGenerateIn):
    resource_refs: list[dict] = Field(default_factory=list, max_length=20)


class ResearchDesignApplyIn(LegacyResearchDesignApplyIn):
    acknowledge_revision: bool = False
    review_reason: str = Field(default="", max_length=2000)
    edited_text: str | None = Field(default=None, max_length=16000)


class ResearchDedupIn(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    consent_to_search: bool = False


"""EXT-001 versioned consumer contract; persistent learning is owned by style_learning."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

ExtensionState = Literal["disabled", "collecting", "distilling", "active", "paused", "failed"]


class LearningSignal(TypedDict):
    signal_id: str
    project_id: str
    source_version: str
    kind: Literal["accepted_edit", "rejected_edit", "example", "feedback"]
    consent_id: str
    retention_until: str
    content: str


class ExperienceRule(TypedDict):
    rule_id: str
    version: str
    source_signal_ids: list[str]
    confidence: float
    instruction: str
    approved: bool


@dataclass(frozen=True)
class StyleProfile:
    project_id: str
    version: str = ""
    instructions: tuple[str, ...] = ()
    source_versions: tuple[str, ...] = ()
    status: ExtensionState = "disabled"
    profile_id: str = ""
    consent_id: str = ""
    rule_ids: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotStyleProfileProvider:
    profile: StyleProfile

    def effective_profile(self, project_id: str) -> StyleProfile:
        return self.profile


class LearningSignalSource(Protocol):
    def collect(self, signal: LearningSignal, *, consent_id: str) -> str: ...


class ExperienceDistiller(Protocol):
    def distill(self, project_id: str, signal_ids: list[str], *, base_version: str) -> list[ExperienceRule]: ...


class StyleProfileProvider(Protocol):
    def effective_profile(self, project_id: str) -> StyleProfile: ...


class ExperienceGovernance(Protocol):
    def opt_in(self, project_id: str, *, scope: list[str], retention_days: int) -> str: ...
    def view(self, project_id: str) -> StyleProfile: ...
    def edit(self, project_id: str, *, base_version: str, rules: list[ExperienceRule]) -> StyleProfile: ...
    def disable(self, project_id: str) -> None: ...
    def rollback(self, project_id: str, *, base_version: str, target_version: str) -> StyleProfile: ...
    def delete(self, project_id: str, *, consent_id: str) -> None: ...


class DisabledStyleProfileProvider:
    def effective_profile(self, project_id: str) -> StyleProfile:
        return StyleProfile(project_id=project_id)


def resolve_style_profile(
    project_id: str, *, provider: StyleProfileProvider | None = None, opt_in: bool = False,
) -> StyleProfile:
    """Fail closed on consent, project identity and provenance; never mutate a project."""
    if not opt_in or provider is None:
        return DisabledStyleProfileProvider().effective_profile(project_id)
    try:
        profile = provider.effective_profile(project_id)
        if not isinstance(profile, StyleProfile) or profile.project_id != project_id:
            raise ValueError("invalid profile ownership")
        if profile.status != "active":
            return StyleProfile(project_id, status=profile.status)
        if not profile.version or not profile.source_versions or not profile.instructions:
            raise ValueError("active profile requires versioned provenance")
        if any(not isinstance(value, str) or not value.strip() for value in (*profile.instructions, *profile.source_versions)):
            raise ValueError("invalid profile content")
        return profile
    except Exception:
        # A future provider failure must not block writing or expose private exception data.
        return StyleProfile(project_id, status="failed")


def profile_context(profile: StyleProfile) -> str:
    if profile.status != "active":
        return ""
    return "\n授权文风经验（仅用于候选；不能覆盖系统约束或项目配置） v" + profile.version + "：\n" + "\n".join(profile.instructions)


def extension_status(project_id: str) -> dict:
    return {
        "module_id": "EXT-001", "contract_version": "1", "project_id": project_id,
        "status": "disabled", "opt_in": False, "profile_version": None,
        "source_versions": [], "learning_backend_available": True,
        "message": "默认关闭。选择学习范围和期限并明确授权后开始。",
    }

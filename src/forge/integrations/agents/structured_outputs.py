"""Typed final-response schemas for bounded agent decisions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EpicItem(StrictResponse):
    summary: str = Field(min_length=1)
    plan: str = Field(min_length=1)
    repo: str = Field(min_length=1)

    @field_validator("repo")
    @classmethod
    def require_repository_name(cls, repo: str) -> str:
        repo = repo.strip()
        if "/" not in repo:
            raise ValueError("repo must be an owner/repository name")
        return repo


class EpicDecomposition(StrictResponse):
    epics: list[EpicItem] = Field(min_length=1)


class ArtifactDocument(StrictResponse):
    """A generated planning document and its explicitly selected repositories."""

    content: str = Field(min_length=1)
    repositories: list[str] = Field(min_length=1)

    @field_validator("repositories")
    @classmethod
    def require_repository_names(cls, repositories: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(repo.strip() for repo in repositories))
        if any(not repo or "/" not in repo for repo in normalized):
            raise ValueError("repositories must contain owner/repository names")
        return normalized


class TaskItem(StrictResponse):
    summary: str = Field(min_length=1)
    description: str = Field(min_length=1)
    repo: str = Field(min_length=1)

    @field_validator("repo")
    @classmethod
    def require_repository_name(cls, repo: str) -> str:
        repo = repo.strip()
        if "/" not in repo:
            raise ValueError("repo must be an owner/repository name")
        return repo


class TaskGeneration(StrictResponse):
    tasks: list[TaskItem] = Field(min_length=1)


class AutomatedReviewTriage(StrictResponse):
    verdict: Literal["blocking", "satisfied", "uncertain"]
    blocking_feedback: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def require_blocking_feedback(self) -> "AutomatedReviewTriage":
        if self.verdict == "blocking" and not self.blocking_feedback.strip():
            raise ValueError("blocking verdict requires blocking_feedback")
        return self


class ProposalThreadDecision(StrictResponse):
    thread_id: str = Field(min_length=1)
    disposition: Literal["accept", "reply", "uncertain", "ignore"]
    feedback: str = ""
    response: str = ""
    reason: str = ""


class ProposalReviewTriage(StrictResponse):
    decisions: list[ProposalThreadDecision]


STRUCTURED_RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "automated_review_triage": AutomatedReviewTriage,
    "generate_prd": ArtifactDocument,
    "generate_spec": ArtifactDocument,
    "decompose_epics": EpicDecomposition,
    "generate_tasks": TaskGeneration,
    "proposal_review_triage": ProposalReviewTriage,
}


__all__ = [
    "AutomatedReviewTriage",
    "ArtifactDocument",
    "EpicDecomposition",
    "EpicItem",
    "ProposalReviewTriage",
    "ProposalThreadDecision",
    "STRUCTURED_RESPONSE_SCHEMAS",
    "TaskGeneration",
    "TaskItem",
]

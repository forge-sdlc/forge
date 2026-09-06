"""Typed final-response schemas for bounded agent decisions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EpicItem(StrictResponse):
    summary: str = Field(min_length=1)
    plan: str = Field(min_length=1)
    repository: str = Field(min_length=1)


class EpicDecomposition(StrictResponse):
    epics: list[EpicItem] = Field(min_length=1)


class TaskItem(StrictResponse):
    summary: str = Field(min_length=1)
    description: str = Field(min_length=1)


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
    "decompose_epics": EpicDecomposition,
    "generate_tasks": TaskGeneration,
    "proposal_review_triage": ProposalReviewTriage,
}


__all__ = [
    "AutomatedReviewTriage",
    "EpicDecomposition",
    "EpicItem",
    "ProposalReviewTriage",
    "ProposalThreadDecision",
    "STRUCTURED_RESPONSE_SCHEMAS",
    "TaskGeneration",
    "TaskItem",
]

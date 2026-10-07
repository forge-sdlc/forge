"""Artifact models representing Jira tickets and their content.

These models represent the in-memory state of Jira tickets.
Workflow status is tracked via labels (see forge.models.workflow).
"""

from dataclasses import dataclass, field
from datetime import datetime

from forge.utils.time import utc_now_naive


@dataclass
class Feature:
    """Represents a Jira Feature ticket containing PRD and Spec."""

    jira_key: str
    labels: list[str] = field(default_factory=list)
    prd_content: str = ""
    spec_content: str = ""
    epic_keys: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)

    @property
    def has_prd(self) -> bool:
        """Check if PRD content has been generated."""
        return bool(self.prd_content.strip())

    @property
    def has_spec(self) -> bool:
        """Check if specification has been generated."""
        return bool(self.spec_content.strip())

    @property
    def is_forge_managed(self) -> bool:
        """Check if this feature is managed by Forge."""
        return "forge:managed" in self.labels


@dataclass
class Epic:
    """Represents a Jira Epic ticket containing implementation plan."""

    jira_key: str
    feature_key: str
    labels: list[str] = field(default_factory=list)
    summary: str = ""
    plan_content: str = ""
    task_keys: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)

    @property
    def has_plan(self) -> bool:
        """Check if implementation plan has been generated."""
        return bool(self.plan_content.strip())


@dataclass
class Task:
    """Represents a Jira Task ticket with implementation details."""

    jira_key: str
    epic_key: str
    labels: list[str] = field(default_factory=list)
    summary: str = ""
    description: str = ""
    target_repo: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    pr_url: str | None = None
    created_at: datetime = field(default_factory=utc_now_naive)
    updated_at: datetime = field(default_factory=utc_now_naive)

    @property
    def has_implementation_details(self) -> bool:
        """Check if task has sufficient implementation details."""
        return bool(self.description.strip() and self.target_repo.strip())

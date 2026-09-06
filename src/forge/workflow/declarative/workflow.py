"""BaseWorkflow adapter for a validated declarative definition."""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler, WorkflowValidationError
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import WorkflowDefinition
from forge.workflow.preconditions import project_capabilities


class DeclarativeWorkflow(BaseWorkflow):
    def __init__(self, definition: WorkflowDefinition, project_key: str) -> None:
        self.definition = definition
        self.project_key = project_key.upper()
        self.name = definition.metadata.name
        self.description = definition.metadata.description
        self._profile = get_state_profile(definition.spec.state)
        DeclarativeWorkflowCompiler(definition).validate()

    @property
    def cache_key(self) -> str:
        return (
            f"custom:{self.project_key}:{self.name}:"
            f"{self.definition.metadata.revision}:{self.definition.digest}"
        )

    @property
    def state_schema(self) -> type:
        return self._profile.schema

    @property
    def observation_policy(self) -> str | None:
        """Return the observation policy derived from the selected state profile."""
        return DeclarativeWorkflowCompiler(self.definition).effective_observation_policy()

    def resolve_observation_policy(self) -> str | None:
        """Resolve the selected policy through the profile allowlist.

        Compilation performs the same validation during construction.  This
        explicit lookup gives the orchestrator a single definition-backed
        entry point when it begins applying an external observation.
        """
        policy = self.observation_policy
        if policy is None:
            return None
        if policy not in self._profile.observation_policy_targets:
            raise WorkflowValidationError(f"unknown observation policy '{policy}'")
        return policy

    def matches(
        self,
        _ticket_type: TicketType,
        _labels: list[str],
        _event: dict[str, Any],
    ) -> bool:
        return False

    def supports_ticket_type(self, ticket_type: TicketType) -> bool:
        supported = {
            "feature": {TicketType.FEATURE, TicketType.STORY},
            "bug": {TicketType.BUG},
            "task_takeover": {TicketType.TASK, TicketType.EPIC},
        }
        return (
            ticket_type == TicketType.UNKNOWN
            or ticket_type in supported[self.definition.spec.state]
        )

    def build_graph(self) -> StateGraph[Any]:
        return DeclarativeWorkflowCompiler(self.definition).build_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        state = dict(self._profile.initializer(ticket_key, **kwargs))
        state.update(self.workflow_metadata())
        state["capabilities"] = project_capabilities(state)
        return state

    def workflow_metadata(self) -> dict[str, Any]:
        return {
            "workflow_name": self.name,
            "workflow_revision": self.definition.metadata.revision,
            "workflow_digest": self.definition.digest,
            "workflow_definition_revision": self.definition.metadata.revision,
            "workflow_definition_digest": self.definition.digest,
            "workflow_definition": self.definition.canonical_dict(),
            "workflow_pin_status": "pinned",
            "workflow_state_profile": self.definition.spec.state,
            "workflow_project_key": self.project_key,
            "workflow_transition_count": 0,
        }

    @staticmethod
    def pin_status(state: dict[str, Any]) -> str:
        """Classify checkpoint identity without changing the checkpoint.

        A checkpoint written before immutable definitions were introduced has a
        workflow name (or no workflow identity at all), but no revision/digest.
        Keeping this classification explicit lets callers choose a deliberate
        legacy default instead of accidentally treating the active property as
        an instance migration.
        """
        if not state.get("workflow_name"):
            return "unidentified"
        revision = state.get("workflow_definition_revision", state.get("workflow_revision"))
        digest = state.get("workflow_definition_digest", state.get("workflow_digest"))
        if revision is None or digest is None:
            return "legacy_unpinned"
        return "pinned"

    def validate_pinned_state(self, state: dict[str, Any]) -> None:
        """Reject a checkpoint whose durable artifact identity is inconsistent."""
        if not state.get("workflow_name"):
            return
        if state.get("workflow_name") != self.name:
            raise WorkflowValidationError("checkpoint workflow name does not match definition")
        try:
            revisions = {
                int(value)
                for value in (
                    state.get("workflow_definition_revision"),
                    state.get("workflow_revision"),
                )
                if value is not None
            }
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("checkpoint workflow revision is invalid") from exc
        digests = {
            str(value)
            for value in (
                state.get("workflow_definition_digest"),
                state.get("workflow_digest"),
            )
            if value is not None
        }
        if len(revisions) > 1 or len(digests) > 1:
            raise WorkflowValidationError("checkpoint contains conflicting workflow identities")
        revision = next(iter(revisions), None)
        digest = next(iter(digests), None)
        if revision is None or digest is None:
            return
        if revision != self.definition.metadata.revision or digest != self.definition.digest:
            raise WorkflowValidationError(
                "checkpoint is pinned to an unavailable or different workflow definition"
            )
        canonical = state.get("workflow_definition")
        if canonical is not None:
            try:
                persisted = load_workflow_value(canonical)
            except Exception as exc:
                raise WorkflowValidationError(
                    "checkpoint contains an invalid workflow definition"
                ) from exc
            if persisted.digest != self.definition.digest:
                raise WorkflowValidationError(
                    "checkpoint definition digest does not match its identity"
                )

    def migrate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Adopt this definition while refusing ambiguous or unsafe migration."""
        if not state.get("workflow_name"):
            return state
        if state.get("workflow_name") != self.name:
            raise WorkflowValidationError("an active checkpoint cannot switch workflow identity")

        # A canonical artifact is authoritative.  A caller must use this
        # method explicitly to change revision; normal resume validates and
        # resolves the pinned artifact instead.
        canonical = state.get("workflow_definition")
        if canonical is not None:
            try:
                persisted = load_workflow_value(canonical)
            except Exception as exc:
                raise WorkflowValidationError(
                    "checkpoint contains an invalid workflow definition"
                ) from exc
            old_digest = state.get("workflow_definition_digest", state.get("workflow_digest"))
            if persisted.digest != old_digest:
                raise WorkflowValidationError(
                    "checkpoint definition digest does not match its identity"
                )
            if persisted.metadata.name != self.name:
                raise WorkflowValidationError(
                    "checkpoint definition name does not match its identity"
                )
            if persisted.metadata.revision != int(
                state.get("workflow_definition_revision", state.get("workflow_revision", 0))
            ):
                raise WorkflowValidationError(
                    "checkpoint definition revision does not match its identity"
                )
        if state.get("workflow_state_profile") != self.definition.spec.state:
            raise WorkflowValidationError("an active workflow cannot change state profile")

        old_revision = int(
            state.get("workflow_definition_revision", state.get("workflow_revision", 0))
        )
        old_digest = state.get("workflow_definition_digest", state.get("workflow_digest"))
        if old_revision < 1 or not old_digest:
            raise WorkflowValidationError(
                "explicit migration requires a pinned source revision and digest"
            )
        new_revision = self.definition.metadata.revision
        if old_revision == new_revision and old_digest != self.definition.digest:
            raise WorkflowValidationError("workflow content changed without incrementing revision")
        if new_revision < old_revision:
            raise WorkflowValidationError("workflow revision rollback is not allowed")

        migrated = {**state, **self.workflow_metadata()}
        migrated["workflow_transition_count"] = state.get("workflow_transition_count", 0)
        current_node = str(state.get("current_node", ""))
        control_nodes = {"", "start", "entry", "__end__", "complete"}
        if current_node not in control_nodes and current_node not in self.definition.spec.steps:
            mapping = self.definition.spec.resume.from_revisions.get(old_revision, {})
            if current_node not in mapping:
                raise WorkflowValidationError(
                    f"revision {new_revision} removed saved node '{current_node}' without a "
                    f"resume mapping from revision {old_revision}"
                )
            migrated["current_node"] = mapping[current_node]
        return migrated

"""Generic, deterministic preconditions for workflow nodes.

Contracts are opt-in: wrapping a node without a contract is behaviorally equivalent
to calling the node directly.  They deliberately operate on mappings so the same
implementation can be used by built-in and declaratively compiled graphs.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import Any, TypeVar, cast


class CapabilityName(StrEnum):
    """Capabilities commonly exchanged between Forge workflow stages."""

    PLANNING_CONTEXT = "planning_context_available"
    REPOSITORIES = "repositories_resolved"
    WORKSPACE = "workspace_ready"
    CODE_CHANGES = "code_changes_present"
    COMMIT = "commit_available"
    BRANCH = "branch_available"
    PULL_REQUEST_EXPECTED = "pull_request_expected"
    PULL_REQUEST = "pull_request_available"
    CI_EXPECTED = "ci_expected"
    CI_CHECKS = "ci_checks_available"
    FAILED_CI = "failed_ci_available"


class PreconditionAction(StrEnum):
    """Standard outcome when a node's requirements are evaluated."""

    PROCEED = "proceed"
    SKIP = "skip"
    BLOCK = "block"
    PAUSE = "pause"
    RETRY = "retry"


CapabilityPredicate = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True, slots=True)
class Requirement:
    """One required capability and the policy used when it is absent."""

    capability: CapabilityName | str
    on_missing: PreconditionAction = PreconditionAction.BLOCK
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.on_missing is PreconditionAction.PROCEED:
            raise ValueError("a missing capability cannot use the proceed action")


@dataclass(frozen=True, slots=True)
class NodeContract:
    """Inputs and outputs advertised by a workflow node."""

    requires: tuple[Requirement, ...] = ()
    provides: frozenset[CapabilityName | str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class PreconditionResult:
    """Machine-readable result of evaluating a node contract."""

    action: PreconditionAction
    missing: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def should_run(self) -> bool:
        return self.action is PreconditionAction.PROCEED

    def as_dict(self, *, node_name: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": self.action.value,
            "missing": list(self.missing),
        }
        if node_name is not None:
            result["node"] = node_name
        if self.reason is not None:
            result["reason"] = self.reason
        return result


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _any_present(*keys: str) -> CapabilityPredicate:
    return lambda state: any(_present(state.get(key)) for key in keys)


def _any_true(*keys: str) -> CapabilityPredicate:
    return lambda state: any(state.get(key) is True for key in keys)


def _repositories_resolved(state: Mapping[str, Any]) -> bool:
    if _any_present("current_repo", "repos_to_process", "tasks_by_repo")(state):
        return True
    context = state.get("context")
    if not isinstance(context, Mapping):
        return False
    payload = context.get("payload")
    if not isinstance(payload, Mapping):
        return False
    issue = payload.get("issue")
    if not isinstance(issue, Mapping):
        return False
    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        return False
    labels = fields.get("labels", [])
    return isinstance(labels, list) and any(
        isinstance(label, str) and label.startswith("repo:") and "/" in label[5:]
        for label in labels
    )


BUILTIN_PREDICATES: Mapping[str, CapabilityPredicate] = {
    CapabilityName.PLANNING_CONTEXT.value: _any_present(
        "execution_brief",
        "work_unit",
        "task_keys",
        "epic_keys",
        "plan_content",
        "spec_content",
        "rca_content",
        "prd_content",
    ),
    CapabilityName.REPOSITORIES.value: _repositories_resolved,
    CapabilityName.WORKSPACE.value: _any_present("workspace_path"),
    CapabilityName.CODE_CHANGES.value: _any_true("code_changes_present", "changes_made"),
    CapabilityName.COMMIT.value: _any_present("commit_sha", "commit_hash"),
    CapabilityName.BRANCH.value: _any_present("branch_name", "fork_branch"),
    CapabilityName.PULL_REQUEST_EXPECTED.value: _any_true("pull_request_expected"),
    CapabilityName.PULL_REQUEST.value: _any_present(
        "current_pr_url", "pr_urls", "pull_requests", "pr_url"
    ),
    CapabilityName.CI_EXPECTED.value: _any_true("ci_expected"),
    CapabilityName.CI_CHECKS.value: _any_present("ci_status", "ci_checks", "ci_failed_checks"),
    CapabilityName.FAILED_CI.value: _any_present("ci_failed_checks"),
}


def project_capabilities(state: Mapping[str, Any]) -> dict[str, bool]:
    """Project workflow output into the explicit capability contract."""
    return {name: bool(predicate(state)) for name, predicate in BUILTIN_PREDICATES.items()}


def has_capability(
    state: Mapping[str, Any],
    capability: CapabilityName | str,
    *,
    predicates: Mapping[str, CapabilityPredicate] | None = None,
) -> bool:
    """Resolve an explicitly projected capability."""

    name = capability.value if isinstance(capability, CapabilityName) else capability
    declared = state.get("capabilities", {})
    if isinstance(declared, Mapping) and name in declared:
        return declared[name] is True
    if predicates is not None:
        predicate = predicates.get(name)
        return bool(predicate and predicate(state))
    return False


_ACTION_PRIORITY = {
    PreconditionAction.SKIP: 0,
    PreconditionAction.RETRY: 1,
    PreconditionAction.PAUSE: 2,
    PreconditionAction.BLOCK: 3,
}


async def evaluate_preconditions(
    state: Mapping[str, Any],
    contract: NodeContract | None,
    *,
    predicates: Mapping[str, CapabilityPredicate] | None = None,
) -> PreconditionResult:
    """Evaluate a contract without side effects.

    If missing requirements specify different policies, the safest policy wins:
    block, pause, retry, then skip.
    """

    if contract is None or not contract.requires:
        return PreconditionResult(PreconditionAction.PROCEED)

    missing = tuple(
        requirement
        for requirement in contract.requires
        if not has_capability(state, requirement.capability, predicates=predicates)
    )
    if not missing:
        return PreconditionResult(PreconditionAction.PROCEED)

    selected = max(missing, key=lambda requirement: _ACTION_PRIORITY[requirement.on_missing])
    names = tuple(
        requirement.capability.value
        if isinstance(requirement.capability, CapabilityName)
        else requirement.capability
        for requirement in missing
    )
    reason = selected.reason or f"missing required capabilities: {', '.join(names)}"
    return PreconditionResult(selected.on_missing, names, reason)


State = TypeVar("State", bound=MutableMapping[str, Any])
Node = Callable[[State], State | Awaitable[State]]


def with_preconditions(
    node: Node[State],
    contract: NodeContract | None,
    *,
    node_name: str | None = None,
    predicates: Mapping[str, CapabilityPredicate] | None = None,
) -> Callable[[State], Awaitable[State]]:
    """Wrap a sync or async node with opt-in precondition enforcement."""

    name = node_name or getattr(node, "__name__", "workflow_node")

    @wraps(node)
    async def guarded(state: State) -> State:
        result = await evaluate_preconditions(state, contract, predicates=predicates)
        if result.should_run:
            node_result = node(state)
            if inspect.isawaitable(node_result):
                node_result = await node_result
            return node_result

        updated = cast(State, dict(state))
        record = result.as_dict(node_name=name)
        history = list(state.get("precondition_history", []))
        history.append(record)
        updated["precondition_result"] = record
        updated["precondition_history"] = history
        if result.action is PreconditionAction.BLOCK:
            updated["last_error"] = result.reason
            updated["is_blocked"] = True
        elif result.action is PreconditionAction.PAUSE:
            updated["last_error"] = result.reason
            updated["is_paused"] = True
        elif result.action is PreconditionAction.RETRY:
            updated["last_error"] = result.reason
        return updated

    return guarded

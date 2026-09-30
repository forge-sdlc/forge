"""Pure, typed facts and predicates for authored workflow edges."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from forge.workflow.declarative.models import WorkflowPredicate


@dataclass(frozen=True)
class Fact:
    value_type: type
    project: Callable[[Mapping[str, Any]], Any]
    values: frozenset[Any] | None = None


# Keep this registry narrow. Future template nodes can register a versioned,
# typed result projection without exposing arbitrary checkpoint contents.
FACTS: dict[str, Fact] = {
    "workflow.paused": Fact(bool, lambda state: bool(state.get("is_paused"))),
    "workflow.has_error": Fact(bool, lambda state: bool(state.get("last_error"))),
    "ci.status": Fact(
        str,
        lambda state: state.get("ci_status"),
        frozenset(
            {"fixing", "failed", "blocked", "no_prs", "pending", "passed", "external_failure"}
        ),
    ),
    "pr.merged": Fact(bool, lambda state: bool(state.get("pr_merged"))),
    "review.revision_requested": Fact(bool, lambda state: bool(state.get("revision_requested"))),
}
PROFILE_FACTS: dict[str, frozenset[str]] = {
    "feature": frozenset(FACTS),
    "bug": frozenset(FACTS),
    "task_takeover": frozenset(FACTS),
}
FACT_CONTRACT_VERSION = "1"


def validate_predicate(predicate: WorkflowPredicate, state_profile: str, depth: int = 0) -> None:
    if depth > 3:
        raise ValueError("predicate nesting exceeds 3 levels")
    if predicate.fact is None:
        children = (*predicate.all_of, *predicate.any_of)
        if predicate.not_ is not None:
            children = (*children, predicate.not_)
        for child in children:
            validate_predicate(child, state_profile, depth + 1)
        return
    if predicate.fact not in PROFILE_FACTS[state_profile]:
        raise ValueError(f"unknown conditional fact '{predicate.fact}' for state '{state_profile}'")
    fact = FACTS[predicate.fact]
    if predicate.op == "isNull":
        return
    values = predicate.value if predicate.op == "in" else [predicate.value]
    if predicate.op == "in" and (not isinstance(values, list) or not 1 <= len(values) <= 32):
        raise ValueError("in predicate requires a list of 1 to 32 values")
    for value in values:
        if type(value) is not fact.value_type:
            raise ValueError(
                f"conditional fact '{predicate.fact}' requires {fact.value_type.__name__} values"
            )
        if fact.values is not None and value not in fact.values:
            raise ValueError(f"unknown value {value!r} for conditional fact '{predicate.fact}'")


def evaluate_predicate(predicate: WorkflowPredicate, state: Mapping[str, Any]) -> bool:
    if predicate.all_of:
        return all(evaluate_predicate(child, state) for child in predicate.all_of)
    if predicate.any_of:
        return any(evaluate_predicate(child, state) for child in predicate.any_of)
    if predicate.not_ is not None:
        return not evaluate_predicate(predicate.not_, state)
    assert predicate.fact is not None
    value = FACTS[predicate.fact].project(state)
    if predicate.op == "isNull":
        return value is None
    if predicate.op == "equals":
        return value == predicate.value
    return value in predicate.value

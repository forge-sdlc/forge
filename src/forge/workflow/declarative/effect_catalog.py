"""Trusted effect policies for registered declarative workflow nodes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeEffectPolicy:
    """Authority a node needs and authority it can optionally operate without."""

    required: frozenset[str]
    optional: frozenset[str] = frozenset()

    @property
    def default(self) -> tuple[str, ...]:
        """Return the catalog-owned default authority in stable order."""
        return tuple(sorted(self.required | self.optional))

    def resolve(self, declared: tuple[str, ...] | None) -> tuple[str, ...]:
        """Resolve an optional author restriction against the catalog policy."""
        if declared is None:
            return self.default
        requested = set(declared)
        missing = self.required - requested
        if missing:
            raise ValueError(f"omits required effect capability '{sorted(missing)[0]}'")
        unsupported = requested - (self.required | self.optional)
        if unsupported:
            raise ValueError(f"requests unsupported effect capability '{sorted(unsupported)[0]}'")
        return tuple(sorted(requested))


COMMENT = frozenset({"jira.comment"})
JIRA_DOMAIN = frozenset(
    {
        "jira.issue_content",
        "jira.issue_lifecycle",
        "jira.issue_structure",
        "jira.labels",
        "jira.project_configuration",
        "jira.status",
    }
)
SOURCE_CONTROL = frozenset(
    {
        "source_control.branch",
        "source_control.commit",
        "source_control.pull_request",
        "source_control.review",
    }
)


def _policy(*effects: str) -> NodeEffectPolicy:
    return NodeEffectPolicy(required=COMMENT | frozenset(effects))


def _domain_policy(*effects: str) -> NodeEffectPolicy:
    return NodeEffectPolicy(required=COMMENT | JIRA_DOMAIN | frozenset(effects))


COMMON_EFFECT_POLICIES: dict[str, NodeEffectPolicy] = {
    "attempt_ci_fix": _policy("source_control.commit"),
    "ci_evaluator": _policy("jira.labels"),
    "create_pr": _policy(
        "jira.issue_structure",
        "jira.labels",
        "jira.status",
        "source_control.branch",
        "source_control.commit",
        "source_control.pull_request",
        "source_control.review",
    ),
    "escalate_blocked": _domain_policy("source_control.review"),
    "human_review_gate": _domain_policy("source_control.commit", "source_control.review"),
    "implement_review": _policy("source_control.commit", "source_control.review"),
    "implement_work": _policy("source_control.commit"),
    "review_response_gate": _policy(),
    # Workspace setup publishes the newly-created branch before checkpointing,
    # and records implementation progress on the feature and its children.
    # This is shared by feature, bug, and task-takeover workflows.
    "setup_workspace": _policy("jira.labels", "jira.status", "source_control.commit"),
    "teardown_workspace": _policy(),
    "update_documentation": _policy(),
}

FEATURE_EFFECT_POLICIES: dict[str, NodeEffectPolicy] = {
    **COMMON_EFFECT_POLICIES,
    "aggregate_epic_status": _domain_policy(),
    "aggregate_feature_status": _domain_policy(),
    "answer_question": _policy("source_control.review"),
    "complete_tasks": _domain_policy(),
    "decompose_epics": _domain_policy(*SOURCE_CONTROL),
    "generate_prd": _domain_policy(*SOURCE_CONTROL),
    "generate_spec": _domain_policy(*SOURCE_CONTROL),
    "generate_tasks": _policy("jira.issue_structure", "jira.labels"),
    "local_review": _policy("source_control.commit"),
    "plan_approval_gate": _policy(),
    "prd_approval_gate": _policy(),
    "provision_epics": _domain_policy(),
    "provision_tasks": _domain_policy(),
    "regenerate_all_epics": _domain_policy(*SOURCE_CONTROL),
    "regenerate_all_tasks": _policy("jira.issue_lifecycle", "jira.issue_structure", "jira.labels"),
    "regenerate_epic_tasks": _policy("jira.issue_lifecycle", "jira.issue_structure", "jira.labels"),
    "regenerate_prd": _domain_policy(*SOURCE_CONTROL),
    "regenerate_spec": _domain_policy(*SOURCE_CONTROL),
    "spec_approval_gate": _policy(),
    "task_approval_gate": _policy(),
    "task_router": _domain_policy(),
    "update_single_epic": _domain_policy(*SOURCE_CONTROL),
    "update_single_task": _domain_policy(*SOURCE_CONTROL),
}

BUG_EFFECT_POLICIES: dict[str, NodeEffectPolicy] = {
    **COMMON_EFFECT_POLICIES,
    "analyze_bug": _policy("jira.labels"),
    "answer_question": _domain_policy("source_control.review"),
    "decompose_plan": _domain_policy(),
    "local_review": _policy("source_control.commit"),
    "plan_approval_gate": _policy(),
    "plan_bug_fix": _policy("jira.labels"),
    "post_merge_summary": _domain_policy(),
    "rca_option_gate": _domain_policy(),
    "reflect_rca": _policy(),
    "regenerate_plan": _policy("jira.labels"),
    "regenerate_rca": _domain_policy(),
    "triage_check": _domain_policy(),
    "triage_gate": _policy(),
}

TASK_TAKEOVER_EFFECT_POLICIES: dict[str, NodeEffectPolicy] = {
    **COMMON_EFFECT_POLICIES,
    "answer_question": _domain_policy("source_control.review"),
    "complete_task_takeover": _domain_policy(),
    "generate_plan": _policy("jira.labels"),
    "run_qualitative_review": _policy(),
    "task_plan_approval_gate": _policy(),
    "triage_check": _domain_policy(),
    "triage_gate": _policy(),
}


EFFECT_POLICIES_BY_PROFILE = {
    "feature": FEATURE_EFFECT_POLICIES,
    "bug": BUG_EFFECT_POLICIES,
    "task_takeover": TASK_TAKEOVER_EFFECT_POLICIES,
}

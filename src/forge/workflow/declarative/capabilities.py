"""Runtime capability scope for effects emitted by compiled process steps."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_EFFECT_CAPABILITIES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "forge_workflow_effect_capabilities", default=None
)

JIRA_EFFECT_CAPABILITIES = frozenset(
    {
        "jira.comment",
        "jira.labels",
        "jira.status",
        "jira.issue_content",
        "jira.issue_lifecycle",
        "jira.issue_structure",
        "jira.project_configuration",
    }
)
SOURCE_CONTROL_EFFECT_CAPABILITIES = frozenset(
    {
        "source_control.branch",
        "source_control.commit",
        "source_control.pull_request",
        "source_control.review",
    }
)
KNOWN_EFFECT_CAPABILITIES = JIRA_EFFECT_CAPABILITIES | SOURCE_CONTROL_EFFECT_CAPABILITIES

_OPERATION_CAPABILITIES = {
    "jira.comment.create": "jira.comment",
    "jira.structured_comment.create": "jira.comment",
    "jira.label.set": "jira.labels",
    "jira.labels.add": "jira.labels",
    "jira.labels.remove": "jira.labels",
    "jira.issue.transition": "jira.status",
    "jira.description.update": "jira.issue_content",
    "jira.custom_field.update": "jira.issue_content",
    "jira.attachment.replace": "jira.issue_content",
    "jira.issue.archive": "jira.issue_lifecycle",
    "jira.task.create": "jira.issue_structure",
    "jira.epic.create": "jira.issue_structure",
    "jira.issue_link.create": "jira.issue_structure",
    "jira.remote_link.create": "jira.issue_structure",
    "jira.project_property.set": "jira.project_configuration",
    "jira.project_property.delete": "jira.project_configuration",
    "source_control.branch.create": "source_control.branch",
    "source_control.file.put": "source_control.commit",
    "source_control.change_request.create": "source_control.pull_request",
    "source_control.change_request.update": "source_control.pull_request",
    "source_control.comment.create": "source_control.review",
    "source_control.comment.reply": "source_control.review",
}


@contextmanager
def effect_capability_scope(capabilities: tuple[str, ...]) -> Iterator[None]:
    token = _EFFECT_CAPABILITIES.set(capabilities)
    try:
        yield
    finally:
        _EFFECT_CAPABILITIES.reset(token)


def require_effect_capability(operation: str) -> None:
    """Reject an effect not authorized by the currently executing process step.

    A missing scope denotes a direct/local or legacy invocation. Compiled workflows
    always install a scope, including an empty one, before invoking a node.
    """
    capabilities = _EFFECT_CAPABILITIES.get()
    if capabilities is None:
        return
    required = _OPERATION_CAPABILITIES.get(operation)
    if required is None:
        raise PermissionError(f"effect operation '{operation}' has no governed capability")
    if required in capabilities:
        return
    raise PermissionError(
        f"process step is not allowed to emit effect operation '{operation}'"
    )

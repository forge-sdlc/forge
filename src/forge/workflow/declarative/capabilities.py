"""Runtime capability scope for effects emitted by compiled process steps."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_EFFECT_CAPABILITIES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "forge_workflow_effect_capabilities", default=None
)


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
    if any(
        operation == capability
        or (capability.endswith("*") and operation.startswith(capability[:-1]))
        for capability in capabilities
    ):
        return
    raise PermissionError(
        f"process step is not allowed to emit effect operation '{operation}'"
    )

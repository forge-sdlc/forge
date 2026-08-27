"""Default workflow registry."""

from forge.workflow.declarative.builtins import (
    BugGoldenWorkflow,
    FeatureGoldenWorkflow,
    TaskTakeoverGoldenWorkflow,
)
from forge.workflow.router import WorkflowRouter


def create_default_router() -> WorkflowRouter:
    """Create router with built-in workflows."""
    router = WorkflowRouter()
    router.register(TaskTakeoverGoldenWorkflow)
    router.register(FeatureGoldenWorkflow)
    router.register(BugGoldenWorkflow)
    return router

"""Default workflow registry."""

from forge.workflow.bug import BugWorkflow
from forge.workflow.declarative.builtins import FeatureGoldenWorkflow
from forge.workflow.router import WorkflowRouter
from forge.workflow.task_takeover import TaskTakeoverWorkflow


def create_default_router() -> WorkflowRouter:
    """Create router with built-in workflows."""
    router = WorkflowRouter()
    router.register(TaskTakeoverWorkflow)
    router.register(FeatureGoldenWorkflow)
    router.register(BugWorkflow)
    return router

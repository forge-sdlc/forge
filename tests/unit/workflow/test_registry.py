"""Tests for workflow registry."""

from forge.models.workflow import TicketType


class TestDefaultRouter:
    """Tests for create_default_router."""

    def test_creates_router_with_workflows(self):
        """create_default_router returns router with workflows."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()
        workflows = router.list_workflows()

        assert len(workflows) >= 3

    def test_resolves_feature_to_feature_workflow(self):
        """Feature tickets resolve to FeatureWorkflow."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()
        workflow = router.resolve(TicketType.FEATURE, [], {})

        assert workflow is not None
        assert workflow.name == "feature"

    def test_resolves_bug_to_bug_workflow(self):
        """Bug tickets resolve to BugWorkflow."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()
        workflow = router.resolve(TicketType.BUG, [], {})

        assert workflow is not None
        assert workflow.name == "bug"

    def test_resolves_standalone_task_and_epic_to_task_takeover_workflow(self):
        """Managed standalone Task/Epic tickets resolve to TaskTakeoverWorkflow."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()

        workflow = router.resolve(
            TicketType.TASK,
            ["forge:managed"],
            {},
        )
        assert workflow is not None
        assert workflow.name == "task_takeover"

        workflow = router.resolve(
            TicketType.EPIC,
            ["forge:managed"],
            {},
        )
        assert workflow is not None
        assert workflow.name == "task_takeover"

    def test_resolves_feature_and_bug_to_general_workflows(self):
        """Managed Feature/Bug tickets resolve to their general workflows."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()

        workflow = router.resolve(
            TicketType.FEATURE,
            ["forge:managed"],
            {},
        )
        assert workflow is not None
        assert workflow.name == "feature"

        workflow = router.resolve(
            TicketType.BUG,
            ["forge:managed"],
            {},
        )
        assert workflow is not None
        assert workflow.name == "bug"

    def test_internal_task_labels_do_not_override_ticket_type(self):
        """Task identity labels do not force Feature/Bug tickets into Task Takeover."""
        from forge.workflow.registry import create_default_router

        router = create_default_router()

        workflow = router.resolve(
            TicketType.BUG,
            ["forge:managed", "forge:managed:task"],
            {},
        )
        assert workflow is not None
        assert workflow.name == "bug"

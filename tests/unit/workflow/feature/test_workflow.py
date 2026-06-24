"""Tests for FeatureWorkflow."""


from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.feature.graph import (
    _route_after_epic_regeneration,
    _route_after_epic_task_regeneration,
    _route_after_prd_regeneration,
    _route_after_single_epic_update,
    _route_after_single_task_update,
    _route_after_spec_regeneration,
    _route_after_task_regeneration,
    build_feature_graph,
    route_by_ticket_type,
)


class TestFeatureWorkflow:
    """Tests for FeatureWorkflow class."""

    def test_workflow_has_name(self):
        """FeatureWorkflow has name attribute."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        assert workflow.name == "feature"

    def test_workflow_has_description(self):
        """FeatureWorkflow has description."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        assert "PRD" in workflow.description

    def test_matches_feature_type(self):
        """Matches Feature ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.FEATURE, [], {}) is True

    def test_matches_story_type(self):
        """Matches Story ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.STORY, [], {}) is True

    def test_does_not_match_bug(self):
        """Does not match Bug ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.BUG, [], {}) is False

    def test_state_schema_returns_feature_state(self):
        """state_schema returns FeatureState."""
        from forge.workflow.feature import FeatureWorkflow
        from forge.workflow.feature.state import FeatureState

        workflow = FeatureWorkflow()

        assert workflow.state_schema is FeatureState

    def test_build_graph_returns_state_graph(self):
        """build_graph returns a StateGraph."""
        from langgraph.graph import StateGraph

        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        graph = workflow.build_graph()

        assert isinstance(graph, StateGraph)

    def test_create_initial_state(self):
        """create_initial_state returns FeatureState with defaults."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        state = workflow.create_initial_state("TEST-123")

        assert state["ticket_key"] == "TEST-123"
        assert state["ticket_type"] == TicketType.FEATURE
        assert state["prd_content"] == ""

    def test_resume_regenerate_all_epics_stays_on_regeneration_node(self):
        """Retrying full plan regeneration should not restart raw epic decomposition."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_all_epics",
        }

        assert route_by_ticket_type(state) == "regenerate_all_epics"

    def test_resume_update_single_epic_stays_on_update_node(self):
        """Retrying an Epic-level plan update should not create new epics."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "update_single_epic",
        }

        assert route_by_ticket_type(state) == "update_single_epic"

    def test_resume_regenerate_prd_stays_on_regeneration_node(self):
        """Retrying PRD revision should not restart initial PRD generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_prd",
        }

        assert route_by_ticket_type(state) == "regenerate_prd"

    def test_resume_regenerate_spec_stays_on_regeneration_node(self):
        """Retrying spec revision should not restart initial spec generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_spec",
        }

        assert route_by_ticket_type(state) == "regenerate_spec"

    def test_resume_regenerate_all_tasks_stays_on_regeneration_node(self):
        """Retrying full task regeneration should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_all_tasks",
        }

        assert route_by_ticket_type(state) == "regenerate_all_tasks"

    def test_resume_update_single_task_stays_on_update_node(self):
        """Retrying a Task-level update should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "update_single_task",
        }

        assert route_by_ticket_type(state) == "update_single_task"

    def test_resume_implement_task_stays_on_implementation_node(self):
        """Retrying feature implementation should not reroute through task_router."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implement_task",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_legacy_implementation_alias_stays_on_implementation_node(self):
        """Legacy feature implementation checkpoints should retry implementation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implementation",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_polluted_bug_implementation_node_stays_on_implementation_node(self):
        """Feature checkpoints polluted with bug node names should not restart PRD generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implement_bug_fix",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_regenerate_epic_tasks_stays_on_regeneration_node(self):
        """Retrying epic-level task regeneration should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_epic_tasks",
        }

        assert route_by_ticket_type(state) == "regenerate_epic_tasks"

    def test_resume_setup_workspace_stays_on_setup_node(self):
        """Retrying setup should not reset repo progress through task_router."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "setup_workspace",
        }

        assert route_by_ticket_type(state) == "setup_workspace"

    def test_resume_create_pr_stays_on_create_pr_node(self):
        """Retrying PR creation should not reset repo progress through task_router."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "create_pr",
        }

        assert route_by_ticket_type(state) == "create_pr"

    def test_resume_teardown_workspace_stays_on_teardown_node(self):
        """Retrying teardown should not reset repo progress through task_router."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "teardown_workspace",
        }

        assert route_by_ticket_type(state) == "teardown_workspace"

    def test_resume_feature_terminal_chain_stays_on_terminal_node(self):
        """Feature terminal-chain nodes are feature-specific resume targets."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "complete_tasks",
        }

        assert route_by_ticket_type(state) == "complete_tasks"

    def test_resume_aggregate_epic_status_stays_on_node(self):
        """aggregate_epic_status resumes at itself, not END."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "aggregate_epic_status",
        }

        assert route_by_ticket_type(state) == "aggregate_epic_status"

    def test_resume_aggregate_feature_status_stays_on_node(self):
        """aggregate_feature_status resumes at itself, not END."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "aggregate_feature_status",
        }

        assert route_by_ticket_type(state) == "aggregate_feature_status"

    def test_resume_blocked_routes_to_create_pr(self):
        """Retrying from blocked state should resume at create_pr."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "blocked",
        }

        assert route_by_ticket_type(state) == "create_pr"

    def test_failed_regenerate_epic_tasks_preserves_retry_node(self):
        """Failed epic-level task regeneration should not re-enter the approval gate."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_epic_tasks",
            "last_error": "No replacement Tasks generated",
        }

        assert _route_after_epic_task_regeneration(state) == END

    def test_successful_regenerate_epic_tasks_returns_to_task_approval_gate(self):
        """Successful epic-level task regeneration returns to task approval."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "task_approval_gate",
            "last_error": None,
        }

        assert _route_after_epic_task_regeneration(state) == "task_approval_gate"

    def test_failed_prd_regeneration_stops_current_invocation(self):
        state = {"current_node": "regenerate_prd", "last_error": "agent failed"}

        assert _route_after_prd_regeneration(state) == END

    def test_successful_prd_regeneration_returns_to_gate(self):
        state = {"current_node": "prd_approval_gate", "last_error": None}

        assert _route_after_prd_regeneration(state) == "prd_approval_gate"

    def test_failed_spec_regeneration_stops_current_invocation(self):
        state = {"current_node": "regenerate_spec", "last_error": "agent failed"}

        assert _route_after_spec_regeneration(state) == END

    def test_successful_spec_regeneration_returns_to_gate(self):
        state = {"current_node": "spec_approval_gate", "last_error": None}

        assert _route_after_spec_regeneration(state) == "spec_approval_gate"

    def test_failed_full_epic_regeneration_stops_current_invocation(self):
        state = {"current_node": "decompose_epics", "last_error": "jira failed"}

        assert _route_after_epic_regeneration(state) == END

    def test_successful_full_epic_regeneration_returns_to_gate(self):
        state = {"current_node": "plan_approval_gate", "last_error": None}

        assert _route_after_epic_regeneration(state) == "plan_approval_gate"

    def test_failed_single_epic_update_stops_current_invocation(self):
        state = {"current_node": "update_single_epic", "last_error": "jira failed"}

        assert _route_after_single_epic_update(state) == END

    def test_successful_single_epic_update_returns_to_gate(self):
        state = {"current_node": "plan_approval_gate", "last_error": None}

        assert _route_after_single_epic_update(state) == "plan_approval_gate"

    def test_failed_full_task_regeneration_stops_current_invocation(self):
        state = {"current_node": "generate_tasks", "last_error": "jira failed"}

        assert _route_after_task_regeneration(state) == END

    def test_successful_full_task_regeneration_returns_to_gate(self):
        state = {"current_node": "task_approval_gate", "last_error": None}

        assert _route_after_task_regeneration(state) == "task_approval_gate"

    def test_failed_single_task_update_stops_current_invocation(self):
        state = {"current_node": "update_single_task", "last_error": "jira failed"}

        assert _route_after_single_task_update(state) == END

    def test_successful_single_task_update_returns_to_gate(self):
        state = {"current_node": "task_approval_gate", "last_error": None}

        assert _route_after_single_task_update(state) == "task_approval_gate"

    def test_rebase_can_return_to_post_pr_nodes(self):
        graph = build_feature_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "rebase_pr"}

        assert {
            "wait_for_ci_gate",
            "implement_review",
            "review_response_gate",
            "create_pr",
            "teardown_workspace",
        }.issubset(targets)

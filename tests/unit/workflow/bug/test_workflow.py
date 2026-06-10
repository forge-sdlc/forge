"""Tests for BugWorkflow."""



from forge.models.workflow import TicketType
from forge.workflow.bug.state import create_initial_bug_state


class TestBugWorkflow:
    """Tests for BugWorkflow class."""

    def test_workflow_has_name(self):
        """BugWorkflow has name attribute."""
        from forge.workflow.bug import BugWorkflow

        workflow = BugWorkflow()
        assert workflow.name == "bug"

    def test_matches_bug_type(self):
        """Matches Bug ticket type."""
        from forge.workflow.bug import BugWorkflow

        workflow = BugWorkflow()

        assert workflow.matches(TicketType.BUG, [], {}) is True

    def test_does_not_match_feature(self):
        """Does not match Feature ticket type."""
        from forge.workflow.bug import BugWorkflow

        workflow = BugWorkflow()

        assert workflow.matches(TicketType.FEATURE, [], {}) is False

    def test_state_schema_returns_bug_state(self):
        """state_schema returns BugState."""
        from forge.workflow.bug import BugWorkflow
        from forge.workflow.bug.state import BugState

        workflow = BugWorkflow()

        assert workflow.state_schema is BugState

    def test_build_graph_returns_state_graph(self):
        """build_graph returns a StateGraph."""
        from langgraph.graph import StateGraph

        from forge.workflow.bug import BugWorkflow

        workflow = BugWorkflow()
        graph = workflow.build_graph()

        assert isinstance(graph, StateGraph)


class TestBugStateNewFields:
    """New BugState fields from redesign serialize correctly and default safely."""

    def test_new_fields_have_correct_defaults(self):
        """create_initial_bug_state populates all new fields with correct defaults."""
        state = create_initial_bug_state(ticket_key="BUG-1")
        assert state["triage_passed"] is False
        assert state["triage_missing_fields"] == []
        assert state["reflection_count"] == 0
        assert state["reflection_critique"] is None
        assert state["rca_options"] == []
        assert state["selected_fix_option"] is None
        assert state["selected_fix_approach"] is None
        assert state["plan_content"] is None
        assert state["linked_task_keys"] == []
        assert state["local_review_verdict"] is None
        assert state["qualitative_retry_count"] == 0
        assert state["qualitative_review_failed"] is False

    def test_old_state_without_new_fields_does_not_crash_route_entry(self):
        """A state dict missing all new fields can be passed to route_entry without KeyError."""
        from forge.workflow.bug.graph import route_entry
        minimal_old_state = {
            "ticket_key": "BUG-OLD",
            "ticket_type": "bug",
            "current_node": "implement_bug_fix",
            "is_paused": False,
            # All new fields absent — simulating an in-flight pre-redesign ticket
        }
        result = route_entry(minimal_old_state)
        assert result == "implement_bug_fix"

    def test_rca_approval_gate_checkpoint_maps_correctly(self):
        """In-flight state with current_node='rca_approval_gate' routes to rca_option_gate."""
        from forge.workflow.bug.graph import route_entry
        state = {
            "ticket_key": "BUG-OLD",
            "current_node": "rca_approval_gate",
            "is_paused": True,
        }
        assert route_entry(state) == "rca_option_gate"

    def test_new_fields_not_required_for_route_entry(self):
        """route_entry handles state dicts missing new fields — uses .get() throughout."""
        from forge.workflow.bug.graph import route_entry
        for node, expected in [
            ("triage_check", "triage_check"),
            ("analyze_bug", "analyze_bug"),
            ("setup_workspace", "setup_workspace"),
            ("escalate_blocked", "escalate_blocked"),
        ]:
            result = route_entry({"current_node": node})
            assert result == expected


class TestTasksByRepoInBugState:
    """tasks_by_repo must be declared in BugState so LangGraph checkpoints it."""

    def test_tasks_by_repo_declared_in_bug_state_annotations(self):
        """tasks_by_repo is declared in BugState so LangGraph includes it in the checkpoint schema."""
        from forge.workflow.bug.state import BugState
        all_annotations: dict = {}
        for cls in BugState.__mro__:
            all_annotations.update(getattr(cls, "__annotations__", {}))
        assert "tasks_by_repo" in all_annotations, (
            "tasks_by_repo is not declared in BugState. "
            "LangGraph will not persist it across checkpoints, silently skipping implementation."
        )

    def test_initial_bug_state_has_empty_tasks_by_repo(self):
        """create_initial_bug_state includes tasks_by_repo defaulting to {}."""
        state = create_initial_bug_state(ticket_key="BUG-1")
        assert state["tasks_by_repo"] == {}


class TestNewStateFixtures:
    """Validate the new workflow state fixtures."""

    def test_state_triage_pending_has_correct_fields(self):
        """STATE_TRIAGE_PENDING represents a paused triage state correctly."""
        from tests.fixtures.workflow_states import STATE_TRIAGE_PENDING
        assert STATE_TRIAGE_PENDING["is_paused"] is True
        assert STATE_TRIAGE_PENDING["current_node"] == "triage_gate"
        assert STATE_TRIAGE_PENDING["triage_passed"] is False
        assert len(STATE_TRIAGE_PENDING.get("triage_missing_fields", [])) > 0

    def test_state_rca_option_pending_has_options(self):
        """STATE_RCA_OPTION_PENDING has at least 2 RCA options with required keys."""
        from tests.fixtures.workflow_states import STATE_RCA_OPTION_PENDING
        options = STATE_RCA_OPTION_PENDING.get("rca_options", [])
        assert len(options) >= 2
        for opt in options:
            assert "title" in opt
            assert "description" in opt
            assert "tradeoffs" in opt

    def test_state_bug_plan_pending_has_plan_content(self):
        """STATE_BUG_PLAN_PENDING has non-empty plan_content."""
        from tests.fixtures.workflow_states import STATE_BUG_PLAN_PENDING
        assert STATE_BUG_PLAN_PENDING["current_node"] == "plan_approval_gate"
        assert STATE_BUG_PLAN_PENDING.get("plan_content", "")

    def test_triage_pending_fixture_routes_to_triage_gate(self):
        """STATE_TRIAGE_PENDING route_entry returns 'triage_gate'."""
        from tests.fixtures.workflow_states import STATE_TRIAGE_PENDING

        from forge.workflow.bug.graph import route_entry
        assert route_entry(STATE_TRIAGE_PENDING) == "triage_gate"

    def test_rca_option_pending_fixture_routes_to_rca_option_gate(self):
        """STATE_RCA_OPTION_PENDING route_entry returns 'rca_option_gate'."""
        from tests.fixtures.workflow_states import STATE_RCA_OPTION_PENDING

        from forge.workflow.bug.graph import route_entry
        assert route_entry(STATE_RCA_OPTION_PENDING) == "rca_option_gate"

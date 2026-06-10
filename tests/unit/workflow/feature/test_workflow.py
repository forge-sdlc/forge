"""Tests for FeatureWorkflow."""


from forge.models.workflow import TicketType


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

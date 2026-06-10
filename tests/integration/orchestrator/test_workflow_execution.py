"""Integration tests for LangGraph workflow execution.

These tests verify the actual graph executes correctly, not just routing functions.
They use real LangGraph with SQLite checkpointer but mock external services.

NOTE: These tests need to be updated for the new pluggable workflows architecture.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from forge.models.workflow import TicketType
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state

pytestmark = pytest.mark.skip(reason="Needs update for pluggable workflows architecture")


@pytest.fixture
def temp_checkpoint_db():
    """Create a temporary SQLite database for checkpointing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)
    # Cleanup handled by OS


@pytest.fixture
def mock_jira_client():
    """Mock JiraClient for workflow tests."""
    from forge.integrations.jira.models import JiraIssue

    mock = MagicMock()
    mock.get_issue = AsyncMock(
        return_value=JiraIssue(
            key="TEST-123",
            id="10001",
            summary="Test Feature: User authentication",
            description="As a user, I want to log in securely.",
            status="New",
            issue_type="Feature",
            labels=["forge:managed"],
        )
    )
    mock.update_description = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.add_structured_comment = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_agent():
    """Mock ForgeAgent for workflow tests."""
    mock = MagicMock()
    mock.generate_prd = AsyncMock(
        return_value="""# Product Requirements Document

## Overview
User authentication feature for secure login.

## Requirements
1. Email/password authentication
2. Session management
3. Password reset flow

## Acceptance Criteria
- Users can log in with valid credentials
- Invalid credentials show error message
"""
    )
    mock.run_task = AsyncMock(
        return_value="""# Root Cause Analysis

## Summary
Login fails due to unescaped special characters in password validation.

## Root Cause
The password validator regex does not handle $ and @ symbols.

## Recommended Fix
Update the regex pattern in validators.py to allow special characters.
"""
    )
    mock.close = AsyncMock()
    return mock


class TestWorkflowRouting:
    """Test that workflow routes correctly based on ticket type."""

    async def test_feature_ticket_routes_to_prd_generation(self, temp_checkpoint_db):
        """Feature tickets should route to generate_prd node."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="TEST-123",
                ticket_key="TEST-123",
                ticket_type=TicketType.FEATURE,
            )

            # Check the graph structure - feature should go to generate_prd
            create_workflow_graph()

            # Test routing function directly
            from forge.orchestrator.graph import route_by_ticket_type

            route = route_by_ticket_type(initial_state)
            assert route == "generate_prd", f"Feature should route to generate_prd, got {route}"

    async def test_bug_ticket_routes_to_analyze_bug(self, temp_checkpoint_db):
        """Bug tickets should route to analyze_bug node."""
        initial_state = create_initial_state(
            thread_id="TEST-456",
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
        )

        from forge.orchestrator.graph import route_by_ticket_type

        route = route_by_ticket_type(initial_state)
        assert route == "analyze_bug", f"Bug should route to analyze_bug, got {route}"

    async def test_task_ticket_routes_to_task_workflow(self, temp_checkpoint_db):
        """Task tickets should route to task_workflow node."""
        initial_state = create_initial_state(
            thread_id="TEST-789",
            ticket_key="TEST-789",
            ticket_type=TicketType.TASK,
        )

        from forge.orchestrator.graph import route_by_ticket_type

        route = route_by_ticket_type(initial_state)
        assert route == "task_workflow", f"Task should route to task_workflow, got {route}"


class TestFeatureWorkflowExecution:
    """Test feature workflow execution with real LangGraph."""

    @pytest.mark.slow
    async def test_feature_runs_through_prd_and_pauses(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Feature workflow should generate PRD and pause at approval gate."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="TEST-123",
                ticket_key="TEST-123",
                ticket_type=TicketType.FEATURE,
            )

            # Mock external dependencies
            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                # Run workflow
                config = {"configurable": {"thread_id": "TEST-123"}}
                result = await workflow.ainvoke(initial_state, config)

                # Verify PRD was generated
                assert result.get("prd_content"), "PRD content should be populated"
                assert "Product Requirements Document" in result["prd_content"]

                # Verify workflow paused at approval gate
                assert result.get("is_paused"), "Workflow should be paused"
                assert result.get("current_node") == "prd_approval_gate"

                # Verify external calls were made
                mock_jira_client.get_issue.assert_called_once_with("TEST-123")
                mock_agent.generate_prd.assert_called_once()
                mock_jira_client.set_workflow_label.assert_called()

    @pytest.mark.slow
    async def test_workflow_state_persisted_via_checkpointer(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Workflow state should be persisted and retrievable."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="TEST-123",
                ticket_key="TEST-123",
                ticket_type=TicketType.FEATURE,
            )

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                config = {"configurable": {"thread_id": "TEST-123"}}
                await workflow.ainvoke(initial_state, config)

                # Verify state was checkpointed
                checkpoint = await checkpointer.aget(config)
                assert checkpoint is not None, "Checkpoint should exist"

                # Verify checkpoint contains our state
                channel_values = checkpoint.get("channel_values", {})
                # LangGraph stores state in channel_values
                assert channel_values, "Channel values should contain state"


class TestBugWorkflowExecution:
    """Test bug workflow execution with real LangGraph."""

    @pytest.mark.slow
    async def test_bug_runs_through_rca_and_pauses(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Bug workflow should generate RCA and pause at approval gate."""
        # Update mock for bug issue
        from forge.integrations.jira.models import JiraIssue
        mock_jira_client.get_issue = AsyncMock(
            return_value=JiraIssue(
                key="BUG-456",
                id="10002",
                summary="Login fails with special characters",
                description="Steps to reproduce:\n1. Enter password with $@!\n2. Click login\n\nExpected: Success\nActual: 500 error",
                status="New",
                issue_type="Bug",
                labels=["forge:managed"],
            )
        )

        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="BUG-456",
                ticket_key="BUG-456",
                ticket_type=TicketType.BUG,
            )

            with patch("forge.workflow.nodes.bug_workflow.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.bug_workflow.ForgeAgent") as MockAgent, \
                 patch("forge.workflow.nodes.bug_workflow.get_settings") as mock_settings:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent
                mock_settings.return_value = MagicMock()

                config = {"configurable": {"thread_id": "BUG-456"}}
                result = await workflow.ainvoke(initial_state, config)

                # Verify RCA was generated
                assert result.get("rca_content"), "RCA content should be populated"
                assert "Root Cause Analysis" in result["rca_content"]

                # Verify workflow paused at approval gate
                assert result.get("is_paused"), "Workflow should be paused"
                assert result.get("current_node") == "rca_approval_gate"


class TestWorkflowResumption:
    """Test workflow resume from checkpoint."""

    @pytest.mark.slow
    async def test_workflow_resumes_from_checkpoint(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Workflow should resume from checkpointed state after approval."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="TEST-123",
                ticket_key="TEST-123",
                ticket_type=TicketType.FEATURE,
            )

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                config = {"configurable": {"thread_id": "TEST-123"}}

                # First run - generates PRD and pauses
                result = await workflow.ainvoke(initial_state, config)
                assert result.get("is_paused")
                assert result.get("current_node") == "prd_approval_gate"

            # Verify we can retrieve the checkpoint
            checkpoint = await checkpointer.aget(config)
            assert checkpoint is not None, "Should be able to retrieve checkpoint after pause"


class TestConditionalEdges:
    """Test conditional edge routing in the workflow."""

    async def test_prd_approval_routes_to_spec_on_approval(self):
        """PRD approval should route to spec generation when approved."""
        from forge.orchestrator.gates import route_prd_approval

        # State after approval (not paused, no revision requested)
        state: WorkflowState = {
            "ticket_key": "TEST-123",
            "is_paused": False,
            "revision_requested": False,
            "prd_content": "# PRD\n\nApproved content",
        }

        route = route_prd_approval(state)
        assert route == "generate_spec", f"Approved PRD should route to generate_spec, got {route}"

    async def test_prd_approval_routes_to_regenerate_on_rejection(self):
        """PRD approval should route to regenerate when revision requested."""
        from forge.orchestrator.gates import route_prd_approval

        # State after rejection with feedback
        state: WorkflowState = {
            "ticket_key": "TEST-123",
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": "Please add more detail about personas",
            "prd_content": "# PRD\n\nOriginal content",
        }

        route = route_prd_approval(state)
        assert route == "regenerate_prd", f"Rejected PRD should route to regenerate, got {route}"

    async def test_prd_approval_pauses_when_waiting(self):
        """PRD approval should return END when waiting for approval."""
        from langgraph.graph import END

        from forge.orchestrator.gates import route_prd_approval

        # State while waiting for approval
        state: WorkflowState = {
            "ticket_key": "TEST-123",
            "is_paused": True,
            "revision_requested": False,
            "prd_content": "# PRD\n\nContent awaiting approval",
        }

        route = route_prd_approval(state)
        assert route == END, f"Paused PRD should return END, got {route}"


class TestGraphStructure:
    """Test that the workflow graph is structured correctly."""

    def test_graph_has_required_nodes(self):
        """Verify all required nodes are present in the graph."""
        graph = create_workflow_graph()

        required_nodes = [
            "route_entry",
            "generate_prd",
            "prd_approval_gate",
            "regenerate_prd",
            "generate_spec",
            "spec_approval_gate",
            "decompose_epics",
            "analyze_bug",
            "rca_approval_gate",
        ]

        for node in required_nodes:
            assert node in graph.nodes, f"Missing required node: {node}"

    def test_graph_compiles_without_error(self):
        """Verify the graph compiles successfully."""
        graph = create_workflow_graph()
        compiled = graph.compile()
        assert compiled is not None, "Graph should compile successfully"

    def test_graph_compiles_with_checkpointer(self, temp_checkpoint_db):
        """Verify the graph compiles with a checkpointer."""
        import asyncio

        async def _test():
            async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
                compiled = compile_workflow(checkpointer=checkpointer)
                assert compiled is not None

        asyncio.run(_test())

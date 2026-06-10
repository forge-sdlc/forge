"""End-to-end test for feature workflow.

This test runs the complete feature workflow happy path with:
- Real LangGraph execution
- Real SQLite checkpointer
- Mocked external services only (Jira, GitHub, Agent)

The test verifies state transitions at each step.

NOTE: These tests need to be updated for the new pluggable workflows architecture.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from forge.integrations.jira.models import JiraIssue
from forge.models.workflow import TicketType
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state

pytestmark = pytest.mark.skip(reason="Needs update for pluggable workflows architecture")


# Realistic mock responses
MOCK_PRD_CONTENT = """# Product Requirements Document

## Overview
This document outlines the requirements for implementing OAuth2 authentication.

## User Story
As a user, I want to authenticate via OAuth2 providers so that I can securely
access the application without managing yet another password.

## Requirements

### Functional Requirements
1. Support Google OAuth2 provider
2. Support GitHub OAuth2 provider
3. Secure token storage with encryption
4. Automatic token refresh before expiry
5. Session management with configurable timeout

### Non-Functional Requirements
1. Authentication flow < 3 seconds
2. 99.9% availability target
3. SOC2 compliance for token storage

## Acceptance Criteria
- [ ] Users can log in with Google account
- [ ] Users can log in with GitHub account
- [ ] Tokens are encrypted at rest
- [ ] Session expires after 24h of inactivity
"""

MOCK_SPEC_CONTENT = """# Behavioral Specification

## Overview
Detailed technical specification for OAuth2 authentication implementation.

## API Endpoints

### POST /auth/oauth/google/initiate
Initiates Google OAuth2 flow by redirecting to Google consent screen.

**Response:** 302 redirect to Google OAuth consent URL

### GET /auth/oauth/google/callback
Handles OAuth callback from Google with authorization code.

**Request Query Params:**
- code: Authorization code from Google
- state: CSRF protection token

**Response:**
```json
{
  "access_token": "jwt-token",
  "refresh_token": "refresh-token",
  "expires_in": 3600
}
```

## Database Schema

### users table
- id: UUID (PK)
- email: VARCHAR(255) UNIQUE
- oauth_provider: VARCHAR(50)
- oauth_id: VARCHAR(255)
- created_at: TIMESTAMP

### refresh_tokens table
- id: UUID (PK)
- user_id: UUID (FK -> users)
- token_hash: VARCHAR(255)
- expires_at: TIMESTAMP
- created_at: TIMESTAMP

## Security Considerations
- All tokens encrypted with AES-256
- PKCE flow for public clients
- State parameter for CSRF protection
"""

MOCK_EPICS_CONTENT = """
Based on the specification, here are the recommended epics:

---
EPIC: Google OAuth2 Provider Integration
REPO: acme/backend
PLAN:
1. Add Google OAuth2 client configuration to settings
2. Create OAuth2 initiate endpoint
3. Create OAuth2 callback handler
4. Implement token exchange with Google API
5. Create user session from OAuth profile
6. Add unit and integration tests
---
EPIC: GitHub OAuth2 Provider Integration
REPO: acme/backend
PLAN:
1. Add GitHub OAuth2 client configuration
2. Reuse callback handler with GitHub-specific logic
3. Map GitHub profile to user model
4. Handle organization membership verification
5. Add GitHub-specific tests
---
EPIC: Token Storage and Security
REPO: acme/backend
PLAN:
1. Create encrypted token storage module
2. Add refresh_tokens database migration
3. Implement token refresh logic
4. Add token expiry cleanup job
5. Security audit and penetration testing
---
"""


@pytest.fixture
def temp_checkpoint_db():
    """Create a temporary SQLite database for checkpointing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture
def mock_jira_client():
    """Mock JiraClient that simulates Jira API."""

    mock = MagicMock()

    # Start with New status
    current_issue = JiraIssue(
        key="FEAT-123",
        id="10001",
        summary="Implement OAuth2 Authentication",
        description="As a user, I want to authenticate via OAuth2 providers.",
        status="New",
        issue_type="Feature",
        labels=["forge:managed"],
    )

    async def get_issue(key):
        return current_issue

    mock.get_issue = AsyncMock(side_effect=get_issue)
    mock.update_description = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.add_structured_comment = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.create_epic = AsyncMock(side_effect=lambda **kwargs: f"EPIC-{len(kwargs)}")
    mock.close = AsyncMock()

    return mock


@pytest.fixture
def mock_agent():
    """Mock ForgeAgent that returns realistic responses."""
    mock = MagicMock()

    mock.generate_prd = AsyncMock(return_value=MOCK_PRD_CONTENT)
    mock.generate_spec = AsyncMock(return_value=MOCK_SPEC_CONTENT)
    mock.generate_epics = AsyncMock(
        return_value=[
            {
                "summary": "Google OAuth2 Provider Integration",
                "repo": "acme/backend",
                "plan": "1. Add Google OAuth2 client configuration...",
            },
            {
                "summary": "GitHub OAuth2 Provider Integration",
                "repo": "acme/backend",
                "plan": "1. Add GitHub OAuth2 client configuration...",
            },
            {
                "summary": "Token Storage and Security",
                "repo": "acme/backend",
                "plan": "1. Create encrypted token storage module...",
            },
        ]
    )
    mock.close = AsyncMock()

    return mock


@pytest.mark.slow
class TestFeatureWorkflowE2E:
    """End-to-end tests for the feature workflow."""

    async def test_full_feature_workflow_prd_generation_and_pause(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Feature workflow generates PRD and pauses for approval."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="FEAT-123",
                ticket_key="FEAT-123",
                ticket_type=TicketType.FEATURE,
            )

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                config = {"configurable": {"thread_id": "FEAT-123"}}

                # Step 1: Run workflow - should generate PRD and pause
                result = await workflow.ainvoke(initial_state, config)

                # Verify PRD was generated
                assert result.get("prd_content"), "PRD should be generated"
                assert len(result["prd_content"]) > 100, "PRD should be substantive"
                assert "OAuth2" in result["prd_content"]

                # Verify workflow paused at approval gate
                assert result.get("is_paused"), "Workflow should be paused"
                assert result.get("current_node") == "prd_approval_gate"

                # Verify Jira was updated
                mock_agent.generate_prd.assert_called_once()
                mock_jira_client.set_workflow_label.assert_called()

                # Verify state was checkpointed
                checkpoint = await checkpointer.aget(config)
                assert checkpoint is not None, "State should be checkpointed"

    async def test_prd_approval_routing_logic(self, temp_checkpoint_db):
        """Verify PRD approval gate routes correctly after approval.

        Note: Full workflow continuation is tested via routing functions directly,
        as LangGraph always starts from the entry point when invoking.
        """
        from forge.orchestrator.gates import route_prd_approval

        # State after user approves (not paused, no revision)
        state_approved: WorkflowState = {
            "ticket_key": "FEAT-123",
            "ticket_type": TicketType.FEATURE,
            "is_paused": False,
            "revision_requested": False,
            "prd_content": MOCK_PRD_CONTENT,
        }

        # Verify routing logic
        route = route_prd_approval(state_approved)
        assert route == "generate_spec", "Approved PRD should route to spec generation"

        # State while still waiting (paused)
        state_waiting: WorkflowState = {
            "ticket_key": "FEAT-123",
            "is_paused": True,
            "revision_requested": False,
            "prd_content": MOCK_PRD_CONTENT,
        }

        from langgraph.graph import END
        route = route_prd_approval(state_waiting)
        assert route == END, "Waiting PRD should return END (pause)"

        # State with revision requested
        state_revision: WorkflowState = {
            "ticket_key": "FEAT-123",
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": "Add more detail",
            "prd_content": MOCK_PRD_CONTENT,
        }

        route = route_prd_approval(state_revision)
        assert route == "regenerate_prd", "Revision should route to regenerate"

    async def test_workflow_state_transitions_are_tracked(
        self, temp_checkpoint_db, mock_jira_client, mock_agent
    ):
        """Verify state transitions are properly tracked."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="FEAT-456",
                ticket_key="FEAT-456",
                ticket_type=TicketType.FEATURE,
            )

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                config = {"configurable": {"thread_id": "FEAT-456"}}
                result = await workflow.ainvoke(initial_state, config)

                # Verify core state fields
                assert result["ticket_key"] == "FEAT-456"
                assert result["ticket_type"] == TicketType.FEATURE
                assert "created_at" in result
                assert "updated_at" in result

                # Verify timestamps are valid ISO format
                from datetime import datetime
                datetime.fromisoformat(result["created_at"])
                datetime.fromisoformat(result["updated_at"])

    async def test_error_handling_preserves_state(
        self, temp_checkpoint_db, mock_jira_client
    ):
        """Errors should be captured in state without losing progress."""
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
            workflow = compile_workflow(checkpointer=checkpointer)

            initial_state = create_initial_state(
                thread_id="FEAT-ERR",
                ticket_key="FEAT-ERR",
                ticket_type=TicketType.FEATURE,
            )

            # Mock agent that fails
            mock_failing_agent = MagicMock()
            mock_failing_agent.generate_prd = AsyncMock(
                side_effect=Exception("API rate limit exceeded")
            )
            mock_failing_agent.close = AsyncMock()

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_failing_agent

                config = {"configurable": {"thread_id": "FEAT-ERR"}}
                result = await workflow.ainvoke(initial_state, config)

                # Verify error was captured
                assert result.get("last_error"), "Error should be recorded"
                assert "rate limit" in result["last_error"].lower()

                # Verify retry count increased
                assert result.get("retry_count", 0) > 0

                # Verify we're still at the failing node (not advanced)
                assert result.get("current_node") == "generate_prd"


class TestWorkflowCheckpointing:
    """Test checkpoint persistence and recovery."""

    async def test_checkpoint_survives_restart(self, temp_checkpoint_db, mock_jira_client, mock_agent):
        """Checkpointed state should survive 'restart' (new checkpointer instance)."""
        config = {"configurable": {"thread_id": "PERSIST-123"}}

        # First "session" - generate PRD
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer1:
            workflow = compile_workflow(checkpointer=checkpointer1)

            initial_state = create_initial_state(
                thread_id="PERSIST-123",
                ticket_key="PERSIST-123",
                ticket_type=TicketType.FEATURE,
            )

            with patch("forge.workflow.nodes.prd_generation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.prd_generation.ForgeAgent") as MockAgent:

                MockJira.return_value = mock_jira_client
                MockAgent.return_value = mock_agent

                result = await workflow.ainvoke(initial_state, config)
                assert result.get("prd_content")
                assert result.get("is_paused")

        # Second "session" - verify state persisted
        async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer2:
            checkpoint = await checkpointer2.aget(config)

            assert checkpoint is not None, "Checkpoint should persist across sessions"

            channel_values = checkpoint.get("channel_values", {})
            # The checkpointed state should contain our data
            assert channel_values, "Channel values should be populated"


class TestWorkflowRouting:
    """Test workflow routing decisions."""

    async def test_revision_requested_routes_to_regenerate(self, temp_checkpoint_db, mock_jira_client, mock_agent):
        """When revision is requested, workflow routes to regenerate node."""
        from forge.orchestrator.gates import route_prd_approval

        # State after user requests changes
        state_with_feedback: WorkflowState = {
            "ticket_key": "FEAT-REV",
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": "Please add more detail about security requirements.",
            "prd_content": MOCK_PRD_CONTENT,
        }

        route = route_prd_approval(state_with_feedback)
        assert route == "regenerate_prd", "Should route to regenerate when revision requested"

    async def test_approval_routes_to_next_stage(self, temp_checkpoint_db):
        """When approved, workflow routes to next generation stage."""
        from forge.orchestrator.gates import route_prd_approval

        # State after user approves
        state_approved: WorkflowState = {
            "ticket_key": "FEAT-APP",
            "is_paused": False,
            "revision_requested": False,
            "prd_content": MOCK_PRD_CONTENT,
        }

        route = route_prd_approval(state_approved)
        assert route == "generate_spec", "Should route to spec generation when approved"

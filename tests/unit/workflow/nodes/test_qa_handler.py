"""Tests for Q&A handler node."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.qa_handler import (
    _determine_artifact_type,
    _get_artifact_content,
    answer_question,
    extract_question_text,
)


class TestExtractQuestionText:
    """Tests for extract_question_text function."""

    def test_strips_question_mark_prefix(self):
        """extract_question_text removes leading ? prefix."""
        assert extract_question_text("?What is this feature about?") == "What is this feature about?"

    def test_strips_question_mark_prefix_with_whitespace(self):
        """extract_question_text handles ? with leading/trailing whitespace."""
        assert extract_question_text("  ?  What is this?  ") == "What is this?"

    def test_strips_at_forge_ask_prefix(self):
        """extract_question_text removes @forge ask prefix."""
        result = extract_question_text("@forge ask Why did you choose this approach?")
        assert result == "Why did you choose this approach?"

    def test_at_forge_ask_case_insensitive(self):
        """extract_question_text handles @Forge Ask in any case."""
        result = extract_question_text("@Forge Ask What about performance?")
        assert result == "What about performance?"

    def test_at_forge_ask_with_whitespace(self):
        """extract_question_text handles @forge ask with extra whitespace."""
        result = extract_question_text("  @forge ask   How does this work?  ")
        assert result == "How does this work?"

    def test_no_prefix_returns_original(self):
        """extract_question_text returns original text when no prefix."""
        assert extract_question_text("This is just a comment") == "This is just a comment"

    def test_empty_string(self):
        """extract_question_text handles empty string."""
        assert extract_question_text("") == ""

    def test_only_question_mark(self):
        """extract_question_text handles lone ? prefix."""
        assert extract_question_text("?") == ""

    def test_only_at_forge_ask(self):
        """extract_question_text handles @forge ask without question."""
        assert extract_question_text("@forge ask") == ""


class TestDetermineArtifactType:
    """Tests for _determine_artifact_type helper."""

    def test_prd_node(self):
        """Nodes containing 'prd' return 'prd' type."""
        assert _determine_artifact_type("prd_approval_gate") == "prd"
        assert _determine_artifact_type("regenerate_prd") == "prd"

    def test_spec_node(self):
        """Nodes containing 'spec' return 'spec' type."""
        assert _determine_artifact_type("spec_approval_gate") == "spec"
        assert _determine_artifact_type("generate_spec") == "spec"

    def test_rca_node(self):
        """Nodes containing 'rca' return 'rca' type."""
        assert _determine_artifact_type("rca_approval_gate") == "rca"
        assert _determine_artifact_type("analyze_rca") == "rca"

    def test_plan_node(self):
        """Nodes containing 'plan' return 'plan' type."""
        assert _determine_artifact_type("plan_approval_gate") == "plan"
        assert _determine_artifact_type("epic_plan_review") == "plan"

    def test_unknown_node(self):
        """Nodes not matching any pattern return 'unknown'."""
        assert _determine_artifact_type("human_review") == "unknown"
        assert _determine_artifact_type("") == "unknown"


class TestGetArtifactContent:
    """Tests for _get_artifact_content helper."""

    def test_gets_prd_content(self):
        """Returns prd_content for prd artifact type."""
        state = create_initial_feature_state(ticket_key="TEST-1")
        state["prd_content"] = "PRD content here"
        assert _get_artifact_content(state, "prd") == "PRD content here"

    def test_gets_spec_content(self):
        """Returns spec_content for spec artifact type."""
        state = create_initial_feature_state(ticket_key="TEST-1")
        state["spec_content"] = "Spec content here"
        assert _get_artifact_content(state, "spec") == "Spec content here"

    def test_unknown_type_returns_empty(self):
        """Returns empty string for unknown artifact type."""
        state = create_initial_feature_state(ticket_key="TEST-1")
        assert _get_artifact_content(state, "unknown") == ""

    def test_missing_content_returns_empty(self):
        """Returns empty string when content field is missing."""
        state = create_initial_feature_state(ticket_key="TEST-1")
        assert _get_artifact_content(state, "prd") == ""


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock(return_value=MagicMock(id="comment-123"))
    return mock


def create_mock_forge_agent():
    """Create a mock ForgeAgent with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.answer_question = AsyncMock(return_value="This is the answer to your question.")
    return mock


class TestAnswerQuestion:
    """Tests for answer_question node function."""

    @pytest.mark.asyncio
    async def test_posts_answer_to_jira(self):
        """answer_question posts formatted Q&A to Jira."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?What does this feature do?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD Content"
        state["is_question"] = True

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await answer_question(state)

        # Verify Jira comment was posted
        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args
        assert call_args[0][0] == "TEST-123"
        assert "*Q: What does this feature do?*" in call_args[0][1]
        assert "This is the answer to your question." in call_args[0][1]

    @pytest.mark.asyncio
    async def test_stays_paused_at_same_node(self):
        """answer_question keeps workflow paused at the same gate."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Why this approach?"
        state["current_node"] = "spec_approval_gate"
        state["spec_content"] = "# Spec Content"
        state["is_question"] = True

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await answer_question(state)

        # Verify state remains paused at same node
        assert result["is_paused"] is True
        assert result["current_node"] == "spec_approval_gate"
        assert result["is_question"] is False
        assert result["feedback_comment"] is None
        assert result["revision_requested"] is False

    @pytest.mark.asyncio
    async def test_records_in_qa_history(self):
        """answer_question appends to qa_history."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_agent.answer_question = AsyncMock(return_value="The answer is 42.")

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?What is the meaning of life?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD Content"
        state["is_question"] = True
        state["qa_history"] = []

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await answer_question(state)

        # Verify qa_history was updated
        assert len(result["qa_history"]) == 1
        qa_entry = result["qa_history"][0]
        assert qa_entry["question"] == "What is the meaning of life?"
        assert qa_entry["answer"] == "The answer is 42."
        assert qa_entry["artifact_type"] == "prd"
        assert "timestamp" in qa_entry

        # Verify timestamp is valid ISO format
        timestamp = datetime.fromisoformat(qa_entry["timestamp"])
        assert timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_appends_to_existing_qa_history(self):
        """answer_question appends to existing qa_history entries."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Second question?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD Content"
        state["is_question"] = True
        state["qa_history"] = [
            {
                "question": "First question?",
                "answer": "First answer.",
                "artifact_type": "prd",
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
        ]

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await answer_question(state)

        # Verify both entries exist
        assert len(result["qa_history"]) == 2
        assert result["qa_history"][0]["question"] == "First question?"
        assert result["qa_history"][1]["question"] == "Second question?"

    @pytest.mark.asyncio
    async def test_passes_context_to_agent(self):
        """answer_question passes artifact content and context to agent."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Explain the auth flow"
        state["current_node"] = "spec_approval_gate"
        state["spec_content"] = "# Spec with auth details"
        state["generation_context"] = {
            "spec": {"prd_content": "Original PRD", "generated_at": "2024-01-01"},
        }
        state["is_question"] = True

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await answer_question(state)

        # Verify agent was called with correct parameters
        mock_agent.answer_question.assert_called_once()
        call_kwargs = mock_agent.answer_question.call_args.kwargs
        assert call_kwargs["question"] == "Explain the auth flow"
        assert call_kwargs["artifact_content"] == "# Spec with auth details"
        assert call_kwargs["context"]["artifact_type"] == "spec"
        assert call_kwargs["context"]["ticket_key"] == "TEST-123"
        assert "prd_content" in call_kwargs["context"]["generation_context"]

    @pytest.mark.asyncio
    async def test_handles_no_question_gracefully(self):
        """answer_question returns unchanged state when no feedback_comment."""
        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = ""  # Empty question
        state["current_node"] = "prd_approval_gate"

        result = await answer_question(state)

        # Should return unchanged state
        assert result == state

    @pytest.mark.asyncio
    async def test_handles_agent_error_gracefully(self):
        """answer_question posts error message to Jira on agent failure."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_agent.answer_question = AsyncMock(side_effect=Exception("API error"))

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Why did this fail?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD"
        state["is_question"] = True

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            result = await answer_question(state)

        # Verify error comment was posted
        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args
        assert "Error: API error" in call_args[0][1]

        # Verify state is cleared and remains paused
        assert result["is_paused"] is True
        assert result["feedback_comment"] is None
        assert result["is_question"] is False

    @pytest.mark.asyncio
    async def test_closes_clients_on_success(self):
        """answer_question closes Jira and Agent clients after success."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Question"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD"

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await answer_question(state)

        mock_jira.close.assert_called_once()
        mock_agent.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_closes_clients_on_error(self):
        """answer_question closes Jira and Agent clients after error."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_agent.answer_question = AsyncMock(side_effect=Exception("Error"))

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Question"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD"

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await answer_question(state)

        mock_jira.close.assert_called_once()
        mock_agent.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_posts_answer_to_github_pr_in_pr_mode(self):
        """When prd_pr_number exists, Q&A answer goes to GitHub PR."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_gh = MagicMock()
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?What does this feature do?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD Content"
        state["is_question"] = True
        state["prd_pr_number"] = 7
        state["prd_pr_repo"] = "org/proposals"

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
            patch("forge.workflow.nodes.qa_handler.GitHubClient", return_value=mock_gh),
        ):
            await answer_question(state)

        mock_gh.create_issue_comment.assert_called_once()
        call_args = mock_gh.create_issue_comment.call_args[0]
        assert call_args[0] == "org"
        assert call_args[1] == "proposals"
        assert call_args[2] == 7
        mock_jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_spec_answer_to_github_pr_in_pr_mode(self):
        """When spec_pr_number exists, spec Q&A answer goes to GitHub PR."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()
        mock_gh = MagicMock()
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?Why use this design?"
        state["current_node"] = "spec_approval_gate"
        state["spec_content"] = "# Spec Content"
        state["is_question"] = True
        state["spec_pr_number"] = 12
        state["spec_pr_repo"] = "org/proposals"

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
            patch("forge.workflow.nodes.qa_handler.GitHubClient", return_value=mock_gh),
        ):
            await answer_question(state)

        mock_gh.create_issue_comment.assert_called_once()
        call_args = mock_gh.create_issue_comment.call_args[0]
        assert call_args[0] == "org"
        assert call_args[1] == "proposals"
        assert call_args[2] == 12
        mock_jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_answer_to_jira_when_no_prd_pr(self):
        """Without prd_pr_number, Q&A answer goes to Jira as before."""
        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["feedback_comment"] = "?What does this feature do?"
        state["current_node"] = "prd_approval_gate"
        state["prd_content"] = "# PRD Content"
        state["is_question"] = True

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
        ):
            await answer_question(state)

        mock_jira.add_comment.assert_called_once()


class TestDetermineArtifactTypeBugGates:
    """Bug workflow gate artifact type detection."""

    def test_triage_gate_returns_triage(self):
        assert _determine_artifact_type("triage_gate") == "triage"

    def test_triage_check_returns_triage(self):
        assert _determine_artifact_type("triage_check") == "triage"

    def test_rca_option_gate_returns_rca(self):
        assert _determine_artifact_type("rca_option_gate") == "rca"

    def test_plan_approval_gate_returns_plan(self):
        assert _determine_artifact_type("plan_approval_gate") == "plan"


class TestGetArtifactContentBugGates:
    """Artifact content retrieval for bug workflow state fields."""

    def test_triage_returns_summary_and_description(self):
        state = {
            "ticket_key": "BUG-1",
            "summary": "Login fails with special chars",
            "description": "Users cant log in when password has dollar sign.",
        }
        result = _get_artifact_content(state, "triage")
        assert "Login fails" in result
        assert "Users cant" in result

    def test_triage_handles_missing_fields(self):
        state = {"ticket_key": "BUG-1"}
        result = _get_artifact_content(state, "triage")
        assert isinstance(result, str)

    def test_plan_returns_plan_content_for_bug_workflow(self):
        state = {
            "ticket_key": "BUG-1",
            "plan_content": "## Fix Plan",
            "generation_context": {},
        }
        assert _get_artifact_content(state, "plan") == "## Fix Plan"

    def test_plan_falls_back_to_generation_context(self):
        state = {"ticket_key": "FEAT-1", "generation_context": {"plan": "Feature plan"}}
        assert _get_artifact_content(state, "plan") == "Feature plan"

    def test_rca_returns_rca_content(self):
        state = {"ticket_key": "BUG-1", "rca_content": "## Root Cause"}
        assert _get_artifact_content(state, "rca") == "## Root Cause"



class TestAnswerQuestionBugGates:
    """answer_question stays paused at all three new bug workflow gates."""

    def _make_bug_state(self, node: str) -> dict:
        return {
            "ticket_key": "BUG-42",
            "current_node": node,
            "is_paused": True,
            "is_question": True,
            "feedback_comment": "?Why was this root cause identified?",
            "rca_content": "## Root Cause",
            "plan_content": "## Plan",
            "qa_history": [],
            "generation_context": {},
            "revision_requested": False,
        }

    @pytest.mark.asyncio
    async def test_answer_question_at_triage_gate_stays_paused(self):
        """answer_question at triage_gate returns is_paused=True, node unchanged."""
        state = self._make_bug_state("triage_gate")
        state["summary"] = "Login breaks with special chars"
        state["description"] = "Password containing $ fails login."
        state["feedback_comment"] = "?Can you clarify what fields are missing?"

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
        ):
            result = await answer_question(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "triage_gate"
        assert result["is_question"] is False
        assert result["feedback_comment"] is None

    @pytest.mark.asyncio
    async def test_answer_question_at_rca_option_gate_stays_paused(self):
        """answer_question at rca_option_gate returns is_paused=True, node unchanged."""
        state = self._make_bug_state("rca_option_gate")
        state["feedback_comment"] = "?Why is Option 1 lower risk than Option 2?"

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
        ):
            result = await answer_question(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "rca_option_gate"
        assert result["is_question"] is False
        assert result["feedback_comment"] is None

    @pytest.mark.asyncio
    async def test_answer_question_at_plan_approval_gate_stays_paused(self):
        """answer_question at plan_approval_gate returns is_paused=True, node unchanged."""
        state = self._make_bug_state("plan_approval_gate")
        state["feedback_comment"] = "?Will this plan handle the edge case with empty passwords?"

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_forge_agent()

        with (
            patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent),
        ):
            result = await answer_question(state)

        assert result["is_paused"] is True
        assert result["current_node"] == "plan_approval_gate"
        assert result["is_question"] is False
        assert result["feedback_comment"] is None

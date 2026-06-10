"""Integration tests for Q&A mode."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.utils.comment_classifier import CommentType, classify_comment


class TestQAModeIntegration:
    """Integration tests for Q&A mode flow."""

    def test_question_comment_classified_correctly(self):
        """Verify comment classifier detects questions."""
        assert classify_comment("?Why REST?") == CommentType.QUESTION
        assert classify_comment("@forge ask explain") == CommentType.QUESTION
        assert classify_comment("Add more detail") == CommentType.FEEDBACK
        assert classify_comment("LGTM") == CommentType.FEEDBACK

    def test_state_has_qa_fields(self):
        """Verify initial state includes Q&A fields."""
        state = create_initial_feature_state("TEST-123")

        assert "qa_history" in state
        assert state["qa_history"] == []
        assert "generation_context" in state
        assert state["generation_context"] == {}
        assert "is_question" in state
        assert state["is_question"] is False

    @pytest.mark.asyncio
    async def test_answer_question_node_posts_to_jira(self):
        """Verify answer_question node posts answer to Jira."""
        from forge.workflow.nodes.qa_handler import answer_question

        state = create_initial_feature_state("TEST-123")
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = "# PRD\n\nTest content"
        state["feedback_comment"] = "?Why this approach?"
        state["generation_context"] = {"prd": {"raw_requirements": "Build API"}}

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.answer_question = AsyncMock(return_value="Because of X")
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent):
                result = await answer_question(state)

        # Verify Jira comment was posted
        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args[0]
        assert call_args[0] == "TEST-123"
        assert "Why this approach?" in call_args[1]
        assert "Because of X" in call_args[1]

        # Verify state updates
        assert result["is_paused"] is True
        assert result["current_node"] == "prd_approval_gate"
        assert result["is_question"] is False
        assert result["feedback_comment"] is None
        assert len(result["qa_history"]) == 1

    def test_gate_routing_for_questions(self):
        """Verify approval gates route questions to answer_question."""
        from forge.workflow.gates.prd_approval import route_prd_approval
        from forge.workflow.gates.spec_approval import route_spec_approval

        state = create_initial_feature_state("TEST-123")
        state["is_question"] = True
        state["feedback_comment"] = "?Why?"

        assert route_prd_approval(state) == "answer_question"
        assert route_spec_approval(state) == "answer_question"

    def test_gate_routing_for_feedback(self):
        """Verify approval gates route feedback to regenerate."""
        from forge.workflow.gates.prd_approval import route_prd_approval

        state = create_initial_feature_state("TEST-123")
        state["revision_requested"] = True
        state["feedback_comment"] = "Add more detail"

        assert route_prd_approval(state) == "regenerate_prd"


class TestQASummary:
    """Tests for Q&A summary posting."""

    @pytest.mark.asyncio
    async def test_posts_summary_on_approval(self):
        """Verify Q&A summary is posted when artifact has Q&A history."""
        from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

        qa_history = [
            {"question": "Why REST?", "answer": "Performance", "artifact_type": "prd"},
            {"question": "Why not GraphQL?", "answer": "Simpler", "artifact_type": "prd"},
        ]

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.workflow.utils.qa_summary.JiraClient", return_value=mock_jira):
            await post_qa_summary_if_needed("TEST-123", qa_history, "prd")

        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args[0]
        assert "Q&A Summary for PRD" in call_args[1]
        assert "Why REST?" in call_args[1]
        assert "Performance" in call_args[1]

    @pytest.mark.asyncio
    async def test_skips_summary_when_no_relevant_qa(self):
        """Verify Q&A summary is skipped when no relevant Q&A history."""
        from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

        qa_history = [
            {"question": "Why REST?", "answer": "Performance", "artifact_type": "spec"},
        ]

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.workflow.utils.qa_summary.JiraClient", return_value=mock_jira):
            await post_qa_summary_if_needed("TEST-123", qa_history, "prd")

        # Should not post because Q&A is for spec, not prd
        mock_jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_summary_when_empty_qa_history(self):
        """Verify Q&A summary is skipped when Q&A history is empty."""
        from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.workflow.utils.qa_summary.JiraClient", return_value=mock_jira):
            await post_qa_summary_if_needed("TEST-123", [], "prd")

        mock_jira.add_comment.assert_not_called()


class TestQAHandlerEdgeCases:
    """Edge case tests for Q&A handler."""

    @pytest.mark.asyncio
    async def test_answer_question_handles_no_feedback(self):
        """Verify answer_question handles missing feedback_comment gracefully."""
        from forge.workflow.nodes.qa_handler import answer_question

        state = create_initial_feature_state("TEST-123")
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        state["feedback_comment"] = None

        result = await answer_question(state)

        # Should return state unchanged (except for what the function touches)
        assert result["ticket_key"] == "TEST-123"

    @pytest.mark.asyncio
    async def test_answer_question_handles_agent_error(self):
        """Verify answer_question handles agent errors gracefully."""
        from forge.workflow.nodes.qa_handler import answer_question

        state = create_initial_feature_state("TEST-123")
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = "# PRD\n\nTest content"
        state["feedback_comment"] = "?Why this approach?"

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.answer_question = AsyncMock(side_effect=Exception("API Error"))
        mock_agent.close = AsyncMock()

        with patch("forge.workflow.nodes.qa_handler.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.qa_handler.ForgeAgent", return_value=mock_agent):
                result = await answer_question(state)

        # Should still clear question state and stay paused
        assert result["is_paused"] is True
        assert result["is_question"] is False
        assert result["feedback_comment"] is None

    def test_extract_question_text_removes_prefix(self):
        """Verify question text extraction removes prefixes correctly."""
        from forge.workflow.nodes.qa_handler import extract_question_text

        assert extract_question_text("?Why REST?") == "Why REST?"
        assert extract_question_text("@forge ask explain this") == "explain this"
        assert extract_question_text("  ?  spaced  ") == "spaced"
        assert extract_question_text("plain text") == "plain text"

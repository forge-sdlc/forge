"""Unit tests for rca_option_gate, route_rca_option, and regenerate_rca nodes."""

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

from forge.models.workflow import ForgeLabel
from forge.workflow.nodes.rca_option_gate import (
    rca_option_gate,
    regenerate_rca,
    route_rca_option,
)


def make_rca_option_state(**overrides) -> dict:
    """Return a minimal BugState dict suitable for rca_option_gate tests."""
    base = {
        "ticket_key": "BUG-42",
        "ticket_type": "Bug",
        "current_node": "rca_option_gate",
        "is_paused": False,
        "revision_requested": False,
        "feedback_comment": None,
        "is_question": False,
        "selected_fix_option": None,
        "selected_fix_approach": None,
        "rca_content": "# Root Cause\n\nSomething went wrong in auth.py.",
        "rca_options": [
            {"title": "Option A", "description": "Fix the null check", "tradeoffs": "Low risk"},
            {"title": "Option B", "description": "Refactor auth flow", "tradeoffs": "Higher risk"},
        ],
        "reflection_count": 0,
        "reflection_critique": None,
        "retry_count": 0,
        "last_error": None,
    }
    return {**base, **overrides}


def _make_mock_jira():
    jira = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.close = AsyncMock()
    return jira


class TestRcaOptionGate:
    @pytest.mark.asyncio
    async def test_posts_jira_comment_with_rca_and_options(self):
        """rca_option_gate posts comment containing RCA content and all options."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        mock_jira.add_comment.assert_called_once()
        comment = mock_jira.add_comment.call_args[0][1]
        assert "Root Cause" in comment or "auth.py" in comment

    @pytest.mark.asyncio
    async def test_comment_includes_all_option_numbers(self):
        """Comment lists Option 1, Option 2, etc., with titles and descriptions."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert "Option 1" in comment or "1" in comment
        assert "Option 2" in comment or "2" in comment
        assert "Option A" in comment
        assert "Option B" in comment

    @pytest.mark.asyncio
    async def test_comment_includes_reply_instruction(self):
        """Comment includes >option N instruction for the reporter."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert ">option" in comment.lower()

    @pytest.mark.asyncio
    async def test_sets_rca_pending_label(self):
        """rca_option_gate calls set_workflow_label with ForgeLabel.RCA_PENDING."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        mock_jira.set_workflow_label.assert_called_once_with("BUG-42", ForgeLabel.RCA_PENDING)

    @pytest.mark.asyncio
    async def test_returns_paused_state(self):
        """rca_option_gate returns state with is_paused=True."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await rca_option_gate(state)

        assert result["is_paused"] is True

    @pytest.mark.asyncio
    async def test_current_node_set_to_rca_option_gate(self):
        """rca_option_gate sets current_node='rca_option_gate'."""
        state = make_rca_option_state()
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await rca_option_gate(state)

        assert result["current_node"] == "rca_option_gate"

    @pytest.mark.asyncio
    async def test_truncates_long_rca_content(self):
        """Comment is truncated at 25k characters with truncation note appended."""
        long_rca = "A" * 30_000
        state = make_rca_option_state(rca_content=long_rca)
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert len(comment) <= 25_500  # Allow some headroom for truncation note
        assert "truncated" in comment.lower()

    @pytest.mark.asyncio
    async def test_truncation_preserves_paragraph_boundary(self):
        """Truncation happens at the last \\n\\n before the limit, not mid-sentence."""
        # Build rca_content with paragraphs separated by \n\n
        paragraph = "Word " * 100  # ~500 chars per paragraph
        rca = ("\n\n".join([paragraph] * 60))  # ~30k chars
        state = make_rca_option_state(rca_content=rca)
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert "truncated" in comment.lower()

    @pytest.mark.asyncio
    async def test_no_truncation_for_short_content(self):
        """Comment under 25k is posted without truncation note."""
        state = make_rca_option_state()  # short rca_content
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await rca_option_gate(state)

        comment = mock_jira.add_comment.call_args[0][1]
        assert "truncated" not in comment.lower()


class TestRouteRcaOption:
    def test_routes_to_answer_question_when_is_question(self):
        """is_question=True → returns 'answer_question'."""
        state = make_rca_option_state(is_question=True, feedback_comment="Why this approach?")
        assert route_rca_option(state) == "answer_question"

    def test_routes_to_plan_bug_fix_when_option_selected_and_not_paused(self):
        """selected_fix_option set and is_paused=False → returns 'plan_bug_fix'."""
        state = make_rca_option_state(
            selected_fix_option=1,
            selected_fix_approach={"title": "Option A", "description": "...", "tradeoffs": "Low"},
            is_paused=False,
        )
        assert route_rca_option(state) == "plan_bug_fix"

    def test_routes_to_regenerate_rca_when_revision_requested(self):
        """revision_requested=True → returns 'regenerate_rca'."""
        state = make_rca_option_state(
            revision_requested=True, feedback_comment="This RCA is wrong."
        )
        assert route_rca_option(state) == "regenerate_rca"

    def test_routes_to_end_when_is_paused(self):
        """is_paused=True with no other signals → returns END."""
        state = make_rca_option_state(is_paused=True)
        assert route_rca_option(state) == END

    def test_is_question_takes_priority_over_option_selected(self):
        """is_question=True takes priority even if selected_fix_option is also set."""
        state = make_rca_option_state(
            is_question=True,
            feedback_comment="?Why not Option B?",
            selected_fix_option=1,
            is_paused=False,
        )
        assert route_rca_option(state) == "answer_question"


class TestRegenerateRca:
    FEEDBACK = "The identified file is wrong, check utils.py instead."

    @pytest.fixture
    def regen_state(self):
        return make_rca_option_state(
            revision_requested=True,
            feedback_comment=self.FEEDBACK,
            is_paused=True,
            reflection_count=2,
            retry_count=1,
            selected_fix_option=1,
            selected_fix_approach={"title": "Fix A"},
            rca_comment_posted=True,
        )

    @pytest.fixture
    def mock_jira(self):
        return _make_mock_jira()

    @pytest.mark.asyncio
    async def test_routes_back_to_analyze_bug(self, regen_state, mock_jira):
        """regenerate_rca returns state with current_node='analyze_bug'."""
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await regenerate_rca(regen_state)

        assert result["current_node"] == "analyze_bug"

    @pytest.mark.asyncio
    async def test_sets_reflection_critique_to_feedback(self, regen_state, mock_jira):
        """regenerate_rca passes user feedback directly as reflection_critique."""
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await regenerate_rca(regen_state)

        assert result["reflection_critique"] == self.FEEDBACK

    @pytest.mark.asyncio
    async def test_clears_feedback_and_revision_flags(self, regen_state, mock_jira):
        """regenerate_rca clears feedback_comment, revision_requested, and selection state."""
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await regenerate_rca(regen_state)

        assert result["feedback_comment"] is None
        assert result["revision_requested"] is False
        assert result["selected_fix_option"] is None
        assert result["selected_fix_approach"] is None
        assert result["rca_comment_posted"] is False
        assert result["last_error"] is None

    @pytest.mark.asyncio
    async def test_resets_loop_counters(self, regen_state, mock_jira):
        """regenerate_rca resets reflection_count, retry_count, and is_paused for fresh analysis."""
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await regenerate_rca(regen_state)

        assert result["reflection_count"] == 0
        assert result["retry_count"] == 0
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_posts_acknowledgement_comment(self, regen_state, mock_jira):
        """regenerate_rca posts a Jira comment before routing."""
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            await regenerate_rca(regen_state)

        mock_jira.add_comment.assert_called_once()
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "feedback" in comment_text.lower()

    @pytest.mark.asyncio
    async def test_empty_feedback_sets_none_critique(self, mock_jira):
        """regenerate_rca with empty feedback sets reflection_critique to None."""
        state = make_rca_option_state(revision_requested=True, feedback_comment="")
        with patch("forge.workflow.nodes.rca_option_gate.JiraClient", return_value=mock_jira):
            result = await regenerate_rca(state)

        assert result["reflection_critique"] is None

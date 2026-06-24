"""Tests for comment classification functionality."""

from forge.workflow.utils import CommentType, classify_comment


class TestClassifyComment:
    """Test cases for the classify_comment function."""

    # Question detection tests
    def test_question_with_question_mark_prefix(self) -> None:
        """Comments starting with ? should be classified as questions."""
        assert classify_comment("?Why REST?") == CommentType.QUESTION

    def test_question_with_question_mark_and_space(self) -> None:
        """Question mark followed by space should be a question."""
        assert classify_comment("? What is the reason for this?") == CommentType.QUESTION

    def test_question_with_forge_ask_prefix(self) -> None:
        """Comments starting with @forge ask should be questions."""
        assert classify_comment("@forge ask explain this") == CommentType.QUESTION

    def test_question_with_forge_ask_case_insensitive(self) -> None:
        """@forge ask should be case insensitive."""
        assert classify_comment("@Forge Ask why") == CommentType.QUESTION
        assert classify_comment("@FORGE ASK details") == CommentType.QUESTION
        assert classify_comment("@Forge ask more info") == CommentType.QUESTION

    def test_question_with_forge_ask_no_trailing_text(self) -> None:
        """@forge ask with minimal content."""
        assert classify_comment("@forge ask") == CommentType.QUESTION

    def test_question_mark_with_leading_whitespace(self) -> None:
        """Question mark with leading whitespace should be a question."""
        assert classify_comment("  ?Why REST?") == CommentType.QUESTION

    def test_forge_ask_with_leading_whitespace(self) -> None:
        """@forge ask with leading whitespace should be a question."""
        assert classify_comment("  @forge ask explain") == CommentType.QUESTION

    # Revision (feedback) tests — requires ! prefix
    def test_revision_with_exclamation_prefix(self) -> None:
        """Comments starting with ! should be classified as feedback."""
        assert classify_comment("!Please add more detail") == CommentType.FEEDBACK
        assert classify_comment("!Fix the error handling section") == CommentType.FEEDBACK

    def test_revision_with_exclamation_and_space(self) -> None:
        """Exclamation with space should be feedback."""
        assert classify_comment("! Add the missing auth section") == CommentType.FEEDBACK

    def test_revision_with_leading_whitespace(self) -> None:
        """Exclamation with leading whitespace should be feedback."""
        assert classify_comment("  !Revise this") == CommentType.FEEDBACK

    def test_revision_exclamation_only(self) -> None:
        """Bare exclamation mark should be feedback."""
        assert classify_comment("!") == CommentType.FEEDBACK

    # Informational tests (default — no prefix)
    def test_plain_text_is_informational(self) -> None:
        """Comments without a prefix should be informational."""
        assert classify_comment("Please add more detail") == CommentType.INFORMATIONAL
        assert classify_comment("Can you expand on this section") == CommentType.INFORMATIONAL

    def test_approval_words_are_informational(self) -> None:
        """Approval keywords are informational — approvals use label changes only."""
        assert classify_comment("Approved") == CommentType.INFORMATIONAL
        assert classify_comment("LGTM") == CommentType.INFORMATIONAL
        assert classify_comment("looks good to me") == CommentType.INFORMATIONAL
        assert classify_comment("looks good") == CommentType.INFORMATIONAL

    def test_question_mark_in_middle_is_informational(self) -> None:
        """Question mark not at the start should be informational."""
        assert classify_comment("What about this? Add more") == CommentType.INFORMATIONAL
        assert classify_comment("Is this correct? Please check") == CommentType.INFORMATIONAL

    def test_exclamation_in_middle_is_informational(self) -> None:
        """Exclamation not at the start should be informational."""
        assert classify_comment("Great work! Thanks") == CommentType.INFORMATIONAL
        assert classify_comment("This is awesome!") == CommentType.INFORMATIONAL

    def test_empty_comment_is_informational(self) -> None:
        """Empty comments should be informational."""
        assert classify_comment("") == CommentType.INFORMATIONAL

    def test_whitespace_only_comment_is_informational(self) -> None:
        """Whitespace-only comments should be informational."""
        assert classify_comment("   ") == CommentType.INFORMATIONAL
        assert classify_comment("\n\t") == CommentType.INFORMATIONAL

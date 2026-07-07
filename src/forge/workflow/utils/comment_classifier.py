"""Comment classification for Forge Q&A mode."""

import re
from enum import StrEnum


class CommentType(StrEnum):
    """Type of comment detected in Jira comments."""

    QUESTION = "question"
    FEEDBACK = "feedback"
    INFORMATIONAL = "informational"


# Legacy @forge ask pattern (case insensitive).
_FORGE_ASK_PATTERN = re.compile(r"^\s*@forge\s+ask", re.IGNORECASE)

# Pattern for question mark at start (allowing leading whitespace)
_QUESTION_MARK_PATTERN = re.compile(r"^\s*\?")

# Pattern for revision prefix (allowing leading whitespace)
_REVISION_PATTERN = re.compile(r"^\s*!")


def classify_comment(comment_text: str) -> CommentType:
    """Classify a comment into question, feedback, or informational.

    Classification rules:
    - Questions: Comments starting with '?'
    - Feedback (revision request): Comments starting with '!'
    - Informational: Everything else — ignored by the workflow

    Approvals are handled exclusively via label changes (forge:*-approved),
    not via comment text.

    Args:
        comment_text: The text of the comment to classify.

    Returns:
        The classified comment type.
    """
    if not comment_text or not comment_text.strip():
        return CommentType.INFORMATIONAL

    if _QUESTION_MARK_PATTERN.match(comment_text):
        return CommentType.QUESTION

    if _FORGE_ASK_PATTERN.match(comment_text):
        return CommentType.QUESTION

    if _REVISION_PATTERN.match(comment_text):
        return CommentType.FEEDBACK

    return CommentType.INFORMATIONAL

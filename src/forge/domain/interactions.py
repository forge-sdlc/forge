"""Provider-neutral classification of human workflow interactions."""

import re
from enum import StrEnum


class CommentType(StrEnum):
    QUESTION = "question"
    FEEDBACK = "feedback"
    INFORMATIONAL = "informational"


_FORGE_ASK_PATTERN = re.compile(r"^\s*@forge\s+ask", re.IGNORECASE)
_QUESTION_MARK_PATTERN = re.compile(r"^\s*\?")
_REVISION_PATTERN = re.compile(r"^\s*!")


def classify_comment(comment_text: str) -> CommentType:
    if not comment_text or not comment_text.strip():
        return CommentType.INFORMATIONAL
    if _QUESTION_MARK_PATTERN.match(comment_text) or _FORGE_ASK_PATTERN.match(comment_text):
        return CommentType.QUESTION
    if _REVISION_PATTERN.match(comment_text):
        return CommentType.FEEDBACK
    return CommentType.INFORMATIONAL

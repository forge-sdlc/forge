"""Provider-neutral rendering used by workflow effects."""

import re

_EMOJI_PREFIX_RE = re.compile(r"^\s*(?:[\u2600-\u27BF\U0001F300-\U0001FAFF]|\u2139)")


def format_status_comment(message: str) -> str:
    """Ensure a workflow status comment starts with a matching emoji."""
    if _EMOJI_PREFIX_RE.match(message):
        return message
    normalized = message.lower()
    emoji = "ℹ️"
    if any(word in normalized for word in ("fail", "error", "conflict", "cannot", "missing")):
        emoji = "⚠️"
    elif any(word in normalized for word in ("complete", "success", "approved", "merged")):
        emoji = "✅"
    elif "prd" in normalized:
        emoji = "📝"
    elif "spec" in normalized or "specification" in normalized:
        emoji = "📋"
    elif "plan" in normalized:
        emoji = "🧭"
    elif "task" in normalized or "implement" in normalized:
        emoji = "⚙️"
    elif "pull request" in normalized or " pr " in f" {normalized} ":
        emoji = "🔀"
    elif " ci " in f" {normalized} ":
        emoji = "🧪"
    elif "review" in normalized:
        emoji = "👀"
    elif "question" in normalized or "q&a" in normalized:
        emoji = "❓"
    elif "triage" in normalized or "checking" in normalized:
        emoji = "🔎"
    elif "rca" in normalized or "root cause" in normalized or "analysis" in normalized:
        emoji = "🔍"
    return f"{emoji} {message}"

"""Compatibility import for the provider-neutral interaction classifier."""

import re
from typing import Any

from forge.domain.interactions import CommentType, classify_comment

_COMMAND = re.compile(r"^\s*/forge\s+([a-zA-Z0-9_-]+)", re.IGNORECASE)
_PAIR = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s\'"]+))')


def _parse_pairs(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    position = 0
    while position < len(text):
        match = _PAIR.match(text, position)
        if not match:
            raise ValueError(f"Malformed parameters or trailing junk near: '{text[position:]}'")
        result[match.group(1)] = next(value for value in match.groups()[1:] if value is not None)
        position = match.end()
    return result


def parse_comment_command(comment_text: str) -> dict[str, Any] | None:
    """Parse PR 242 draft-edit commands while classification stays provider-neutral."""
    match = _COMMAND.match(comment_text or "")
    if not match:
        return None
    command = match.group(1).lower()
    if command not in {"remove", "exclude", "add", "update", "approve"}:
        return None
    arguments = comment_text[match.end() :].strip()
    if command == "approve":
        return (
            {"command": command}
            if not arguments
            else {
                "command": command,
                "error": "The approve command does not accept parameters",
            }
        )
    if command in {"remove", "exclude"}:
        if arguments.isdigit():
            return {"command": command, "id": int(arguments)}
        return {"command": command, "error": f"Invalid integer ID for {command} command"}
    if command == "add":
        if not arguments:
            return {"command": command, "error": "The add command requires parameters"}
        try:
            return {"command": command, "params": _parse_pairs(arguments)}
        except ValueError as exc:
            return {"command": command, "error": str(exc)}
    identifier, _, pairs = arguments.partition(" ")
    if not identifier.isdigit():
        return {"command": command, "error": "Invalid integer ID for update command"}
    try:
        return {"command": command, "id": int(identifier), "params": _parse_pairs(pairs)}
    except ValueError as exc:
        return {"command": command, "error": str(exc)}


__all__ = ["CommentType", "classify_comment", "parse_comment_command"]

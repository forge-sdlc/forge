"""Prompt templates for Forge SDLC agent."""

import re
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent

# Default version, can be overridden via config
_default_version: str = "v1"


def set_default_version(version: str) -> None:
    """Set the default prompt version.

    Args:
        version: Version string (e.g., 'v1', 'v2').
    """
    global _default_version
    _default_version = version


def get_default_version() -> str:
    """Get the current default prompt version."""
    return _default_version


def load_prompt(
    name: str, version: str | None = None, *, strict: bool = False, **kwargs: Any
) -> str:
    """Load a prompt template and format with variables.

    Args:
        name: Prompt file name (without .md extension).
        version: Prompt version (e.g., 'v1', 'v2'). Uses default if not specified.
        strict: Reject missing named placeholders before rendering.
        **kwargs: Variables to substitute in the template.

    Returns:
        Formatted prompt string.

    Raises:
        FileNotFoundError: If prompt template not found.
    """
    ver = version or _default_version
    prompt_file = PROMPTS_DIR / ver / f"{name}.md"

    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt template not found: {prompt_file}")

    template = prompt_file.read_text()

    # Render only placeholders in the template, never text inserted by another
    # substitution. JSON braces and placeholder-like user data remain literal.
    pattern = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z_0-9]*)\}(?!\})")
    missing = set(pattern.findall(template)) - kwargs.keys()
    if strict and missing:
        raise ValueError(f"Missing inputs for {name}: {', '.join(sorted(missing))}")
    return pattern.sub(
        lambda match: str(kwargs[match[1]]) if match[1] in kwargs else match[0], template
    )


def list_versions() -> list[str]:
    """List available prompt versions.

    Returns:
        List of version directory names.
    """
    return sorted(
        [d.name for d in PROMPTS_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]
    )


def list_prompts(version: str | None = None) -> list[str]:
    """List available prompts for a version.

    Args:
        version: Prompt version. Uses default if not specified.

    Returns:
        List of prompt names (without .md extension).
    """
    ver = version or _default_version
    version_dir = PROMPTS_DIR / ver

    if not version_dir.exists():
        return []

    return sorted([f.stem for f in version_dir.glob("*.md")])


__all__ = [
    "load_prompt",
    "set_default_version",
    "get_default_version",
    "list_versions",
    "list_prompts",
    "PROMPTS_DIR",
]

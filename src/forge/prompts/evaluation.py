"""Render offline fixtures using the production prompt templates."""

from typing import Any

from forge.prompts import load_prompt


def build_evaluation_prompt(
    skill_name: str,
    requirements: str,
    project_key: str,
    summary: str,
    *,
    prompt_inputs: dict[str, Any] | None = None,
    available_repos: list[str] | None = None,
) -> str:
    """Supply stage-specific primary inputs and reject incomplete fixtures."""
    values: dict[str, Any] = {
        "project_key": project_key,
        "context": {
            "project_key": project_key,
            "summary": summary,
            "available_repos": available_repos or [],
        },
    }
    if skill_name == "generate-prd":
        values["raw_requirements"] = requirements
    elif skill_name == "generate-spec":
        values["prd_content"] = requirements
    elif skill_name == "decompose-epics":
        values.update(
            spec_content=requirements,
            feature_summary=summary,
            repo_instruction="Available repositories:\n" + "\n".join(available_repos or []),
        )
    elif skill_name == "generate-tasks":
        values.update(
            epic_plan=requirements,
            epic_summary=summary,
            sibling_epics_section="None",
            existing_tasks_section="None",
        )
    values.update(prompt_inputs or {})
    if (
        skill_name in {"generate-prd", "generate-spec", "decompose-epics", "generate-tasks"}
        and not available_repos
    ):
        raise ValueError("available_repos must list the fixture's exact owner/repository names")
    try:
        return load_prompt(skill_name, strict=True, **values)
    except FileNotFoundError:
        return f"Use the {skill_name} skill to complete the following task:\n\n{requirements}"

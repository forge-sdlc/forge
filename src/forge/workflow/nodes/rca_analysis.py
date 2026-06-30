"""RCA analysis nodes: analyze_bug and reflect_rca."""

import json
import logging
import tempfile
from pathlib import Path

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient, MissingProjectConfig
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.bug.state import BugState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment

logger = logging.getLogger(__name__)

_RCA_REQUIRED_KEYS = {
    "summary",
    "code_location",
    "mechanism",
    "trigger_to_symptom",
    "hypothesis_log",
    "introduced_in",
    "confidence",
    "options",
    "reproducibility",
}
_OPTION_REQUIRED_KEYS = {"title", "description", "tradeoffs"}

MAX_ANALYSIS_RETRIES = 3
MAX_REFLECTION_ITERATIONS = 3


async def analyze_bug(state: BugState) -> BugState:
    """Run hypothesis-driven codebase analysis and write rca.json.

    Spawns a container that clones the relevant repo(s) and writes
    .forge/rca.json. Parses the artifact into rca_options and rca_content.
    Passes reflection_critique if a previous reflect_rca pass returned a critique.

    Returns:
        Updated state with rca_options and rca_content populated,
        current_node="reflect_rca". On repeated failure: current_node="escalate_blocked".
    """
    ticket_key = state["ticket_key"]
    retry_count = state.get("retry_count", 0)
    reflection_critique = state.get("reflection_critique") or ""

    settings = get_settings()
    jira = JiraClient()

    try:
        await post_status_comment(
            jira,
            ticket_key,
            "🔍 Forge is analyzing the bug root cause — this may take a few minutes.",
        )

        issue = await jira.get_issue(ticket_key)

        try:
            repos = await jira.get_project_repos(issue.project_key)
        except MissingProjectConfig as e:
            await jira.add_comment(
                ticket_key,
                f"Cannot start RCA: repository configuration is missing for project "
                f"`{issue.project_key}`.\n\n"
                f"Set `forge.repos` on the Jira project to a comma-separated list of "
                f"`owner/repo` values, then add `forge:retry` to resume.\n\n"
                f"Details: {e}",
            )
            await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
            return {
                **state,
                "last_error": str(e),
                "current_node": "analyze_bug",
            }

        task_description = load_prompt(
            "analyze-bug",
            ticket_key=ticket_key,
            bug_summary=issue.summary or "",
            bug_description=issue.description or "",
            known_repos="\n".join(repos),
            reflection_critique=reflection_critique,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            runner = ContainerRunner(settings)
            result = await runner.run(
                workspace_path=workspace_path,
                task_summary=f"RCA analysis for {ticket_key}",
                task_description=task_description,
                ticket_key=ticket_key,
                task_key=f"{ticket_key}-analysis",
            )

            if not result.success:
                raise RuntimeError(
                    f"Container failed with exit_code={result.exit_code}: {result.stderr}"
                )

            data = _harvest_rca_json(workspace_path)

        return update_state_timestamp(
            {
                **state,
                "rca_options": data["options"],
                "rca_content": _format_rca_content(data),
                "reproducibility_assessment": _format_reproducibility(data),
                "current_node": "reflect_rca",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"analyze_bug failed for {ticket_key}: {e}")
        new_retry = retry_count + 1
        next_node = "escalate_blocked" if new_retry >= MAX_ANALYSIS_RETRIES else "analyze_bug"
        return {
            **state,
            "last_error": str(e),
            "current_node": next_node,
            "retry_count": new_retry,
        }

    finally:
        await jira.close()


def _harvest_rca_json(workspace_path: Path) -> dict:
    """Read and parse .forge/rca.json from the container workspace.

    Raises:
        FileNotFoundError: if rca.json was not written.
        ValueError: if required top-level or option keys are absent.
    """
    rca_file = workspace_path / ".forge" / "rca.json"
    if not rca_file.exists():
        raise FileNotFoundError(f"rca.json not found at {rca_file}")

    data = json.loads(rca_file.read_text())

    missing_keys = _RCA_REQUIRED_KEYS - set(data.keys())
    if missing_keys:
        raise ValueError(f"rca.json missing required keys: {missing_keys}")

    options = data.get("options", [])
    if not isinstance(options, list) or not (1 <= len(options) <= 4):
        raise ValueError(
            f"rca.json options must be a list of 1–4 items, got "
            f"{len(options) if isinstance(options, list) else type(options)}"
        )

    for i, opt in enumerate(options):
        missing_opt_keys = _OPTION_REQUIRED_KEYS - set(opt.keys())
        if missing_opt_keys:
            raise ValueError(f"option[{i}] missing required keys: {missing_opt_keys}")

    return data


def _format_rca_content(data: dict) -> str:
    """Build the human-readable rca_content string from the parsed rca.json."""
    loc = data.get("code_location", {})
    location_str = (
        f"{loc.get('file', '?')}:{loc.get('function', '?')} (lines {loc.get('line_range', '?')})"
    )

    introduced = data.get("introduced_in", {})
    intro_str = f"commit {introduced.get('commit', '?')} ({introduced.get('date', 'unknown date')})"
    if introduced.get("pr"):
        intro_str += f", PR {introduced['pr']}"

    confidence = data.get("confidence", {})
    conf_str = (
        f"{confidence.get('level', '?')} ({confidence.get('percentage', '?')}%) "
        f"— {confidence.get('rationale', '')}"
    )

    return (
        f"## Summary\n{data.get('summary', '')}\n\n"
        f"## Code Location\n{location_str}\n\n"
        f"## Mechanism\n{data.get('mechanism', '')}\n\n"
        f"## Trigger to Symptom\n{data.get('trigger_to_symptom', '')}\n\n"
        f"## Introduced In\n{intro_str}\n\n"
        f"## Confidence\n{conf_str}"
    )


def _format_reproducibility(data: dict) -> str:
    """Build the reproducibility_assessment string."""
    repro = data.get("reproducibility", {})
    feasible = repro.get("feasible", False)
    lines = [f"Reproducible: {'Yes' if feasible else 'No'}"]
    if repro.get("conditions"):
        lines.append(f"Conditions: {repro['conditions']}")
    if repro.get("test_source"):
        lines.append(f"\n```python\n{repro['test_source']}\n```")
    return "\n".join(lines)


async def reflect_rca(state: BugState) -> BugState:
    """Validate the RCA from analyze_bug for completeness and evidence quality.

    Runs a container using reflect-rca.md. Container returns either "VALID"
    or a structured critique string.

    On VALID: routes to rca_option_gate.
    On critique with reflection_count < 3: stores critique, routes back to analyze_bug.
    On critique with reflection_count >= 3: posts warning comment, routes to rca_option_gate.

    Returns:
        Updated state with current_node set appropriately.
    """
    ticket_key = state["ticket_key"]
    rca_content = state.get("rca_content") or ""
    rca_options = state.get("rca_options") or []
    reflection_count = state.get("reflection_count", 0)
    reflect_rca_retry_count = state.get("reflect_rca_retry_count", 0)

    settings = get_settings()
    jira = JiraClient()

    try:
        task_description = load_prompt(
            "reflect-rca",
            rca_content=rca_content,
            rca_options_json=json.dumps(rca_options, indent=2),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            runner = ContainerRunner(settings)
            task_key = f"{ticket_key}-reflect"
            result = await runner.run(
                workspace_path=workspace_path,
                task_summary=f"RCA reflection for {ticket_key}",
                task_description=task_description,
                ticket_key=ticket_key,
                task_key=task_key,
            )

            if not result.success:
                raise RuntimeError(
                    f"Reflection container failed with exit_code={result.exit_code}: {result.stderr}"
                )

            verdict = _extract_reflection_verdict(workspace_path, task_key, result.stdout)

        if verdict.upper().strip() == "VALID":
            return update_state_timestamp(
                {
                    **state,
                    "reflection_critique": None,
                    "current_node": "rca_option_gate",
                }
            )

        new_reflection_count = reflection_count + 1

        if new_reflection_count >= MAX_REFLECTION_ITERATIONS:
            await jira.add_comment(
                ticket_key,
                f"Reflection cap reached — proceeding with best available RCA after "
                f"{new_reflection_count} validation attempts.",
            )
            return update_state_timestamp(
                {
                    **state,
                    "reflection_critique": verdict,
                    "reflection_count": new_reflection_count,
                    "current_node": "rca_option_gate",
                }
            )

        return update_state_timestamp(
            {
                **state,
                "reflection_critique": verdict,
                "reflection_count": new_reflection_count,
                "current_node": "analyze_bug",
            }
        )

    except Exception as e:
        logger.error(f"reflect_rca failed for {ticket_key}: {e}")
        new_reflect_retry = reflect_rca_retry_count + 1
        next_node = (
            "escalate_blocked" if new_reflect_retry >= MAX_ANALYSIS_RETRIES else "reflect_rca"
        )
        return {
            **state,
            "last_error": str(e),
            "current_node": next_node,
            "reflect_rca_retry_count": new_reflect_retry,
        }

    finally:
        await jira.close()


def _extract_reflection_verdict(workspace_path: Path, task_key: str, stdout: str) -> str:
    """Read the reflector's final assistant message, falling back to stdout.

    Container stdout contains entrypoint logs. The entrypoint saves the actual
    Deep Agents conversation to ``.forge/history/{task_key}.json``; for RCA
    reflection that final assistant message is the verdict we care about.
    """
    history_file = workspace_path / ".forge" / "history" / f"{task_key}.json"
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
            messages = history.get("messages", [])
            for message in reversed(messages):
                role = str(message.get("role", "")).lower()
                if role not in {"ai", "assistant"}:
                    continue
                content = _stringify_message_content(message.get("content", ""))
                if content.strip():
                    return content.strip()
        except Exception as e:
            logger.warning(f"Could not read reflection history {history_file}: {e}")

    return (stdout or "").strip()


def _stringify_message_content(content: object) -> str:
    """Convert LangChain message content variants into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)

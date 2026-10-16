#!/usr/bin/env python3
"""Forge container entrypoint for AI-powered code implementation.

This script runs inside the development sandbox container and:
1. Reads task details from environment or mounted config
2. Loads repository guardrails (constitution.md/agents.md)
3. Runs Deep Agents with full tool access to implement the task
4. Runs local tests to validate implementation
5. Creates git commit with changes
6. Exits with status code indicating success/failure

The orchestrator (outside container) handles git push and PR creation.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from review import (
    ReviewCycleData,
    Verdict,
    detect_review_md,
    parse_review_config,
    parse_verdict,
    write_cycle_file,
)

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def command_timeout(default: int) -> int:
    """Return the configured per-command timeout, or the caller's default."""
    raw_value = os.environ.get("CONTAINER_COMMAND_TIMEOUT")
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning(
            "Invalid CONTAINER_COMMAND_TIMEOUT=%r; using %d seconds",
            raw_value,
            default,
        )
        return default


def resolve_llm_backend() -> str:
    """Read the required model backend."""
    backend = os.environ.get("LLM_BACKEND")
    if not backend:
        raise ValueError("LLM_BACKEND is required")
    return backend


def resolve_llm_model() -> str:
    """Read the required model name."""
    model = os.environ.get("LLM_MODEL")
    if not model:
        raise ValueError("LLM_MODEL is required")
    return model


# Enable LangChain debug/verbose mode if requested
if os.environ.get("LANGCHAIN_VERBOSE", "").lower() in ("true", "1", "yes"):
    try:
        from langchain_core.globals import set_debug, set_verbose

        set_verbose(True)
        set_debug(True)
        logger.info("LangChain verbose/debug mode enabled")
    except ImportError:
        pass

# Exit codes
EXIT_SUCCESS = 0
EXIT_TASK_FAILED = 1
EXIT_TESTS_FAILED = 2
EXIT_CONFIG_ERROR = 3

# Context7 MCP configuration for library documentation lookup
CONTEXT7_MCP_CONFIG = {
    "context7": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "env": {
            # Suppress npm update notices — they print to stdout and corrupt the
            # MCP stdio JSON protocol before the server has a chance to start.
            "NO_UPDATE_NOTIFIER": "1",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        },
    }
}


async def load_context7_tools() -> list[Any]:
    """Load Context7 MCP tools for library documentation lookup.

    Returns:
        List of MCP tools, or empty list if loading fails.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        logger.info("Loading Context7 MCP tools...")
        client = MultiServerMCPClient(CONTEXT7_MCP_CONFIG)
        tools = await client.get_tools()
        logger.info(f"Loaded {len(tools)} Context7 tools")
        return tools
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed, Context7 unavailable")
        return []
    except Exception as e:
        logger.warning(f"Failed to load Context7 MCP: {e}")
        return []


def load_guardrails(workspace: Path) -> str:
    """Load repository guardrails from constitution.md or agents.md."""
    guardrails = ""

    for filename in ["CLAUDE.md", "AGENTS.md", "constitution.md", "agents.md"]:
        filepath = workspace / filename
        if filepath.exists():
            logger.info(f"Loading guardrails from {filename}")
            guardrails += f"\n\n# {filename}\n"
            guardrails += filepath.read_text()

    if not guardrails:
        logger.warning("No guardrails file found in workspace")

    return guardrails


def detect_test_command(workspace: Path) -> str | None:
    """Detect the appropriate test command for the repository."""
    # Check for common test configurations
    checks = [
        # Python
        (workspace / "pyproject.toml", "pytest"),
        (workspace / "setup.py", "pytest"),
        (workspace / "pytest.ini", "pytest"),
        # Go
        (workspace / "go.mod", "go test ./..."),
        # Node.js
        (workspace / "package.json", "npm test"),
        # Rust
        (workspace / "Cargo.toml", "cargo test"),
        # Make
        (workspace / "Makefile", "make test"),
    ]

    for marker_file, command in checks:
        if marker_file.exists():
            # For package.json, verify test script exists
            if marker_file.name == "package.json":
                try:
                    pkg = json.loads(marker_file.read_text())
                    if "test" not in pkg.get("scripts", {}):
                        continue
                except (json.JSONDecodeError, KeyError):
                    continue

            # For Makefile, verify test target exists
            if marker_file.name == "Makefile":
                content = marker_file.read_text()
                if "test:" not in content and "test :" not in content:
                    continue

            logger.info(f"Detected test command: {command}")
            return command

    logger.warning("No test command detected")
    return None


def run_tests(workspace: Path, test_command: str) -> bool:
    """Run tests and return True if they pass."""
    logger.info(f"Running tests: {test_command}")

    timeout = command_timeout(600)
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode == 0:
            logger.info("Tests passed")
            return True
        else:
            logger.error(f"Tests failed with exit code {result.returncode}")
            logger.error(f"stdout: {result.stdout[:2000]}")
            logger.error(f"stderr: {result.stderr[:2000]}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("Tests timed out after %d seconds", timeout)
        return False
    except Exception as e:
        logger.error(f"Error running tests: {e}")
        return False


def configure_git() -> None:
    """Configure git user name and email from environment variables."""
    user_name = os.environ.get("GIT_USER_NAME", "Forge")
    user_email = os.environ.get("GIT_USER_EMAIL", "forge@example.com")

    subprocess.run(
        ["git", "config", "--global", "user.name", user_name],
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", user_email],
        capture_output=True,
    )
    logger.info(f"Configured git as {user_name} <{user_email}>")


def git_commit(workspace: Path, message: str) -> bool:
    """Stage all changes and create a commit."""
    try:
        # Keep Forge handoff/history files local even if ignore setup is missing
        # or an earlier run accidentally staged them.
        subprocess.run(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", ".forge"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )

        # Stage tracked file changes (modifications + deletions)
        result = subprocess.run(
            ["git", "add", "-u"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Git add -u failed: {result.stderr}")
            return False

        # Stage new untracked files (respects .gitignore, explicitly skips .forge/)
        # Use -z for null-byte separators — safe for non-ASCII and special characters
        ls_result = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=workspace,
            capture_output=True,
        )
        if ls_result.returncode != 0:
            logger.error(f"git ls-files failed: {ls_result.stderr}")
            return False
        else:
            new_files = [
                f
                for f in ls_result.stdout.split(b"\0")
                if f and not f.startswith(b".forge/") and f != b".forge"
            ]
        if new_files:
            result = subprocess.run(
                ["git", "add", "--"]
                + [f.decode("utf-8", errors="surrogateescape") for f in new_files],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(f"Git add new files failed: {result.stderr}")
                return False

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
        )

        if result.returncode == 0:
            logger.info("No changes to commit")
            return True

        # Create commit
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Git commit failed: {result.stderr}")
            return False

        logger.info(f"Created commit: {message}")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Git operation failed: {e}")
        if hasattr(e, "stderr") and e.stderr:
            logger.error(f"stderr: {e.stderr}")
        return False


def build_system_prompt(
    workspace: Path,
    task_key: str,
    task_summary: str,
    task_description: str,
    guardrails: str,
    previous_task_keys: list[str] | None = None,
) -> str:
    """Build the system prompt from template.

    Loads prompt template from FORGE_SYSTEM_PROMPT_TEMPLATE env var
    and interpolates task-specific values.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key being implemented.
        task_summary: Short task summary.
        task_description: Detailed task description.
        guardrails: Repository guidelines.
        previous_task_keys: List of previously implemented task keys for handoff context.

    Raises:
        ValueError: If FORGE_SYSTEM_PROMPT_TEMPLATE env var is not set.
    """
    template = os.environ.get("FORGE_SYSTEM_PROMPT_TEMPLATE")

    if not template:
        raise ValueError(
            "FORGE_SYSTEM_PROMPT_TEMPLATE environment variable is not set. "
            "The orchestrator must pass the system prompt template to the container."
        )

    # Format previous task keys for display
    prev_keys_str = (
        ", ".join(previous_task_keys) if previous_task_keys else "(none - this is the first task)"
    )

    # Interpolate template variables
    return template.format(
        workspace_path=str(workspace),
        task_key=task_key,
        task_summary=task_summary,
        task_description=task_description,
        guardrails=guardrails if guardrails else "No specific guidelines provided.",
        previous_task_keys=prev_keys_str,
        command_timeout_seconds=command_timeout(600),
    )


def resolve_container_trace_fields(trace_state: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Resolve Langfuse tags/metadata from container env without full app settings."""
    try:
        from forge.integrations.langfuse.fields import (
            parse_trace_fields,
            resolve_field,
        )
    except Exception as e:
        logger.debug(f"Trace field resolution unavailable: {e}")
        return [], {}

    tags: list[str] = []
    for field in parse_trace_fields(os.environ.get("LANGFUSE_TRACE_TAGS", ""), allow_tags=True):
        value = resolve_field(field, trace_state)
        if value:
            tags.append(value)

    metadata: dict[str, Any] = {}
    for field in parse_trace_fields(
        os.environ.get("LANGFUSE_TRACE_METADATA", ""),
        allow_tags=False,
    ):
        value = resolve_field(field, trace_state)
        if value is not None:
            metadata[field.value] = value

    return tags, metadata


def _create_llm_model(max_tokens_default: int = 16384):
    """Create the LLM model from environment configuration.

    Args:
        max_tokens_default: Default max tokens if LLM_MAX_TOKENS is not set.

    Returns:
        Tuple of (model_name, model) where model is a LangChain chat model.

    Raises:
        RuntimeError: If credentials are missing or Gemini is used without Vertex AI.
    """
    backend_name = resolve_llm_backend()
    model_name = resolve_llm_model()
    is_gemini = model_name.lower().startswith(("gemini", "models/gemini"))
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", str(max_tokens_default)))
    temperature_raw = os.environ.get("LLM_TEMPERATURE")
    tuning = {"temperature": float(temperature_raw)} if temperature_raw is not None else {}

    if backend_name == "vertex-ai":
        vertex_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not vertex_project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for the vertex-ai backend")
        vertex_region = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        if is_gemini:
            from langchain_google_genai import ChatGoogleGenerativeAI

            logger.info(
                f"Using Vertex AI Gemini model: {model_name}, max_output_tokens={max_tokens}"
            )
            model = ChatGoogleGenerativeAI(
                model=model_name,
                project=vertex_project,
                location=vertex_region,
                vertexai=True,
                max_output_tokens=max_tokens,
                **tuning,
            )
        else:
            from langchain_google_vertexai.model_garden import ChatAnthropicVertex

            logger.info(f"Using Vertex AI Anthropic model: {model_name}, max_tokens={max_tokens}")
            model = ChatAnthropicVertex(
                model_name=model_name,
                project=vertex_project,
                location=vertex_region,
                max_tokens=max_tokens,
                **tuning,
            )
    elif backend_name == "google-genai":
        if not is_gemini:
            raise RuntimeError(f"Model '{model_name}' is not supported by google-genai")
        google_api_key = os.environ.get("GOOGLE_API_KEY")
        if not google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for the google-genai backend")

        from langchain_google_genai import ChatGoogleGenerativeAI

        logger.info(f"Using Gemini API model: {model_name}, max_output_tokens={max_tokens}")
        model = ChatGoogleGenerativeAI(
            model=model_name,
            api_key=google_api_key,
            max_output_tokens=max_tokens,
            **tuning,
        )
    elif backend_name == "anthropic":
        if is_gemini:
            raise RuntimeError(f"Model '{model_name}' is not supported by anthropic")
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for the anthropic backend")
        from langchain_anthropic import ChatAnthropic

        logger.info(f"Using direct Anthropic model: {model_name}, max_tokens={max_tokens}")
        model = ChatAnthropic(
            model=model_name,
            api_key=anthropic_api_key,
            max_tokens=max_tokens,
            **tuning,
        )
    elif backend_name == "openai-compatible":
        base_url = os.environ.get("OPENAI_BASE_URL")
        if not base_url:
            raise RuntimeError("OPENAI_BASE_URL is required for the openai-compatible backend")
        from langchain_openai import ChatOpenAI

        logger.info("Using OpenAI-compatible model %s at %s", model_name, base_url)
        model = ChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=os.environ.get("OPENAI_API_KEY") or "not-required",
            max_tokens=max_tokens,
            **tuning,
        )
    else:
        raise RuntimeError(f"Unsupported LLM_BACKEND: {backend_name}")

    return model_name, model


def _discover_skill_paths(workspace: Path) -> list[str]:
    """Parse AGENT_SKILL_PATHS env var and auto-discover workspace skill dirs.

    Parses comma-separated paths from AGENT_SKILL_PATHS, ensures trailing
    slashes, and checks common workspace locations (.claude/skills,
    .agents/skills).

    Args:
        workspace: Path to the workspace directory.

    Returns:
        List of skill directory paths with trailing slashes.
    """
    skill_paths: list[str] = []
    skill_paths_env = os.environ.get("AGENT_SKILL_PATHS", "")
    if skill_paths_env:
        for path in skill_paths_env.split(","):
            path = path.strip()
            if path:
                # Ensure trailing slash for directory paths
                if not path.endswith("/"):
                    path = f"{path}/"
                skill_paths.append(path)

    # Auto-discover skill directories in the workspace
    # Check common locations for project-specific skills
    workspace_skill_dirs = [
        workspace / ".claude" / "skills",
        workspace / ".agents" / "skills",
    ]
    for skill_dir in workspace_skill_dirs:
        if skill_dir.is_dir():
            skill_path = f"{skill_dir}/"
            if skill_path not in skill_paths:
                skill_paths.append(skill_path)
                logger.info(f"Auto-discovered workspace skills: {skill_dir}")

    if skill_paths:
        logger.info(f"Agent skills: {skill_paths}")

    return skill_paths


def _setup_langfuse_tracing(task_key: str, trace_state: dict) -> tuple[dict, bool]:
    """Set up Langfuse tracing if credentials are available.

    Imports CallbackHandler, creates handler, and prepares the config dict
    with callbacks.  The caller must use ``propagate_attributes`` to wrap
    the agent invocation when ``langfuse_enabled`` is True.

    Args:
        task_key: Jira task key for log context.
        trace_state: Workflow fields forwarded to Langfuse (passed through
            for the caller to use with ``propagate_attributes``).

    Returns:
        Tuple of (config dict with callbacks, langfuse_enabled flag).
    """
    _ = trace_state  # consumed by caller for propagate_attributes
    config: dict = {}
    langfuse_enabled = False
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        try:
            from langfuse.langchain import CallbackHandler

            handler = CallbackHandler()
            config["callbacks"] = [handler]
            langfuse_enabled = True
            logger.info(f"Langfuse tracing enabled for task {task_key}")
        except ImportError:
            logger.debug("Langfuse not installed, skipping tracing")
        except Exception as e:
            logger.warning(f"Failed to initialize Langfuse: {e}")

    return config, langfuse_enabled


def _save_conversation_history(
    workspace: Path, task_key: str, task_summary: str, result: dict
) -> None:
    """Save conversation messages to .forge/history/{task_key}.json.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key.
        task_summary: Short task summary.
        result: Agent result dict containing messages.
    """
    try:
        history_dir = workspace / ".forge" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_file = history_dir / f"{task_key}.json"

        # Extract messages from result and serialize
        messages = result.get("messages", [])
        history_data = {
            "task_key": task_key,
            "task_summary": task_summary,
            "messages": [
                {
                    "role": getattr(msg, "type", "unknown"),
                    "content": getattr(msg, "content", str(msg)),
                    "tool_calls": getattr(msg, "tool_calls", None),
                    "status": getattr(msg, "status", None),
                    "response_metadata": getattr(msg, "response_metadata", None),
                }
                for msg in messages
            ],
        }
        history_file.write_text(json.dumps(history_data, indent=2, default=str))
        logger.info(f"Saved conversation history to {history_file}")
    except Exception as e:
        logger.warning(f"Failed to save conversation history: {e}")


async def run_agent_task(
    workspace: Path,
    task_key: str,
    task_summary: str,
    task_description: str,
    guardrails: str,
    previous_task_keys: list[str] | None = None,
    trace_context: dict[str, Any] | None = None,
) -> bool:
    """Run Deep Agents to implement the task.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key being implemented.
        task_summary: Short task summary.
        task_description: Detailed task description.
        guardrails: Repository guidelines.
        previous_task_keys: List of previously implemented task keys for handoff context.
        trace_context: Workflow fields forwarded to Langfuse only.
    """
    logger.info(f"Implementing task: {task_summary}")

    try:
        from deepagents import create_deep_agent
        from deepagents.backends import LocalShellBackend

        model_name, model = _create_llm_model(max_tokens_default=16384)
        logger.info(f"Model: {model_name}")

        backend = LocalShellBackend(
            root_dir=str(workspace),
            inherit_env=True,
            virtual_mode=False,
            timeout=command_timeout(600),
        )

        # Build system prompt from template
        system_prompt = build_system_prompt(
            workspace, task_key, task_summary, task_description, guardrails, previous_task_keys
        )
        trace_state = {
            **(trace_context or {}),
            "system_prompt_length": len(system_prompt),
            "llm_model": model_name,
        }

        # Load Context7 MCP tools for library documentation
        mcp_tools = await load_context7_tools()

        # Discover skill paths from env and workspace
        skill_paths = _discover_skill_paths(workspace)

        # Create and run the agent.
        # Note: create_deep_agent already adds SummarizationMiddleware internally —
        # do not pass it again or deepagents raises a duplicate middleware error.
        agent = create_deep_agent(
            model=model,
            backend=backend,
            system_prompt=system_prompt,
            tools=mcp_tools if mcp_tools else None,
            skills=skill_paths if skill_paths else None,
        )

        # Set up Langfuse tracing
        config, langfuse_enabled = _setup_langfuse_tracing(task_key, trace_state)

        # Run the agent (with Langfuse session context if enabled)
        initial_message = {
            "messages": [{"role": "user", "content": f"Implement this task:\n\n{task_description}"}]
        }

        if langfuse_enabled:
            from langfuse import propagate_attributes

            trace_tags, trace_metadata = resolve_container_trace_fields(trace_state)
            tags = ["forge-container", "task-implementation", *trace_tags]
            metadata = {"task_summary": task_summary, **trace_metadata}
            with propagate_attributes(
                session_id=task_key,
                tags=tags,
                metadata=metadata,
            ):
                result = await agent.ainvoke(initial_message, config=config)
        else:
            result = await agent.ainvoke(initial_message, config=config)

        # Flush Langfuse traces before exit
        if langfuse_enabled:
            try:
                from langfuse import get_client

                get_client().flush()
            except Exception:
                pass

        # Save conversation history
        _save_conversation_history(workspace, task_key, task_summary, result)

        logger.info("Agent completed task execution")
        return True

    except ImportError as e:
        logger.error(f"Missing dependency: {e}")
        return False
    except Exception as e:
        logger.error(f"Agent execution failed: {e}")
        return False


async def run_reviewer_agent(
    workspace: Path,
    review_instructions: str,
    task_key: str,
    task_description: str = "",
    trace_context: dict[str, Any] | None = None,
    review_cycle: int | None = None,
) -> str:
    """Run reviewer agent with review.md instructions.

    Args:
        workspace: Path to the workspace directory.
        review_instructions: Instructions from review.md body.
        task_key: Jira task key for tracing.
        task_description: Implementation context, including repository scope.
        trace_context: Workflow fields forwarded to Langfuse only.
        review_cycle: Current review cycle, used as trace metadata.

    Returns:
        The reviewer agent's output text.
    """
    logger.info(f"Running reviewer agent for {task_key}")

    from deepagents import create_deep_agent
    from deepagents.backends import LocalShellBackend

    model_name, model = _create_llm_model(max_tokens_default=8192)
    logger.info(f"Model: {model_name}")

    backend = LocalShellBackend(
        root_dir=str(workspace),
        inherit_env=True,
        virtual_mode=False,
        timeout=command_timeout(600),
    )

    system_prompt = f"""You are a code reviewer agent. Your job is to review the implementation and provide a verdict.

## Review Instructions
{review_instructions}

## Task Context and Repository Scope
{task_description}

Evaluate completeness only within the repository scope stated above. Requirements assigned
to other repositories are not missing from this implementation; they are handled separately.

## Verdict Format
After reviewing the code, you MUST output your verdict as either:
- APPROVED - if the implementation meets all requirements
- REJECTED - followed by your feedback if the implementation needs changes

Example outputs:
- "The implementation looks good. APPROVED"
- "REJECTED: The function is missing error handling. Please add try/except blocks."

Be specific in your feedback if rejecting."""

    # Create reviewer agent (no skills needed for review)
    agent = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=system_prompt,
    )

    trace_state = {
        **(trace_context or {}),
        "system_prompt_length": len(system_prompt),
        "llm_model": model_name,
    }
    config, langfuse_enabled = _setup_langfuse_tracing(task_key, trace_state)
    if langfuse_enabled:
        config["run_name"] = f"review:{task_key}"

    # Run the reviewer
    initial_message = {
        "messages": [
            {
                "role": "user",
                "content": "Please review the implementation in the workspace and provide your verdict.",
            }
        ]
    }

    if langfuse_enabled:
        from langfuse import propagate_attributes

        trace_tags, trace_metadata = resolve_container_trace_fields(trace_state)
        tags = ["forge-container", "task-review", *trace_tags]
        metadata = {
            "review_cycle": review_cycle,
            "review_instructions_length": len(review_instructions),
            **trace_metadata,
        }
        with propagate_attributes(
            session_id=task_key,
            tags=tags,
            metadata=metadata,
        ):
            result = await agent.ainvoke(initial_message, config=config)
    else:
        result = await agent.ainvoke(initial_message, config=config)

    if langfuse_enabled:
        try:
            from langfuse import get_client

            get_client().flush()
        except Exception:
            pass

    # Extract output text from result
    messages = result.get("messages", [])
    if messages:
        last_message = messages[-1]
        content = getattr(last_message, "content", str(last_message))
        return content if isinstance(content, str) else str(content)
    return ""


async def run_worker_with_feedback(
    workspace: Path,
    task_key: str,
    task_summary: str,
    task_description: str,
    guardrails: str,
    feedback: str,
    previous_task_keys: list[str] | None = None,
    trace_context: dict[str, Any] | None = None,
) -> bool:
    """Re-run worker agent with reviewer feedback injected.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key being implemented.
        task_summary: Short task summary.
        task_description: Detailed task description.
        guardrails: Repository guidelines.
        feedback: Reviewer feedback to inject.
        previous_task_keys: List of previously implemented task keys.
        trace_context: Workflow fields forwarded to Langfuse only.

    Returns:
        True if agent completed successfully.
    """
    # Inject reviewer feedback section into task description
    feedback_section = f"\n\n## Reviewer Feedback\n\n{feedback}\n"
    enhanced_description = task_description + feedback_section

    logger.info(f"Re-running worker with feedback for {task_key}")

    return await run_agent_task(
        workspace=workspace,
        task_key=task_key,
        task_summary=task_summary,
        task_description=enhanced_description,
        guardrails=guardrails,
        previous_task_keys=previous_task_keys,
        trace_context=trace_context,
    )


async def run_review_loop(
    workspace: Path,
    task_key: str,
    task_summary: str,
    task_description: str,
    guardrails: str,
    skill_name: str,
    review_md_path: Path,
    previous_task_keys: list[str] | None = None,
    trace_context: dict[str, Any] | None = None,
) -> bool:
    """Run the review loop after initial skill execution.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key being implemented.
        task_summary: Short task summary.
        task_description: Detailed task description.
        guardrails: Repository guidelines.
        skill_name: Name of the skill being reviewed.
        review_md_path: Path to the review.md file.
        previous_task_keys: List of previously implemented task keys.
        trace_context: Workflow fields forwarded to Langfuse only.

    Returns:
        True if review loop completed successfully (approved or max retries).
    """
    # Parse review configuration (SC-004, SC-005)
    config = parse_review_config(review_md_path)
    max_retries = config.max_retries
    instructions = config.instructions

    logger.info(f"Starting review loop for {skill_name} (max_retries={max_retries})")
    logger.info(
        f"Review instructions: {instructions[:200]}..."
        if len(instructions) > 200
        else f"Review instructions: {instructions}"
    )

    if max_retries == 0:
        logger.info(f"Review disabled (max_retries=0) for {skill_name}")
        return True

    cycle = 0
    while cycle < max_retries:
        cycle += 1
        cycle_start = time.perf_counter()
        logger.info(f"Review cycle {cycle}/{max_retries}")

        # Run reviewer agent (SC-001)
        try:
            reviewer_output = await run_reviewer_agent(
                workspace=workspace,
                review_instructions=instructions,
                task_key=task_key,
                task_description=task_description,
                trace_context=trace_context,
                review_cycle=cycle,
            )
        except Exception as e:
            logger.error(f"Reviewer agent failed: {e}")
            # Treat reviewer failure as rejection
            reviewer_output = f"REJECTED: Reviewer agent error: {e}"

        # Parse verdict (SC-002)
        verdict, feedback = parse_verdict(reviewer_output)
        elapsed = time.perf_counter() - cycle_start
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(f"Review verdict: {verdict}, elapsed: {elapsed:.2f}s")
        if feedback:
            logger.info(
                f"Feedback: {feedback[:200]}..." if len(feedback) > 200 else f"Feedback: {feedback}"
            )

        # Write cycle file (SC-007)
        cycle_data = ReviewCycleData(
            cycle=cycle,
            max_cycles=max_retries,
            verdict=verdict,
            feedback=feedback,
            skill=skill_name,
            elapsed_seconds=elapsed,
            timestamp=timestamp,
        )
        write_cycle_file(workspace, task_key, skill_name, cycle_data)

        # On APPROVED: Exit loop successfully (SC-002)
        if verdict == Verdict.APPROVED:
            logger.info(f"Review approved on cycle {cycle}")
            return True

        # On REJECTED with retries remaining (SC-003)
        if cycle < max_retries:
            logger.info(f"Retrying with feedback (attempt {cycle + 1}/{max_retries})")
            worker_success = await run_worker_with_feedback(
                workspace=workspace,
                task_key=task_key,
                task_summary=task_summary,
                task_description=task_description,
                guardrails=guardrails,
                feedback=feedback,
                previous_task_keys=previous_task_keys,
                trace_context=trace_context,
            )
            if not worker_success:
                logger.error(f"Worker retry failed on cycle {cycle}")
                # Write a synthetic final cycle so the orchestrator sees
                # exhaustion and surfaces it in the PR body (BR-005).
                # Use cycle=max_retries so review_exhausted triggers and
                # the file number is unique (not overwriting the real cycle).
                write_cycle_file(
                    workspace,
                    task_key,
                    skill_name,
                    ReviewCycleData(
                        cycle=max_retries,
                        max_cycles=max_retries,
                        verdict=Verdict.REJECTED,
                        feedback=f"Worker retry failed after review rejection. Original feedback: {feedback}",
                        skill=skill_name,
                        elapsed_seconds=0.0,
                        timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
                break

    # Max retries exhausted - exit with success (BR-005)
    if max_retries > 0:
        logger.warning(f"Review loop exhausted max_retries ({max_retries}) for {skill_name}")
    return True


def _parse_task_config(args) -> dict:
    """Parse task configuration from task file or CLI args.

    Returns:
        Dict with keys: task_key, task_summary, task_description,
        skill_name, previous_task_keys, trace_context.

    Raises:
        SystemExit: With EXIT_CONFIG_ERROR on missing/invalid config.
    """
    previous_task_keys: list[str] = []
    trace_context: dict[str, Any] = {}
    task_key: str = "UNKNOWN"
    if args.task_file:
        if not args.task_file.exists():
            logger.error(f"Task file not found: {args.task_file}")
            sys.exit(EXIT_CONFIG_ERROR)

        task_data = json.loads(args.task_file.read_text())
        task_key = task_data.get("task_key", "UNKNOWN")
        task_summary = task_data.get("summary", "")
        task_description = task_data.get("description", "")
        previous_task_keys = task_data.get("previous_task_keys", [])
        raw_trace_context = task_data.get("trace_context", {})
        trace_context = raw_trace_context if isinstance(raw_trace_context, dict) else {}
        skill_name = task_data.get("skill_name", "")
    elif args.task_summary and args.task_description:
        task_summary = args.task_summary
        task_description = args.task_description
        skill_name = ""
    else:
        logger.error(
            "Task details required: use --task-file or --task-summary + --task-description"
        )
        sys.exit(EXIT_CONFIG_ERROR)

    return {
        "task_key": task_key,
        "task_summary": task_summary,
        "task_description": task_description,
        "skill_name": skill_name,
        "previous_task_keys": previous_task_keys,
        "trace_context": trace_context,
    }


def _fallback_commit(workspace: Path, task_key: str, task_summary: str) -> None:
    """Check if workspace is a git repo and run git_commit() as fallback.

    Skips if workspace is not a git repo -- analysis tasks (RCA, reflection)
    write artifacts to .forge/ without needing a commit.

    Raises:
        SystemExit: With EXIT_TASK_FAILED if commit fails.
    """
    is_git_repo = (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace,
            capture_output=True,
        ).returncode
        == 0
    )
    if is_git_repo:
        fallback_message = (
            f"[{task_key}] {task_summary}\n\nAuto-committed by Forge container fallback."
        )
        if not git_commit(workspace, fallback_message):
            logger.error("Failed to commit changes")
            sys.exit(EXIT_TASK_FAILED)


def main():
    parser = argparse.ArgumentParser(
        description="Forge container entrypoint for AI code implementation"
    )
    parser.add_argument(
        "--task-file",
        type=Path,
        help="Path to JSON file with task details",
    )
    parser.add_argument(
        "--task-summary",
        type=str,
        help="Short task summary",
    )
    parser.add_argument(
        "--task-description",
        type=str,
        help="Detailed task description",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("/workspace"),
        help="Workspace directory (default: /workspace)",
    )
    # Kept for backwards compatibility but no longer used
    parser.add_argument("--skip-tests", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--max-retries", type=int, default=3, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Parse task configuration
    task_config = _parse_task_config(args)
    task_key = task_config["task_key"]
    task_summary = task_config["task_summary"]
    task_description = task_config["task_description"]
    skill_name = task_config["skill_name"]
    previous_task_keys = task_config["previous_task_keys"]
    trace_context = task_config["trace_context"]

    workspace = args.workspace
    if not workspace.exists():
        logger.error(f"Workspace not found: {workspace}")
        sys.exit(EXIT_CONFIG_ERROR)

    # Get skill_name from env var if not in task data
    if not skill_name:
        skill_name = os.environ.get("FORGE_SKILL_NAME", "")

    # Configure git for commits
    configure_git()

    logger.info(f"Workspace: {workspace}")
    logger.info(f"Task: {task_summary}")

    # Load guardrails
    guardrails = load_guardrails(workspace)

    # Ensure .forge directory exists for handoff (this dir is excluded from commits)
    forge_dir = workspace / ".forge"
    forge_dir.mkdir(exist_ok=True)
    history_dir = forge_dir / "history"
    history_dir.mkdir(exist_ok=True)

    # Run agent to implement task
    # The agent has full tool access (bash, file ops, Context7 docs) and is responsible for:
    # - Reading/understanding the codebase
    # - Implementing the changes
    # - Running relevant tests as it sees fit
    # - Committing changes when ready
    if not asyncio.run(
        run_agent_task(
            workspace,
            task_key,
            task_summary,
            task_description,
            guardrails,
            previous_task_keys,
            trace_context,
        )
    ):
        logger.error("Task implementation failed")
        sys.exit(EXIT_TASK_FAILED)

    # Check for review.md and run review loop if it exists (SC-001, SC-010)
    if skill_name:
        review_md_path = detect_review_md(skill_name)

        if review_md_path:
            logger.info(f"Found review.md at {review_md_path}, starting review loop")
            # run_review_loop always returns True (exhaustion proceeds to PR per BR-005)
            asyncio.run(
                run_review_loop(
                    workspace=workspace,
                    task_key=task_key,
                    task_summary=task_summary,
                    task_description=task_description,
                    guardrails=guardrails,
                    skill_name=skill_name,
                    review_md_path=review_md_path,
                    previous_task_keys=previous_task_keys,
                    trace_context=trace_context,
                )
            )
        else:
            logger.info(f"No review.md found for skill {skill_name}, skipping review loop")
    else:
        logger.info("No skill_name provided, skipping review loop")

    # Ensure changes are committed (agent should have done this, but as fallback)
    _fallback_commit(workspace, task_key, task_summary)

    logger.info("Task completed successfully")
    sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()

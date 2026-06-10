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
from pathlib import Path
from typing import Any

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

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

    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
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
        logger.error("Tests timed out after 10 minutes")
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
        # Stage all changes
        result = subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Git add failed: {result.stderr}")
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
    )


async def run_agent_task(
    workspace: Path,
    task_key: str,
    task_summary: str,
    task_description: str,
    guardrails: str,
    previous_task_keys: list[str] | None = None,
) -> bool:
    """Run Deep Agents to implement the task.

    Args:
        workspace: Path to the workspace directory.
        task_key: Jira task key being implemented.
        task_summary: Short task summary.
        task_description: Detailed task description.
        guardrails: Repository guidelines.
        previous_task_keys: List of previously implemented task keys for handoff context.
    """
    # Support both new (LLM_MODEL) and legacy (CLAUDE_MODEL) env var names
    model_name = os.environ.get("LLM_MODEL") or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5@20250929")
    logger.info(f"Implementing task: {task_summary}")
    logger.info(f"Model: {model_name}")

    try:
        from deepagents import create_deep_agent
        from deepagents.backends import LocalShellBackend

        # Check for API credentials
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")

        if not api_key and not vertex_project:
            logger.error(
                "No API credentials found (ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID)"
            )
            return False

        # Create the agent with local shell backend (enables git commands)
        # virtual_mode=False: we want real filesystem access, not virtual paths
        # timeout=600: 10 minutes — allows long builds, test suites, and codegen
        backend = LocalShellBackend(
            root_dir=str(workspace),
            inherit_env=True,
            virtual_mode=False,
            timeout=600,
        )

        # Build system prompt from template
        system_prompt = build_system_prompt(
            workspace, task_key, task_summary, task_description, guardrails, previous_task_keys
        )

        # Determine model type (Gemini vs Claude)
        is_gemini = model_name.lower().startswith(("gemini", "models/gemini"))

        # Get max tokens from env (default 16384)
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "16384"))

        if vertex_project:
            if is_gemini:
                # Gemini models via ChatGoogleGenerativeAI with Vertex AI backend
                from langchain_google_genai import ChatGoogleGenerativeAI

                logger.info(f"Using Gemini model: {model_name}, max_output_tokens={max_tokens}")
                model = ChatGoogleGenerativeAI(
                    model=model_name,
                    project=vertex_project,
                    location=os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5"),
                    vertexai=True,
                    max_output_tokens=max_tokens,
                )
            else:
                # Claude models via ChatAnthropicVertex
                from langchain_google_vertexai.model_garden import ChatAnthropicVertex

                logger.info(f"Using Claude model: {model_name}, max_tokens={max_tokens}")
                model = ChatAnthropicVertex(
                    model_name=model_name,
                    project=vertex_project,
                    location=os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5"),
                    max_tokens=max_tokens,
                )
        else:
            if is_gemini:
                logger.error(f"Gemini model '{model_name}' requires Vertex AI credentials")
                return False

            from langchain_anthropic import ChatAnthropic

            logger.info(f"Using Claude model: {model_name}, max_tokens={max_tokens}")
            model = ChatAnthropic(
                model=model_name,
                api_key=api_key,
                max_tokens=max_tokens,
            )

        # Load Context7 MCP tools for library documentation
        mcp_tools = await load_context7_tools()

        # Parse skill paths from environment (comma-separated)
        skill_paths = []
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

        # Set up Langfuse tracing if credentials are available
        config: dict = {}
        langfuse_enabled = False
        if os.environ.get("LANGFUSE_PUBLIC_KEY"):
            try:
                from langfuse import propagate_attributes
                from langfuse.langchain import CallbackHandler

                handler = CallbackHandler()
                config["callbacks"] = [handler]
                langfuse_enabled = True
                logger.info(f"Langfuse tracing enabled for task {task_key}")
            except ImportError:
                logger.debug("Langfuse not installed, skipping tracing")
            except Exception as e:
                logger.warning(f"Failed to initialize Langfuse: {e}")

        # Run the agent (with Langfuse session context if enabled)
        initial_message = {
            "messages": [
                {"role": "user", "content": f"Implement this task:\n\n{task_description}"}
            ]
        }

        if langfuse_enabled:
            with propagate_attributes(
                session_id=task_key,
                tags=["forge-container", "task-implementation"],
                metadata={"task_summary": task_summary},
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

        # Save conversation history to .forge/history/{task_key}.json
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
                    }
                    for msg in messages
                ],
            }
            history_file.write_text(json.dumps(history_data, indent=2, default=str))
            logger.info(f"Saved conversation history to {history_file}")
        except Exception as e:
            logger.warning(f"Failed to save conversation history: {e}")

        logger.info("Agent completed task execution")
        return True

    except ImportError as e:
        logger.error(f"Missing dependency: {e}")
        return False
    except Exception as e:
        logger.error(f"Agent execution failed: {e}")
        return False


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

    # Load task details
    previous_task_keys: list[str] = []
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
    elif args.task_summary and args.task_description:
        task_summary = args.task_summary
        task_description = args.task_description
    else:
        logger.error(
            "Task details required: use --task-file or --task-summary + --task-description"
        )
        sys.exit(EXIT_CONFIG_ERROR)

    workspace = args.workspace
    if not workspace.exists():
        logger.error(f"Workspace not found: {workspace}")
        sys.exit(EXIT_CONFIG_ERROR)

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
            workspace, task_key, task_summary, task_description, guardrails, previous_task_keys
        )
    ):
        logger.error("Task implementation failed")
        sys.exit(EXIT_TASK_FAILED)

    # Ensure changes are committed (agent should have done this, but as fallback).
    # Skip if workspace is not a git repo — analysis tasks (RCA, reflection) write
    # artifacts to .forge/ without needing a commit.
    is_git_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        capture_output=True,
    ).returncode == 0
    if is_git_repo:
        fallback_message = f"[{task_key}] {task_summary}\n\nAuto-committed by Forge container fallback."
        if not git_commit(workspace, fallback_message):
            logger.error("Failed to commit changes")
            sys.exit(EXIT_TASK_FAILED)

    logger.info("Task completed successfully")
    sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()

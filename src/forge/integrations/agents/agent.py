"""Deep Agents client for AI-powered SDLC orchestration.

Uses LangChain Deep Agents to provide agentic capabilities including
tool use, file operations, and configurable skill paths.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver

# Optional MCP support
try:
    from langchain_core.tools import StructuredTool
    from langchain_mcp_adapters.client import MultiServerMCPClient

    HAS_MCP = True
except ImportError:
    StructuredTool = None  # type: ignore[misc, assignment]
    HAS_MCP = False

from forge.config import Settings, get_settings
from forge.integrations.langfuse import get_langfuse_config, get_langfuse_context
from forge.integrations.langfuse.fields import resolve_trace_fields
from forge.prompts import load_prompt, set_default_version
from forge.skills.resolver import resolve_skill_paths

# Optional Vertex AI support (Claude and Gemini)
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_google_vertexai.model_garden import ChatAnthropicVertex

    HAS_VERTEX = True
except ImportError:
    ChatGoogleGenerativeAI = None  # type: ignore[misc, assignment]
    HAS_VERTEX = False

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent

# Default MCP servers config file locations (checked in order)
MCP_CONFIG_PATHS = [
    PROJECT_ROOT / "mcp-servers.json",
    PROJECT_ROOT / "forge-mcp.json",
    Path.home() / ".forge" / "mcp-servers.json",
]

logger = logging.getLogger(__name__)

_TRACE_FIELD_KEYS = frozenset(
    {
        "ticket_key",
        "ticket_type",
        "current_node",
        "current_repo",
        "current_pr_number",
        "ci_status",
        "event_type",
        "event_source",
        "retry_count",
        "repo",
        "pr_number",
        "is_question",
        "is_revision",
        "revision_requested",
        "task",
    }
)


def _forward_trace_fields(context: dict[str, Any] | None) -> dict[str, Any]:
    """Extract trace-relevant fields from an incoming context dict."""
    extracted = {k: v for k, v in context.items() if k in _TRACE_FIELD_KEYS} if context else {}

    # Resolve executing node name from LangGraph config if available
    try:
        from langchain_core.runnables.config import ensure_config

        config = ensure_config()
        langgraph_node = config.get("metadata", {}).get("langgraph_node")
        if langgraph_node:
            extracted["current_node"] = langgraph_node
    except Exception:
        pass

    return extracted


def _prompt_context_fields(
    context: dict[str, Any] | None, allowed_keys: tuple[str, ...]
) -> dict[str, Any] | str:
    """Extract task-relevant fields for rendered prompts, excluding trace-only data."""
    if not context:
        return "None provided"

    prompt_context = {
        key: context[key]
        for key in allowed_keys
        if key in context and context[key] not in (None, "")
    }
    return prompt_context or "None provided"


def get_weather(city: str) -> str:
    """Placeholder tool for agent testing."""
    return f"Weather data for {city} not available."


class ForgeAgent:
    """AI agent for SDLC orchestration using Deep Agents.

    Provides autonomous task execution with configurable skill paths
    and tool capabilities. The agent selects the best approach for
    each task based on available skills.
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize the Deep Agent client.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self._ensure_api_key()
        self._checkpointer = MemorySaver()
        self._current_repo: str = ""  # Set per-task for dynamic MCP URLs

        # Set prompt version from config
        set_default_version(self.settings.prompt_version)

    def _ensure_api_key(self) -> None:
        """Ensure Anthropic API key is available."""
        if self.settings.use_vertex_ai:
            logger.info("Using Vertex AI backend")
        elif not os.environ.get("ANTHROPIC_API_KEY"):
            api_key = self.settings.anthropic_api_key.get_secret_value()
            if api_key:
                os.environ["ANTHROPIC_API_KEY"] = api_key

    def _create_model(self, model_name: str | None = None) -> Any:
        """Create the appropriate chat model based on configuration.

        Args:
            model_name: Optional model name override. Uses settings if not provided.

        Returns:
            A LangChain ChatModel instance (ChatAnthropic, ChatAnthropicVertex, or ChatVertexAI).
        """
        model = model_name or self.settings.claude_model
        provider = Settings.detect_model_provider(model)

        if self.settings.use_vertex_ai:
            if not HAS_VERTEX:
                raise ImportError(
                    "langchain-google-vertexai is required for Vertex AI. "
                    "Install with: pip install langchain-google-vertexai"
                )

            if provider == "google":
                # Gemini models via ChatGoogleGenerativeAI with Vertex AI backend
                logger.info(
                    f"Creating ChatGoogleGenerativeAI (Gemini) model: {model} "
                    f"in {self.settings.anthropic_vertex_region}, max_output_tokens={self.settings.llm_max_tokens}"
                )
                return ChatGoogleGenerativeAI(
                    model=model,
                    project=self.settings.anthropic_vertex_project_id,
                    location=self.settings.anthropic_vertex_region,
                    vertexai=True,
                    max_output_tokens=self.settings.llm_max_tokens,
                )
            else:
                # Claude models via ChatAnthropicVertex
                logger.info(
                    f"Creating ChatAnthropicVertex model: {model} "
                    f"in {self.settings.anthropic_vertex_region}, max_tokens={self.settings.llm_max_tokens}"
                )
                return ChatAnthropicVertex(
                    model_name=model,
                    project=self.settings.anthropic_vertex_project_id,
                    location=self.settings.anthropic_vertex_region,
                    max_tokens=self.settings.llm_max_tokens,
                )
        else:
            if provider == "google":
                raise ValueError(
                    f"Gemini model '{model}' requires Vertex AI. "
                    "Set ANTHROPIC_VERTEX_PROJECT_ID or use a Claude model."
                )
            logger.info(
                f"Creating ChatAnthropic model: {model}, max_tokens={self.settings.llm_max_tokens}"
            )
            return ChatAnthropic(
                model=model,
                api_key=self.settings.anthropic_api_key.get_secret_value(),
                max_tokens=self.settings.llm_max_tokens,
            )

    def _get_skill_paths(self, ticket_key: str | None = None) -> list[str]:
        """Return ordered skill source paths for Deep Agents.

        Resolves per-project skill overrides under settings.skills_dir, with
        fallback to skills/default/ for any skill not overridden by the project.
        """
        skills_dir = PROJECT_ROOT / self.settings.skills_dir.rstrip("/")
        paths = resolve_skill_paths(ticket_key or "", skills_dir)
        logger.debug(f"Using skill paths: {paths}")
        return paths

    def _get_allowed_tools(self) -> list[str] | None:
        """Get list of allowed tools based on config.

        Returns:
            List of tool names, or None if all tools allowed.
        """
        if not self.settings.agent_enable_tools:
            logger.debug("Agent tools disabled via config")
            return []

        allowed = self.settings.agent_allowed_tools.strip()
        if allowed == "*":
            logger.debug("All agent tools allowed")
            return None  # None means all tools

        tools = [t.strip() for t in allowed.split(",") if t.strip()]
        logger.debug(f"Allowed agent tools: {tools}")
        return tools

    def _get_root_dir(self) -> Path:
        """Get the root directory for the filesystem backend.

        Returns:
            Path to root directory.
        """
        if self.settings.agent_working_directory:
            return Path(self.settings.agent_working_directory)
        return PROJECT_ROOT

    # Write operation patterns to filter out in read-only mode
    WRITE_TOOL_PATTERNS = (
        # Prefixes
        "create",
        "add",
        "update",
        "delete",
        "remove",
        "push",
        "merge",
        "fork",
        "assign",
        "edit",
        "transition",
        "close",
        "reopen",
        "comment",
        "reply",
        "approve",
        "reject",
        "request",
        "run",
    )
    # Suffixes that indicate write operations
    WRITE_TOOL_SUFFIXES = ("_write",)

    def _wrap_tool_with_error_handling(self, tool: Any) -> Any:
        """Wrap a tool to catch errors and return them as messages.

        This prevents MCP tool errors from crashing the entire workflow.
        Instead, errors are returned to the agent as informative messages
        so it can try alternative approaches.

        Args:
            tool: The original tool to wrap.

        Returns:
            Wrapped tool with error handling.
        """
        original_func = tool.func if hasattr(tool, "func") else None
        original_coroutine = tool.coroutine if hasattr(tool, "coroutine") else None
        tool_name = tool.name if hasattr(tool, "name") else str(tool)

        if original_coroutine:

            @wraps(original_coroutine)
            async def wrapped_async(*args: Any, **kwargs: Any) -> str:
                try:
                    return await original_coroutine(*args, **kwargs)
                except Exception as e:
                    error_msg = f"Tool '{tool_name}' failed: {e}"
                    logger.warning(error_msg)
                    return f"ERROR: {error_msg}. Try a different approach or continue without this information."

            # Create new tool with wrapped coroutine
            return StructuredTool(
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema if hasattr(tool, "args_schema") else None,
                coroutine=wrapped_async,
                return_direct=getattr(tool, "return_direct", False),
            )
        elif original_func:

            @wraps(original_func)
            def wrapped_sync(*args: Any, **kwargs: Any) -> str:
                try:
                    return original_func(*args, **kwargs)
                except Exception as e:
                    error_msg = f"Tool '{tool_name}' failed: {e}"
                    logger.warning(error_msg)
                    return f"ERROR: {error_msg}. Try a different approach or continue without this information."

            # Create new tool with wrapped function
            return StructuredTool(
                name=tool.name,
                description=tool.description,
                args_schema=tool.args_schema if hasattr(tool, "args_schema") else None,
                func=wrapped_sync,
                return_direct=getattr(tool, "return_direct", False),
            )
        else:
            # Can't wrap, return original
            logger.warning(f"Could not wrap tool {tool_name} - no func or coroutine found")
            return tool

    async def _load_mcp_tools(self) -> list[Any]:
        """Load tools from configured MCP servers.

        Returns:
            List of tools from MCP servers (filtered if read-only mode).
        """
        if not HAS_MCP:
            logger.warning("langchain-mcp-adapters not installed, MCP tools unavailable")
            return []

        mcp_config = self._load_mcp_config()

        if not mcp_config:
            logger.debug("No MCP servers configured")
            return []

        logger.info(f"Loading MCP tools from servers: {list(mcp_config.keys())}")

        # Load each server independently so a single failing server does not
        # prevent tools from the other servers from loading.
        all_tools: list[Any] = []
        for server_name, server_config in mcp_config.items():
            try:
                client = MultiServerMCPClient({server_name: server_config})
                server_tools = await client.get_tools()
                logger.info(f"Loaded {len(server_tools)} tools from MCP server '{server_name}'")
                all_tools.extend(server_tools)
            except Exception as e:
                logger.warning(
                    f"Failed to load MCP tools from server '{server_name}' "
                    f"(skipping, other servers unaffected): {e}"
                )

        if not all_tools:
            logger.warning("No MCP tools loaded from any server")
            return []

        logger.info(f"Loaded {len(all_tools)} tools from MCP servers")

        # Filter to read-only tools if configured
        if self.settings.agent_mcp_read_only:
            all_tools = self._filter_read_only_tools(all_tools)

        # Wrap tools with error handling to prevent crashes
        all_tools = [self._wrap_tool_with_error_handling(t) for t in all_tools]
        logger.debug(f"Wrapped {len(all_tools)} MCP tools with error handling")

        return all_tools

    def _filter_read_only_tools(self, tools: list[Any]) -> list[Any]:
        """Filter tools to only read-only operations.

        Args:
            tools: List of MCP tools.

        Returns:
            Filtered list with write operations removed.
        """
        read_only_tools = []
        excluded_count = 0

        for tool in tools:
            name = tool.name if hasattr(tool, "name") else str(tool)
            name_lower = name.lower()

            # Check if tool name matches write patterns
            is_write_tool = (
                # Starts with or contains write prefix
                any(
                    name_lower.startswith(prefix) or f"_{prefix}" in name_lower
                    for prefix in self.WRITE_TOOL_PATTERNS
                )
                # Or ends with write suffix
                or any(name_lower.endswith(suffix) for suffix in self.WRITE_TOOL_SUFFIXES)
            )

            if is_write_tool:
                excluded_count += 1
                logger.debug(f"Excluding write tool: {name}")
            else:
                read_only_tools.append(tool)

        logger.info(
            f"MCP read-only mode: kept {len(read_only_tools)} tools, "
            f"excluded {excluded_count} write tools"
        )
        return read_only_tools

    async def _create_agent_async(
        self,
        system_prompt: str,
        include_tools: bool = True,
        ticket_key: str | None = None,
    ) -> Any:
        """Create a Deep Agent instance with configured skills and MCP tools.

        Args:
            system_prompt: System prompt for the agent.
            include_tools: Whether to include file/search tools.
            ticket_key: Optional ticket key for per-project skill resolution.

        Returns:
            Configured Deep Agent.
        """
        root_dir = self._get_root_dir()
        skill_paths = self._get_skill_paths(ticket_key)

        # Log configuration for visibility
        logger.info(f"Agent config: root_dir={root_dir}, skills={skill_paths}")

        # Create filesystem backend
        backend = FilesystemBackend(root_dir=str(root_dir))

        # Create the model (supports both direct API and Vertex AI)
        model = self._create_model()

        # Load MCP tools if enabled
        mcp_tools = await self._load_mcp_tools() if include_tools else []
        if not include_tools:
            logger.info("Agent tools: disabled (include_tools=False)")
        elif mcp_tools:
            mode = "read-only" if self.settings.agent_mcp_read_only else "full access"
            logger.info(f"Agent tools: {len(mcp_tools)} MCP tools ({mode})")
        else:
            logger.info("Agent tools: none (no MCP tools loaded)")

        # Create the agent with MCP tools.
        # Note: create_deep_agent already adds SummarizationMiddleware internally —
        # do not pass it again or deepagents raises a duplicate middleware error.
        agent = create_deep_agent(
            model=model,
            backend=backend,
            skills=skill_paths,
            system_prompt=system_prompt,
            checkpointer=self._checkpointer,
            tools=mcp_tools if mcp_tools else None,
        )

        return agent

    # Retry settings for rate-limited API calls
    MAX_RETRIES = 3
    INITIAL_BACKOFF_SECONDS = 5

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """Check if an error is a rate limit error that should be retried.

        Args:
            error: The exception that occurred.

        Returns:
            True if this is a rate limit error worth retrying.
        """
        error_str = str(error).lower()
        rate_limit_indicators = (
            "rate limit",
            "rate-limit",
            "ratelimit",
            "429",
            "403",  # GitHub returns 403 for rate limits
            "too many requests",
            "quota exceeded",
        )
        return any(indicator in error_str for indicator in rate_limit_indicators)

    def _is_transient_error(self, error: Exception) -> bool:
        """Check if an error is transient and worth retrying.

        Covers rate limits, network errors, timeouts, and temporary failures.

        Args:
            error: The exception that occurred.

        Returns:
            True if this is a transient error worth retrying.
        """
        # Check rate limits first
        if self._is_rate_limit_error(error):
            return True

        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # Network and connection errors
        transient_indicators = (
            "timeout",
            "timed out",
            "connection reset",
            "connection refused",
            "connection error",
            "connectionerror",
            "network error",
            "temporary failure",
            "service unavailable",
            "503",
            "502",
            "504",
            "bad gateway",
            "gateway timeout",
            "server error",
            "internal server error",
            "internal_failure",
            "500",
            "overloaded",
            "try again",
        )

        # Check error message
        if any(indicator in error_str for indicator in transient_indicators):
            return True

        # Check exception type names
        transient_types = (
            "timeout",
            "connectionerror",
            "connecttimeout",
            "readtimeout",
            "networkerror",
        )
        return any(t in error_type for t in transient_types)

    def _extract_retry_delay(self, error: Exception) -> int | None:
        """Extract retry delay from rate limit error message.

        Args:
            error: The exception with rate limit info.

        Returns:
            Number of seconds to wait, or None if not found.
        """
        error_str = str(error)
        # Look for patterns like "rate reset in 30s" or "retry after 60 seconds"
        import re

        patterns = [
            r"reset in (\d+)s",
            r"retry.{0,10}(\d+)\s*second",
            r"wait (\d+)\s*second",
        ]
        for pattern in patterns:
            match = re.search(pattern, error_str, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    async def _run_agent(
        self,
        prompt: str,
        system_prompt: str,
        include_tools: bool = True,
        session_id: str | None = None,
        trace_name: str | None = None,
        ticket_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Run the agent with the given prompt.

        Implements exponential backoff retry for rate limit errors.

        Args:
            prompt: User prompt to send.
            system_prompt: System prompt for the agent.
            include_tools: Whether to include tools.
            session_id: Optional session ID for Langfuse (e.g., ticket key).
            trace_name: Optional trace name for Langfuse.
            ticket_key: Optional ticket key for per-project skill resolution.
            tags: Optional list of trace tags for Langfuse.
            metadata: Optional metadata dict for Langfuse.

        Returns:
            Agent response text.
        """
        # Use async version to load MCP tools
        agent = await self._create_agent_async(
            system_prompt=system_prompt,
            include_tools=include_tools,
            ticket_key=ticket_key,
        )

        # Generate unique thread ID for this conversation
        thread_id = str(uuid.uuid4())

        # Build config with Langfuse tracing if enabled
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}

        # Add Langfuse callbacks for observability
        langfuse_config = get_langfuse_config(
            trace_name=trace_name or "deep_agent_invocation",
            session_id=session_id,
            tags=tags,
            metadata=metadata,
        )
        if langfuse_config:
            # Extract context params and remove from config
            langfuse_ctx_params = langfuse_config.pop("_langfuse_context", {})
            config.update(langfuse_config)
        else:
            langfuse_ctx_params = {}

        # Invoke the agent with retry logic for transient errors
        # Use async Langfuse context for session tracking (v3+ API)
        async with get_langfuse_context(
            session_id=langfuse_ctx_params.get("session_id"),
            user_id=langfuse_ctx_params.get("user_id"),
            tags=langfuse_ctx_params.get("tags"),
            metadata=langfuse_ctx_params.get("metadata"),
        ):
            last_error: Exception | None = None
            for attempt in range(self.MAX_RETRIES):
                try:
                    result = await agent.ainvoke(
                        {
                            "messages": [{"role": "user", "content": prompt}],
                        },
                        config=config,
                    )
                    break  # Success, exit retry loop
                except Exception as e:
                    last_error = e
                    if self._is_transient_error(e) and attempt < self.MAX_RETRIES - 1:
                        # Calculate backoff delay
                        explicit_delay = self._extract_retry_delay(e)
                        if explicit_delay:
                            delay = explicit_delay + 1  # Add 1s buffer
                        else:
                            delay = self.INITIAL_BACKOFF_SECONDS * (2**attempt)

                        logger.warning(
                            f"Transient error (attempt {attempt + 1}/{self.MAX_RETRIES}), "
                            f"retrying in {delay}s: {e}"
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise
            else:
                # All retries exhausted
                if last_error:
                    raise last_error

        # Extract response text from messages
        # Deep Agents returns LangChain message objects, not dicts
        response_text = []
        messages = result.get("messages", []) if isinstance(result, dict) else []

        for message in messages:
            # Check if it's an AI/Assistant message (LangChain message object)
            msg_type = type(message).__name__
            if msg_type in ("AIMessage", "AIMessageChunk"):
                content = message.content
                if isinstance(content, str):
                    response_text.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            response_text.append(block.get("text", ""))
                        elif hasattr(block, "text"):
                            response_text.append(block.text)

        return "\n".join(response_text)

    @staticmethod
    def _strip_preamble(text: str) -> str:
        """Strip agent narration before the first markdown heading.

        Finds the first proper heading (# / ## / ### followed by a space)
        and returns from there. This strips research narration that the
        agent emits before writing the actual document.

        The space requirement prevents false matches like "#1 item" or
        "#2 finding" in numbered lists.
        """
        if not text or text.lstrip().startswith("#"):
            return text.lstrip()

        lines = text.split("\n")
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if (
                stripped.startswith("# ")
                or stripped.startswith("## ")
                or stripped.startswith("### ")
            ):
                return "\n".join(lines[i:])
        return text

    async def run_task(
        self,
        task: str,
        prompt: str,
        context: dict[str, Any] | None = None,
        trace_context: dict[str, Any] | None = None,
        include_tools: bool = True,
    ) -> str:
        """Run a task, letting the agent choose the best approach.

        Deep Agents discovers skills automatically from the configured paths
        and selects the most appropriate one based on the task description.

        Args:
            task: Short task name for logging (e.g., 'generate-prd').
            prompt: The task description and content to process.
            context: Optional context variables injected into the system prompt.
            trace_context: Optional workflow fields forwarded to Langfuse only —
                not written to the system prompt.
            include_tools: Whether to include MCP tools. Default True.

        Returns:
            Agent response text.
        """
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Set current repo for dynamic MCP URLs (e.g., gitmcp.io/{owner}/{repo})
        if context and context.get("current_repo"):
            self._current_repo = context["current_repo"]

        # Load system prompt from file
        system_prompt = load_prompt("system", current_date=current_date)

        if context:
            system_prompt += "\n\nContext:\n"
            for key, value in context.items():
                system_prompt += f"- {key}: {value}\n"

        # Extract ticket key for session tracking. Prefer prompt context for
        # backward compatibility, but allow trace-only context to carry it.
        ticket_key = context.get("ticket_key") if context else None
        if ticket_key is None and trace_context:
            ticket_key = trace_context.get("ticket_key")

        import time

        from forge.api.routes.metrics import observe_agent_duration, record_agent_invocation

        logger.info(f"Running task '{task}' using Deep Agents")
        record_agent_invocation(task_type=task)
        _start = time.monotonic()
        # Resolve executing node name from LangGraph config if available
        langgraph_node = None
        try:
            from langchain_core.runnables.config import ensure_config

            config = ensure_config()
            langgraph_node = config.get("metadata", {}).get("langgraph_node")
        except Exception:
            pass

        # Merge prompt context + trace-only fields for Langfuse resolution.
        # trace_context fields are intentionally excluded from system_prompt above.
        trace_state: dict[str, Any] = {
            **(context or {}),
            **(trace_context or {}),
            "system_prompt_length": len(system_prompt),
            "llm_model": self.settings.claude_model,
        }

        if langgraph_node:
            trace_state["current_node"] = langgraph_node

        trace_tags, trace_metadata = resolve_trace_fields(trace_state)

        result = await self._run_agent(
            prompt=prompt,
            system_prompt=system_prompt,
            include_tools=include_tools,
            session_id=ticket_key,
            trace_name=f"task:{task}",
            ticket_key=ticket_key,
            tags=trace_tags or None,
            metadata=trace_metadata or None,
        )
        observe_agent_duration(task_type=task, duration=time.monotonic() - _start)

        logger.info(f"Task '{task}' completed ({len(result)} chars)")
        return result

    def _load_mcp_config(self) -> dict[str, Any]:
        """Load MCP server configuration from JSON file.

        Returns:
            Dictionary with server names as keys and configs as values.
            Format matches langchain-mcp-adapters MultiServerMCPClient.
        """
        if not self.settings.agent_enable_mcp:
            logger.debug("MCP disabled via config")
            return {}

        # Load full config from file
        all_servers: dict[str, Any] = {}

        if self.settings.agent_mcp_config_path:
            custom_path = Path(self.settings.agent_mcp_config_path)
            if custom_path.exists():
                all_servers = self._parse_mcp_config(custom_path)
            else:
                logger.warning(f"MCP config not found at {custom_path}")
        else:
            for config_path in MCP_CONFIG_PATHS:
                if config_path.exists():
                    logger.debug(f"Loading MCP config from {config_path}")
                    all_servers = self._parse_mcp_config(config_path)
                    break

        # Filter servers based on agent_mcp_servers setting
        enabled_setting = self.settings.agent_mcp_servers.strip()

        if enabled_setting == "*":
            # All servers enabled
            logger.info(f"MCP enabled with all servers: {list(all_servers.keys())}")
            return all_servers

        # Filter to only enabled servers
        enabled_list = [s.strip() for s in enabled_setting.split(",") if s.strip()]
        filtered_servers = {
            name: config for name, config in all_servers.items() if name in enabled_list
        }

        logger.info(f"MCP enabled with servers: {list(filtered_servers.keys())}")
        return filtered_servers

    def _parse_mcp_config(self, config_path: Path) -> dict[str, Any]:
        """Parse MCP config file and expand environment variables.

        Args:
            config_path: Path to the JSON config file.

        Returns:
            Parsed configuration with env vars expanded.
        """
        with open(config_path) as f:
            config = json.load(f)
        return self._expand_env_vars(config)

    def _expand_env_vars(self, obj: Any) -> Any:
        """Recursively expand ${VAR} and {owner}/{repo} patterns in config values.

        Args:
            obj: Config object (dict, list, or string).

        Returns:
            Object with variables expanded.
        """
        if isinstance(obj, dict):
            return {k: self._expand_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._expand_env_vars(item) for item in obj]
        elif isinstance(obj, str):
            result = obj

            # Expand ${VAR} patterns (environment variables and settings)
            pattern = r"\$\{([^}]+)\}"

            def replace_var(match: re.Match[str]) -> str:
                var_name = match.group(1)
                if var_name in os.environ:
                    return os.environ[var_name]
                return self._get_setting_value(var_name)

            result = re.sub(pattern, replace_var, result)

            # Expand {owner}/{repo} pattern for GitMCP URLs
            if "{owner}/{repo}" in result and self._current_repo:
                result = result.replace("{owner}/{repo}", self._current_repo)

            return result
        return obj

    def _get_setting_value(self, var_name: str) -> str:
        """Get a setting value by name, handling SecretStr.

        Args:
            var_name: Environment variable / setting name.

        Returns:
            The setting value as string, or empty string if not found.
        """
        var_mapping = {
            "GITHUB_TOKEN": lambda: self.settings.github_token.get_secret_value(),
            "GITHUB_PERSONAL_ACCESS_TOKEN": lambda: self.settings.github_token.get_secret_value(),
            "JIRA_API_TOKEN": lambda: self.settings.jira_api_token.get_secret_value(),
            "JIRA_USER_EMAIL": lambda: self.settings.jira_user_email,
            "JIRA_BASE_URL": lambda: self.settings.jira_base_url,
            "JIRA_DOMAIN": lambda: self.settings.jira_domain_resolved,
            "ATLASSIAN_AUTH_BASE64": lambda: self.settings.atlassian_auth_base64,
            "AGENT_WORKING_DIRECTORY": lambda: self.settings.agent_working_directory or os.getcwd(),
            "ANTHROPIC_API_KEY": lambda: self.settings.anthropic_api_key.get_secret_value(),
        }

        if var_name in var_mapping:
            return var_mapping[var_name]()

        attr_name = var_name.lower()
        if hasattr(self.settings, attr_name):
            value = getattr(self.settings, attr_name)
            if hasattr(value, "get_secret_value"):
                return value.get_secret_value()
            return str(value)

        logger.warning(f"Unknown variable: {var_name}")
        return ""

    async def generate_prd(
        self,
        raw_requirements: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Generate a structured PRD from raw requirements.

        Uses the 'generate-prd' skill from configured skill paths.

        Args:
            raw_requirements: Raw requirements text.
            context: Optional additional context.

        Returns:
            Generated PRD content.
        """
        prompt = load_prompt(
            "generate-prd",
            raw_requirements=raw_requirements,
            context=_prompt_context_fields(context, ("project_key", "summary")),
        )

        logger.info("Generating PRD using Deep Agents with skill")
        result = await self.run_task(
            task="generate-prd",
            prompt=prompt,
            context={
                "ticket_key": context.get("ticket_key", "") if context else "",
                "project_key": context.get("project_key", "") if context else "",
            },
            trace_context=_forward_trace_fields(context),
        )

        result = self._strip_preamble(result)
        logger.info(f"Generated PRD ({len(result)} chars)")
        return result

    async def generate_spec(
        self,
        prd_content: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Generate a behavioral specification from a PRD.

        Uses the 'generate-spec' skill from configured skill paths.

        Args:
            prd_content: Approved PRD content.
            context: Optional additional context.

        Returns:
            Generated specification content.
        """
        prompt = load_prompt(
            "generate-spec",
            prd_content=prd_content,
            context=_prompt_context_fields(context, ("project_key", "summary")),
        )

        logger.info("Generating Spec using Deep Agents with skill")
        result = await self.run_task(
            task="generate-spec",
            prompt=prompt,
            context={
                "ticket_key": context.get("ticket_key", "") if context else "",
                "project_key": context.get("project_key", "") if context else "",
            },
            trace_context=_forward_trace_fields(context),
        )

        result = self._strip_preamble(result)
        logger.info(f"Generated specification ({len(result)} chars)")
        return result

    async def generate_epics(
        self,
        spec_content: str,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Generate Epic breakdown from a specification.

        Uses the 'decompose-epics' skill from configured skill paths.

        Args:
            spec_content: Approved specification content.
            context: Optional additional context.

        Returns:
            List of dicts with 'summary' and 'plan' for each Epic.
        """
        available_repos = context.get("available_repos", []) if context else []
        if available_repos:
            repo_list = "\n".join(f"  - {r}" for r in available_repos)
            repo_instruction = f"""
AVAILABLE REPOSITORIES:
{repo_list}

IMPORTANT: Each Epic MUST be assigned to one of the repositories listed above.
Use REPO: <owner/repo> format in your output. Do NOT invent new repositories."""
        else:
            repo_instruction = """
NOTE: No repositories configured. Use REPO: unknown for now."""

        prompt = load_prompt(
            "decompose-epics",
            spec_content=spec_content,
            feature_summary=context.get("feature_summary", "Not provided")
            if context
            else "Not provided",
            project_key=context.get("project_key", "Not provided") if context else "Not provided",
            repo_instruction=repo_instruction,
        )

        feedback = context.get("feedback", "") if context else ""
        if feedback:
            prompt += (
                f"\n\n## Revision Feedback\n\n"
                f"The user has requested the following changes to the Epic breakdown:\n{feedback}\n\n"
                f"Please incorporate this feedback when creating the Epics."
            )

        logger.info("Generating Epics using Deep Agents with skill")
        result = await self.run_task(
            task="decompose-epics",
            prompt=prompt,
            context={
                "ticket_key": context.get("ticket_key", "") if context else "",
                "project_key": context.get("project_key", "") if context else "",
                "feature_summary": context.get("feature_summary", "") if context else "",
                "available_repos": available_repos,
            },
            trace_context=_forward_trace_fields(context),
        )

        epics = self._parse_epics_response(result)
        logger.info(f"Generated {len(epics)} Epics")
        return epics

    async def regenerate_with_feedback(
        self,
        original_content: str,
        feedback: str,
        content_type: str,
        ticket_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Regenerate content incorporating feedback.

        Args:
            original_content: The original generated content.
            feedback: User feedback/revision request.
            content_type: Type of content (prd, spec, epic).
            ticket_key: Optional ticket key for session tracking.
            context: Optional context with trace fields from workflow state.

        Returns:
            Regenerated content.
        """
        skill_map = {
            "prd": "generate-prd",
            "spec": "generate-spec",
            "epic": "decompose-epics",
            "task": "generate-tasks",
        }
        skill_name = skill_map.get(content_type, "generate-prd")

        prompt = load_prompt(
            "regenerate",
            content_type=content_type.upper(),
            original_content=original_content,
            feedback=feedback,
        )

        logger.info(f"Regenerating {content_type} with feedback using Deep Agents")
        result = await self.run_task(
            task=skill_name,
            prompt=prompt,
            context={"is_revision": True, "ticket_key": ticket_key or ""},
            trace_context=_forward_trace_fields(context),
        )

        result = self._strip_preamble(result)
        logger.info(f"Regenerated {content_type} ({len(result)} chars)")
        return result

    @staticmethod
    def _parse_epics_response(response: str) -> list[dict[str, str]]:
        """Parse the Epic generation response into structured data.

        Args:
            response: Raw response from agent.

        Returns:
            List of Epic dicts with 'summary', 'plan', and 'repo'.
        """
        import re

        epics = []
        current_epic: dict[str, str] = {}
        current_section = None
        plan_lines: list[str] = []

        for line in response.split("\n"):
            stripped = line.strip()

            if stripped.startswith("---"):
                if current_epic.get("summary"):
                    current_epic["plan"] = "\n".join(plan_lines).strip()
                    epics.append(current_epic)
                    current_epic = {}
                    plan_lines = []
                continue

            if stripped.startswith("EPIC:"):
                current_epic["summary"] = stripped[5:].strip()
                current_section = "summary"
            elif stripped.startswith("REPO:"):
                # Extract repo (owner/name format)
                repo = stripped[5:].strip()
                # Clean up any extra text
                repo = re.sub(r"[^a-zA-Z0-9/_-]", "", repo)
                if "/" in repo:
                    current_epic["repo"] = repo
            elif stripped.startswith("PLAN:"):
                current_section = "plan"
            elif current_section == "plan":
                plan_lines.append(line)

        if current_epic.get("summary"):
            current_epic["plan"] = "\n".join(plan_lines).strip()
            epics.append(current_epic)

        return epics

    async def answer_question(
        self,
        question: str,
        artifact_content: str,
        context: dict[str, Any],
    ) -> str:
        """Answer a question about a generated artifact.

        Args:
            question: The user's question.
            artifact_content: The content of the artifact being discussed.
            context: Additional context including generation history.

        Returns:
            The answer to the question.
        """
        artifact_type = context.get("artifact_type", "document")
        generation_context = context.get("generation_context", {})
        raw_requirements = (
            generation_context.get("raw_requirements")
            or context.get("description")
            or "Not available"
        )

        ticket_type = context.get("ticket_type")
        ticket_type_str = ""
        if ticket_type is not None:
            ticket_type_str = (
                ticket_type.value if hasattr(ticket_type, "value") else str(ticket_type)
            )

        if (
            artifact_type == "plan"
            and context.get("current_node") == "task_plan_approval_gate"
            and ticket_type_str == "task"
        ):
            prompt = load_prompt(
                "task-takeover-qa",
                ticket_key=context.get("ticket_key", ""),
                summary=context.get("summary", ""),
                description=context.get("description", ""),
                plan_content=artifact_content,
                question=question,
            )
            task_name = "task-takeover-qa"
        else:
            prompt = load_prompt(
                "answer-question",
                artifact_type=artifact_type,
                artifact_content=artifact_content,
                summary=context.get("summary", ""),
                description=context.get("description", ""),
                raw_requirements=raw_requirements,
                question=question,
            )
            task_name = "answer-question"

        logger.info(f"Answering question about {artifact_type} using task={task_name}")
        result = await self.run_task(
            task=task_name,
            prompt=prompt,
            context={
                "artifact_type": artifact_type,
                "ticket_key": context.get("ticket_key", ""),
            },
            trace_context=_forward_trace_fields(context),
            # Q&A gets read-only MCP tools for lookups (filtered by agent_mcp_read_only)
        )

        logger.info(f"Generated answer ({len(result)} chars)")
        return result.strip() if result else ""

    async def close(self) -> None:
        """Close the agent and cleanup resources."""
        pass

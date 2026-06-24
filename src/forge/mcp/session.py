"""Read-only MCP server for Forge session inspection."""

from __future__ import annotations

import json
import logging

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from forge.observability.access import (
    ObservabilityUnavailableError,
    get_model_usage,
    get_observability_health,
    get_session_traces,
    get_ticket_observability,
    get_trace,
    get_workflow_funnel,
)
from forge.sessions.summary import SessionNotFoundError, get_session_summary

logger = logging.getLogger(__name__)


def create_server() -> FastMCP:
    """Create the Forge session MCP server."""
    mcp = FastMCP(
        "Forge Session",
        instructions=(
            "Read-only Forge session and observability inspection. This server "
            "exposes curated workflow summaries, safe observability aggregates, "
            "and explicit full Langfuse trace access for users."
        ),
    )

    @mcp.tool(
        name="get_session_summary",
        description="Return a safe read-only summary for a Forge session by Jira ticket key.",
    )
    async def get_session_summary_tool(ticket_key: str) -> dict:
        try:
            payload = await get_session_summary(ticket_key, logs_limit=0)
            return payload.as_dict()
        except SessionNotFoundError as exc:
            return {
                "summary": {
                    "ticket_key": ticket_key.strip().upper(),
                    "found": False,
                    "status": "not_found",
                    "raw_state_exposed": False,
                },
                "notes": [str(exc)],
            }

    @mcp.tool(
        name="get_ticket_observability",
        description=(
            "Return safe Langfuse observability aggregates for one Jira ticket: "
            "cost, token usage, latency, workflow steps, and recent observation metadata."
        ),
    )
    async def get_ticket_observability_tool(
        ticket_key: str,
        hours: int = 720,
        limit: int = 50,
    ) -> dict:
        try:
            return await get_ticket_observability(ticket_key, hours=hours, limit=limit)
        except (ValueError, ObservabilityUnavailableError) as exc:
            return {"error": str(exc), "raw_trace_data_exposed": False}

    @mcp.tool(
        name="get_session_traces",
        description=(
            "Return Langfuse traces for a Jira ticket session. By default this returns "
            "full trace details, including raw trace input/output fields when Langfuse provides them."
        ),
    )
    async def get_session_traces_tool(
        ticket_key: str,
        hours: int = 720,
        limit: int = 10,
        full: bool = True,
    ) -> dict:
        try:
            return await get_session_traces(ticket_key, hours=hours, limit=limit, full=full)
        except (ValueError, ObservabilityUnavailableError) as exc:
            return {"error": str(exc), "full_trace_data_exposed": False}

    @mcp.tool(
        name="get_trace",
        description=(
            "Return one full Langfuse trace by trace id, including raw trace input/output "
            "fields when Langfuse provides them."
        ),
    )
    async def get_trace_tool(trace_id: str) -> dict:
        try:
            return await get_trace(trace_id)
        except (ValueError, ObservabilityUnavailableError) as exc:
            return {"error": str(exc), "full_trace_data_exposed": False}

    @mcp.tool(
        name="get_model_usage",
        description="Return safe aggregate model usage, cost, tokens, and latency.",
    )
    async def get_model_usage_tool(hours: int = 24, limit: int = 20) -> dict:
        try:
            return await get_model_usage(hours=hours, limit=limit)
        except ObservabilityUnavailableError as exc:
            return {"error": str(exc), "raw_trace_data_exposed": False}

    @mcp.tool(
        name="get_workflow_funnel",
        description="Return safe workflow-step issue, trace, cost, and latency aggregates.",
    )
    async def get_workflow_funnel_tool(hours: int = 24, limit: int = 50) -> dict:
        try:
            return await get_workflow_funnel(hours=hours, limit=limit)
        except ObservabilityUnavailableError as exc:
            return {"error": str(exc), "raw_trace_data_exposed": False}

    @mcp.tool(
        name="get_observability_health",
        description="Return metadata coverage checks for the Forge observability layer.",
    )
    async def get_observability_health_tool(hours: int = 24) -> dict:
        try:
            return await get_observability_health(hours=hours)
        except ObservabilityUnavailableError as exc:
            return {"error": str(exc), "raw_trace_data_exposed": False}

    @mcp.resource(
        "forge://sessions/{ticket_key}",
        name="Forge Session Summary",
        description="Safe JSON summary for a Forge session.",
        mime_type="application/json",
    )
    async def session_summary_resource(ticket_key: str) -> str:
        result = await get_session_summary_tool(ticket_key)
        return json.dumps(result, indent=2, sort_keys=True)

    return mcp


def main() -> None:
    """Run the Forge session MCP server over stdio."""
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    create_server().run("stdio")


if __name__ == "__main__":
    main()

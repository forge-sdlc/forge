"""Tests for the Forge session MCP server wiring."""

from mcp.server.fastmcp import FastMCP

from forge.mcp.session import create_server


def test_create_server_returns_fastmcp_instance() -> None:
    server = create_server()

    assert isinstance(server, FastMCP)
    assert server.name == "Forge Session"

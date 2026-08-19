from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.security import (
    initialize_agent_skills,
    operational_subprocess_env,
    parse_host_tools,
    validate_agent_root,
)
from forge.skills.resolver import resolve_skill_paths


def test_host_tools_default_safe_and_write_tools_prohibited() -> None:
    assert parse_host_tools("ls,read_file,glob,grep") == {"ls", "read_file", "glob", "grep"}
    with pytest.raises(ValueError, match="Prohibited"):
        parse_host_tools("ls,execute")
    with pytest.raises(ValueError, match="unsafe"):
        parse_host_tools("*")
    with pytest.raises(ValueError, match="Unknown"):
        parse_host_tools("web_search")


def test_agent_root_cannot_expose_project_or_workspace(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir()
    assert validate_agent_root(project / ".forge" / "agent", project).is_dir()
    with pytest.raises(ValueError, match="Forge source"):
        validate_agent_root(tmp_path, project)


def test_agent_root_rejects_symlink(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "agent"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        validate_agent_root(link, project)


def test_skill_initialization_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("safe")
    (source / "escape").symlink_to(tmp_path / "outside")
    root = tmp_path / "agent"
    root.mkdir()
    with pytest.raises(ValueError, match="Symlinks"):
        initialize_agent_skills(root, source)


def test_skill_resolution_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsafe project key"):
        resolve_skill_paths("../../-1", tmp_path)


def test_skill_initialization_preserves_default_and_project_layout(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    (source / "default" / "common").mkdir(parents=True)
    (source / "default" / "common" / "SKILL.md").write_text("default")
    (source / "aisos" / "project").mkdir(parents=True)
    (source / "aisos" / "project" / "SKILL.md").write_text("project")
    root = tmp_path / "agent"
    root.mkdir()

    skills_root = initialize_agent_skills(root, source)

    assert (skills_root / "default" / "common" / "SKILL.md").read_text() == "default"
    assert (skills_root / "aisos" / "project" / "SKILL.md").read_text() == "project"


def test_operational_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    env = operational_subprocess_env()
    assert env["PATH"] == "/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "LANGFUSE_SECRET_KEY" not in env


@pytest.mark.asyncio
async def test_mcp_tools_are_default_deny_and_exact_allowlisted() -> None:
    tools = {
        "github": [SimpleNamespace(name="get_issue"), SimpleNamespace(name="create_issue")],
        "jira": [SimpleNamespace(name="get_issue")],
    }

    class Client:
        def __init__(self, config):
            self.server = next(iter(config))

        async def get_tools(self):
            return tools[self.server]

    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = SimpleNamespace(agent_mcp_allowed_tools="github:get_issue")
    agent._load_mcp_config = lambda: {"github": {}, "jira": {}}
    agent._wrap_tool_with_error_handling = lambda tool: tool
    with patch("forge.integrations.agents.agent.MultiServerMCPClient", Client):
        loaded = await agent._load_mcp_tools()
        discovered = await agent.discover_mcp_tools()

    assert [tool.name for tool in loaded] == ["get_issue"]
    assert discovered == ["github:create_issue", "github:get_issue", "jira:get_issue"]


def test_stdio_mcp_environment_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    servers = ForgeAgent._sanitize_mcp_subprocesses(
        {"local": {"transport": "stdio", "command": "tool", "env": {"TOKEN": "needed"}}}
    )
    assert servers["local"]["env"]["PATH"] == "/bin"
    assert servers["local"]["env"]["TOKEN"] == "needed"
    assert "ANTHROPIC_API_KEY" not in servers["local"]["env"]

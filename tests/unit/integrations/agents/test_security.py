import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.security import (
    initialize_agent_skills,
    operational_subprocess_env,
    parse_host_tools,
    validate_agent_root,
)
from forge.skills.resolver import resolve_skill_paths


def test_host_skills_load_through_virtual_backend(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    sources = ["committed-skills/default", "committed-skills/proj", "skills/proj"]
    for index, source in enumerate(sources):
        for name in (f"skill-{index}", "shared"):
            skill = root / source / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Layer {index}\n---\nInstructions\n"
            )

    agent = ForgeAgent.__new__(ForgeAgent)
    with patch.object(agent, "_get_root_dir", return_value=root):
        paths = agent._get_skill_paths("PROJ-123")
    backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)
    result = SkillsMiddleware(backend=backend, sources=paths).before_agent({}, None, {})

    assert not result.get("skills_load_errors")
    skills = {skill["name"]: skill for skill in result["skills_metadata"]}
    assert set(skills) == {"skill-0", "skill-1", "skill-2", "shared"}
    assert skills["shared"]["description"] == "Layer 2"
    content = backend.read(skills["shared"]["path"])
    assert content.error is None
    assert "Instructions" in content.file_data["content"]


class _ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        # Deliberately emit the configured call even if the tool was hidden.
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("allow_read", [False, True])
async def test_host_tool_allowlist_enforces_execution(
    tmp_path: Path, use_async: bool, allow_read: bool
) -> None:
    (tmp_path / "example.txt").write_text("protected file content")
    model = _ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "/example.txt"}, "id": "call-1"}
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = SimpleNamespace(
        agent_allowed_tools="read_file" if allow_read else "",
        agent_enable_tools=True,
    )
    agent._checkpointer = None
    with (
        patch.object(agent, "_get_root_dir", return_value=tmp_path),
        patch.object(agent, "_create_model", return_value=model),
        patch.object(agent, "_load_mcp_tools", new_callable=AsyncMock, return_value=[]),
    ):
        graph = await agent._create_agent_async("Test tool permissions.")

    inputs = {"messages": [HumanMessage(content="Read the example file.")]}
    result = await graph.ainvoke(inputs) if use_async else graph.invoke(inputs)
    messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(messages) == 1
    assert messages[0].tool_call_id == "call-1"
    if allow_read:
        assert messages[0].status == "success"
        assert "protected file content" in messages[0].content
    else:
        assert messages[0].status == "error"
        assert "not allowed" in messages[0].content
        assert "protected file content" not in messages[0].content


def test_host_tools_default_safe_and_write_tools_prohibited() -> None:
    assert parse_host_tools("ls,read_file,glob,grep") == {"ls", "read_file", "glob", "grep"}
    with pytest.raises(ValueError, match="Prohibited"):
        parse_host_tools("ls,execute")
    with pytest.raises(ValueError, match="unsafe"):
        parse_host_tools("*")
    with pytest.raises(ValueError, match="Unknown"):
        parse_host_tools("web_search")


class _ReviewDecision(BaseModel):
    accepted: bool


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [ProviderStrategy, ToolStrategy])
async def test_host_allowlist_preserves_structured_responses(tmp_path: Path, strategy) -> None:
    """Native and fallback output schemas work without granting filesystem access."""
    (tmp_path / "example.txt").write_text("protected file content")
    final_response = (
        AIMessage(content='{"accepted": true}')
        if strategy is ProviderStrategy
        else AIMessage(
            content="",
            tool_calls=[{"name": "_ReviewDecision", "args": {"accepted": True}, "id": "decision"}],
        )
    )
    model = _ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "/example.txt"}, "id": "read"}
                ],
            ),
            final_response,
        ]
    )
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = SimpleNamespace(agent_allowed_tools="", agent_enable_tools=True)
    agent._checkpointer = None
    with (
        patch.object(agent, "_get_root_dir", return_value=tmp_path),
        patch.object(agent, "_create_model", return_value=model),
        patch.object(agent, "_load_mcp_tools", new_callable=AsyncMock, return_value=[]),
    ):
        graph = await agent._create_agent_async(
            "Return a review decision.", response_format=strategy(_ReviewDecision)
        )

    result = await graph.ainvoke({"messages": [HumanMessage(content="Review.")]})

    assert result["structured_response"] == _ReviewDecision(accepted=True)
    denied = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "read"
    )
    assert denied.status == "error"
    assert "not allowed" in denied.content
    assert "protected file content" not in denied.content


def test_agent_root_cannot_expose_project_or_workspace(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir()
    assert validate_agent_root(project / ".forge" / "agent", project).is_dir()
    with pytest.raises(ValueError, match="Forge source"):
        validate_agent_root(tmp_path, project)


def test_agent_root_enforces_private_permissions(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir()
    root = tmp_path / "agent"
    root.mkdir(mode=0o755)

    validate_agent_root(root, project)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_agent_root_accepts_private_fs_group_volume(tmp_path: Path) -> None:
    """A writable group-owned Kubernetes volume need not be chmod-able by the process."""
    project = tmp_path / "forge"
    project.mkdir()
    root = tmp_path / "agent"
    root.mkdir(mode=0o770)

    original_chmod = Path.chmod

    def deny_agent_root_chmod(path: Path, mode: int, *args, **kwargs) -> None:
        if path == root.resolve():
            raise PermissionError("not the volume owner")
        original_chmod(path, mode, *args, **kwargs)

    with patch.object(Path, "chmod", deny_agent_root_chmod):
        assert validate_agent_root(root, project) == root.resolve()


def test_agent_root_rejects_world_accessible_unowned_volume(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir()
    root = tmp_path / "agent"
    root.mkdir(mode=0o777)

    with (
        patch.object(Path, "chmod", side_effect=PermissionError("not the volume owner")),
        pytest.raises(ValueError, match="not private and writable"),
    ):
        validate_agent_root(root, project)


def test_agent_root_does_not_chmod_rejected_path(tmp_path: Path) -> None:
    project = tmp_path / "forge"
    project.mkdir(mode=0o755)
    original_mode = stat.S_IMODE(project.stat().st_mode)

    with pytest.raises(ValueError, match="Forge source"):
        validate_agent_root(project, project)

    assert stat.S_IMODE(project.stat().st_mode) == original_mode


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


def test_skill_initialization_prunes_deleted_committed_skills(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    removed = source / "default" / "removed"
    removed.mkdir(parents=True)
    (removed / "SKILL.md").write_text("unsafe")
    root = tmp_path / "agent"
    root.mkdir()

    skills_root = initialize_agent_skills(root, source)
    assert (skills_root / "default" / "removed" / "SKILL.md").is_file()

    shutil.rmtree(removed)
    initialize_agent_skills(root, source)

    assert not (skills_root / "default" / "removed").exists()


def test_skill_initialization_preserves_runtime_installed_skills(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    (source / "default" / "common").mkdir(parents=True)
    (source / "default" / "common" / "SKILL.md").write_text("default")
    root = tmp_path / "agent"
    runtime_skill = root / "skills" / "aisos" / "runtime"
    runtime_skill.mkdir(parents=True)
    (runtime_skill / "SKILL.md").write_text("runtime")

    initialize_agent_skills(root, source)

    assert (runtime_skill / "SKILL.md").read_text() == "runtime"


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


@pytest.mark.asyncio
async def test_mcp_tools_cannot_reenable_prohibited_builtin_by_name() -> None:
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = SimpleNamespace(agent_mcp_allowed_tools="local:execute")
    agent._load_mcp_config = lambda: {"local": {}}

    with pytest.raises(ValueError, match="collide.*local:execute"):
        await agent._load_mcp_tools()


@pytest.mark.asyncio
async def test_mcp_tools_cannot_shadow_safe_builtin_by_name() -> None:
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = SimpleNamespace(agent_mcp_allowed_tools="local:read_file")
    agent._load_mcp_config = lambda: {"local": {}}

    with pytest.raises(ValueError, match="collide.*local:read_file"):
        await agent._load_mcp_tools()


def test_stdio_mcp_environment_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    servers = ForgeAgent._sanitize_mcp_subprocesses(
        {"local": {"transport": "stdio", "command": "tool", "env": {"TOKEN": "needed"}}}
    )
    assert servers["local"]["env"]["PATH"] == "/bin"
    assert servers["local"]["env"]["TOKEN"] == "needed"
    assert "ANTHROPIC_API_KEY" not in servers["local"]["env"]

"""Security boundaries for host-side Deep Agents."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

SAFE_BUILTIN_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
PROHIBITED_BUILTIN_TOOLS = frozenset({"write_file", "edit_file", "execute"})


class HostToolAllowlistMiddleware(AgentMiddleware):
    """Expose only explicitly granted tools to the host agent model."""

    def __init__(self, allowed: set[str] | frozenset[str]) -> None:
        self.allowed = frozenset(allowed)

    def _request(self, request: ModelRequest) -> ModelRequest:
        return request.override(tools=[tool for tool in request.tools if tool.name in self.allowed])

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return handler(self._request(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return await handler(self._request(request))


def parse_host_tools(value: str, *, enabled: bool = True) -> frozenset[str]:
    """Validate the host built-in tool allowlist."""
    if not enabled:
        return frozenset()
    requested = frozenset(item.strip() for item in value.split(",") if item.strip())
    unknown = requested - SAFE_BUILTIN_TOOLS - PROHIBITED_BUILTIN_TOOLS
    prohibited = requested & PROHIBITED_BUILTIN_TOOLS
    if value.strip() == "*":
        raise ValueError("AGENT_ALLOWED_TOOLS='*' is unsafe for host agents")
    if unknown:
        raise ValueError(f"Unknown host agent tools: {', '.join(sorted(unknown))}")
    if prohibited:
        raise ValueError(f"Prohibited host agent tools: {', '.join(sorted(prohibited))}")
    return requested


def validate_agent_root(root: Path, project_root: Path, workspace_base: str = "") -> Path:
    """Create and validate a dedicated agent root that cannot expose Forge data."""
    if root.is_symlink():
        raise ValueError(f"Agent root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = root.resolve(strict=True)
    project = project_root.resolve(strict=True)
    home = Path.home().resolve()
    protected = [
        project / ".env",
        project / ".git",
        home / ".ssh",
        home / ".aws",
        home / ".config" / "gcloud",
        home / ".config" / "gh",
    ]
    if workspace_base:
        protected.append(Path(workspace_base).resolve())

    # A root below the source tree is safe; a root equal to or above it is not.
    if resolved == project or resolved in project.parents:
        raise ValueError(f"Agent root overlaps Forge source: {resolved}")
    for path in protected:
        if path == resolved or resolved in path.parents:
            raise ValueError(f"Agent root exposes protected path: {path}")
    # mkdir's mode is ignored for an existing directory and is filtered by the
    # process umask for a new one. Enforce the isolation boundary only after the
    # path has passed validation so a rejected path is never chmodded.
    try:
        resolved.chmod(0o700)
    except PermissionError as exc:
        # Kubernetes emptyDir volumes are commonly owned by root and made
        # writable to the workload through fsGroup.  The workload can use the
        # directory but cannot chmod it because it is not the owner.
        mode = stat.S_IMODE(resolved.stat().st_mode)
        accessible = all(os.access(resolved, flag) for flag in (os.R_OK, os.W_OK, os.X_OK))
        if mode & 0o007 or not accessible:
            raise ValueError(
                f"Agent root permissions are not private and writable: {resolved}"
            ) from exc
    return resolved


def _assert_safe_tree(source: Path) -> Path:
    source = source.absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"Skill source must be a real directory: {source}")
    resolved_source = source.resolve(strict=True)
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError(f"Symlinks are not allowed in skills: {entry}")
        try:
            entry.resolve(strict=True).relative_to(resolved_source)
        except ValueError as exc:
            raise ValueError(f"Skill path escapes its source: {entry}") from exc
    return resolved_source


def initialize_agent_skills(agent_root: Path, source_root: Path) -> Path:
    """Seed the isolated runtime skill tree from committed skill directories."""
    safe_source = _assert_safe_tree(source_root)
    skills_root = agent_root / "committed-skills"
    # Keep repository-owned skills separate from runtime-fetched skills. This
    # allows an exact rebuild to remove files or whole skills deleted upstream
    # without destroying packages installed at runtime under agent_root/skills.
    if skills_root.exists():
        if skills_root.is_symlink() or not skills_root.is_dir():
            raise ValueError(f"Committed skill destination must be a real directory: {skills_root}")
        shutil.rmtree(skills_root)
    skills_root.mkdir(parents=True, exist_ok=True)
    for source in sorted(safe_source.iterdir()):
        if source.is_dir():
            shutil.copytree(source, skills_root / source.name, dirs_exist_ok=True)
    return skills_root


def operational_subprocess_env(explicit: dict[str, str] | None = None) -> dict[str, str]:
    """Return the non-secret operational environment allowed in child processes."""
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_USER_NAME",
        "GIT_USER_EMAIL",
    }
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.setdefault("PATH", os.defpath)
    result.setdefault("HOME", str(Path.home()))
    result.setdefault("LANG", "C.UTF-8")
    result.setdefault("TMPDIR", tempfile.gettempdir())
    result.update(explicit or {})
    return result

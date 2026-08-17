import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_skill_paths(
    ticket_key: str,
    skills_dir: Path,
    *,
    skills_install_dir: Path | None = None,
) -> list[str]:
    """Return ordered skill source paths for Deep Agents.

    Deep Agents loads sources in order and deduplicates by skill name,
    with later sources overriding earlier ones (last wins).

    Resolution order (lowest to highest priority):
    1. ``skills_dir/default/``   — committed default skills
    2. ``skills_dir/{project}/`` — committed project overrides
    3. ``skills_install_dir/{project}/``  — runtime-fetched project skills
    """
    default_dir = skills_dir / "default"

    if "-" not in ticket_key:
        logger.info("Skills: default only (no ticket key)")
        return [str(default_dir) + "/"]

    project = ticket_key.split("-")[0].lower()
    if not re.fullmatch(r"[a-z0-9_]+", project):
        raise ValueError(f"Unsafe project key for skill resolution: {project!r}")
    paths: list[str] = [str(default_dir) + "/"]

    override_dir = skills_dir / project
    if override_dir.is_dir():
        paths.append(str(override_dir) + "/")
        logger.info(f"Skills: committed override active for '{project}' ({override_dir})")

    if skills_install_dir is not None:
        cached_dir = skills_install_dir / project
        if cached_dir.is_dir() and cached_dir.resolve() != override_dir.resolve():
            paths.append(str(cached_dir) + "/")
            logger.info(f"Skills: fetched skills active for '{project}' ({cached_dir})")

    if len(paths) == 1:
        logger.info(f"Skills: default only (no override for project '{project}')")

    return paths

"""Orchestration function for ensuring project skills are installed.

Coordinates skill fetching, installation, and lock file management by
delegating to the underlying fetcher, installer, and lock modules.

Typical call site::

    await ensure_skills(project_key, jira_client, skills_dir)
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from forge.integrations.jira.client import JiraClient
from forge.skills.fetcher import (
    CloneError,
    RefResolutionError,
    clone_context,
    resolve_ref_sha,
    should_fetch_entry,
)
from forge.skills.installer import install_path_mode, install_skill_mapping
from forge.skills.lock import read_lock_file, update_lock_file
from forge.skills.models import LockEntry

logger = logging.getLogger(__name__)


async def ensure_skills(
    project_key: str,
    jira_client: JiraClient,
    skills_dir: Path,
    *,
    skills_install_dir: Path | None = None,
) -> None:
    """Ensure all skills configured for *project_key* are installed.

    Reads the ``forge.skills`` Jira project property, resolves each entry's
    ref to a commit SHA, compares against the existing lock file, and fetches
    and installs any entries whose SHA has changed or that are not yet
    installed.

    Args:
        project_key: Jira project key (e.g., ``"MYPROJ"``).
        jira_client: Authenticated Jira client used to read the project
            property.
        skills_dir: Root directory for source-tracked skills (e.g.
            ``skills/default/``).  Used as the install target only when
            *skills_install_dir* is not provided.
        skills_install_dir: When provided, fetched skills and the lock file are
            written here instead of *skills_dir*.  Point this at a location
            outside the source tree to keep runtime artifacts out of it; when
            it coincides with *skills_dir* (the default), fetched artifacts are
            written alongside the committed skills.
    """
    # ------------------------------------------------------------------
    # 1. Fetch the forge.skills configuration from Jira.
    # ------------------------------------------------------------------
    skills_config = await jira_client.get_skills_config(project_key)

    if skills_config is None:
        logger.info(
            "No forge.skills property found for project %s; skipping skill installation.",
            project_key,
        )
        return

    if not skills_config:
        logger.info(
            "forge.skills property for project %s is empty; skipping skill installation.",
            project_key,
        )
        return

    # ------------------------------------------------------------------
    # 2. Determine install root (skills_install_dir if provided, else skills_dir).
    # ------------------------------------------------------------------
    install_root = skills_install_dir if skills_install_dir is not None else skills_dir
    install_root.mkdir(parents=True, exist_ok=True)

    lock_path = install_root / "skills.lock"
    lock = read_lock_file(lock_path)

    # ------------------------------------------------------------------
    # 3. Determine the target directory (lowercase project key).
    # ------------------------------------------------------------------
    target_dir = install_root / project_key.lower()

    # ------------------------------------------------------------------
    # 4. Process each SkillEntry.
    # ------------------------------------------------------------------
    for entry in skills_config:
        logger.debug("Processing skill entry: source=%s ref=%s", entry.source, entry.ref)

        # --- 4a. Resolve the ref to a concrete commit SHA. -----------
        resolved_sha: str | None = None
        if entry.ref is not None:
            try:
                resolved_sha = await resolve_ref_sha(entry.source, entry.ref)
            except RefResolutionError as exc:
                logger.warning(
                    "Failed to resolve ref %r for %s: %s – skipping entry.",
                    entry.ref,
                    entry.source,
                    exc,
                )
                continue

        # --- 4b. Decide whether a fetch is needed. -------------------
        if not should_fetch_entry(entry, resolved_sha, lock):
            logger.info("Skills current for %s at ref %s", entry.source, entry.ref)
            continue

        # --- 4c. Clone, install, and record the lock entry. ----------
        logger.info(
            "Fetching skills from %s at ref %s",
            entry.source,
            entry.ref,
        )

        try:
            async with clone_context(entry.source, entry.ref) as clone_dir:
                installed_skills = _install_entry(entry, clone_dir, target_dir)
        except (CloneError, FileNotFoundError, NotADirectoryError) as exc:
            logger.warning(
                "Failed to fetch/install skills from %s: %s – skipping entry.",
                entry.source,
                exc,
            )
            continue

        # --- 4d. Determine the effective commit SHA for the lock. ----
        effective_sha = resolved_sha if resolved_sha is not None else (entry.ref or "")

        # --- 4e. Update the lock file. --------------------------------
        mode = "path" if entry.path is not None else "skill_mapping"
        lock_entry = LockEntry(
            source=entry.source,
            ref=entry.ref or "",
            resolved_commit=effective_sha,
            mode=mode,  # type: ignore[arg-type]
            path=entry.path,
            skill_mapping=entry.skill_mapping,
            target=project_key.lower(),
            skills=installed_skills,
            fetched_at=datetime.now(tz=UTC),
        )
        update_lock_file(lock_path, lock_entry)

        # Re-read the lock file so subsequent entries in the same run
        # see the updated state.
        lock = read_lock_file(lock_path)

        logger.info(
            "Installed %d skill(s) from %s: %s",
            len(installed_skills),
            entry.source,
            installed_skills,
        )


def _install_entry(
    entry,  # SkillEntry
    clone_dir: Path,
    target_dir: Path,
) -> list[str]:
    """Install a single skill entry from *clone_dir* into *target_dir*.

    Chooses between path mode and skill_mapping mode based on the entry
    configuration.

    Args:
        entry: :class:`~forge.skills.models.SkillEntry` describing the skill.
        clone_dir: Root of the cloned repository.
        target_dir: Directory into which skills should be installed.

    Returns:
        List of installed skill names.
    """
    if entry.path is not None:
        source_dir = clone_dir / entry.path
        return install_path_mode(source_dir, target_dir)

    # skill_mapping mode
    assert entry.skill_mapping is not None, "SkillEntry must have path or skill_mapping"
    return install_skill_mapping(clone_dir, entry.skill_mapping, target_dir)

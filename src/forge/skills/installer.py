"""Skill installation logic for path mode and skill_mapping mode.

Provides two installation strategies:

- :func:`install_path_mode`: copies every subdirectory of a source directory
  into a target directory, treating each subdirectory as an independent skill.
- :func:`install_skill_mapping`: copies only the subdirectories listed in a
  mapping dict, validates that each contains a ``SKILL.md`` file, and renames
  the destination directory according to the mapping key.

Both functions overwrite any existing directories in the target.
"""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_MARKER = "SKILL.md"


def install_path_mode(source_dir: Path, target_dir: Path) -> list[str]:
    """Copy all subdirectories of *source_dir* into *target_dir*.

    Each subdirectory of *source_dir* is treated as a skill.  The directory is
    copied to *target_dir* using the same name, overwriting any existing copy.

    Args:
        source_dir: Directory whose immediate subdirectories are the skills to
            install.  Must exist and be a directory.
        target_dir: Destination directory.  Created if it does not exist.

    Returns:
        List of installed skill names (the subdirectory names, in iteration
        order).

    Raises:
        FileNotFoundError: If *source_dir* does not exist.
        NotADirectoryError: If *source_dir* is not a directory.
    """
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source path is not a directory: {source_dir}")

    target_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []

    for entry in sorted(source_dir.iterdir()):
        if not entry.is_dir():
            logger.debug("Skipping non-directory entry: %s", entry.name)
            continue

        dest = target_dir / entry.name
        _copy_dir(entry, dest)
        installed.append(entry.name)
        logger.debug("Installed skill %r from %s", entry.name, entry)

    if not installed:
        logger.debug("No skill subdirectories found in %s", source_dir)

    return installed


def install_skill_mapping(
    source_dir: Path,
    mapping: dict[str, str],
    target_dir: Path,
) -> list[str]:
    """Copy skills listed in *mapping* from *source_dir* into *target_dir*.

    For each ``target_name -> source_subdir`` entry in *mapping*:

    1. Resolve ``source_dir / source_subdir`` as the skill source.
    2. Validate that ``SKILL.md`` exists inside that directory.
    3. Copy the directory to ``target_dir / target_name``, overwriting any
       existing copy.
    4. Skip entries that fail validation and log a warning.

    Args:
        source_dir: Root of the cloned skill repository.  Must exist and be a
            directory.
        mapping: Dict mapping *target skill name* → *relative source path*
            inside *source_dir*.
        target_dir: Destination directory.  Created if it does not exist.

    Returns:
        List of successfully installed skill names (target names, in mapping
        iteration order).

    Raises:
        FileNotFoundError: If *source_dir* does not exist.
        NotADirectoryError: If *source_dir* is not a directory.
    """
    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source path is not a directory: {source_dir}")

    target_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []

    for target_name, source_subdir in mapping.items():
        target_path = Path(target_name)
        if target_name in {"", ".", ".."} or target_path.name != target_name:
            raise ValueError(f"Unsafe skill target name: {target_name!r}")
        skill_source = source_dir / source_subdir

        if not skill_source.exists() or not skill_source.is_dir():
            logger.warning(
                "Skipping skill %r: source path does not exist or is not a directory: %s",
                target_name,
                skill_source,
            )
            continue

        try:
            skill_source.resolve(strict=True).relative_to(source_dir.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"Skill source escapes cloned repository: {source_subdir!r}") from exc

        skill_marker = skill_source / _SKILL_MARKER
        if not skill_marker.exists():
            logger.warning(
                "Skipping skill %r: %s not found in %s",
                target_name,
                _SKILL_MARKER,
                skill_source,
            )
            continue

        dest = target_dir / target_name
        _copy_dir(skill_source, dest)
        installed.append(target_name)
        logger.debug("Installed skill %r from %s -> %s", target_name, skill_source, dest)

    return installed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _copy_dir(src: Path, dest: Path) -> None:
    """Copy *src* to *dest*, removing *dest* first if it already exists."""
    resolved_source = src.resolve(strict=True)
    if src.is_symlink():
        raise ValueError(f"Skill source must not be a symlink: {src}")
    for entry in src.rglob("*"):
        if entry.is_symlink():
            raise ValueError(f"Symlinks are not allowed in skills: {entry}")
        try:
            entry.resolve(strict=True).relative_to(resolved_source)
        except ValueError as exc:
            raise ValueError(f"Skill path escapes its source: {entry}") from exc
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

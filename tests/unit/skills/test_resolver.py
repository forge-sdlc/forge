from pathlib import Path

import pytest

from forge.skills.resolver import resolve_skill_paths


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a skills directory with a default subdirectory."""
    (tmp_path / "default").mkdir()
    return tmp_path


def test_no_override_returns_default_only(skills_dir: Path) -> None:
    result = resolve_skill_paths("PROJ-123", skills_dir)
    assert result == [str(skills_dir / "default") + "/"]


def test_with_override_returns_default_then_project(skills_dir: Path) -> None:
    (skills_dir / "proj").mkdir()
    result = resolve_skill_paths("PROJ-123", skills_dir)
    assert result == [
        str(skills_dir / "default") + "/",
        str(skills_dir / "proj") + "/",
    ]


def test_project_key_lowercased(skills_dir: Path) -> None:
    (skills_dir / "aisos").mkdir()
    result = resolve_skill_paths("AISOS-456", skills_dir)
    assert result == [
        str(skills_dir / "default") + "/",
        str(skills_dir / "aisos") + "/",
    ]


def test_ticket_key_without_dash_returns_default(skills_dir: Path) -> None:
    result = resolve_skill_paths("NOHYPHEN", skills_dir)
    assert result == [str(skills_dir / "default") + "/"]


def test_nonexistent_project_dir_returns_default(skills_dir: Path) -> None:
    result = resolve_skill_paths("MISSING-1", skills_dir)
    assert result == [str(skills_dir / "default") + "/"]


def test_override_path_is_file_returns_default(skills_dir: Path) -> None:
    (skills_dir / "proj").touch()  # file, not a directory
    result = resolve_skill_paths("PROJ-123", skills_dir)
    assert result == [str(skills_dir / "default") + "/"]


# ---------------------------------------------------------------------------
# skills_install_dir support
# ---------------------------------------------------------------------------


def test_skills_install_dir_appended_after_committed_override(
    skills_dir: Path, tmp_path: Path
) -> None:
    """When both committed override and cached skills exist, all three paths returned."""
    (skills_dir / "proj").mkdir()
    cache = tmp_path / "cache"
    (cache / "proj").mkdir(parents=True)

    result = resolve_skill_paths("PROJ-123", skills_dir, skills_install_dir=cache)
    assert result == [
        str(skills_dir / "default") + "/",
        str(skills_dir / "proj") + "/",
        str(cache / "proj") + "/",
    ]


def test_skills_install_dir_only_fetched(skills_dir: Path, tmp_path: Path) -> None:
    """When only skills_install_dir has a project dir, returns default + cached."""
    cache = tmp_path / "cache"
    (cache / "proj").mkdir(parents=True)

    result = resolve_skill_paths("PROJ-123", skills_dir, skills_install_dir=cache)
    assert result == [
        str(skills_dir / "default") + "/",
        str(cache / "proj") + "/",
    ]


def test_skills_install_dir_none_preserves_old_behavior(skills_dir: Path) -> None:
    """When skills_install_dir is None, behavior matches original (no extra paths)."""
    (skills_dir / "proj").mkdir()
    result = resolve_skill_paths("PROJ-123", skills_dir, skills_install_dir=None)
    assert result == [
        str(skills_dir / "default") + "/",
        str(skills_dir / "proj") + "/",
    ]


def test_skills_install_dir_nonexistent_project_returns_default(
    skills_dir: Path, tmp_path: Path
) -> None:
    """When skills_install_dir exists but has no project subdir, returns default only."""
    cache = tmp_path / "cache"
    cache.mkdir()

    result = resolve_skill_paths("PROJ-123", skills_dir, skills_install_dir=cache)
    assert result == [str(skills_dir / "default") + "/"]


def test_skills_install_dir_same_as_source_does_not_duplicate_project(
    skills_dir: Path,
) -> None:
    """The default local install directory is already the committed override tier."""
    (skills_dir / "proj").mkdir()

    result = resolve_skill_paths("PROJ-123", skills_dir, skills_install_dir=skills_dir)

    assert result == [
        str(skills_dir / "default") + "/",
        str(skills_dir / "proj") + "/",
    ]

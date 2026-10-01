"""Unit tests for forge.skills.installer – install_path_mode and install_skill_mapping."""

import logging
from pathlib import Path

import pytest

from forge.skills.installer import install_path_mode, install_skill_mapping

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(base: Path, name: str, with_marker: bool = True) -> Path:
    """Create a skill subdirectory with optional SKILL.md."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "implementation.py").write_text(f"# {name}\n")
    if with_marker:
        (skill_dir / "SKILL.md").write_text(f"# {name} skill\n")
    return skill_dir


# ===========================================================================
# install_path_mode
# ===========================================================================


class TestInstallPathMode:
    def test_copies_subdirectories_to_target(self, tmp_path: Path) -> None:
        """All subdirectories of source are copied into target."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "skill-a")
        _make_skill(source, "skill-b")

        result = install_path_mode(source, target)

        assert sorted(result) == ["skill-a", "skill-b"]
        assert (target / "skill-a").is_dir()
        assert (target / "skill-b").is_dir()

    def test_returns_list_of_skill_names(self, tmp_path: Path) -> None:
        """Return value contains the copied directory names."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "alpha")
        _make_skill(source, "beta")
        _make_skill(source, "gamma")

        result = install_path_mode(source, target)

        assert sorted(result) == ["alpha", "beta", "gamma"]

    def test_file_contents_are_preserved(self, tmp_path: Path) -> None:
        """File contents in copied subdirectories are intact."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        skill_dir = _make_skill(source, "my-skill")
        (skill_dir / "extra.txt").write_text("hello")

        install_path_mode(source, target)

        assert (target / "my-skill" / "extra.txt").read_text() == "hello"
        assert (target / "my-skill" / "SKILL.md").exists()

    def test_skips_files_in_source(self, tmp_path: Path) -> None:
        """Non-directory entries at source root are ignored."""
        source = tmp_path / "source"
        source.mkdir()
        _make_skill(source, "real-skill")
        (source / "README.md").write_text("top-level file")

        result = install_path_mode(source, target := tmp_path / "target")

        assert result == ["real-skill"]
        assert not (target / "README.md").exists()

    def test_overwrites_existing_target_directory(self, tmp_path: Path) -> None:
        """Existing skill directories in target are overwritten."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "skill-x")
        # Pre-populate target with stale content
        stale = target / "skill-x"
        stale.mkdir(parents=True)
        (stale / "stale.txt").write_text("old")

        install_path_mode(source, target)

        assert not (target / "skill-x" / "stale.txt").exists()
        assert (target / "skill-x" / "SKILL.md").exists()

    def test_creates_target_directory_if_missing(self, tmp_path: Path) -> None:
        """Target directory is created automatically when it doesn't exist."""
        source = tmp_path / "source"
        target = tmp_path / "nested" / "target"
        _make_skill(source, "skill-a")

        install_path_mode(source, target)

        assert target.is_dir()
        assert (target / "skill-a").is_dir()

    def test_empty_source_returns_empty_list(self, tmp_path: Path) -> None:
        """An empty source directory returns an empty list."""
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target"

        result = install_path_mode(source, target)

        assert result == []

    def test_raises_file_not_found_for_missing_source(self, tmp_path: Path) -> None:
        """FileNotFoundError is raised when source directory does not exist."""
        source = tmp_path / "nonexistent"
        target = tmp_path / "target"

        with pytest.raises(FileNotFoundError, match="Source directory does not exist"):
            install_path_mode(source, target)

    def test_raises_not_a_directory_when_source_is_file(self, tmp_path: Path) -> None:
        """NotADirectoryError is raised when source path is a file."""
        source = tmp_path / "not_a_dir.txt"
        source.write_text("oops")
        target = tmp_path / "target"

        with pytest.raises(NotADirectoryError, match="Source path is not a directory"):
            install_path_mode(source, target)

    def test_logs_debug_for_each_installed_skill(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A debug message is emitted for each installed skill."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "skill-a")

        with caplog.at_level(logging.DEBUG, logger="forge.skills.installer"):
            install_path_mode(source, target)

        assert any("skill-a" in msg for msg in caplog.messages)

    def test_logs_debug_when_no_skills_found(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A debug message is emitted when no subdirectories are found."""
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target"

        with caplog.at_level(logging.DEBUG, logger="forge.skills.installer"):
            install_path_mode(source, target)

        assert any("No skill subdirectories" in msg for msg in caplog.messages)

    def test_rejects_symlinks_in_skill_tree(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        skill = _make_skill(source, "unsafe")
        (skill / "escape").symlink_to(tmp_path / "outside")

        with pytest.raises(ValueError, match="Symlinks"):
            install_path_mode(source, tmp_path / "target")


# ===========================================================================
# install_skill_mapping
# ===========================================================================


class TestInstallSkillMapping:
    def test_copies_and_renames_according_to_mapping(self, tmp_path: Path) -> None:
        """Skills are copied to target under the mapping's target name."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source / "skills", "my-skill")

        mapping = {"renamed-skill": "skills/my-skill"}
        result = install_skill_mapping(source, mapping, target)

        assert result == ["renamed-skill"]
        assert (target / "renamed-skill").is_dir()
        assert (target / "renamed-skill" / "SKILL.md").exists()
        assert not (target / "my-skill").exists()

    def test_returns_list_of_installed_target_names(self, tmp_path: Path) -> None:
        """Return value lists the target skill names, not the source names."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "src-a")
        _make_skill(source, "src-b")

        mapping = {"dst-a": "src-a", "dst-b": "src-b"}
        result = install_skill_mapping(source, mapping, target)

        assert sorted(result) == ["dst-a", "dst-b"]

    def test_validates_skill_md_exists(self, tmp_path: Path) -> None:
        """Only entries with SKILL.md in the source subdirectory are installed."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "valid-skill", with_marker=True)
        _make_skill(source, "no-marker", with_marker=False)

        mapping = {"valid": "valid-skill", "bad": "no-marker"}
        result = install_skill_mapping(source, mapping, target)

        assert result == ["valid"]
        assert (target / "valid").is_dir()
        assert not (target / "bad").exists()

    def test_skips_and_warns_when_skill_md_missing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged when SKILL.md is absent, and the skill is skipped."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "no-marker", with_marker=False)

        mapping = {"missing-marker": "no-marker"}
        with caplog.at_level(logging.WARNING, logger="forge.skills.installer"):
            result = install_skill_mapping(source, mapping, target)

        assert result == []
        assert any("missing-marker" in msg for msg in caplog.messages)
        assert any("SKILL.md" in msg for msg in caplog.messages)

    def test_skips_and_warns_when_source_subdir_missing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged when the source subdirectory does not exist."""
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target"

        mapping = {"phantom": "nonexistent/path"}
        with caplog.at_level(logging.WARNING, logger="forge.skills.installer"):
            result = install_skill_mapping(source, mapping, target)

        assert result == []
        assert any("phantom" in msg for msg in caplog.messages)

    def test_overwrites_existing_target_directory(self, tmp_path: Path) -> None:
        """Existing skill directories in target are overwritten."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "skill-a")
        # Pre-populate target with stale content
        stale = target / "renamed"
        stale.mkdir(parents=True)
        (stale / "stale.txt").write_text("old")

        mapping = {"renamed": "skill-a"}
        install_skill_mapping(source, mapping, target)

        assert not (target / "renamed" / "stale.txt").exists()
        assert (target / "renamed" / "SKILL.md").exists()

    def test_creates_target_directory_if_missing(self, tmp_path: Path) -> None:
        """Target directory is created automatically when it doesn't exist."""
        source = tmp_path / "source"
        target = tmp_path / "nested" / "target"
        _make_skill(source, "skill-a")

        mapping = {"skill-a": "skill-a"}
        install_skill_mapping(source, mapping, target)

        assert target.is_dir()
        assert (target / "skill-a").is_dir()

    def test_empty_mapping_returns_empty_list(self, tmp_path: Path) -> None:
        """An empty mapping produces an empty result."""
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target"

        result = install_skill_mapping(source, {}, target)

        assert result == []

    def test_rejects_target_path_traversal(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        _make_skill(source, "safe")

        with pytest.raises(ValueError, match="Unsafe skill target"):
            install_skill_mapping(source, {"../escape": "safe"}, tmp_path / "target")

    def test_rejects_source_path_escape(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        outside = _make_skill(tmp_path, "outside")
        assert outside.is_dir()

        with pytest.raises(ValueError, match="escapes cloned repository"):
            install_skill_mapping(source, {"escape": "../outside"}, tmp_path / "target")

    def test_raises_file_not_found_for_missing_source(self, tmp_path: Path) -> None:
        """FileNotFoundError is raised when source directory does not exist."""
        source = tmp_path / "nonexistent"
        target = tmp_path / "target"

        with pytest.raises(FileNotFoundError, match="Source directory does not exist"):
            install_skill_mapping(source, {"skill": "skill"}, target)

    def test_raises_not_a_directory_when_source_is_file(self, tmp_path: Path) -> None:
        """NotADirectoryError is raised when source path is a file."""
        source = tmp_path / "not_a_dir.txt"
        source.write_text("oops")
        target = tmp_path / "target"

        with pytest.raises(NotADirectoryError, match="Source path is not a directory"):
            install_skill_mapping(source, {"skill": "skill"}, target)

    def test_file_contents_are_preserved(self, tmp_path: Path) -> None:
        """File contents within copied skills are intact."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        skill_dir = _make_skill(source, "src-skill")
        (skill_dir / "data.json").write_text('{"key": "value"}')

        mapping = {"dst-skill": "src-skill"}
        install_skill_mapping(source, mapping, target)

        assert (target / "dst-skill" / "data.json").read_text() == '{"key": "value"}'

    def test_multiple_skills_partial_failure(self, tmp_path: Path) -> None:
        """Only valid skills are installed when some mapping entries fail validation."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "good-a")
        _make_skill(source, "good-b")
        _make_skill(source, "bad-c", with_marker=False)

        mapping = {"a": "good-a", "b": "good-b", "c": "bad-c"}
        result = install_skill_mapping(source, mapping, target)

        assert sorted(result) == ["a", "b"]
        assert (target / "a").is_dir()
        assert (target / "b").is_dir()
        assert not (target / "c").exists()

    def test_logs_debug_for_each_successful_install(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A debug message is emitted for each successfully installed skill."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        _make_skill(source, "skill-a")

        mapping = {"installed": "skill-a"}
        with caplog.at_level(logging.DEBUG, logger="forge.skills.installer"):
            install_skill_mapping(source, mapping, target)

        assert any("installed" in msg for msg in caplog.messages)

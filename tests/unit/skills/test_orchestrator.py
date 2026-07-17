"""Unit tests for forge.skills.orchestrator – ensure_skills()."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.skills.models import LockEntry, LockFile, SkillEntry
from forge.skills.orchestrator import ensure_skills

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_URL = "https://github.com/example/skills.git"
RESOLVED_SHA = "abc123def456abc123def456abc123def456abc1"
PROJECT_KEY = "MYPROJ"


def _make_skill_entry(
    source: str = REPO_URL,
    ref: str | None = "main",
    path: str | None = "skills",
    skill_mapping: dict[str, str] | None = None,
) -> SkillEntry:
    return SkillEntry(source=source, ref=ref, path=path, skill_mapping=skill_mapping)


def _make_lock_entry(
    source: str = REPO_URL,
    resolved_commit: str = RESOLVED_SHA,
    skills: list[str] | None = None,
) -> LockEntry:
    return LockEntry(
        source=source,
        ref="main",
        resolved_commit=resolved_commit,
        mode="path",
        path="skills",
        target="myproj",
        skills=skills or ["skill-a"],
        fetched_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _make_jira_client(skills_config) -> MagicMock:
    """Return a mock JiraClient with get_skills_config returning *skills_config*."""
    client = MagicMock()
    client.get_skills_config = AsyncMock(return_value=skills_config)
    return client


# ---------------------------------------------------------------------------
# Early-exit cases
# ---------------------------------------------------------------------------


class TestEnsureSkillsEarlyExit:
    @pytest.mark.asyncio
    async def test_returns_early_when_config_is_none(self, tmp_path: Path, caplog) -> None:
        """When get_skills_config returns None, log info and return without action."""
        jira_client = _make_jira_client(None)

        with caplog.at_level(logging.INFO, logger="forge.skills.orchestrator"):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        jira_client.get_skills_config.assert_awaited_once_with(PROJECT_KEY)
        assert "No forge.skills property found" in caplog.text

    @pytest.mark.asyncio
    async def test_returns_early_when_config_is_empty(self, tmp_path: Path, caplog) -> None:
        """When get_skills_config returns an empty list, log info and return."""
        jira_client = _make_jira_client([])

        with caplog.at_level(logging.INFO, logger="forge.skills.orchestrator"):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        jira_client.get_skills_config.assert_awaited_once_with(PROJECT_KEY)
        assert "is empty" in caplog.text


# ---------------------------------------------------------------------------
# Skill-current (no fetch needed) case
# ---------------------------------------------------------------------------


class TestEnsureSkillsCurrent:
    @pytest.mark.asyncio
    async def test_skips_when_skills_current(self, tmp_path: Path, caplog) -> None:
        """When should_fetch_entry returns False, log 'Skills current' and skip fetch."""
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch(
                "forge.skills.orchestrator.should_fetch_entry",
                return_value=False,
            ),
            patch("forge.skills.orchestrator.clone_context") as mock_clone,
            caplog.at_level(logging.INFO, logger="forge.skills.orchestrator"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # No clone should have been initiated.
        mock_clone.assert_not_called()
        assert "Skills current" in caplog.text

    @pytest.mark.asyncio
    async def test_reads_lock_file_on_entry(self, tmp_path: Path) -> None:
        """Lock file is read once at the start of ensure_skills."""
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=False),
            patch("forge.skills.orchestrator.clone_context"),
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ) as mock_read,
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # read_lock_file called at least once (initial read)
        mock_read.assert_called()
        lock_path_used = mock_read.call_args_list[0][0][0]
        assert lock_path_used == tmp_path / "skills.lock"


# ---------------------------------------------------------------------------
# Fetch, install, and lock update case
# ---------------------------------------------------------------------------


class TestEnsureSkillsFetch:
    @pytest.mark.asyncio
    async def test_fetches_and_installs_when_stale(self, tmp_path: Path, caplog) -> None:
        """When should_fetch_entry returns True, clone, install, and update lock."""
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])
        installed_names = ["skill-a", "skill-b"]

        # Build a minimal fake clone directory
        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)
        (fake_clone / "skills" / "skill-a").mkdir()
        (fake_clone / "skills" / "skill-b").mkdir()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=installed_names,
            ) as mock_install,
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
            caplog.at_level(logging.INFO, logger="forge.skills.orchestrator"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        mock_install.assert_called_once()
        mock_update_lock.assert_called_once()

        # Inspect the LockEntry written to the lock file.
        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.source == REPO_URL
        assert lock_entry.resolved_commit == RESOLVED_SHA
        assert lock_entry.skills == installed_names
        assert lock_entry.target == PROJECT_KEY.lower()
        assert lock_entry.mode == "path"

        assert "Installed" in caplog.text

    @pytest.mark.asyncio
    async def test_uses_lowercase_project_key_as_target(self, tmp_path: Path) -> None:
        """Target directory and lock entry target use lowercase project key."""
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        captured_target_dir: list[Path] = []

        def fake_install(_source_dir: Path, target_dir: Path) -> list[str]:
            captured_target_dir.append(target_dir)
            return []

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                side_effect=fake_install,
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills("MYPROJ", jira_client, tmp_path)

        # Target directory must be skills_dir/myproj (lowercase)
        assert captured_target_dir[0] == tmp_path / "myproj"

        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.target == "myproj"

    @pytest.mark.asyncio
    async def test_skill_mapping_mode(self, tmp_path: Path) -> None:
        """When skill_mapping is set, install_skill_mapping is called."""
        entry = _make_skill_entry(
            path=None,
            skill_mapping={"my-skill": "src/my-skill"},
        )
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        fake_clone.mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_skill_mapping",
                return_value=["my-skill"],
            ) as mock_install,
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        mock_install.assert_called_once()
        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.mode == "skill_mapping"
        assert lock_entry.skill_mapping == {"my-skill": "src/my-skill"}

    @pytest.mark.asyncio
    async def test_uses_entry_ref_as_sha_when_resolved_sha_is_none(self, tmp_path: Path) -> None:
        """When resolve_ref_sha returns None (commit SHA ref), entry.ref is used as the SHA."""
        commit_sha = "deadbeef" * 5  # 40 chars
        entry = _make_skill_entry(ref=commit_sha)
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=None),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.resolved_commit == commit_sha

    @pytest.mark.asyncio
    async def test_skips_entry_when_ref_resolution_fails(self, tmp_path: Path, caplog) -> None:
        """When resolve_ref_sha raises RefResolutionError, log warning and skip entry."""
        from forge.skills.fetcher import RefResolutionError

        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(side_effect=RefResolutionError("network error")),
            ),
            patch("forge.skills.orchestrator.clone_context") as mock_clone,
            caplog.at_level(logging.WARNING, logger="forge.skills.orchestrator"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        mock_clone.assert_not_called()
        assert "Failed to resolve ref" in caplog.text

    @pytest.mark.asyncio
    async def test_skips_entry_when_clone_fails(self, tmp_path: Path, caplog) -> None:
        """When clone_context raises CloneError, log warning and skip entry."""
        from forge.skills.fetcher import CloneError

        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=CloneError("clone failed"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
            caplog.at_level(logging.WARNING, logger="forge.skills.orchestrator"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        mock_update_lock.assert_not_called()
        assert "Failed to fetch/install" in caplog.text

    @pytest.mark.asyncio
    async def test_multiple_entries_processed_independently(self, tmp_path: Path) -> None:
        """Multiple SkillEntry items are each resolved, checked, and installed."""
        entries = [
            _make_skill_entry(source="https://github.com/org/repo-a.git"),
            _make_skill_entry(source="https://github.com/org/repo-b.git"),
        ]
        jira_client = _make_jira_client(entries)

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        resolve_calls: list = []

        async def fake_resolve(source_url, _ref, **_kwargs):
            resolve_calls.append(source_url)
            return RESOLVED_SHA

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=fake_resolve,
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file"),
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # Both entries should have been processed
        assert len(resolve_calls) == 2
        assert "https://github.com/org/repo-a.git" in resolve_calls
        assert "https://github.com/org/repo-b.git" in resolve_calls

    @pytest.mark.asyncio
    async def test_lock_file_updated_with_correct_path(self, tmp_path: Path) -> None:
        """Lock file is written to skills_dir/skills.lock."""
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        lock_path_used = mock_update_lock.call_args[0][0]
        assert lock_path_used == tmp_path / "skills.lock"

    @pytest.mark.asyncio
    async def test_entry_with_no_ref_skips_resolution(self, tmp_path: Path) -> None:
        """When entry.ref is None, resolve_ref_sha is not called."""
        entry = SkillEntry(source=REPO_URL, ref=None, path="skills")
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ) as mock_resolve,
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # resolve_ref_sha should NOT be called when ref is None
        mock_resolve.assert_not_called()

        # resolved_commit should be empty string (entry.ref or "")
        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.resolved_commit == ""
        assert lock_entry.ref == ""


# ---------------------------------------------------------------------------
# Missing lock file scenario
# ---------------------------------------------------------------------------


class TestEnsureSkillsMissingLockFile:
    @pytest.mark.asyncio
    async def test_runs_without_existing_lock_file(self, tmp_path: Path) -> None:
        """ensure_skills proceeds normally when no lock file exists on disk.

        read_lock_file returns an empty LockFile for missing files, so the
        orchestrator should treat all entries as needing a fetch.
        """
        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        # Confirm no lock file exists in tmp_path
        lock_path = tmp_path / "skills.lock"
        assert not lock_path.exists()

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
        ):
            # No patch on read_lock_file — it runs against the real empty tmp_path
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # Lock update should still be called even without a pre-existing lock file
        mock_update_lock.assert_called_once()
        lock_entry: LockEntry = mock_update_lock.call_args[0][1]
        assert lock_entry.source == REPO_URL
        assert lock_entry.resolved_commit == RESOLVED_SHA


# ---------------------------------------------------------------------------
# Invalid / malformed skills config returned by JiraClient
# ---------------------------------------------------------------------------


class TestEnsureSkillsInvalidConfig:
    @pytest.mark.asyncio
    async def test_handles_empty_list_from_invalid_json(self, tmp_path: Path, caplog) -> None:
        """When JiraClient.get_skills_config returns [] due to invalid JSON, exit early.

        JiraClient.get_skills_config returns an empty list (not None) when the
        stored property value cannot be parsed as valid JSON or a SkillEntry schema.
        ensure_skills should treat this the same as an explicitly empty config and
        log an info message, making no git or file-system calls.
        """
        # Simulate JiraClient returning [] due to JSON/schema parse error
        jira_client = _make_jira_client([])

        with (
            patch("forge.skills.orchestrator.clone_context") as mock_clone,
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            caplog.at_level(logging.INFO, logger="forge.skills.orchestrator"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path)

        # No git clone or lock file write should happen
        mock_clone.assert_not_called()
        mock_update_lock.assert_not_called()
        assert "is empty" in caplog.text


# ---------------------------------------------------------------------------
# skills_install_dir support
# ---------------------------------------------------------------------------


class TestEnsureSkillsInstallDir:
    @pytest.mark.asyncio
    async def test_lock_file_written_to_skills_install_dir(self, tmp_path: Path) -> None:
        """When skills_install_dir is provided, lock file is written there instead of skills_dir."""
        skills = tmp_path / "skills"
        skills.mkdir()
        cache = tmp_path / "cache"

        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                return_value=["skill-a"],
            ),
            patch("forge.skills.orchestrator.update_lock_file") as mock_update_lock,
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, skills, skills_install_dir=cache)

        lock_path_used = mock_update_lock.call_args[0][0]
        assert lock_path_used == cache / "skills.lock"
        assert "skills.lock" not in [p.name for p in skills.iterdir()]

    @pytest.mark.asyncio
    async def test_skills_installed_to_skills_install_dir(self, tmp_path: Path) -> None:
        """When skills_install_dir is provided, skills install to skills_install_dir/{project}."""
        skills = tmp_path / "skills"
        skills.mkdir()
        cache = tmp_path / "cache"

        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        fake_clone = tmp_path / "clone"
        (fake_clone / "skills").mkdir(parents=True)

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=fake_clone)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        captured_target_dir: list[Path] = []

        def fake_install(_source_dir: Path, target_dir: Path) -> list[str]:
            captured_target_dir.append(target_dir)
            return ["skill-a"]

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=True),
            patch("forge.skills.orchestrator.clone_context", return_value=mock_cm),
            patch(
                "forge.skills.orchestrator.install_path_mode",
                side_effect=fake_install,
            ),
            patch("forge.skills.orchestrator.update_lock_file"),
            patch(
                "forge.skills.orchestrator.read_lock_file",
                return_value=LockFile(),
            ),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, skills, skills_install_dir=cache)

        assert captured_target_dir[0] == cache / "myproj"

    @pytest.mark.asyncio
    async def test_skills_install_dir_created_if_missing(self, tmp_path: Path) -> None:
        """skills_install_dir is created automatically when it does not exist."""
        cache = tmp_path / "nonexistent" / "cache"
        assert not cache.exists()

        entry = _make_skill_entry()
        jira_client = _make_jira_client([entry])

        with (
            patch(
                "forge.skills.orchestrator.resolve_ref_sha",
                new=AsyncMock(return_value=RESOLVED_SHA),
            ),
            patch("forge.skills.orchestrator.should_fetch_entry", return_value=False),
            patch("forge.skills.orchestrator.clone_context"),
        ):
            await ensure_skills(PROJECT_KEY, jira_client, tmp_path, skills_install_dir=cache)

        assert cache.is_dir()

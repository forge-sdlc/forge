"""Unit tests for forge.skills.fetcher – resolve_ref_sha and clone helpers."""

import asyncio
import shutil
import tempfile
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.skills.fetcher import (
    CloneError,
    RefResolutionError,
    clone_context,
    clone_skill_package,
    resolve_ref_sha,
    should_fetch_entry,
)
from forge.skills.models import LockEntry, LockFile, SkillEntry

REPO_URL = "https://github.com/example/skills.git"
BRANCH_SHA = "abc123def456abc123def456abc123def456abc1"
TAG_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    """Return a mock asyncio subprocess with the given output."""
    process = MagicMock()
    process.returncode = returncode
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.kill = MagicMock()
    return process


def _patch_exec(process: MagicMock):
    """Return a patch context manager that mocks create_subprocess_exec as async."""
    return patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process))


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


class TestResolveRefShaSuccess:
    @pytest.mark.asyncio
    async def test_branch_ref_returns_sha(self):
        """resolve_ref_sha returns the SHA for a valid branch ref."""
        stdout = f"{BRANCH_SHA}\trefs/heads/main\n".encode()
        process = _make_process(stdout)

        with _patch_exec(process) as mock_exec:
            result = await resolve_ref_sha(REPO_URL, "main")

        mock_exec.assert_called_once_with(
            "git",
            "ls-remote",
            "--",
            REPO_URL,
            "main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert result == BRANCH_SHA

    @pytest.mark.asyncio
    async def test_tag_ref_returns_sha(self):
        """resolve_ref_sha returns the SHA for a valid tag ref."""
        stdout = f"{TAG_SHA}\trefs/tags/v1.0.0\n".encode()
        process = _make_process(stdout)

        with _patch_exec(process):
            result = await resolve_ref_sha(REPO_URL, "v1.0.0")

        assert result == TAG_SHA

    @pytest.mark.asyncio
    async def test_multiple_lines_returns_first_sha(self):
        """When ls-remote returns multiple lines, the first SHA is used."""
        other_sha = "1111111111111111111111111111111111111111"
        stdout = (f"{BRANCH_SHA}\trefs/heads/main\n{other_sha}\trefs/heads/main-old\n").encode()
        process = _make_process(stdout)

        with _patch_exec(process):
            result = await resolve_ref_sha(REPO_URL, "main")

        assert result == BRANCH_SHA

    @pytest.mark.asyncio
    async def test_empty_output_returns_none(self):
        """resolve_ref_sha returns None when git ls-remote returns empty output.

        Empty output indicates the ref is likely a direct commit SHA.
        """
        process = _make_process(stdout=b"", stderr=b"")

        with _patch_exec(process):
            result = await resolve_ref_sha(REPO_URL, "abc123def456")

        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_output_returns_none(self):
        """Whitespace-only stdout is treated the same as empty output."""
        process = _make_process(stdout=b"   \n  \n", stderr=b"")

        with _patch_exec(process):
            result = await resolve_ref_sha(REPO_URL, "abc123def456")

        assert result is None

    @pytest.mark.asyncio
    async def test_custom_timeout_is_forwarded(self):
        """The caller-supplied timeout is passed to asyncio.wait_for."""
        stdout = f"{BRANCH_SHA}\trefs/heads/main\n".encode()
        process = _make_process(stdout)

        with _patch_exec(process), patch("asyncio.wait_for", wraps=asyncio.wait_for) as mock_wait:
            result = await resolve_ref_sha(REPO_URL, "main", timeout=60)

        assert result == BRANCH_SHA
        _, kwargs = mock_wait.call_args
        assert kwargs.get("timeout") == 60


# ---------------------------------------------------------------------------
# Error / failure cases
# ---------------------------------------------------------------------------


class TestResolveRefShaErrors:
    @pytest.mark.asyncio
    async def test_nonzero_exit_code_raises(self):
        """Non-zero returncode from git raises RefResolutionError."""
        process = _make_process(
            stdout=b"",
            stderr=b"fatal: repository not found",
            returncode=128,
        )

        with _patch_exec(process), pytest.raises(RefResolutionError, match="exited with code 128"):
            await resolve_ref_sha(REPO_URL, "main")

    @pytest.mark.asyncio
    async def test_os_error_on_exec_raises(self):
        """OSError when spawning the subprocess raises RefResolutionError."""
        with (
            patch("asyncio.create_subprocess_exec", side_effect=OSError("git not found")),
            pytest.raises(RefResolutionError, match="Failed to start git ls-remote"),
        ):
            await resolve_ref_sha(REPO_URL, "main")

    @pytest.mark.asyncio
    async def test_timeout_raises_and_kills_process(self):
        """asyncio.TimeoutError is converted to RefResolutionError; process is killed."""
        process = MagicMock()
        process.kill = MagicMock()

        async def communicate_forever():
            await asyncio.Event().wait()

        process.communicate = MagicMock(side_effect=communicate_forever)

        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            pytest.raises(RefResolutionError, match="timed out"),
        ):
            await resolve_ref_sha(REPO_URL, "main", timeout=0.01)

        process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_message_includes_url(self):
        """RefResolutionError messages include the source URL for diagnostics."""
        process = _make_process(stdout=b"", stderr=b"connection refused", returncode=1)

        with _patch_exec(process), pytest.raises(RefResolutionError, match=REPO_URL):
            await resolve_ref_sha(REPO_URL, "main")

    @pytest.mark.asyncio
    async def test_stderr_included_in_error_message(self):
        """Stderr from git is included in the raised RefResolutionError."""
        process = _make_process(
            stdout=b"",
            stderr=b"fatal: unable to connect to github.com",
            returncode=128,
        )

        with _patch_exec(process), pytest.raises(RefResolutionError, match="unable to connect"):
            await resolve_ref_sha(REPO_URL, "main")


# ---------------------------------------------------------------------------
# clone_skill_package helpers
# ---------------------------------------------------------------------------

COMMIT_SHA = "a" * 40  # looks like a full commit SHA


def _make_git_process(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio subprocess for a git clone/checkout command."""
    process = MagicMock()
    process.returncode = returncode
    process.communicate = AsyncMock(return_value=(b"", stderr))
    process.kill = MagicMock()
    return process


def _patch_git_exec(side_effects: list) -> patch:
    """Patch asyncio.create_subprocess_exec to return processes in order."""
    return patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=side_effects),
    )


# ---------------------------------------------------------------------------
# clone_skill_package – success paths
# ---------------------------------------------------------------------------


class TestCloneSkillPackageSuccess:
    @pytest.mark.asyncio
    async def test_shallow_clone_branch_succeeds(self, tmp_path):
        """Shallow clone is used for branch refs and the cloned path is returned."""
        process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([process]) as mock_exec,
        ):
            result = await clone_skill_package(REPO_URL, "main")

        assert result == tmp_path
        mock_exec.assert_called_once_with(
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "main",
            "--",
            REPO_URL,
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_shallow_clone_tag_succeeds(self, tmp_path):
        """Shallow clone is used for tag refs."""
        process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([process]) as mock_exec,
        ):
            result = await clone_skill_package(REPO_URL, "v1.2.3")

        assert result == tmp_path
        # First (and only) call must be a shallow clone with --branch v1.2.3
        args = mock_exec.call_args_list[0].args
        assert "--depth" in args
        assert "v1.2.3" in args

    @pytest.mark.asyncio
    async def test_full_clone_for_commit_sha(self, tmp_path):
        """Full clone + checkout is used when ref looks like a commit SHA."""
        clone_process = _make_git_process(returncode=0)
        checkout_process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([clone_process, checkout_process]) as mock_exec,
        ):
            result = await clone_skill_package(REPO_URL, COMMIT_SHA)

        assert result == tmp_path
        # Two git calls: clone then checkout; no shallow clone
        assert mock_exec.call_count == 2
        first_args = mock_exec.call_args_list[0].args
        assert "--depth" not in first_args
        second_args = mock_exec.call_args_list[1].args
        assert "checkout" in second_args
        assert COMMIT_SHA in second_args

    @pytest.mark.asyncio
    async def test_full_clone_no_ref(self, tmp_path):
        """Full clone without checkout is used when ref is None."""
        clone_process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([clone_process]) as mock_exec,
        ):
            result = await clone_skill_package(REPO_URL, None)

        assert result == tmp_path
        assert mock_exec.call_count == 1  # no checkout call
        args = mock_exec.call_args_list[0].args
        assert "--depth" not in args
        assert "clone" in args

    @pytest.mark.asyncio
    async def test_fallback_to_full_clone_on_shallow_failure(self, tmp_path):
        """When shallow clone fails, a full clone + checkout is attempted."""
        shallow_fail = _make_git_process(returncode=128, stderr=b"error: Remote branch not found")
        full_clone = _make_git_process(returncode=0)
        checkout = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree"),  # avoid FS side-effects in the test
            _patch_git_exec([shallow_fail, full_clone, checkout]) as mock_exec,
        ):
            result = await clone_skill_package(REPO_URL, "some-branch")

        assert result == tmp_path
        assert mock_exec.call_count == 3  # shallow attempt + full clone + checkout

    @pytest.mark.asyncio
    async def test_returns_path_object(self, tmp_path):
        """clone_skill_package always returns a pathlib.Path."""
        process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([process]),
        ):
            result = await clone_skill_package(REPO_URL, "main")

        assert isinstance(result, Path)

    @pytest.mark.asyncio
    async def test_temp_dir_in_system_temp(self):
        """mkdtemp is called without a specific dir (uses system temp)."""
        process = _make_git_process(returncode=0)
        system_temp = tempfile.gettempdir()

        with (
            patch("forge.skills.fetcher.tempfile.mkdtemp", wraps=tempfile.mkdtemp) as mock_mkdtemp,
            _patch_git_exec([process]),
        ):
            result = await clone_skill_package(REPO_URL, "main")

        mock_mkdtemp.assert_called_once_with()
        # The created directory should be under the system temp location.
        assert str(result).startswith(system_temp)

        # Clean up the real directory created during this test.
        shutil.rmtree(result, ignore_errors=True)


# ---------------------------------------------------------------------------
# clone_skill_package – error paths
# ---------------------------------------------------------------------------


class TestCloneSkillPackageErrors:
    @pytest.mark.asyncio
    async def test_clone_failure_raises_clone_error(self, tmp_path):
        """CloneError is raised when git clone exits with a non-zero code."""
        fail_process = _make_git_process(returncode=128, stderr=b"fatal: repo not found")

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree"),
            _patch_git_exec([fail_process, fail_process]),  # shallow + full both fail
            pytest.raises(CloneError, match="git clone failed"),
        ):
            await clone_skill_package(REPO_URL, "main")

    @pytest.mark.asyncio
    async def test_checkout_failure_raises_clone_error(self, tmp_path):
        """CloneError is raised when git checkout fails after a successful clone."""
        clone_ok = _make_git_process(returncode=0)
        checkout_fail = _make_git_process(returncode=1, stderr=b"error: pathspec 'bad-ref'")

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree"),
            _patch_git_exec([clone_ok, checkout_fail]),
            pytest.raises(CloneError, match="git checkout"),
        ):
            await clone_skill_package(REPO_URL, COMMIT_SHA)

    @pytest.mark.asyncio
    async def test_os_error_raises_clone_error(self, tmp_path):
        """OSError when spawning git raises CloneError."""
        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree"),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("git not found")),
            pytest.raises(CloneError, match="Failed to start git"),
        ):
            await clone_skill_package(REPO_URL, "main")

    @pytest.mark.asyncio
    async def test_temp_dir_cleaned_up_on_error(self, tmp_path):
        """Temporary directory is removed when cloning raises an error."""
        fail_process = _make_git_process(returncode=128, stderr=b"fatal: not found")

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree") as mock_rmtree,
            _patch_git_exec([fail_process, fail_process]),
            pytest.raises(CloneError),
        ):
            await clone_skill_package(REPO_URL, "main")

        mock_rmtree.assert_called()


# ---------------------------------------------------------------------------
# clone_context – context manager
# ---------------------------------------------------------------------------


class TestCloneContext:
    @pytest.mark.asyncio
    async def test_yields_path_and_cleans_up_on_success(self, tmp_path):
        """clone_context yields the cloned path and removes it afterwards."""
        process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([process]),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            async with clone_context(REPO_URL, "main") as repo:
                yielded = repo

        assert yielded == tmp_path
        mock_rmtree.assert_called_once_with(tmp_path, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception_inside_block(self, tmp_path):
        """clone_context removes the directory even when the body raises."""
        process = _make_git_process(returncode=0)

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            _patch_git_exec([process]),
            patch("shutil.rmtree") as mock_rmtree,
            pytest.raises(RuntimeError, match="boom"),
        ):
            async with clone_context(REPO_URL, "main"):
                raise RuntimeError("boom")

        mock_rmtree.assert_called_once_with(tmp_path, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_clone_error_propagates(self, tmp_path):
        """CloneError raised during cloning propagates out of the context manager."""
        fail_process = _make_git_process(returncode=128, stderr=b"fatal: not found")

        with (
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("shutil.rmtree"),
            _patch_git_exec([fail_process, fail_process]),
            pytest.raises(CloneError),
        ):
            async with clone_context(REPO_URL, "main"):
                pass  # pragma: no cover


# ---------------------------------------------------------------------------
# should_fetch_entry – change detection
# ---------------------------------------------------------------------------

LOCK_SHA = "1111111111111111111111111111111111111111"
NEW_SHA = "2222222222222222222222222222222222222222"
COMMIT_SHA_REF = "aaaa111122223333444455556666777788889999"


def _make_skill_entry(source: str = REPO_URL, ref: str | None = "main") -> SkillEntry:
    """Return a minimal SkillEntry for testing."""
    return SkillEntry(source=source, ref=ref, path="skills/")


def _make_lock_entry(source: str = REPO_URL, resolved_commit: str = LOCK_SHA) -> LockEntry:
    """Return a minimal LockEntry for testing."""
    from datetime import datetime

    return LockEntry(
        source=source,
        ref="main",
        resolved_commit=resolved_commit,
        mode="path",
        path="skills/",
        target="workspace",
        skills=["my-skill"],
        fetched_at=datetime.now(tz=UTC),
    )


def _make_lock(*entries: LockEntry) -> LockFile:
    """Return a LockFile containing the given entries."""
    return LockFile(packages=list(entries))


class TestShouldFetchEntry:
    # ------------------------------------------------------------------
    # No lock entry present
    # ------------------------------------------------------------------

    def test_returns_true_when_no_lock_entry(self):
        """Returns True when the lock file has no entry for the source URL."""
        entry = _make_skill_entry()
        lock = _make_lock()  # empty

        assert should_fetch_entry(entry, resolved_sha=NEW_SHA, lock=lock) is True

    def test_returns_true_when_different_source_in_lock(self):
        """Returns True when the lock only contains entries for other sources."""
        entry = _make_skill_entry(source=REPO_URL)
        other_lock_entry = _make_lock_entry(source="https://github.com/other/repo.git")
        lock = _make_lock(other_lock_entry)

        assert should_fetch_entry(entry, resolved_sha=NEW_SHA, lock=lock) is True

    # ------------------------------------------------------------------
    # resolved_sha is not None (branch / tag refs)
    # ------------------------------------------------------------------

    def test_returns_false_when_resolved_sha_matches_lock(self):
        """Returns False when resolved_sha equals the lock entry's resolved_commit."""
        entry = _make_skill_entry()
        lock_entry = _make_lock_entry(resolved_commit=LOCK_SHA)
        lock = _make_lock(lock_entry)

        assert should_fetch_entry(entry, resolved_sha=LOCK_SHA, lock=lock) is False

    def test_returns_true_when_resolved_sha_differs_from_lock(self):
        """Returns True when resolved_sha differs from the lock entry's resolved_commit."""
        entry = _make_skill_entry()
        lock_entry = _make_lock_entry(resolved_commit=LOCK_SHA)
        lock = _make_lock(lock_entry)

        assert should_fetch_entry(entry, resolved_sha=NEW_SHA, lock=lock) is True

    # ------------------------------------------------------------------
    # resolved_sha is None (commit SHA refs)
    # ------------------------------------------------------------------

    def test_returns_false_when_commit_sha_ref_matches_lock(self):
        """Returns False when entry.ref (commit SHA) matches the lock's resolved_commit."""
        entry = _make_skill_entry(ref=COMMIT_SHA_REF)
        lock_entry = _make_lock_entry(resolved_commit=COMMIT_SHA_REF)
        lock = _make_lock(lock_entry)

        assert should_fetch_entry(entry, resolved_sha=None, lock=lock) is False

    def test_returns_true_when_commit_sha_ref_differs_from_lock(self):
        """Returns True when entry.ref (commit SHA) differs from the lock's resolved_commit."""
        entry = _make_skill_entry(ref=COMMIT_SHA_REF)
        lock_entry = _make_lock_entry(resolved_commit=LOCK_SHA)
        lock = _make_lock(lock_entry)

        assert should_fetch_entry(entry, resolved_sha=None, lock=lock) is True

    # ------------------------------------------------------------------
    # Logging behaviour
    # ------------------------------------------------------------------

    def test_logs_when_no_lock_entry(self, caplog):
        """Logs a debug message when no lock entry is found."""
        import logging

        entry = _make_skill_entry()
        lock = _make_lock()

        with caplog.at_level(logging.DEBUG, logger="forge.skills.fetcher"):
            should_fetch_entry(entry, resolved_sha=NEW_SHA, lock=lock)

        assert any("fetch required" in record.message for record in caplog.records)

    def test_logs_when_skill_is_stale(self, caplog):
        """Logs a debug message when the skill SHA has changed."""
        import logging

        entry = _make_skill_entry()
        lock_entry = _make_lock_entry(resolved_commit=LOCK_SHA)
        lock = _make_lock(lock_entry)

        with caplog.at_level(logging.DEBUG, logger="forge.skills.fetcher"):
            should_fetch_entry(entry, resolved_sha=NEW_SHA, lock=lock)

        assert any("stale" in record.message for record in caplog.records)

    def test_logs_when_skill_is_current(self, caplog):
        """Logs a debug message when the skill is already up-to-date."""
        import logging

        entry = _make_skill_entry()
        lock_entry = _make_lock_entry(resolved_commit=LOCK_SHA)
        lock = _make_lock(lock_entry)

        with caplog.at_level(logging.DEBUG, logger="forge.skills.fetcher"):
            should_fetch_entry(entry, resolved_sha=LOCK_SHA, lock=lock)

        assert any("current" in record.message for record in caplog.records)

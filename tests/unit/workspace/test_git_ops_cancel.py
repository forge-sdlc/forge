"""Cancellation coverage for blocking Git operations owned by effect leases."""

import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import GitCredentials
from forge.workspace.git_ops import GitOperationCancelled, GitOperations
from forge.workspace.manager import Workspace


def test_cancellation_terminates_git_and_prevents_followup_conflict_commands(
    tmp_path: Path,
) -> None:
    cancellation_event = threading.Event()
    workspace = Workspace(
        path=tmp_path / "repo",
        repo_name="acme/widgets",
        branch_name="forge/test",
        ticket_key="TEST-1",
    )
    workspace.path.mkdir()
    git = GitOperations(
        workspace,
        GitCredentials(host="github.com", token="test-token"),
        cancellation_event=cancellation_event,
    )
    process = MagicMock()
    process.pid = 123
    process.poll.return_value = None

    first_poll = True

    def communicate(*, timeout):
        nonlocal first_poll
        if first_poll:
            first_poll = False
            cancellation_event.set()
            raise subprocess.TimeoutExpired(["git", "fetch"], timeout)
        return "", ""

    process.communicate.side_effect = communicate
    with (
        patch("forge.workspace.git_ops.subprocess.Popen", return_value=process) as popen,
        patch("forge.workspace.git_ops.os.killpg") as killpg,
        pytest.raises(GitOperationCancelled),
    ):
        git.push()

    popen.assert_called_once()
    killpg.assert_called_once_with(process.pid, signal.SIGTERM)

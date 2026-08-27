"""Tests for surfacing persisted agent transcript failures."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

from forge.sandbox.runner import (
    ContainerRunner,
    _extract_agent_transcript_errors,
)


def _runner_without_init() -> ContainerRunner:
    runner = object.__new__(ContainerRunner)
    runner.settings = MagicMock(container_keep=False)
    return runner


def test_extracts_errors_from_recent_assistant_turns(tmp_path: Path) -> None:
    history_dir = tmp_path / ".forge" / "history"
    history_dir.mkdir(parents=True)
    transcript = history_dir / "TASK-1.json"
    transcript.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "assistant", "content": "I cannot handle the old step."},
                    {"role": "assistant", "content": "Trying another approach."},
                    {
                        "role": "tool",
                        "content": {"result": "error", "message": "permission denied"},
                    },
                    {
                        "role": "assistant",
                        "content": "The context ended.",
                        "response_metadata": {"stop_reason": "max_tokens"},
                    },
                    {"role": "assistant", "content": "I'm unable to continue."},
                ]
            }
        )
    )

    path, errors = _extract_agent_transcript_errors(
        tmp_path,
        "TASK-1",
        max_assistant_turns=3,
    )

    assert path == transcript
    assert len(errors) == 3
    assert any("tool returned an error" in error for error in errors)
    assert any("stopped with max_tokens" in error for error in errors)
    assert any("assistant refusal" in error for error in errors)
    assert all("old step" not in error for error in errors)


def test_failed_result_logs_transcript_path_and_context(
    tmp_path: Path,
    caplog,
) -> None:
    runner = _runner_without_init()
    transcript = tmp_path / "TASK-1.json"

    with caplog.at_level(logging.ERROR):
        runner._build_container_result(
            exit_code=1,
            stdout_str="",
            stderr_str="agent failed",
            collected_cycles=[],
            container_name="forge-task-1",
            transcript_path=transcript,
            transcript_errors=["assistant refusal (assistant): cannot continue"],
        )

    assert f"Agent transcript: {transcript}" in caplog.text
    assert "assistant refusal (assistant): cannot continue" in caplog.text

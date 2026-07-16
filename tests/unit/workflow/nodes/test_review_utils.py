"""Tests for shared workflow review primitives."""

import json
from unittest.mock import AsyncMock

import pytest

from forge.sandbox.runner import ContainerResult
from forge.workflow.nodes.review_utils import collect_review_output, run_review_container


def test_collect_review_output_reads_ai_text_from_history(tmp_path) -> None:
    history_dir = tmp_path / ".forge" / "history"
    history_dir.mkdir(parents=True)
    (history_dir / "TASK-1-review.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "human", "content": "Review this"},
                    {
                        "role": "ai",
                        "content": [
                            {
                                "type": "text",
                                "text": "verdict: adequate\n\nfeedback: All checks passed.",
                            }
                        ],
                    },
                ]
            }
        )
    )

    output = collect_review_output(
        tmp_path,
        "TASK-1-review",
        "Agent completed task execution",
        "",
    )

    assert output == "verdict: adequate\n\nfeedback: All checks passed."


def test_collect_review_output_falls_back_to_process_output(tmp_path) -> None:
    output = collect_review_output(
        tmp_path,
        "TASK-1-review",
        "verdict: tests_incomplete",
        "feedback: Add tests",
    )

    assert output == "verdict: tests_incomplete\nfeedback: Add tests"


def test_collect_review_output_uses_only_final_ai_message(tmp_path) -> None:
    history_dir = tmp_path / ".forge" / "history"
    history_dir.mkdir(parents=True)
    (history_dir / "TASK-1-review.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "ai", "content": "verdict: tests_incomplete"},
                    {"role": "tool", "content": "tests passed"},
                    {
                        "role": "ai",
                        "content": "verdict: adequate\nfeedback: Final assessment",
                    },
                ]
            }
        )
    )

    output = collect_review_output(tmp_path, "TASK-1-review", "ignored", "")

    assert output == "verdict: adequate\nfeedback: Final assessment"


@pytest.mark.asyncio
async def test_run_review_container_removes_stale_history_before_attempt(tmp_path) -> None:
    history_dir = tmp_path / ".forge" / "history"
    history_dir.mkdir(parents=True)
    history_file = history_dir / "TASK-1-review.json"
    history_file.write_text(
        json.dumps({"messages": [{"role": "ai", "content": "verdict: adequate"}]})
    )

    async def _run(**_kwargs):
        assert not history_file.exists()
        return ContainerResult(
            success=False,
            exit_code=1,
            stdout="verdict: tests_incomplete",
            stderr="review failed",
            error_message="review failed",
        )

    runner = AsyncMock()
    runner.run.side_effect = _run

    _, output = await run_review_container(
        runner,
        workspace_path=tmp_path,
        task_summary="Review",
        task_description="Review changes",
        ticket_key="TASK-1",
        task_key="TASK-1-review",
        repo_name="owner/repo",
    )

    assert output == "verdict: tests_incomplete\nreview failed"

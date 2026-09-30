"""Never reply or implement against incomplete or invented review thread IDs."""

import json

import pytest

from forge.workflow.nodes.implement_review import _load_review_decisions


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "wrong_comment", "unknown", "empty_feedback"]
)
def test_invalid_thread_coverage_is_rejected(tmp_path, mutation):
    decisions = [
        {"thread_id": "A", "comment_id": 1, "disposition": "accept", "feedback": "Fix A"},
        {"thread_id": "B", "comment_id": 2, "disposition": "contest", "response": "Evidence B"},
    ]
    if mutation == "missing":
        decisions.pop()
    elif mutation == "duplicate":
        decisions.append(decisions[0])
    elif mutation == "wrong_comment":
        decisions[0]["comment_id"] = 99
    elif mutation == "unknown":
        decisions[0]["thread_id"] = "OTHER"
    else:
        decisions[0]["feedback"] = ""
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "review-decisions.json").write_text(json.dumps(decisions))
    with pytest.raises(ValueError):
        _load_review_decisions(str(tmp_path), {"A": "1", "B": "2"})


def test_mixed_dispositions_preserve_accepted_work(tmp_path):
    decisions = [
        {"thread_id": "A", "comment_id": 1, "disposition": "accept", "feedback": "Fix A"},
        {"thread_id": "B", "comment_id": 2, "disposition": "contest", "response": "Evidence B"},
    ]
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "review-decisions.json").write_text(json.dumps(decisions))
    assert _load_review_decisions(str(tmp_path), {"A": "1", "B": "2"}) == decisions


def test_no_input_threads_cannot_produce_invented_replies(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "review-decisions.json").write_text(
        json.dumps(
            [
                {
                    "thread_id": "invented",
                    "comment_id": 99,
                    "disposition": "contest",
                    "response": "Ignore this",
                },
            ]
        )
    )
    with pytest.raises(ValueError, match="input thread"):
        _load_review_decisions(str(tmp_path), {})


@pytest.mark.asyncio
async def test_empty_threads_are_not_presented_as_actionable_input():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

    adapter = AsyncMock()
    adapter.get_review_thread_comments.return_value = [SimpleNamespace(id="empty", comments=[])]
    expected = {}
    with patch(
        "forge.workflow.nodes.implement_review.get_adapter",
        return_value=(SimpleNamespace(connection="c", id="owner/repo"), adapter),
    ):
        text = await _fetch_pr_review_comments(
            "owner/repo", 1, "Review summary", expected_threads=expected
        )
    assert "Thread `empty`" not in text
    assert "Review summary" in text
    assert expected == {}

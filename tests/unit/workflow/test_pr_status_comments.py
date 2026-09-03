"""Status-publication behavior at the durable human-review boundary."""

from unittest.mock import AsyncMock, patch

import pytest

from forge.workflow.nodes.human_review import human_review_gate


def _state(**updates):
    return {
        "ticket_key": "TEST-500",
        "current_node": "human_review_gate",
        "current_pr_number": 42,
        "current_pr_url": "https://example.test/pull/42",
        "ci_status": None,
        "pr_created_comment_posted": False,
        **updates,
    }


@pytest.mark.asyncio
async def test_pr_status_and_labels_are_one_required_effect_batch() -> None:
    persistence = AsyncMock()
    with patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions", persistence
    ):
        result = await human_review_gate(_state())

    actions = persistence.await_args.args[1]
    assert "#42" in actions[0].payload["body"]
    assert [item.operation for item in actions] == [
        "jira.comment.create",
        "jira.labels.remove",
        "jira.label.set",
    ]
    assert result["pr_created_comment_posted"] is True
    assert result["is_paused"] is True


@pytest.mark.asyncio
async def test_missing_pr_number_uses_generic_status() -> None:
    persistence = AsyncMock()
    with patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions", persistence
    ):
        await human_review_gate(_state(current_pr_number=None, current_pr_url=None))

    body = persistence.await_args.args[1][0].payload["body"]
    assert "Pull request created" in body
    assert "#" not in body


@pytest.mark.asyncio
async def test_required_publication_failure_prevents_checkpoint_advance() -> None:
    persistence = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    with (
        patch("forge.workflow.nodes.human_review.execute_persistence_actions", persistence),
        pytest.raises(RuntimeError, match="provider unavailable"),
    ):
        await human_review_gate(_state())


@pytest.mark.asyncio
async def test_reentry_does_not_emit_duplicate_publication() -> None:
    persistence = AsyncMock()
    with patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions", persistence
    ):
        result = await human_review_gate(
            _state(pr_created_comment_posted=True, pending_ci_event=True)
        )

    persistence.assert_not_awaited()
    assert result["is_paused"] is True

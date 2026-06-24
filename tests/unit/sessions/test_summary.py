"""Tests for safe Forge session summaries."""

from unittest.mock import AsyncMock

import pytest

from forge.models.workflow import TicketType
from forge.sessions.summary import (
    SessionNotFoundError,
    build_session_summary,
    get_session_summary,
)


def test_build_session_summary_excludes_raw_prompt_and_artifact_data() -> None:
    state = {
        "ticket_key": "TEST-123",
        "ticket_type": TicketType.FEATURE,
        "current_node": "ci_evaluator",
        "is_paused": False,
        "retry_count": 2,
        "created_at": "2026-06-18T10:00:00",
        "updated_at": "2026-06-18T10:05:00",
        "current_repo": "org/repo",
        "current_pr_number": 42,
        "current_pr_url": "https://github.com/org/repo/pull/42",
        "ci_status": "failed",
        "ci_fix_attempts": 1,
        "ci_failed_checks": [{"name": "unit-tests", "details_url": "https://ci.example"}],
        "ai_review_status": "passed",
        "human_review_status": "pending",
        "implemented_tasks": ["TEST-124"],
        "repos_to_process": ["org/repo"],
        "repos_completed": [],
        "prd_content": "raw PRD content",
        "spec_content": "raw spec content",
        "messages": [{"role": "user", "content": "SECRET_PROMPT_VALUE"}],
        "context": {"trace_metadata": {"secret": "do-not-leak"}},
        "generation_context": {"prompt": "SECRET_GENERATION_CONTEXT"},
        "feedback_comment": "raw feedback",
    }

    payload = build_session_summary("test-123", state, logs=["Started", b"CI failed"])
    result = payload.as_dict()

    assert result["summary"]["ticket_key"] == "TEST-123"
    assert result["summary"]["status"] == "running"
    assert result["summary"]["failed_check_names"] == ["unit-tests"]
    assert result["summary"]["artifacts_present"]["prd"] is True
    assert result["summary"]["artifacts_present"]["spec"] is True
    assert result["summary"]["recent_events"] == ["Started", "CI failed"]
    assert result["summary"]["raw_state_exposed"] is False

    serialized = str(result)
    assert "raw PRD content" not in serialized
    assert "raw spec content" not in serialized
    assert "SECRET_PROMPT_VALUE" not in serialized
    assert "SECRET_GENERATION_CONTEXT" not in serialized
    assert "do-not-leak" not in serialized
    assert "raw feedback" not in serialized


def test_build_session_summary_derives_waiting_status() -> None:
    payload = build_session_summary(
        "TEST-123",
        {
            "ticket_key": "TEST-123",
            "current_node": "prd_approval_gate",
            "is_paused": True,
        },
    )

    assert payload.summary.status == "waiting_for_input"


def test_build_session_summary_raises_for_missing_state() -> None:
    with pytest.raises(SessionNotFoundError):
        build_session_summary("TEST-123", None)


@pytest.mark.asyncio
async def test_get_session_summary_reads_checkpoint_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = AsyncMock()
    redis.lrange = AsyncMock(return_value=["latest event"])

    async def fake_get_checkpoint_state(ticket_key: str) -> dict:
        assert ticket_key == "TEST-123"
        return {
            "ticket_key": "TEST-123",
            "current_node": "implementation",
            "current_task_key": "TEST-124",
        }

    monkeypatch.setattr("forge.sessions.summary.get_checkpoint_state", fake_get_checkpoint_state)
    monkeypatch.setattr("forge.sessions.summary.get_redis_client", AsyncMock(return_value=redis))

    payload = await get_session_summary("test-123", logs_limit=3)

    assert payload.summary.ticket_key == "TEST-123"
    assert payload.summary.current_node == "implementation"
    assert payload.summary.current_task_key == "TEST-124"
    redis.lrange.assert_awaited_once_with("forge:logs:TEST-123", 0, 2)


@pytest.mark.asyncio
async def test_get_session_summary_skips_redis_logs_when_limit_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_checkpoint_state(_ticket_key: str) -> dict:
        return {"ticket_key": "TEST-123", "current_node": "implementation"}

    get_redis_client = AsyncMock()
    monkeypatch.setattr("forge.sessions.summary.get_checkpoint_state", fake_get_checkpoint_state)
    monkeypatch.setattr("forge.sessions.summary.get_redis_client", get_redis_client)

    payload = await get_session_summary("TEST-123", logs_limit=0)

    assert payload.summary.recent_events == []
    get_redis_client.assert_not_called()

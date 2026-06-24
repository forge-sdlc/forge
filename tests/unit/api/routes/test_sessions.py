"""Tests for session inspection endpoints."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from forge.main import app
from forge.sessions.models import SessionSummary, SessionSummaryPayload
from forge.sessions.summary import SessionNotFoundError


@pytest.mark.asyncio
async def test_session_summary_returns_safe_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    get_session_summary = AsyncMock(
        return_value=SessionSummaryPayload(
            summary=SessionSummary(
                ticket_key="TEST-123",
                current_node="implementation",
                status="running",
                raw_state_exposed=False,
            ),
            notes=["safe"],
        )
    )
    monkeypatch.setattr("forge.api.routes.sessions.get_session_summary", get_session_summary)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/test-123/summary")

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["ticket_key"] == "TEST-123"
    assert data["summary"]["current_node"] == "implementation"
    assert data["summary"]["raw_state_exposed"] is False
    get_session_summary.assert_awaited_once_with("test-123", logs_limit=0)


@pytest.mark.asyncio
async def test_session_summary_passes_bounded_logs_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    get_session_summary = AsyncMock(
        return_value=SessionSummaryPayload(
            summary=SessionSummary(ticket_key="TEST-123", status="running")
        )
    )
    monkeypatch.setattr("forge.api.routes.sessions.get_session_summary", get_session_summary)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/TEST-123/summary?logs_limit=3")

    assert response.status_code == 200
    get_session_summary.assert_awaited_once_with("TEST-123", logs_limit=3)


@pytest.mark.asyncio
async def test_session_summary_returns_404_for_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_not_found(_ticket_key: str, **_kwargs: object) -> None:
        raise SessionNotFoundError("missing")

    monkeypatch.setattr("forge.api.routes.sessions.get_session_summary", raise_not_found)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/test-404/summary")

    assert response.status_code == 404
    assert response.json()["detail"] == "No Forge session found for TEST-404"


@pytest.mark.asyncio
async def test_session_summary_rejects_large_logs_limit() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/TEST-123/summary?logs_limit=51")

    assert response.status_code == 422

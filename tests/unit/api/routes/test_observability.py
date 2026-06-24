"""Tests for observability data endpoints."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from forge.main import app
from forge.observability.access import ObservabilityUnavailableError


@pytest.mark.asyncio
async def test_ticket_observability_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    get_ticket_observability = AsyncMock(
        return_value={
            "ticket_key": "TEST-123",
            "raw_trace_data_exposed": False,
            "totals": {"total_cost": 0.1},
        }
    )
    monkeypatch.setattr(
        "forge.api.routes.observability.get_ticket_observability",
        get_ticket_observability,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/tickets/test-123?hours=2&limit=3")

    assert response.status_code == 200
    assert response.json()["ticket_key"] == "TEST-123"
    get_ticket_observability.assert_awaited_once_with("test-123", hours=2, limit=3)


@pytest.mark.asyncio
async def test_ticket_observability_returns_422_for_bad_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_value_error(*_args: object, **_kwargs: object) -> None:
        raise ValueError("bad ticket")

    monkeypatch.setattr(
        "forge.api.routes.observability.get_ticket_observability",
        raise_value_error,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/tickets/bad")

    assert response.status_code == 422
    assert response.json()["detail"] == "bad ticket"


@pytest.mark.asyncio
async def test_model_usage_endpoint_returns_503_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_unavailable(*_args: object, **_kwargs: object) -> None:
        raise ObservabilityUnavailableError("Langfuse API keys are not configured")

    monkeypatch.setattr("forge.api.routes.observability.get_model_usage", raise_unavailable)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/model-usage")

    assert response.status_code == 503
    assert response.json()["detail"] == "Langfuse API keys are not configured"


@pytest.mark.asyncio
async def test_ticket_traces_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    get_session_traces = AsyncMock(
        return_value={"ticket_key": "TEST-123", "full_trace_data_exposed": True, "traces": []}
    )
    monkeypatch.setattr(
        "forge.api.routes.observability.get_session_traces",
        get_session_traces,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/observability/tickets/test-123/traces?hours=2&limit=3&full=false"
        )

    assert response.status_code == 200
    assert response.json()["full_trace_data_exposed"] is True
    get_session_traces.assert_awaited_once_with("test-123", hours=2, limit=3, full=False)


@pytest.mark.asyncio
async def test_trace_detail_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    get_trace = AsyncMock(
        return_value={"trace_id": "trace-1", "full_trace_data_exposed": True, "trace": {}}
    )
    monkeypatch.setattr("forge.api.routes.observability.get_trace", get_trace)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/traces/trace-1")

    assert response.status_code == 200
    assert response.json()["trace_id"] == "trace-1"
    get_trace.assert_awaited_once_with("trace-1")


@pytest.mark.asyncio
async def test_workflow_funnel_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    get_workflow_funnel = AsyncMock(return_value={"steps": [{"workflow_step": "plan"}]})
    monkeypatch.setattr(
        "forge.api.routes.observability.get_workflow_funnel",
        get_workflow_funnel,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/workflow-funnel?hours=4")

    assert response.status_code == 200
    assert response.json()["steps"][0]["workflow_step"] == "plan"
    get_workflow_funnel.assert_awaited_once_with(hours=4, limit=50)


@pytest.mark.asyncio
async def test_observability_health_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    get_observability_health = AsyncMock(
        return_value={"metadata_coverage": {"missing_session_id": "0"}}
    )
    monkeypatch.setattr(
        "forge.api.routes.observability.get_observability_health",
        get_observability_health,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/observability/health?hours=6")

    assert response.status_code == 200
    assert response.json()["metadata_coverage"]["missing_session_id"] == "0"
    get_observability_health.assert_awaited_once_with(hours=6)

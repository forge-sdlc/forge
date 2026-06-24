"""Tests for read-only Langfuse observability access helpers."""

from types import SimpleNamespace

import pytest

from forge.observability import access


class FakeTraceApi:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.get_calls: list[str] = []

    async def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return {
            "data": [
                {
                    "id": "trace-1",
                    "name": "implement",
                    "sessionId": "TEST-123",
                    "timestamp": "2026-06-18T10:00:00Z",
                    "metadata": {"workflow_step": "implement_task", "project_id": "TEST"},
                    "totalCost": 0.12,
                    "inputTokens": 100,
                    "outputTokens": 25,
                    "totalTokens": 125,
                    "latency": 3.5,
                }
            ]
        }

    async def get(self, trace_id: str):
        self.get_calls.append(trace_id)
        return {
            "id": trace_id,
            "input": {"prompt": "full input"},
            "output": {"text": "full output"},
            "observations": [{"id": "obs-1"}],
        }


class FakeObservationsApi:
    def __init__(self) -> None:
        self.get_many_calls: list[dict] = []

    async def get_many(self, **kwargs):
        self.get_many_calls.append(kwargs)
        return {
            "data": [
                {
                    "providedModelName": "claude",
                    "totalCost": 0.1,
                    "usageDetails": {"input": 100, "output": 25, "total": 125},
                    "latency": 2.5,
                }
            ]
        }


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.trace = FakeTraceApi()
        self.observations = FakeObservationsApi()
        self.legacy = SimpleNamespace(observations_v1=self.observations)
        self.async_api = SimpleNamespace(trace=self.trace, legacy=self.legacy)


@pytest.fixture
def fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> FakeLangfuseClient:
    client = FakeLangfuseClient()
    monkeypatch.setattr(access, "get_langfuse_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_ticket_observability_uses_langfuse_trace_api(fake_langfuse: FakeLangfuseClient) -> None:
    result = await access.get_ticket_observability("test-123", hours=12, limit=5)

    assert result["ticket_key"] == "TEST-123"
    assert result["source"] == "langfuse_api"
    assert result["raw_trace_data_exposed"] is False
    assert result["totals"]["trace_count"] == 1
    assert result["steps"][0]["workflow_step"] == "implement_task"
    assert result["recent_traces"][0]["trace_id"] == "trace-1"
    assert fake_langfuse.trace.list_calls[0]["session_id"] == "TEST-123"
    assert fake_langfuse.trace.list_calls[0]["fields"] == "core,metrics,io"


@pytest.mark.asyncio
async def test_ticket_observability_rejects_invalid_ticket_key() -> None:
    with pytest.raises(ValueError, match="ticket_key"):
        await access.get_ticket_observability("not a key")


@pytest.mark.asyncio
async def test_model_usage_uses_langfuse_metrics_api(fake_langfuse: FakeLangfuseClient) -> None:
    result = await access.get_model_usage(hours=999999, limit=999999)

    assert result["window_hours"] == 24 * 90
    assert result["models"][0]["model"] == "claude"
    assert result["models"][0]["calls"] == 1
    assert result["models"][0]["input_tokens"] == 100
    assert fake_langfuse.observations.get_many_calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_health_checks_metadata_coverage(fake_langfuse: FakeLangfuseClient) -> None:
    result = await access.get_observability_health(hours=24)

    assert result["source"] == "langfuse_api"
    assert result["metadata_coverage"]["sampled_trace_count"] == 1
    assert result["metadata_coverage"]["missing_project_id"] == 0
    assert result["metadata_coverage"]["missing_ticket_type"] == 1
    assert fake_langfuse.trace.list_calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_session_traces_hydrates_full_traces(fake_langfuse: FakeLangfuseClient) -> None:
    result = await access.get_session_traces("TEST-123", limit=1)

    assert result["full_trace_data_exposed"] is True
    assert result["traces"][0]["input"]["prompt"] == "full input"
    assert fake_langfuse.trace.get_calls == ["trace-1"]


@pytest.mark.asyncio
async def test_get_trace_returns_full_trace(fake_langfuse: FakeLangfuseClient) -> None:
    result = await access.get_trace("trace-1")

    assert result["full_trace_data_exposed"] is True
    assert result["trace"]["output"]["text"] == "full output"
    assert fake_langfuse.trace.get_calls == ["trace-1"]

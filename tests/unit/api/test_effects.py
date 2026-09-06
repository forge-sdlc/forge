from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.api.routes.effects import get_effect_service, get_timeline_store
from forge.domain import (
    EffectCommand,
    EffectResult,
    EffectResultStatus,
    ResourceIdentity,
    WorkflowIdentity,
)
from forge.effects import EffectExecutorRegistry, EffectService, InMemoryEffectJournal
from forge.main import app
from forge.read_models import InMemoryExecutionTimelineStore


def _command() -> EffectCommand:
    return EffectCommand(
        effect_id="effect-1",
        idempotency_key="effect-1",
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        operation="test.write",
        target=ResourceIdentity(resource_type="issue", external_id="FORGE-1"),
    )


@pytest.mark.asyncio
async def test_effect_history_requires_configured_operator_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "forge.api.routes.effects.get_settings",
        lambda: SimpleNamespace(effect_operator_token=None),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/effects/workflow/FORGE-1")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_operator_can_inspect_workflow_effect_history(monkeypatch) -> None:
    journal = InMemoryEffectJournal()
    service = EffectService(journal, EffectExecutorRegistry())
    await service.submit(_command())
    app.dependency_overrides[get_effect_service] = lambda: service
    monkeypatch.setattr(
        "forge.api.routes.effects.get_settings",
        lambda: SimpleNamespace(effect_operator_token=SecretStr("operator-secret")),
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/effects/workflow/FORGE-1",
                headers={"Authorization": "Bearer operator-secret"},
            )
        assert response.status_code == 200
        assert response.json()[0]["command"]["idempotency_key"] == "effect-1"
    finally:
        app.dependency_overrides.pop(get_effect_service, None)


@pytest.mark.asyncio
async def test_authenticated_effect_replay_is_durable_operator_timeline_evidence(monkeypatch) -> None:
    journal = InMemoryEffectJournal()
    service = EffectService(journal, EffectExecutorRegistry())
    command = _command()
    await service.submit(command)
    await journal.complete(
        EffectResult(
            effect_id=command.effect_id,
            idempotency_key=command.idempotency_key,
            status=EffectResultStatus.TERMINAL_FAILURE,
            completed_at=datetime.now(UTC),
            error_message="provider unavailable",
        )
    )
    timeline = InMemoryExecutionTimelineStore()
    app.dependency_overrides[get_effect_service] = lambda: service
    app.dependency_overrides[get_timeline_store] = lambda: timeline
    monkeypatch.setattr(
        "forge.api.routes.effects.get_settings",
        lambda: SimpleNamespace(effect_operator_token=SecretStr("operator-secret")),
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/effects/effect-1/replay",
                headers={"Authorization": "Bearer operator-secret"},
            )
        assert response.status_code == 200
        records = await timeline.list("FORGE-1")
        assert len(records) == 1
        assert records[0].kind == "operator_action"
        assert records[0].details["target"] == "FORGE-1"
        assert records[0].details["result_status"] == "pending"
    finally:
        app.dependency_overrides.pop(get_effect_service, None)
        app.dependency_overrides.pop(get_timeline_store, None)


@pytest.mark.asyncio
async def test_unauthenticated_effect_replay_does_not_write_operator_evidence(monkeypatch) -> None:
    timeline = InMemoryExecutionTimelineStore()
    app.dependency_overrides[get_timeline_store] = lambda: timeline
    monkeypatch.setattr(
        "forge.api.routes.effects.get_settings",
        lambda: SimpleNamespace(effect_operator_token=SecretStr("operator-secret")),
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/effects/effect-1/replay")
        assert response.status_code == 401
        assert await timeline.list("FORGE-1") == ()
    finally:
        app.dependency_overrides.pop(get_timeline_store, None)

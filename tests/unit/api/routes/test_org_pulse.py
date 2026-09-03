from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.api.routes import org_pulse as route
from forge.main import app


@pytest.mark.asyncio
async def test_org_pulse_endpoint_requires_operator_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "forge.api.routes.executions.get_settings",
        lambda: SimpleNamespace(forge_operator_token=SecretStr("pulse-secret")),
    )
    monkeypatch.setattr(route, "load_execution_read_model", AsyncMock(return_value=None))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/org-pulse/workflows/FORGE-7")
        missing = await client.get(
            "/api/v1/org-pulse/workflows/FORGE-7",
            headers={"Authorization": "Bearer pulse-secret"},
        )
    assert unauthorized.status_code == 401
    assert missing.status_code == 404

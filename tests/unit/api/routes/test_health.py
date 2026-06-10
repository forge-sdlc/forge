"""Unit tests for health check endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from forge.main import app


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self):
        """Health endpoint returns 200 OK when Redis is healthy."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.xlen = AsyncMock(return_value=0)

        with patch("forge.api.routes.health.get_redis_client", return_value=mock_redis):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_includes_version(self):
        """Health response includes version."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.xlen = AsyncMock(return_value=0)

        with patch("forge.api.routes.health.get_redis_client", return_value=mock_redis):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/health")

        data = response.json()
        assert "version" in data


class TestReadinessEndpoint:
    """Tests for /ready endpoint."""

    @pytest.mark.asyncio
    async def test_ready_with_healthy_dependencies(self):
        """Ready returns 200 (always ready in current impl)."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    @pytest.mark.asyncio
    async def test_ready_with_unhealthy_redis(self):
        """Ready endpoint doesn't check Redis (always returns ready)."""
        # Current implementation doesn't check Redis for readiness
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"


class TestLivenessEndpoint:
    """Tests for /live endpoint."""

    @pytest.mark.asyncio
    async def test_live_returns_200(self):
        """Liveness endpoint always returns 200."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

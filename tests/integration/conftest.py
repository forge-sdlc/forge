"""Integration test fixtures - tests with mocked external services."""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
import respx
from httpx import ASGITransport, AsyncClient, Response
from testcontainers.redis import RedisContainer

from forge.config import Settings
from forge.main import app


@pytest.fixture
def mock_settings() -> Settings:
    """Create settings for integration tests."""
    return Settings(
        redis_url="redis://localhost:6379/1",  # Use different DB for integration tests
        jira_base_url="https://test.atlassian.net",
        jira_api_token="test-token",
        jira_user_email="test@example.com",
        jira_webhook_secret="test-webhook-secret",
        github_token="test-github-token",
        github_webhook_secret="test-github-webhook-secret",
        anthropic_api_key="test-anthropic-key",
    )


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client for FastAPI integration tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def jira_mock() -> Generator[respx.MockRouter, None, None]:
    """Create a respx mock router for Jira API calls."""
    with respx.mock(base_url="https://test.atlassian.net/rest/api/3") as router:
        # Default responses for common endpoints
        router.get("/issue/TEST-123").mock(
            return_value=Response(
                200,
                json={
                    "id": "10001",
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Feature"},
                        "status": {"name": "New"},
                        "summary": "Test Feature",
                        "description": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Raw requirements"}],
                                }
                            ],
                        },
                        "labels": ["forge:managed"],
                        "project": {"key": "TEST"},
                    },
                },
            )
        )
        yield router


@pytest.fixture
def github_mock() -> Generator[respx.MockRouter, None, None]:
    """Create a respx mock router for GitHub API calls."""
    with respx.mock(base_url="https://api.github.com") as router:
        # Default responses for common endpoints
        router.get("/repos/org/repo/pulls/42").mock(
            return_value=Response(
                200,
                json={
                    "number": 42,
                    "state": "open",
                    "title": "TEST-123: Test PR",
                    "head": {"ref": "feature/TEST-123", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "mergeable": True,
                },
            )
        )
        yield router


@pytest.fixture
def anthropic_mock() -> Generator[respx.MockRouter, None, None]:
    """Create a respx mock router for Anthropic API calls."""
    with respx.mock(base_url="https://api.anthropic.com") as router:
        # Mock messages endpoint
        router.post("/v1/messages").mock(
            return_value=Response(
                200,
                json={
                    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Generated content from Claude.",
                        }
                    ],
                    "model": "claude-sonnet-4-20250514",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                },
            )
        )
        yield router


@pytest.fixture
def mock_redis() -> MagicMock:
    """Create a mock Redis client for integration tests."""
    mock = MagicMock()
    mock.ping = AsyncMock(return_value=True)
    mock.xlen = AsyncMock(return_value=0)
    mock.xadd = AsyncMock(return_value="1234567890-0")
    mock.xreadgroup = AsyncMock(return_value=[])
    mock.xack = AsyncMock(return_value=1)
    mock.xgroup_create = AsyncMock()
    mock.xgroup_destroy = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock(return_value=True)
    mock.delete = AsyncMock(return_value=1)
    mock.exists = AsyncMock(return_value=0)
    return mock


def compute_jira_webhook_signature(payload: bytes, secret: str) -> str:
    """Compute Jira webhook signature for testing.

    Args:
        payload: Webhook payload bytes.
        secret: Webhook secret.

    Returns:
        Signature string.
    """
    import hashlib
    import hmac

    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def compute_github_webhook_signature(payload: bytes, secret: str) -> str:
    """Compute GitHub webhook signature for testing.

    Args:
        payload: Webhook payload bytes.
        secret: Webhook secret.

    Returns:
        Signature string with sha256= prefix.
    """
    import hashlib
    import hmac

    signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={signature}"


# Redis testcontainers fixtures for integration tests with real Redis


def _container_runtime_available() -> bool:
    """Check if Podman/Docker is available for testcontainers."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def redis_container():
    """Start a Redis Stack container for the test session.

    This fixture uses testcontainers to spin up a real Redis Stack instance.
    Redis Stack includes RediSearch module required by langgraph-checkpoint-redis.
    The container is shared across all tests in the session for efficiency.
    """
    if not _container_runtime_available():
        pytest.skip("Podman/Docker not available - skipping testcontainers tests")

    with RedisContainer("redis/redis-stack-server:latest") as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container) -> str:
    """Get the Redis URL from the container."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncGenerator[aioredis.Redis, None]:
    """Create an async Redis client connected to the test container.

    Each test gets a fresh client. The database is flushed before each test.
    """
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushdb()  # Clean state for each test
    try:
        yield client
    finally:
        await client.flushdb()  # Clean up after test
        await client.aclose()

"""Host-agent graph limit precedence and bounded Jira property reads."""

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _policy():
    return importlib.import_module("forge.agent_recursion_policy")


@pytest.mark.parametrize("raw", [None, 75, 100])
def test_project_limit_validator_accepts_null_or_positive_integer(raw: int | None) -> None:
    assert _policy().validate_project_agent_recursion_limit(raw, "AISOS") == raw


@pytest.mark.parametrize("raw", [True, False, "75", 1.5, [], {}, 0, -1])
def test_project_limit_validator_rejects_invalid_non_null_values(raw: object) -> None:
    with pytest.raises(ValueError, match="forge.agent_recursion_limit for AISOS"):
        _policy().validate_project_agent_recursion_limit(raw, "AISOS")


@pytest.mark.asyncio
async def test_no_ticket_uses_global_limit_without_jira() -> None:
    jira = AsyncMock()
    settings = MagicMock(agent_recursion_limit=150)

    assert (
        await _policy().resolve_agent_recursion_limit_for_project(settings, None, jira=jira) == 150
    )
    jira.get_project_property.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [(None, 150), (75, 75), (150, 150)])
async def test_project_override_or_null_global_fallback(
    raw: int | None, expected: int, caplog: pytest.LogCaptureFixture
) -> None:
    jira = AsyncMock()
    jira.get_project_property.return_value = raw
    settings = MagicMock(agent_recursion_limit=150)

    result = await _policy().resolve_agent_recursion_limit_for_project(settings, "AISOS", jira=jira)

    assert result == expected
    jira.get_project_property.assert_awaited_once_with("AISOS", "forge.agent_recursion_limit")
    assert ("source=global" if raw is None else "source=project") in caplog.text


@pytest.mark.asyncio
async def test_changed_project_property_takes_effect_on_next_run() -> None:
    jira = AsyncMock()
    jira.get_project_property.side_effect = [75, 80]
    settings = MagicMock(agent_recursion_limit=150)

    first = await _policy().resolve_agent_recursion_limit_for_project(settings, "AISOS", jira=jira)
    second = await _policy().resolve_agent_recursion_limit_for_project(settings, "AISOS", jira=jira)

    assert (first, second) == (75, 80)
    assert jira.get_project_property.await_count == 2


def _http_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://jira.example.test/project/AISOS/properties/limit")
    response = httpx.Response(
        status,
        request=request,
        headers={"Retry-After": retry_after} if retry_after is not None else {},
    )
    return httpx.HTTPStatusError("secret provider detail", request=request, response=response)


@pytest.mark.asyncio
async def test_temporary_jira_read_retries_then_uses_project_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(policy, "_sleep", record_sleep, raising=False)
    jira = AsyncMock()
    jira.get_project_property.side_effect = [_http_error(503), 75]

    result = await policy.read_agent_recursion_property(jira, "AISOS")

    assert result == 75
    assert jira.get_project_property.await_count == 2
    assert delays == [0.5]


@pytest.mark.asyncio
async def test_jira_read_stops_after_three_transient_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(policy, "_sleep", record_sleep, raising=False)
    jira = AsyncMock()
    jira.get_project_property.side_effect = _http_error(503)

    with pytest.raises(RuntimeError, match="forge.agent_recursion_limit.*AISOS.*3 attempts") as exc:
        await policy.read_agent_recursion_property(jira, "AISOS")

    assert "secret provider detail" not in str(exc.value)
    assert jira.get_project_property.await_count == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_jira_auth_or_request_error_fails_without_retry(status: int) -> None:
    jira = AsyncMock()
    jira.get_project_property.side_effect = _http_error(status)

    with pytest.raises(RuntimeError, match=f"forge.agent_recursion_limit.*AISOS.*{status}"):
        await _policy().read_agent_recursion_property(jira, "AISOS")

    jira.get_project_property.assert_awaited_once()


@pytest.mark.asyncio
async def test_jira_404_is_successful_unset() -> None:
    jira = AsyncMock()
    jira.get_project_property.side_effect = _http_error(404)

    assert await _policy().read_agent_recursion_property(jira, "AISOS") is None
    jira.get_project_property.assert_awaited_once()


@pytest.mark.asyncio
async def test_jira_retry_after_inside_budget_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(policy, "_sleep", record_sleep, raising=False)
    jira = AsyncMock()
    jira.get_project_property.side_effect = [_http_error(429, "2"), None]

    assert await policy.read_agent_recursion_property(jira, "AISOS") is None
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_jira_retry_after_beyond_total_budget_fails_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    sleep = AsyncMock()
    monkeypatch.setattr(policy, "_sleep", sleep, raising=False)
    jira = AsyncMock()
    jira.get_project_property.side_effect = _http_error(429, "31")

    with pytest.raises(RuntimeError, match="forge.agent_recursion_limit.*AISOS.*budget"):
        await policy.read_agent_recursion_property(jira, "AISOS")

    jira.get_project_property.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_jira_read_enforces_per_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    timeouts: list[float] = []

    async def timed_attempt(awaitable: object, timeout: float) -> object:
        timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(policy, "_await_with_timeout", timed_attempt, raising=False)
    monkeypatch.setattr(policy, "_sleep", AsyncMock(), raising=False)
    jira = AsyncMock()

    with pytest.raises(RuntimeError, match="3 attempts"):
        await policy.read_agent_recursion_property(jira, "AISOS")

    assert len(timeouts) == 3
    assert all(0 < timeout <= 10 for timeout in timeouts)


@pytest.mark.asyncio
async def test_jira_read_stops_at_total_elapsed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    clock = {"now": 0.0}
    monkeypatch.setattr(policy, "_monotonic", lambda: clock["now"], raising=False)
    jira = AsyncMock()

    async def exceed_budget(*_args: object) -> None:
        clock["now"] = 31.0
        raise httpx.ConnectError("secret transport detail")

    jira.get_project_property.side_effect = exceed_budget
    with pytest.raises(RuntimeError, match="forge.agent_recursion_limit.*AISOS.*budget"):
        await policy.read_agent_recursion_property(jira, "AISOS")

    jira.get_project_property.assert_awaited_once()


@pytest.mark.asyncio
async def test_jira_cancellation_and_malformed_response_do_not_retry() -> None:
    for error in (asyncio.CancelledError(), ValueError("malformed property JSON")):
        jira = AsyncMock()
        jira.get_project_property.side_effect = error
        with pytest.raises(type(error)):
            await _policy().read_agent_recursion_property(jira, "AISOS")
        jira.get_project_property.assert_awaited_once()


@pytest.mark.asyncio
async def test_owned_jira_client_closes_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    settings = MagicMock(agent_recursion_limit=100)
    clients: list[AsyncMock] = []

    def make_client(**_kwargs: object) -> AsyncMock:
        client = AsyncMock()
        client.get_project_property.side_effect = [75] if not clients else [ValueError("bad")]
        clients.append(client)
        return client

    monkeypatch.setattr("forge.integrations.jira.client.JiraClient", make_client)
    assert await policy.resolve_agent_recursion_limit_for_project(settings, "AISOS") == 75
    with pytest.raises(ValueError):
        await policy.resolve_agent_recursion_limit_for_project(settings, "AISOS")
    assert len(clients) == 2
    for client in clients:
        client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_injected_jira_client_is_never_closed() -> None:
    settings = MagicMock(agent_recursion_limit=100)
    jira = AsyncMock()
    jira.get_project_property.return_value = 75

    assert (
        await _policy().resolve_agent_recursion_limit_for_project(settings, "AISOS", jira=jira)
        == 75
    )
    jira.close.assert_not_awaited()

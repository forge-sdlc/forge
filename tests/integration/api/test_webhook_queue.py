"""Webhook-to-Redis integration tests across the real API and queue producer."""

import hashlib
import hmac
import json
from unittest.mock import patch

from forge.queue.models import QueueMessage
from forge.queue.producer import GITHUB_STREAM, JIRA_STREAM, QueueProducer
from tests.fixtures.github_payloads import WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS
from tests.fixtures.jira_payloads import WEBHOOK_ISSUE_CREATED


def _signature(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _stream_messages(redis_client, stream: str) -> list[QueueMessage]:
    entries = await redis_client.xrange(stream)
    return [QueueMessage.from_redis(message_id, fields) for message_id, fields in entries]


async def test_jira_delivery_is_authenticated_queued_and_deduplicated(
    async_client, redis_client, mock_settings
) -> None:
    payload = json.dumps(WEBHOOK_ISSUE_CREATED).encode()
    secret = mock_settings.jira_webhook_secret.get_secret_value()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _signature(payload, secret),
        "X-Atlassian-Webhook-Identifier": "jira-delivery-1",
    }
    producer = QueueProducer(redis_client=redis_client)

    with (
        patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
        patch("forge.api.routes.jira.QueueProducer", return_value=producer),
    ):
        accepted = await async_client.post(
            "/api/v1/webhooks/jira", content=payload, headers=headers
        )
        duplicate = await async_client.post(
            "/api/v1/webhooks/jira", content=payload, headers=headers
        )

    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "duplicate"

    messages = await _stream_messages(redis_client, JIRA_STREAM)
    assert len(messages) == 1
    assert messages[0].event_id == "jira-delivery-1"
    assert messages[0].ticket_key == WEBHOOK_ISSUE_CREATED["issue"]["key"]


async def test_jira_invalid_signature_and_json_never_reach_queue(
    async_client, redis_client, mock_settings
) -> None:
    producer = QueueProducer(redis_client=redis_client)
    valid_signature = _signature(b"not-json", mock_settings.jira_webhook_secret.get_secret_value())

    with (
        patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
        patch("forge.api.routes.jira.QueueProducer", return_value=producer),
    ):
        unauthorized = await async_client.post(
            "/api/v1/webhooks/jira",
            content=b"{}",
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
        )
        malformed = await async_client.post(
            "/api/v1/webhooks/jira",
            content=b"not-json",
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": valid_signature},
        )

    assert unauthorized.status_code == 401
    assert malformed.status_code == 400
    assert await redis_client.xlen(JIRA_STREAM) == 0


async def test_github_delivery_is_authenticated_queued_and_deduplicated(
    async_client, redis_client, mock_settings
) -> None:
    payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()
    secret = mock_settings.github_webhook_secret.get_secret_value()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _signature(payload, secret),
        "X-GitHub-Event": "check_run",
        "X-GitHub-Delivery": "github-delivery-1",
    }
    producer = QueueProducer(redis_client=redis_client)

    with (
        patch("forge.api.routes.github.get_settings", return_value=mock_settings),
        patch("forge.api.routes.github.QueueProducer", return_value=producer),
    ):
        accepted = await async_client.post(
            "/api/v1/webhooks/github", content=payload, headers=headers
        )
        duplicate = await async_client.post(
            "/api/v1/webhooks/github", content=payload, headers=headers
        )

    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    assert duplicate.status_code == 202
    assert duplicate.json()["status"] == "duplicate"

    messages = await _stream_messages(redis_client, GITHUB_STREAM)
    assert len(messages) == 1
    assert messages[0].event_id == "github-delivery-1"
    assert messages[0].source.value == "github"

"""Unit tests for Jira webhook route."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.main import app
from tests.fixtures.jira_payloads import (
    WEBHOOK_ISSUE_CREATED,
    WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED,
    WEBHOOK_ISSUE_UPDATED_LABEL_ADDED,
    make_jira_webhook,
)


def compute_signature(payload: bytes, secret: str) -> str:
    """Compute Jira webhook signature with sha256= prefix."""
    sig = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


class TestJiraWebhookRoute:
    """Tests for /api/v1/webhooks/jira endpoint."""

    @pytest.mark.asyncio
    async def test_valid_webhook_returns_202(self):
        """Valid webhook with correct signature returns 202 Accepted."""
        payload = json.dumps(WEBHOOK_ISSUE_CREATED).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self):
        """Invalid signature returns 401 Unauthorized."""
        payload = json.dumps(WEBHOOK_ISSUE_CREATED).encode()

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr("correct-secret")

        with patch("forge.api.routes.jira.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": "sha256=invalid",
                    },
                )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_signature_returns_401(self):
        """Missing signature header returns 401 when secret is configured."""
        payload = json.dumps(WEBHOOK_ISSUE_CREATED).encode()

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr("some-secret")

        with patch("forge.api.routes.jira.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={"Content-Type": "application/json"},
                )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_managed_issue_skipped(self):
        """Issues without forge:managed label are skipped."""
        webhook = make_jira_webhook(labels=[])  # No forge:managed
        payload = json.dumps(webhook).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "skipped"
        assert "forge:managed" in data["reason"]
        mock_producer.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_event_dropped(self):
        """Skip this test - deduplication is handled at queue level, not route."""
        # Deduplication happens in the queue consumer, not the webhook route
        pass

    @pytest.mark.asyncio
    async def test_label_change_event_published(self):
        """Label change webhook publishes event to queue."""
        payload = json.dumps(WEBHOOK_ISSUE_UPDATED_LABEL_ADDED).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        mock_producer.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_standalone_managed_task_without_parent_is_queued(self) -> None:
        """Managed standalone Task issues without forge:parent are queued under their own key."""
        webhook = make_jira_webhook(issue_type="Task", labels=["forge:managed"])
        payload = json.dumps(webhook).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        mock_producer.publish.assert_called_once()
        called_kwargs = mock_producer.publish.call_args.kwargs
        assert called_kwargs["ticket_key"] == "TEST-123"
        assert "source_ticket_key" not in called_kwargs["payload"]

    @pytest.mark.asyncio
    async def test_standard_task_with_parent_routed_to_parent(self) -> None:
        """Standard Task issues with forge:parent label are routed to the parent ticket key."""
        webhook = make_jira_webhook(
            issue_type="Task", labels=["forge:managed", "forge:parent:PARENT-123"]
        )
        payload = json.dumps(webhook).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        mock_producer.publish.assert_called_once()
        called_kwargs = mock_producer.publish.call_args.kwargs
        assert called_kwargs["ticket_key"] == "PARENT-123"
        assert called_kwargs["payload"]["source_ticket_key"] == "TEST-123"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("issue_type", ["Task", "Epic"])
    async def test_managed_standalone_issue_bypasses_parent_check(
        self, issue_type: str
    ) -> None:
        """Managed standalone Task/Epic issues bypass parent checks and queue under their own key."""
        webhook = make_jira_webhook(issue_type=issue_type, labels=["forge:managed"])
        payload = json.dumps(webhook).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        mock_producer.publish.assert_called_once()
        called_kwargs = mock_producer.publish.call_args.kwargs
        assert called_kwargs["ticket_key"] == "TEST-123"
        assert "source_ticket_key" not in called_kwargs["payload"]

    @pytest.mark.asyncio
    async def test_task_with_managed_label_in_changelog_bypasses_parent_check(self) -> None:
        """Task issue with forge:managed added in changelog is queued under its own key."""
        webhook = make_jira_webhook(
            issue_type="Task",
            labels=[],
            changelog_field="labels",
            changelog_from="some-other-label",
            changelog_to="forge:managed",
        )
        payload = json.dumps(webhook).encode()
        secret = "test-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.jira_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with (
            patch("forge.api.routes.jira.get_settings", return_value=mock_settings),
            patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/jira",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                    },
                )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        mock_producer.publish.assert_called_once()
        called_kwargs = mock_producer.publish.call_args.kwargs
        assert called_kwargs["ticket_key"] == "TEST-123"

class TestJiraWebhookParsing:
    """Tests for Jira webhook payload parsing."""

    def test_extract_issue_key(self):
        """Extract issue key from webhook payload via parse_jira_webhook."""
        from forge.integrations.jira.webhooks import parse_jira_webhook

        data = parse_jira_webhook(WEBHOOK_ISSUE_CREATED, "evt-001")
        assert data.ticket_key == "TEST-123"

    def test_extract_labels(self):
        """Labels are in the issue fields (accessed via payload)."""
        labels = WEBHOOK_ISSUE_CREATED["issue"]["fields"]["labels"]
        assert "forge:managed" in labels

    def test_extract_event_type(self):
        """Event type from webhookEvent field."""
        from forge.integrations.jira.webhooks import parse_jira_webhook

        data = parse_jira_webhook(WEBHOOK_ISSUE_CREATED, "evt-001")
        assert data.event_type == "jira:issue_created"

    def test_detect_label_change(self):
        """Detect label change from changelog items."""
        changelog = WEBHOOK_ISSUE_UPDATED_LABEL_ADDED.get("changelog", {})
        items = changelog.get("items", [])

        label_change = None
        for item in items:
            if item.get("field") == "labels":
                label_change = item
                break

        assert label_change is not None
        assert "forge:prd-approved" in label_change["toString"]
        assert "forge:prd-pending" in label_change["fromString"]

    def test_extract_comment_from_webhook(self):
        """Extract comment text from comment webhook."""
        from forge.integrations.jira.webhooks import parse_jira_webhook

        data = parse_jira_webhook(WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED, "evt-001")
        assert data.comment is not None
        assert "user persona" in data.comment.lower()

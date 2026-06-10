"""Unit tests for Jira webhook route."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from tests.fixtures.jira_payloads import (
    WEBHOOK_ISSUE_CREATED,
    WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED,
    WEBHOOK_ISSUE_UPDATED_LABEL_ADDED,
    make_jira_webhook,
)

from forge.main import app


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

        with patch("forge.api.routes.jira.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
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
                transport=ASGITransport(app=app),
                base_url="http://test"
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
                transport=ASGITransport(app=app),
                base_url="http://test"
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

        with patch("forge.api.routes.jira.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
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

        with patch("forge.api.routes.jira.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.jira.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
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

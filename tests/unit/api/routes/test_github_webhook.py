"""Unit tests for GitHub webhook route."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from tests.fixtures.github_payloads import (
    WEBHOOK_CHECK_RUN_COMPLETED_FAILURE,
    WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS,
    WEBHOOK_PULL_REQUEST_REVIEW_APPROVED,
)

from forge.main import app


def compute_signature(payload: bytes, secret: str) -> str:
    """Compute GitHub webhook signature with sha256= prefix."""
    sig = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


class TestGitHubWebhookRoute:
    """Tests for /api/v1/webhooks/github endpoint."""

    @pytest.mark.asyncio
    async def test_valid_webhook_returns_202(self):
        """Valid webhook with correct signature returns 202 Accepted."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.github.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/webhooks/github",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": signature,
                            "X-GitHub-Event": "check_run",
                            "X-GitHub-Delivery": "delivery-123",
                        },
                    )

        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self):
        """Invalid signature returns 401 Unauthorized."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr("correct-secret")

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": "sha256=invalid",
                        "X-GitHub-Event": "check_run",
                    },
                )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_signature_returns_401(self):
        """Missing signature header returns 401 when secret is configured."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr("some-secret")

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "check_run",
                    },
                )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_check_run_success_published(self):
        """Check run success event is published."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.github.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/webhooks/github",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": signature,
                            "X-GitHub-Event": "check_run",
                            "X-GitHub-Delivery": "delivery-123",
                        },
                    )

        assert response.status_code == 202
        mock_producer.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_run_failure_published(self):
        """Check run failure event is published."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_FAILURE).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.github.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/webhooks/github",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": signature,
                            "X-GitHub-Event": "check_run",
                            "X-GitHub-Delivery": "delivery-123",
                        },
                    )

        assert response.status_code == 202
        mock_producer.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_pr_review_approved_published(self):
        """PR review approved event is published."""
        payload = json.dumps(WEBHOOK_PULL_REQUEST_REVIEW_APPROVED).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = MagicMock()
        mock_settings.github_webhook_secret = SecretStr(secret)

        mock_producer = MagicMock()
        mock_producer.publish = AsyncMock()

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            with patch("forge.api.routes.github.QueueProducer", return_value=mock_producer):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/webhooks/github",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": signature,
                            "X-GitHub-Event": "pull_request_review",
                            "X-GitHub-Delivery": "delivery-123",
                        },
                    )

        assert response.status_code == 202


class TestGitHubWebhookParsing:
    """Tests for GitHub webhook payload parsing via parse_github_webhook."""

    def test_extract_pr_number(self):
        """Extract PR number from check_run webhook."""
        from forge.integrations.github.webhooks import parse_github_webhook

        data = parse_github_webhook(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS, "check_run", "evt-001")
        assert data.pr_number == 42

    def test_extract_check_conclusion(self):
        """Extract check run conclusion."""
        from forge.integrations.github.webhooks import parse_github_webhook

        success_data = parse_github_webhook(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS, "check_run", "evt-001")
        failure_data = parse_github_webhook(WEBHOOK_CHECK_RUN_COMPLETED_FAILURE, "check_run", "evt-002")

        assert success_data.check_conclusion == "success"
        assert failure_data.check_conclusion == "failure"

    def test_extract_repository(self):
        """Extract repository from webhook."""
        from forge.integrations.github.webhooks import parse_github_webhook

        data = parse_github_webhook(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS, "check_run", "evt-001")
        assert data.repo_full_name == "org/repo"

    def test_extract_review_state(self):
        """Extract review state from PR review webhook."""
        # Review state is in the raw payload's review object
        review = WEBHOOK_PULL_REQUEST_REVIEW_APPROVED.get("review", {})
        assert review.get("state") == "approved"

    def test_extract_ci_output(self):
        """Extract CI output from failed check run (via payload)."""
        check_run = WEBHOOK_CHECK_RUN_COMPLETED_FAILURE.get("check_run", {})
        output = check_run.get("output", {})
        text = output.get("text", "")

        assert "test_login_validation" in text
        assert "AssertionError" in text

    def test_detect_forge_branch(self):
        """Detect ticket key from branch name."""
        from forge.integrations.github.webhooks import parse_github_webhook

        data = parse_github_webhook(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS, "check_run", "evt-001")
        # Branch is feature/TEST-123, should extract ticket key
        assert data.ticket_key == "TEST-123"

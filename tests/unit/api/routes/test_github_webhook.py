"""Unit tests for GitHub webhook route."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.config import get_settings
from forge.integrations.source_control.contracts import (
    Connection,
    EventKind,
    Provider,
    RepositoryRef,
    ResolvedRepository,
)
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError
from forge.main import app
from tests.fixtures.github_payloads import (
    WEBHOOK_CHECK_RUN_COMPLETED_FAILURE,
    WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS,
    WEBHOOK_PULL_REQUEST_REVIEW_APPROVED,
)


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
    async def test_valid_webhook_returns_202(self, mock_settings):
        """Valid webhook with correct signature returns 202 Accepted."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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
    async def test_invalid_signature_returns_401(self, mock_settings):
        """Invalid signature returns 401 Unauthorized."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr("correct-secret")}
        )

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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
    async def test_missing_signature_returns_401(self, mock_settings):
        """Missing signature header returns 401 when secret is configured."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr("some-secret")}
        )

        with patch("forge.api.routes.github.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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
    async def test_check_run_success_published(self, mock_settings):
        """Check run success event is published."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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
        mock_producer.publish_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_run_failure_published(self, mock_settings):
        """Check run failure event is published."""
        payload = json.dumps(WEBHOOK_CHECK_RUN_COMPLETED_FAILURE).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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
        mock_producer.publish_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_pr_review_approved_published(self, mock_settings):
        """PR review approved event is published."""
        payload = json.dumps(WEBHOOK_PULL_REQUEST_REVIEW_APPROVED).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
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

    @pytest.mark.asyncio
    async def test_webhook_delivery_comment_from_app_bot(self, mock_settings):
        """Standard App bot comment webhook delivery is received and queued successfully."""
        comment_payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/42"},
            },
            "comment": {
                "id": 999,
                "body": "Some comment body from App bot",
                "user": {"login": "forge-bot[bot]", "type": "Bot"},
            },
            "repository": {
                "id": 123456,
                "name": "repo",
                "full_name": "org/repo",
            },
            "sender": {"login": "forge-bot[bot]", "type": "Bot"},
        }
        payload = json.dumps(comment_payload).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-comment-bot",
                    },
                )

        assert response.status_code == 202
        mock_producer.publish_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_delivery_comment_from_custom_dev_pat(self, mock_settings):
        """Custom dev PAT user comment webhook delivery is received and queued successfully."""
        comment_payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/42"},
            },
            "comment": {
                "id": 1000,
                "body": "!This is human feedback or custom dev PAT comment.",
                "user": {"login": "dev-user", "type": "User"},
            },
            "repository": {
                "id": 123456,
                "name": "repo",
                "full_name": "org/repo",
            },
            "sender": {"login": "dev-user", "type": "User"},
        }
        payload = json.dumps(comment_payload).encode()
        secret = "test-github-webhook-secret"
        signature = compute_signature(payload, secret)

        mock_settings = mock_settings.model_copy(
            update={"github_webhook_secret": SecretStr(secret)}
        )

        mock_producer = MagicMock()
        mock_producer.publish_event = AsyncMock()

        with (
            patch("forge.api.routes.github.get_settings", return_value=mock_settings),
            patch("forge.api.routes.github.QueueProducer", return_value=mock_producer),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": signature,
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-comment-pat",
                    },
                )

        assert response.status_code == 202
        mock_producer.publish_event.assert_called_once()


class TestWebhookRouteNormalizedEventCutover:
    """Route behavior post-cutover: publishes a NormalizedEvent via
    QueueProducer.publish_event, using GitHubAdapter.verify_webhook/.parse_webhook
    and the process-wide Registry instead of the old parse_github_webhook path.
    """

    def _sign(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self):
        # get_settings() is a process-wide @lru_cache'd singleton (forge.config),
        # already primed by an earlier import of forge.main in this test session.
        # These tests rely on monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", ...) to
        # change what the route sees, which only works if the cache is cleared so
        # the next call rebuilds Settings from the current environment. Cleared
        # again on teardown so this doesn't leak a stale secret into other test
        # modules that call the real get_settings().
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_pull_request_opened_publishes_normalized_event(self, monkeypatch):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "html_url": "https://github.com/acme/payments/pull/42",
                "title": "PROJ-123 Add feature",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "feature/PROJ-123"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/payments"},
            "sender": {"login": "octocat", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        published = {}

        async def fake_publish_event(_self, event, ticket_key):
            published["event"] = event
            published["ticket_key"] = ticket_key
            return "msg-1"

        with patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-1",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert published["ticket_key"] == "PROJ-123"
        assert published["event"].kind == EventKind.CR_OPENED
        assert published["event"].repo_ref.namespace == "acme/payments"

    @pytest.mark.asyncio
    async def test_push_event_extracts_ticket_key_from_ref(self, monkeypatch):
        """Push events carry no change_request, so the ticket key must be
        recovered from the pushed branch ref instead of being dropped."""
        payload = {
            "ref": "refs/heads/forge/PROJ-123-add-feature",
            "repository": {"full_name": "acme/payments"},
            "sender": {"login": "octocat", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        published = {}

        async def fake_publish_event(_self, event, ticket_key):
            published["event"] = event
            published["ticket_key"] = ticket_key
            return "msg-push-1"

        with patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "push",
                        "X-GitHub-Delivery": "delivery-push-1",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert published["ticket_key"] == "PROJ-123"
        assert published["event"].kind == EventKind.PUSH

    @pytest.mark.asyncio
    async def test_check_suite_without_pr_stub_extracts_ticket_key_from_head_branch(
        self, monkeypatch
    ):
        """check_suite can fire before GitHub attaches a pull_requests stub;
        the ticket key must still be recovered from head_branch so CI that
        starts before PR creation isn't dropped."""
        payload = {
            "action": "completed",
            "check_suite": {
                "head_branch": "forge/PROJ-456-add-feature",
                "head_sha": "abc123",
                "pull_requests": [],
            },
            "repository": {"full_name": "acme/payments"},
            "sender": {"login": "octocat", "type": "Bot"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        published = {}

        async def fake_publish_event(_self, event, ticket_key):
            published["event"] = event
            published["ticket_key"] = ticket_key
            return "msg-check-suite-1"

        with patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "check_suite",
                        "X-GitHub-Delivery": "delivery-check-suite-1",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert published["ticket_key"] == "PROJ-456"
        assert published["event"].kind == EventKind.CHECK_UPDATED

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
        body = json.dumps({"action": "opened"}).encode()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": "sha256=invalid",
                },
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_per_connection_webhook_secret_used_for_verification(self, monkeypatch):
        """A repo resolved to a connection with its own webhook_secret_env is
        verified against that connection's secret, not the global default --
        and a signature computed with the global secret must be rejected."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "html_url": "x",
                "title": "t",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "a"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/custom-repo"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        global_secret = "global-secret"
        custom_secret = "custom-connection-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", global_secret)
        monkeypatch.setenv("CUSTOM_ORG_WEBHOOK_SECRET", custom_secret)

        resolved = ResolvedRepository(
            repo_ref=RepositoryRef(
                id="custom-repo",
                provider=Provider.GITHUB,
                connection="custom-org",
                namespace="acme/custom-repo",
                default_branch="main",
                change_request_mode="fork",
            ),
            connection=Connection(
                name="custom-org",
                provider=Provider.GITHUB,
                base_url="https://api.github.com",
                credential_env="GITHUB_TOKEN",
                webhook_secret_env="CUSTOM_ORG_WEBHOOK_SECRET",
            ),
        )

        async def fake_publish_event(_self, _event, _ticket_key):
            return "msg-1"

        with (
            patch(
                "forge.integrations.source_control.registry.Registry.resolve",
                return_value=resolved,
            ),
            patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                accepted = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-custom-1",
                        "X-Hub-Signature-256": self._sign(body, custom_secret),
                    },
                )
                rejected = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-custom-2",
                        "X-Hub-Signature-256": self._sign(body, global_secret),
                    },
                )

        assert accepted.status_code == 202
        assert rejected.status_code == 401

    @pytest.mark.asyncio
    async def test_unmanaged_repository_acks_and_drops(self, monkeypatch):
        """resolve() raising NotFoundError -- the repo isn't managed by Forge --
        must ack (202) and drop, not error."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "html_url": "x",
                "title": "t",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "a"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "unmanaged/repo"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        with patch(
            "forge.integrations.source_control.registry.Registry.resolve",
            side_effect=NotFoundError("unmanaged"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-3",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert response.json()["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_misconfigured_connection_acks_and_drops(self, monkeypatch):
        """adapter.parse_webhook's internal resolver.resolve() call can raise
        ProviderConfigError (distinct from NotFoundError) when a repo resolves
        to a connection that isn't usable (e.g. no credential configured).
        This must also ack (202) and drop, not fall through to a 500 that
        GitHub would treat as retryable."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "html_url": "x",
                "title": "t",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "a"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "misconfigured/repo"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        with patch(
            "forge.integrations.source_control.registry.Registry.resolve",
            side_effect=ProviderConfigError("credential not set"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-3b",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert response.json()["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, monkeypatch):
        """A malformed body must surface as 400, not the generic 500 handler.

        adapter.parse_webhook raises json.JSONDecodeError internally when given
        a malformed body, but the route decodes the body itself first
        specifically to preserve the pre-cutover 400-on-malformed-JSON behavior.
        """
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
        body = b"{not valid json"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/webhooks/github",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-4",
                    "X-Hub-Signature-256": self._sign(body, secret),
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid JSON payload"

    @pytest.mark.asyncio
    async def test_duplicate_event_returns_duplicate_status(self, monkeypatch):
        """publish_event returning None (a duplicate delivery id already seen)
        must surface as a 202 "duplicate" ack, not silently look like success."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "html_url": "x",
                "title": "t",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "a"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/payments"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        with patch(
            "forge.queue.producer.QueueProducer.publish_event",
            AsyncMock(return_value=None),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "delivery-5",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        assert response.json()["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_missing_delivery_header_fallback_flows_into_published_event(self, monkeypatch):
        """When X-GitHub-Delivery is absent, the route's generated fallback id
        must be the id actually published, not just echoed in the response --
        otherwise the queued event and what's logged/returned diverge."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "html_url": "x",
                "title": "t",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "a"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "acme/payments"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        secret = "test-secret"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

        published = {}

        async def fake_publish_event(_self, event, _ticket_key):
            published["event"] = event
            return "msg-1"

        with patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "X-GitHub-Event": "pull_request",
                        "X-Hub-Signature-256": self._sign(body, secret),
                    },
                )

        assert response.status_code == 202
        response_event_id = response.json()["event_id"]
        assert response_event_id != ""
        assert published["event"].id == response_event_id

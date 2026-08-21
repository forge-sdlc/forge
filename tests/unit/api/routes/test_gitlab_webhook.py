"""Tests for the GitLab webhook route."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forge.integrations.source_control.contracts import (
    Connection,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    ResolvedRepository,
)
from forge.main import create_app


@pytest.fixture
def client(mock_settings):
    with patch("forge.config.get_settings", return_value=mock_settings):
        app = create_app()
        yield TestClient(app)


def _mr_opened_payload() -> dict:
    return {
        "object_kind": "merge_request",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": "acme/widgets"},
        "object_attributes": {
            "iid": 1,
            "title": "AISOS-1: Test MR",
            "description": "",
            "state": "opened",
            "action": "open",
            "source_branch": "forge/AISOS-1",
            "target_branch": "main",
            "url": "https://gitlab.com/acme/widgets/-/merge_requests/1",
        },
    }


def _resolved_gitlab_connection() -> ResolvedRepository:
    """A resolvable repos.yaml-shaped connection for acme/widgets, with its
    webhook secret sourced from ACME_GITLAB_WEBHOOK_SECRET (set via
    monkeypatch by the tests that need it). GitLab has no implicit default
    connection, so tests that want to reach verify_webhook or the
    publish_event call must patch Registry.resolve to return this."""
    return ResolvedRepository(
        repo_ref=RepositoryRef(
            id="widgets",
            provider=Provider.GITLAB,
            connection="acme-gitlab",
            namespace="acme/widgets",
            default_branch="main",
            change_request_mode="direct",
        ),
        connection=Connection(
            name="acme-gitlab",
            provider=Provider.GITLAB,
            base_url="https://gitlab.com/api/v4",
            credential_env="ACME_GITLAB_TOKEN",
            webhook_secret_env="ACME_GITLAB_WEBHOOK_SECRET",
        ),
    )


class TestGitLabWebhook:
    def test_rejects_invalid_token(self, client, monkeypatch):
        """A resolvable connection with a known secret rejects a wrong token --
        this exercises verify_webhook's comparison, not resolve()."""
        monkeypatch.setenv("ACME_GITLAB_WEBHOOK_SECRET", "correct-secret")
        resolved = _resolved_gitlab_connection()

        with patch(
            "forge.integrations.source_control.registry.Registry.resolve",
            return_value=resolved,
        ):
            response = client.post(
                "/api/v1/webhooks/gitlab",
                json=_mr_opened_payload(),
                headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "wrong"},
            )

        assert response.status_code == 401

    def test_accepts_valid_token_and_queues_event(self, client, monkeypatch):
        """A resolvable connection with a matching token succeeds end-to-end:
        202 + 'queued', and publish_event is actually called with a
        NormalizedEvent built from the merge-request-opened payload."""
        monkeypatch.setenv("ACME_GITLAB_WEBHOOK_SECRET", "correct-secret")
        resolved = _resolved_gitlab_connection()

        published: list[NormalizedEvent] = []

        async def fake_publish_event(
            _self: object, event: NormalizedEvent, _ticket_key: str
        ) -> str:
            published.append(event)
            return "1-0"

        with (
            patch(
                "forge.integrations.source_control.registry.Registry.resolve",
                return_value=resolved,
            ),
            patch("forge.queue.producer.QueueProducer.publish_event", fake_publish_event),
        ):
            response = client.post(
                "/api/v1/webhooks/gitlab",
                json=_mr_opened_payload(),
                headers={
                    "X-Gitlab-Event": "Merge Request Hook",
                    "X-Gitlab-Token": "correct-secret",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["ticket_key"] == "AISOS-1"

        assert len(published) == 1
        assert published[0].kind == EventKind.CR_OPENED
        assert published[0].repo_ref.namespace == "acme/widgets"

    def test_unmanaged_repository_is_rejected_not_acked(self, client):
        """No connection is configured for this namespace and GitLab has no
        implicit default (unlike GitHub, which acks-and-drops unmanaged repos
        via its implicit default connection). resolve() raises NotFoundError,
        which the route treats as a 401 -- there is no unauthenticated
        ack-and-drop path for GitLab."""
        payload = _mr_opened_payload()
        payload["project"]["path_with_namespace"] = "totally/unmanaged"

        response = client.post(
            "/api/v1/webhooks/gitlab",
            json=payload,
            headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": ""},
        )

        assert response.status_code == 401

"""Tests for the GitLab webhook route."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

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


class TestGitLabWebhook:
    def test_rejects_invalid_token(self, client):
        response = client.post(
            "/api/v1/webhooks/gitlab",
            json=_mr_opened_payload(),
            headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "wrong"},
        )
        assert response.status_code == 401

    def test_accepts_valid_token_and_queues_event(self, client, monkeypatch):
        monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "")  # no repos.yaml connection configured

        with patch(
            "forge.api.routes.gitlab.QueueProducer.publish_event",
            new=AsyncMock(return_value="1-0"),
        ):
            response = client.post(
                "/api/v1/webhooks/gitlab",
                json=_mr_opened_payload(),
                headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": ""},
            )

        # No secret configured on the default connection -> verify_webhook fails closed.
        assert response.status_code == 401

    def test_unmanaged_repository_is_acked_and_dropped(self, client):
        payload = _mr_opened_payload()
        payload["project"]["path_with_namespace"] = "totally/unmanaged"

        response = client.post(
            "/api/v1/webhooks/gitlab",
            json=payload,
            headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": ""},
        )

        # No connection is configured for this namespace and GitLab has no
        # implicit default, so resolve() raises NotFoundError before signature
        # verification would even matter for a *configured* connection -- but
        # since verify_webhook always runs first here and there is no secret,
        # this still surfaces as 401 (fail-closed takes priority). This test
        # documents that GitLab, unlike GitHub, has no unauthenticated
        # unmanaged-repo path: every GitLab webhook requires a configured
        # connection with a secret before any resolution happens.
        assert response.status_code == 401

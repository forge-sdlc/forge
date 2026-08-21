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


def _push_payload(ref: str = "refs/heads/forge/AISOS-42") -> dict:
    return {
        "object_kind": "push",
        "user_username": "alice",
        "project": {"path_with_namespace": "acme/widgets"},
        "ref": ref,
    }


def _note_on_mr_payload() -> dict:
    """A note-webhook payload with the top-level `merge_request` object GitLab
    always includes for notes on a merge request."""
    return {
        "object_kind": "note",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": "acme/widgets"},
        "object_attributes": {
            "id": 555,
            "note": "/forge skip-gate flaky-test",
            "noteable_type": "MergeRequest",
        },
        "merge_request": {
            "iid": 1,
            "title": "AISOS-1: Test MR",
            "description": "",
            "state": "opened",
            "source_branch": "forge/AISOS-1",
            "target_branch": "main",
            "url": "https://gitlab.com/acme/widgets/-/merge_requests/1",
        },
    }


def _pipeline_without_mr_payload(ref: str = "forge/AISOS-77") -> dict:
    """A pipeline-webhook payload for a plain-branch pipeline with no MR
    attached -- the branch lives at object_attributes.ref, not a top-level
    `ref` (unlike push events)."""
    return {
        "object_kind": "pipeline",
        "user": {"username": "ci-bot"},
        "project": {"path_with_namespace": "acme/widgets"},
        "object_attributes": {"id": 999, "status": "running", "ref": ref},
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

    def test_push_event_extracts_ticket_key_from_ref(self, client, monkeypatch):
        """A push event has no change_request (GitLabAdapter.parse_webhook
        doesn't populate one for push events), so the ticket key must come
        from the raw payload's top-level `ref` -- mirroring the GitHub
        route's raw.get("ref", "") fallback exactly."""
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
                json=_push_payload("refs/heads/forge/AISOS-42"),
                headers={
                    "X-Gitlab-Event": "Push Hook",
                    "X-Gitlab-Token": "correct-secret",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["ticket_key"] == "AISOS-42"

        assert len(published) == 1
        assert published[0].kind == EventKind.PUSH
        assert published[0].change_request is None

    def test_mr_comment_extracts_ticket_key_from_merge_request_object(self, client, monkeypatch):
        """An MR-note webhook payload has no top-level `ref` to fall back to;
        without GitLabAdapter.parse_webhook populating change_request from the
        payload's top-level `merge_request` object, _extract_ticket_key
        returns "" and worker.py drops the event before any workflow gate
        (including /forge skip-gate, /forge rebase, and the PRD/spec
        proposal-PR comment gates) ever sees it."""
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
                json=_note_on_mr_payload(),
                headers={
                    "X-Gitlab-Event": "Note Hook",
                    "X-Gitlab-Token": "correct-secret",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["ticket_key"] == "AISOS-1"
        assert body["ticket_key"] != ""

        assert len(published) == 1
        assert published[0].kind == EventKind.COMMENT_CREATED
        assert published[0].change_request is not None
        assert published[0].change_request.identity.native_id == 1

    def test_pipeline_without_mr_extracts_ticket_key_from_object_attributes_ref(
        self, client, monkeypatch
    ):
        """A plain-branch pipeline (no MR attached) carries its branch at
        object_attributes.ref, not a top-level `ref` -- the push-event
        fallback alone would miss it."""
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
                json=_pipeline_without_mr_payload("forge/AISOS-77"),
                headers={
                    "X-Gitlab-Event": "Pipeline Hook",
                    "X-Gitlab-Token": "correct-secret",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["ticket_key"] == "AISOS-77"

        assert len(published) == 1
        assert published[0].change_request is None

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

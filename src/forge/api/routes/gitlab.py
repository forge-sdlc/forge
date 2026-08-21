"""GitLab webhook endpoint for receiving repository events."""

import json
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request, status

from forge.api.routes.metrics import (
    record_webhook_failed,
    record_webhook_processed,
    record_webhook_received,
)
from forge.config import get_settings
from forge.integrations.source_control.contracts import NormalizedEvent, Provider
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError
from forge.integrations.source_control.gitlab.adapter import GitLabAdapter
from forge.integrations.source_control.registry import get_registry, resolve_env_value
from forge.observability.config import get_tracer
from forge.observability.context import get_correlation_id
from forge.queue.producer import QueueProducer

logger = logging.getLogger(__name__)
tracer = get_tracer("forge.api.gitlab")

router = APIRouter(prefix="/api/v1/webhooks", tags=["gitlab"])

TICKET_PATTERN = re.compile(r"([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)


def _extract_ticket_key(event: NormalizedEvent) -> str:
    """Extract a Jira ticket key from a NormalizedEvent (mirrors the GitHub
    route's helper: prefer the change request's title/branch, no raw-payload
    fallback since GitLab's push payload carries no equivalent branch field
    Forge currently reads)."""
    if event.change_request is not None:
        for text in (event.change_request.title, event.change_request.source_branch):
            match = TICKET_PATTERN.search(text or "")
            if match:
                return match.group(1).upper()
    return ""


@router.post(
    "/gitlab",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Event accepted for processing"},
        401: {"description": "Invalid webhook token"},
    },
)
async def receive_gitlab_webhook(
    request: Request,
    x_gitlab_event: str = Header(default=""),
    x_gitlab_token: str = Header(default=""),
) -> dict[str, str]:
    """Receive and queue GitLab webhook events.

    Every GitLab connection must be explicitly configured in repos.yaml with
    a webhook_secret_env (GitLab has no implicit default connection, unlike
    GitHub) -- there is no unauthenticated "unmanaged repository" ack-and-drop
    path here the way the GitHub route has for its implicit default.
    """
    settings = get_settings()
    span = tracer.start_span(
        "gitlab_webhook",
        attributes={
            "correlation_id": get_correlation_id(),
            "forge.source": "gitlab",
            "forge.event_type": x_gitlab_event,
        },
    )

    try:
        body = await request.body()

        try:
            sniff_payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            sniff_payload = {}
        repo_namespace = sniff_payload.get("project", {}).get("path_with_namespace", "")

        registry = get_registry()
        try:
            connection = registry.resolve(repo_namespace, provider_hint=Provider.GITLAB).connection
        except (NotFoundError, ProviderConfigError):
            span.set_attribute("error", True)
            span.set_attribute("error.type", "auth_failure")
            logger.warning("GitLab webhook for unconfigured repository %r rejected", repo_namespace)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
            )

        webhook_secret = (
            resolve_env_value(connection.webhook_secret_env, settings)
            if connection.webhook_secret_env
            else None
        )
        adapter = GitLabAdapter(connection=connection, webhook_secret=webhook_secret)

        if not await adapter.verify_webhook({"X-Gitlab-Token": x_gitlab_token}, body):
            span.set_attribute("error", True)
            span.set_attribute("error.type", "auth_failure")
            logger.warning("Invalid GitLab webhook token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
            )

        try:
            event = await adapter.parse_webhook({"X-Gitlab-Event": x_gitlab_event}, body, registry)
        except (NotFoundError, ProviderConfigError):
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "unmanaged_repository")
            record_webhook_received(source="gitlab", event_type=x_gitlab_event)
            return {"status": "ignored", "event_id": ""}

        ticket_key = _extract_ticket_key(event)
        span.set_attribute("forge.ticket_key", ticket_key)
        span.set_attribute("forge.event_id", event.id)

        record_webhook_received(source="gitlab", event_type=x_gitlab_event)

        producer = QueueProducer()
        message_id = await producer.publish_event(event, ticket_key)

        if message_id is None:
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "duplicate event")
            return {"status": "duplicate", "event_id": event.id, "ticket_key": ticket_key}

        span.set_attribute("forge.queued", True)
        logger.info(
            f"GitLab webhook queued: event_id={event.id}, kind={event.kind}, repo={event.repo_ref.namespace}"
        )
        record_webhook_processed(source="gitlab", event_type=x_gitlab_event)

        return {"status": "queued", "event_id": event.id, "ticket_key": ticket_key}

    except HTTPException:
        raise
    except Exception as e:
        span.set_attribute("error", True)
        span.set_attribute("error.type", "internal_error")
        logger.error(f"Failed to process GitLab webhook: {e}")
        record_webhook_failed(
            source="gitlab", event_type=x_gitlab_event, error_type="internal_error"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process webhook"
        )
    finally:
        span.end()

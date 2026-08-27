"""GitHub webhook endpoint for receiving repository events."""

import hashlib
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from forge.api.routes.metrics import (
    record_webhook_failed,
    record_webhook_processed,
    record_webhook_received,
)
from forge.config import get_settings
from forge.integrations.source_control.contracts import Connection, NormalizedEvent, Provider
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError
from forge.integrations.source_control.github.adapter import GitHubAdapter
from forge.integrations.source_control.registry import get_registry, resolve_env_value
from forge.observability.config import get_tracer
from forge.observability.context import get_correlation_id
from forge.queue.producer import QueueProducer

_DEFAULT_GITHUB_CONNECTION = Connection(
    name="default-github",
    provider=Provider.GITHUB,
    base_url="https://api.github.com",
    credential_env="GITHUB_TOKEN",
    webhook_secret_env="GITHUB_WEBHOOK_SECRET",
)

logger = logging.getLogger(__name__)
tracer = get_tracer("forge.api.github")

router = APIRouter(prefix="/api/v1/webhooks", tags=["github"])

TICKET_PATTERN = re.compile(r"([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)


def _extract_ticket_key(event: NormalizedEvent) -> str:
    """Extract a Jira ticket key from a NormalizedEvent.

    Prefers the change request's title/branch when one is present (PR, review,
    and check events carrying a pull_requests stub). Falls back to whatever
    branch name the raw payload carries when there is no change request:
    a push event's ref, or a check_suite/check_run event that fired before
    GitHub attached a pull_requests stub to it (both still carry head_branch).
    """
    if event.change_request is not None:
        for text in (event.change_request.title, event.change_request.source_branch):
            match = TICKET_PATTERN.search(text or "")
            if match:
                return match.group(1).upper()
    raw = event.raw
    branch_sources = (
        raw.get("ref", ""),
        raw.get("check_suite", {}).get("head_branch", ""),
        raw.get("check_run", {}).get("check_suite", {}).get("head_branch", ""),
    )
    for text in branch_sources:
        match = TICKET_PATTERN.search(str(text))
        if match:
            return match.group(1).upper()
    return ""


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Event accepted for processing"},
        400: {"description": "Invalid payload"},
        401: {"description": "Invalid webhook signature"},
    },
)
async def receive_github_webhook(
    request: Request,
    x_github_event: str = Header(default="ping"),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> dict[str, str]:
    """Receive and queue GitHub webhook events.

    This endpoint:
    1. Validates webhook signature
    2. Parses the webhook payload into a NormalizedEvent
    3. Queues the event for async processing
    4. Returns immediately (<500ms target)

    Args:
        request: FastAPI request object.
        x_github_event: Event type (e.g., "push", "pull_request").
        x_github_delivery: Unique delivery ID from GitHub.
        x_hub_signature_256: HMAC signature for verification.

    Returns:
        Acknowledgment with event ID.
    """
    settings = get_settings()
    span = tracer.start_span(
        "github_webhook",
        attributes={
            "correlation_id": get_correlation_id(),
            "forge.source": "github",
            "forge.event_type": x_github_event,
        },
    )

    try:
        # Handle ping events
        if x_github_event == "ping":
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "ping")
            return {"status": "pong", "event_id": x_github_delivery}

        # Read raw body for signature verification
        body = await request.body()

        try:
            sniff_payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            sniff_payload = {}
        repo_namespace = sniff_payload.get("repository", {}).get("full_name", "")

        registry = get_registry()
        connection = _DEFAULT_GITHUB_CONNECTION
        if repo_namespace:
            try:
                connection = registry.resolve(
                    repo_namespace, provider_hint=Provider.GITHUB
                ).connection
            except (NotFoundError, ProviderConfigError):
                connection = _DEFAULT_GITHUB_CONNECTION

        webhook_secret = (
            resolve_env_value(connection.webhook_secret_env, settings)
            if connection.webhook_secret_env
            else None
        )
        adapter = GitHubAdapter(connection=connection, webhook_secret=webhook_secret)
        verify_headers = {
            "X-GitHub-Event": x_github_event,
            "X-GitHub-Delivery": x_github_delivery,
            "X-Hub-Signature-256": x_hub_signature_256,
        }

        if not await adapter.verify_webhook(verify_headers, body):
            span.set_attribute("error", True)
            span.set_attribute("error.type", "auth_failure")
            logger.warning("Invalid GitHub webhook signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

        # Parse JSON payload
        try:
            payload: dict[str, Any] = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            span.set_attribute("error", True)
            span.set_attribute("error.type", "parse_error")
            logger.error(f"Failed to parse GitHub webhook payload: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON payload",
            )

        event_id = x_github_delivery or _generate_event_id(payload)
        span.set_attribute("forge.event_id", event_id)
        headers = {**verify_headers, "X-GitHub-Delivery": event_id}

        # Parse webhook data
        try:
            event = await adapter.parse_webhook(headers, body, registry)
        except (NotFoundError, ProviderConfigError):
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "unmanaged_repository")
            logger.info("Skipping webhook for unmanaged repository")
            record_webhook_received(source="github", event_type=x_github_event)
            return {"status": "ignored", "event_id": event_id}

        ticket_key = _extract_ticket_key(event)
        span.set_attribute("forge.ticket_key", ticket_key)

        # Record webhook received metric
        record_webhook_received(source="github", event_type=x_github_event)

        # Queue for async processing
        producer = QueueProducer()
        message_id = await producer.publish_event(event, ticket_key)

        if message_id is None:
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "duplicate event")
            return {"status": "duplicate", "event_id": event_id, "ticket_key": ticket_key}

        span.set_attribute("forge.queued", True)
        logger.info(
            f"GitHub webhook queued: event_id={event_id}, "
            f"kind={event.kind}, repo={event.repo_ref.namespace}"
        )

        # Record webhook processed metric
        record_webhook_processed(source="github", event_type=x_github_event)

        return {"status": "queued", "event_id": event_id, "ticket_key": ticket_key}

    except HTTPException:
        raise
    except ValueError as e:
        span.set_attribute("error", True)
        span.set_attribute("error.type", "validation_error")
        logger.error(f"Failed to parse GitHub webhook: {e}")
        record_webhook_failed(
            source="github", event_type=x_github_event, error_type="validation_error"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        )
    except Exception as e:
        span.set_attribute("error", True)
        span.set_attribute("error.type", "internal_error")
        logger.error(f"Failed to process GitHub webhook: {e}")
        record_webhook_failed(
            source="github", event_type=x_github_event, error_type="internal_error"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process webhook",
        )
    finally:
        span.end()


def _generate_event_id(payload: dict[str, Any]) -> str:
    """Generate a deterministic event ID from payload.

    Used as a fallback when X-GitHub-Delivery is empty.

    Args:
        payload: Webhook payload.

    Returns:
        SHA256-based event ID.
    """
    content = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]

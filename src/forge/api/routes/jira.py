"""Jira webhook endpoint for receiving issue events."""

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from forge.api.routes.metrics import (
    record_webhook_failed,
    record_webhook_processed,
    record_webhook_received,
)
from forge.config import get_settings
from forge.integrations.jira.webhooks import (
    create_webhook_event,
    parse_jira_webhook,
)
from forge.models.events import EventSource
from forge.observability.config import get_tracer
from forge.observability.context import get_correlation_id
from forge.queue.producer import QueueProducer

logger = logging.getLogger(__name__)
tracer = get_tracer("forge.api.jira")

router = APIRouter(prefix="/api/v1/webhooks", tags=["jira"])


@router.post(
    "/jira",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Event accepted for processing"},
        400: {"description": "Invalid payload"},
        401: {"description": "Invalid webhook signature"},
    },
)
async def receive_jira_webhook(
    request: Request,
    x_atlassian_webhook_identifier: str = Header(default=""),
) -> dict[str, str]:
    """Receive and queue Jira webhook events.

    This endpoint:
    1. Validates webhook signature (if configured)
    2. Parses the webhook payload
    3. Queues the event for async processing
    4. Returns immediately (<500ms target)

    Args:
        request: FastAPI request object.
        x_atlassian_webhook_identifier: Unique webhook event ID from Jira.

    Returns:
        Acknowledgment with event ID.
    """
    settings = get_settings()
    span = tracer.start_span(
        "jira_webhook",
        attributes={
            "correlation_id": get_correlation_id(),
            "forge.source": "jira",
        },
    )

    try:
        # Read raw body for signature verification
        body = await request.body()

        # Validate signature if webhook secret is configured
        if settings.jira_webhook_secret.get_secret_value():
            x_hub_signature = request.headers.get("X-Hub-Signature-256", "")
            if not _verify_jira_signature(
                body,
                x_hub_signature,
                settings.jira_webhook_secret.get_secret_value(),
            ):
                span.set_attribute("error", True)
                span.set_attribute("error.type", "auth_failure")
                logger.warning("Invalid Jira webhook signature")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid webhook signature",
                )

        # Parse JSON payload
        try:
            payload: dict[str, Any] = await request.json()
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.type", "parse_error")
            logger.error(f"Failed to parse Jira webhook payload: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON payload",
            )

        # Generate event ID if not provided
        event_id = x_atlassian_webhook_identifier or _generate_event_id(payload)
        span.set_attribute("forge.event_id", event_id)

        # Parse webhook data
        webhook_data = parse_jira_webhook(payload, event_id)
        span.set_attribute("forge.ticket_key", webhook_data.ticket_key)
        span.set_attribute("forge.event_type", webhook_data.event_type)

        # Record webhook received metric
        record_webhook_received(source="jira", event_type=webhook_data.event_type)

        # Filter: only process issues with forge:managed label
        issue_labels = payload.get("issue", {}).get("fields", {}).get("labels", [])
        has_forge_managed = "forge:managed" in issue_labels

        # Also check if forge:managed is being added in this event
        changelog_items = payload.get("changelog", {}).get("items", [])
        for item in changelog_items:
            if item.get("field") == "labels":
                to_labels = item.get("toString", "")
                if "forge:managed" in to_labels:
                    has_forge_managed = True
                    break

        if not has_forge_managed:
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "missing forge:managed label")
            logger.debug(f"Skipping {webhook_data.ticket_key}: missing forge:managed label")
            return {
                "status": "skipped",
                "event_id": event_id,
                "ticket_key": webhook_data.ticket_key,
                "reason": "missing forge:managed label",
            }

        # Filter: ignore comment events authored by Forge itself
        comment_author_email = payload.get("comment", {}).get("author", {}).get("emailAddress", "")
        if comment_author_email and comment_author_email == settings.jira_user_email:
            span.set_attribute("forge.skipped", True)
            span.set_attribute("forge.skip_reason", "self-comment")
            logger.debug(f"Skipping self-comment on {webhook_data.ticket_key}")
            return {
                "status": "skipped",
                "event_id": event_id,
                "ticket_key": webhook_data.ticket_key,
                "reason": "self-comment",
            }

        # Check if this is a child ticket (Epic/Task) - route to parent Feature
        issue_type = payload.get("issue", {}).get("fields", {}).get("issuetype", {}).get("name", "")
        routing_ticket_key = webhook_data.ticket_key
        source_ticket_key = None

        if issue_type in ("Epic", "Task", "Sub-task"):
            # Look for forge:parent label to find parent Feature
            parent_feature_key = _extract_parent_from_labels(issue_labels)
            if parent_feature_key:
                source_ticket_key = webhook_data.ticket_key
                routing_ticket_key = parent_feature_key
                span.set_attribute("forge.source_ticket_key", source_ticket_key)
                span.set_attribute("forge.routing_ticket_key", routing_ticket_key)
                logger.info(
                    f"Routing {issue_type} {source_ticket_key} webhook "
                    f"to parent Feature {routing_ticket_key}"
                )
            elif issue_type in ("Epic", "Task"):
                # Bypass parent validation for standalone managed Epic/Task issues.
                # routing_ticket_key remains webhook_data.ticket_key, source_ticket_key remains None.
                logger.info(
                    f"Bypassing parent checks for standalone {issue_type} "
                    f"{webhook_data.ticket_key}."
                )
            else:
                # Epics/Tasks without forge:parent are invalid - reject
                span.set_attribute("forge.skipped", True)
                span.set_attribute("forge.skip_reason", f"{issue_type} missing forge:parent label")
                logger.warning(
                    f"Skipping {webhook_data.ticket_key}: {issue_type} must have "
                    "forge:parent label to be processed"
                )
                return {
                    "status": "skipped",
                    "event_id": event_id,
                    "ticket_key": webhook_data.ticket_key,
                    "reason": f"{issue_type} must have forge:parent label",
                }

        webhook_event = create_webhook_event(webhook_data)

        # Add source_ticket_key to payload if routing to parent
        event_payload = webhook_event.payload
        if source_ticket_key:
            event_payload = {
                **event_payload,
                "source_ticket_key": source_ticket_key,
            }

        # Queue for async processing
        producer = QueueProducer()
        await producer.publish(
            event_id=webhook_event.event_id,
            source=EventSource.JIRA,
            event_type=webhook_event.event_type,
            ticket_key=routing_ticket_key,  # Route to parent Feature if child
            payload=event_payload,
        )

        span.set_attribute("forge.queued", True)
        logger.info(f"Queued Jira event {event_id} for {webhook_data.ticket_key}")

        # Record webhook processed metric
        record_webhook_processed(source="jira", event_type=webhook_data.event_type)

        return {
            "status": "accepted",
            "event_id": event_id,
            "ticket_key": webhook_data.ticket_key,
        }

    except HTTPException:
        raise
    except ValueError as e:
        span.set_attribute("error", True)
        span.set_attribute("error.type", "validation_error")
        logger.error(f"Failed to parse Jira webhook: {e}")
        record_webhook_failed(source="jira", event_type="unknown", error_type="validation_error")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        span.set_attribute("error", True)
        span.set_attribute("error.type", "internal_error")
        logger.error(f"Failed to queue Jira event: {e}")
        record_webhook_failed(source="jira", event_type="unknown", error_type="internal_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue event",
        )
    finally:
        span.end()


def _verify_jira_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Jira webhook signature.

    Args:
        payload: Raw request body.
        signature: X-Hub-Signature-256 header value.
        secret: Webhook secret.

    Returns:
        True if signature is valid.
    """
    if not signature:
        return False

    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(signature, expected)


def _generate_event_id(payload: dict[str, Any]) -> str:
    """Generate a deterministic event ID from payload.

    Args:
        payload: Webhook payload.

    Returns:
        SHA256-based event ID.
    """
    import json

    content = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _extract_parent_from_labels(labels: list[str]) -> str | None:
    """Extract parent Feature key from forge:parent label.

    Args:
        labels: List of Jira labels.

    Returns:
        Parent Feature key (e.g., "PROJ-123") or None if not found.
    """
    for label in labels:
        if label.startswith("forge:parent:"):
            return label[13:]  # len("forge:parent:") == 13
    return None

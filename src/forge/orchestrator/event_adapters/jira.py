"""Jira webhook evidence adapter."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from forge.domain import (
    Observation,
    ObservationSource,
    ResourceIdentity,
    observation_identity,
    stable_identity,
)
from forge.models.events import EventSource
from forge.models.workflow import TicketType
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, IngressMessage

logger = logging.getLogger(__name__)


def _comment_text(value: Any) -> str:
    """Flatten Jira text or ADF into provider-independent comment evidence."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    own_text = value.get("text")
    parts = [own_text] if isinstance(own_text, str) else []
    for child in value.get("content", []):
        text = _comment_text(child)
        if text:
            parts.append(text)
    return "\n".join(parts)


class JiraEventAdapter:
    source = EventSource.JIRA

    def adapt(self, message: IngressMessage) -> AdaptedEvent:
        issue = message.payload.get("issue", {})
        issue_fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        ticket_key = str(issue.get("key") or message.ticket_key)
        ticket_type_name = str(issue_fields.get("issuetype", {}).get("name", "Unknown"))
        if ticket_type_name in {"Epic", "Task", "Sub-task"} and message.payload.get(
            "source_ticket_key"
        ):
            ticket_type = TicketType.UNKNOWN
        else:
            try:
                ticket_type = TicketType(ticket_type_name)
            except ValueError:
                logger.warning("Unknown ticket type '%s' for %s", ticket_type_name, ticket_key)
                ticket_type = TicketType.UNKNOWN
        resource = ResourceIdentity(resource_type="issue", external_id=ticket_key)
        comment = message.payload.get("comment")
        observation = Observation(
            observation_id=observation_identity(
                source_system="jira",
                provider_event_id=message.event_id,
                resource=resource,
            ),
            source=ObservationSource.WEBHOOK,
            source_system="jira",
            resource=resource,
            resource_revision=_jira_revision(message.payload),
            revision_order=_jira_revision_order(message.payload),
            observed_at=message.timestamp,
            received_at=message.timestamp,
            facts=_canonical_facts(
                message.event_type,
                ticket_key=ticket_key,
                issue_fields=issue_fields,
                comment=comment,
                source_ticket_key=message.payload.get("source_ticket_key"),
            ),
            correlation={
                "provider_event_id": message.event_id,
                "transport_event_id": message.event_id,
                "workflow_ticket_key": message.ticket_key,
            },
        )
        return AdaptedEvent(
            source=message.source,
            event_id=message.event_id,
            ticket_key=message.ticket_key,
            ticket_type=ticket_type,
            observation=observation,
        )


def _canonical_facts(
    event_type: str,
    *,
    ticket_key: str,
    issue_fields: dict[str, Any],
    comment: Any,
    source_ticket_key: Any,
) -> dict[str, Any]:
    """Build provider-neutral Jira facts shared by webhook and poller paths.

    Jira webhooks commonly contain a full issue, author metadata, changelog
    history, and ADF comment objects.  The poller intentionally forwards a
    smaller webhook-shaped payload.  None of those provider details are
    needed for command selection: only ticket identity/type/status/labels and
    normalized comment text are.  Keeping that small stable projection makes
    equivalent revisions compare equal in the observation ledger.
    """
    issue_type = issue_fields.get("issuetype", {})
    status = issue_fields.get("status", {})
    labels = issue_fields.get("labels", [])
    if not isinstance(labels, list | tuple | set):
        labels = []
    canonical_fields: dict[str, Any] = {}
    if isinstance(issue_type, dict) and issue_type.get("name") is not None:
        canonical_fields["issuetype"] = {"name": str(issue_type["name"])}
    if isinstance(status, dict) and status.get("name") is not None:
        canonical_fields["status"] = {"name": str(status["name"])}
    if isinstance(labels, (list, tuple, set)):
        canonical_fields["labels"] = sorted({str(label) for label in labels})
    return {
        "event_type": event_type,
        "issue": {
            "key": ticket_key,
            "fields": canonical_fields,
        },
        # Changelog and comment objects contain transport/provider-specific
        # metadata.  Keep their historical empty/null compatibility shape;
        # command interpretation uses the original ingress payload for
        # changelog routing and only needs normalized comment text here.
        "changelog": {},
        "comment": None,
        "comment_text": _comment_text(comment.get("body", ""))
        if isinstance(comment, dict)
        else "",
        "source_ticket_key": str(source_ticket_key) if source_ticket_key else None,
    }


def _jira_revision(payload: dict[str, Any]) -> str | None:
    """Return a stable revision for a Jira issue observation.

    Comment IDs are immutable provider identities and take precedence over the
    issue update timestamp.  For issue/label changes, Jira's ``updated`` field
    is the only native revision exposed by the issue endpoint.  A changelog
    fingerprint is used when a webhook has changelog data but no ``updated``
    field.  The final ``None`` fallback keeps malformed/legacy payloads
    observable without pretending that their UUID delivery ID is orderable.
    """
    comment = payload.get("comment")
    if isinstance(comment, dict) and comment.get("id") is not None:
        return f"comment:{comment['id']}"

    issue = payload.get("issue", {})
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    updated = fields.get("updated")
    if isinstance(updated, str) and updated:
        return f"updated:{updated}"

    changelog = payload.get("changelog")
    if isinstance(changelog, dict) and changelog.get("items"):
        return stable_identity("jira-changelog", {"items": changelog["items"]})
    return None


def _jira_revision_order(payload: dict[str, Any]) -> int | None:
    """Convert Jira's update timestamp to comparable monotonic metadata."""
    issue = payload.get("issue", {})
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    value = fields.get("updated")
    if not isinstance(value, str) or not value:
        comment = payload.get("comment")
        if isinstance(comment, dict):
            value = comment.get("created") or comment.get("updated")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return max(0, int(parsed.timestamp() * 1_000_000))

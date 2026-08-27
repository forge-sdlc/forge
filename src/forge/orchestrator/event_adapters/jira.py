"""Jira webhook evidence adapter."""

from __future__ import annotations

import logging
from typing import Any

from forge.domain import Observation, ObservationSource, ResourceIdentity, stable_identity
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
        ticket_key = str(issue.get("key") or message.ticket_key)
        ticket_type_name = str(issue.get("fields", {}).get("issuetype", {}).get("name", "Unknown"))
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
        observation = Observation(
            observation_id=stable_identity(
                "observation", {"source_system": "jira", "event_id": message.event_id}
            ),
            source=ObservationSource.WEBHOOK,
            source_system="jira",
            resource=ResourceIdentity(resource_type="issue", external_id=ticket_key),
            observed_at=message.timestamp,
            received_at=message.timestamp,
            facts={
                "event_type": message.event_type,
                "issue": issue,
                "changelog": message.payload.get("changelog", {}),
                "comment": message.payload.get("comment"),
                "comment_text": _comment_text(
                    message.payload.get("comment", {}).get("body", "")
                ),
                "source_ticket_key": message.payload.get("source_ticket_key"),
            },
            correlation={"workflow_ticket_key": message.ticket_key},
        )
        return AdaptedEvent(
            source=message.source,
            event_id=message.event_id,
            ticket_key=message.ticket_key,
            ticket_type=ticket_type,
            observation=observation,
        )

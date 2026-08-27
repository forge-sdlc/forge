"""Jira webhook evidence adapter."""

from __future__ import annotations

import logging

from forge.domain import Observation, ObservationSource, ResourceIdentity, stable_identity
from forge.models.events import EventSource
from forge.models.workflow import TicketType
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, IngressMessage

logger = logging.getLogger(__name__)


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

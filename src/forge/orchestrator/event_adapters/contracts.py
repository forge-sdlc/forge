"""Infrastructure-free contracts for ingress event adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from forge.domain import Observation
from forge.integrations.source_control.contracts import NormalizedEvent
from forge.models.events import EventSource
from forge.models.workflow import TicketType


class IngressMessage(Protocol):
    event_id: str
    source: EventSource
    event_type: str
    ticket_key: str
    payload: dict[str, Any]
    normalized_event: dict[str, Any] | None
    timestamp: datetime


@dataclass(frozen=True)
class AdaptedEvent:
    source: EventSource
    event_id: str
    ticket_key: str
    ticket_type: TicketType
    observation: Observation
    normalized_event: NormalizedEvent | None = None
    change_request_url: str | None = None
    requires_ticket_correlation: bool = False


class EventAdapter(Protocol):
    source: EventSource

    def adapt(self, message: IngressMessage) -> AdaptedEvent: ...

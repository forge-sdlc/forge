"""Source-control queue evidence adapter."""

from __future__ import annotations

from forge.integrations.source_control.observations import normalized_event_to_observation
from forge.models.events import EventSource
from forge.models.workflow import TicketType
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, IngressMessage
from forge.queue.models import normalized_event_from_dict


def extract_change_request_url(payload: dict) -> str | None:
    """Extract a canonical browser URL from supported source-control payload shapes."""
    repo = payload.get("repository", {}).get("full_name", "")
    api_url = payload.get("review", {}).get("pull_request_url", "")
    suite_prs = (
        payload.get("check_suite", {}).get("pull_requests")
        or payload.get("check_run", {}).get("pull_requests")
        or []
    )
    number = (
        payload.get("pull_request", {}).get("number")
        or payload.get("issue", {}).get("number")
        or (suite_prs[0].get("number") if suite_prs else None)
    )
    return (
        payload.get("pull_request", {}).get("html_url")
        or payload.get("review", {}).get("html_url")
        or (f"https://github.com/{repo}/pull/{number}" if repo and number else None)
        or (
            api_url.replace("https://api.github.com/repos/", "https://github.com/").replace(
                "/pulls/", "/pull/"
            )
            if api_url
            else None
        )
    )


class SourceControlEventAdapter:
    source = EventSource.SOURCE_CONTROL

    def adapt(self, message: IngressMessage) -> AdaptedEvent:
        if message.normalized_event is None:
            raise ValueError(
                f"Source-control event {message.event_id} has no normalized event envelope"
            )
        event = normalized_event_from_dict(message.normalized_event)
        change_request_url = (
            event.change_request.url
            if event.change_request
            else extract_change_request_url(message.payload)
        )
        return AdaptedEvent(
            source=message.source,
            event_id=message.event_id,
            ticket_key=message.ticket_key,
            ticket_type=TicketType.UNKNOWN,
            observation=normalized_event_to_observation(event),
            normalized_event=event,
            change_request_url=change_request_url,
            requires_ticket_correlation=not bool(message.ticket_key),
        )

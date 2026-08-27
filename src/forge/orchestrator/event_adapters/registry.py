"""Registry keeping ingress-source growth out of the central worker."""

from __future__ import annotations

from forge.models.events import EventSource
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, EventAdapter, IngressMessage
from forge.orchestrator.event_adapters.jira import JiraEventAdapter
from forge.orchestrator.event_adapters.source_control import SourceControlEventAdapter


class EventAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[EventSource, EventAdapter] = {}

    @property
    def sources(self) -> tuple[EventSource, ...]:
        return tuple(self._adapters)

    def register(self, adapter: EventAdapter) -> None:
        if adapter.source in self._adapters:
            raise ValueError(f"Adapter already registered for {adapter.source.value}")
        self._adapters[adapter.source] = adapter

    def adapt(self, message: IngressMessage) -> AdaptedEvent:
        try:
            adapter = self._adapters[message.source]
        except KeyError as exc:
            raise ValueError(f"No event adapter registered for {message.source.value}") from exc
        return adapter.adapt(message)


def create_default_event_adapter_registry() -> EventAdapterRegistry:
    registry = EventAdapterRegistry()
    registry.register(JiraEventAdapter())
    registry.register(SourceControlEventAdapter())
    return registry

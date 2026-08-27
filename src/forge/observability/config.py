"""OpenTelemetry configuration for distributed tracing."""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from forge.config import get_settings

logger = logging.getLogger(__name__)

# Global tracer provider
_tracer_provider: TracerProvider | None = None


def configure_tracing(
    service_name: str = "forge",
    use_console: bool = False,
) -> TracerProvider:
    """Configure OpenTelemetry tracing.

    Sets up the tracer provider with appropriate exporters based on
    configuration. Supports OTLP (Jaeger, etc.) and console output.

    Args:
        service_name: Name of this service for trace attribution.
        use_console: If True, also export to console (for debugging).

    Returns:
        Configured TracerProvider.
    """
    global _tracer_provider

    if _tracer_provider is not None:
        return _tracer_provider

    settings = get_settings()

    # Create resource with service metadata
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "1.0.0",
            "deployment.environment": settings.log_level.lower(),
        }
    )

    # Create tracer provider
    _tracer_provider = TracerProvider(resource=resource)

    # Add OTLP exporter if endpoint configured
    otlp_endpoint = getattr(settings, "otlp_endpoint", None)
    if otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        _tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(f"OTLP tracing configured: {otlp_endpoint}")

    # Add console exporter for debugging
    if use_console or settings.log_level == "DEBUG":
        console_exporter = ConsoleSpanExporter()
        _tracer_provider.add_span_processor(BatchSpanProcessor(console_exporter))
        logger.info("Console trace exporter enabled")

    # Set as global tracer provider
    trace.set_tracer_provider(_tracer_provider)

    logger.info(f"Tracing configured for service: {service_name}")
    return _tracer_provider


def get_tracer(name: str = "forge") -> trace.Tracer:
    """Get a tracer instance.

    Automatically configures tracing if not already done.

    Args:
        name: Tracer name (typically module or component name).

    Returns:
        Tracer instance for creating spans.
    """
    if _tracer_provider is None:
        configure_tracing()

    return trace.get_tracer(name)


async def shutdown_tracing() -> None:
    """Shutdown tracing and flush pending spans."""
    global _tracer_provider

    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
        logger.info("Tracing shutdown complete")


def create_span(
    name: str,
    attributes: dict | None = None,
    tracer_name: str = "forge",
):
    """Create a span context manager.

    Convenience function for creating spans with common attributes.

    Args:
        name: Span name (operation being traced).
        attributes: Optional span attributes.
        tracer_name: Tracer to use.

    Returns:
        Span context manager.
    """
    tracer = get_tracer(tracer_name)
    return tracer.start_as_current_span(
        name,
        attributes=attributes or {},
    )

"""Structured logging configuration for Forge orchestrator."""

import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from forge.config import get_settings


class StructuredFormatter(logging.Formatter):
    """JSON-formatted structured log output.

    Produces logs suitable for aggregation in ELK, CloudWatch, etc.
    """

    def __init__(
        self,
        include_extra: bool = True,
        exclude_fields: set[str] | None = None,
    ) -> None:
        """Initialize formatter.

        Args:
            include_extra: Include extra fields from log records.
            exclude_fields: Fields to exclude from output.
        """
        super().__init__()
        self.include_extra = include_extra
        self.exclude_fields = exclude_fields or {
            "args",
            "exc_info",
            "exc_text",
            "stack_info",
            "msg",
            "message",
        }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON.

        Args:
            record: Log record to format.

        Returns:
            JSON-formatted log string.
        """
        log_data: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields from record
        if self.include_extra:
            for key, value in record.__dict__.items():
                if (
                    key not in self.exclude_fields
                    and key not in log_data
                    and not key.startswith("_")
                ):
                    try:
                        # Ensure value is JSON serializable
                        json.dumps(value)
                        log_data[key] = value
                    except (TypeError, ValueError):
                        log_data[key] = str(value)

        return json.dumps(log_data)


class ContextLogger(logging.LoggerAdapter):
    """Logger adapter that adds context to all log messages.

    Useful for adding request-scoped or workflow-scoped context.
    """

    def process(
        self,
        msg: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Process log message to add context.

        Args:
            msg: Log message.
            kwargs: Keyword arguments.

        Returns:
            Processed message and kwargs.
        """
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(
    level: str | None = None,
    json_format: bool = True,
    log_file: str | None = None,
) -> None:
    """Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_format: Use JSON structured format.
        log_file: Path to log file (overrides settings).
    """
    settings = get_settings()
    log_level = level or settings.log_level
    file_path = log_file or settings.log_file

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Create formatter
    if json_format:
        formatter = StructuredFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Create file handler if configured
    if file_path:
        # Ensure parent directory exists
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(getattr(logging, log_level.upper()))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        root_logger.info(f"Logging to file: {file_path}")

    # Set levels for noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    # GitHub Copilot MCP only supports POST (not the optional SSE GET stream).
    # The mcp library retries the GET stream on 405, generating INFO noise.
    # Tools still load correctly via POST — suppress the reconnect chatter.
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)


def get_context_logger(
    name: str,
    **context: Any,
) -> ContextLogger:
    """Get a logger with attached context.

    Args:
        name: Logger name.
        **context: Context fields to attach.

    Returns:
        Context logger adapter.
    """
    base_logger = logging.getLogger(name)
    return ContextLogger(base_logger, context)


def log_workflow_event(
    logger: logging.Logger,
    event: str,
    ticket_key: str,
    **extra: Any,
) -> None:
    """Log a workflow event with standard fields.

    Args:
        logger: Logger to use.
        event: Event name.
        ticket_key: Jira ticket key.
        **extra: Additional fields.
    """
    logger.info(
        f"{event} for {ticket_key}",
        extra={
            "event": event,
            "ticket_key": ticket_key,
            **extra,
        },
    )


def log_api_call(
    logger: logging.Logger,
    service: str,
    method: str,
    endpoint: str,
    status_code: int | None = None,
    duration_ms: float | None = None,
    **extra: Any,
) -> None:
    """Log an external API call.

    Args:
        logger: Logger to use.
        service: Service name (jira, github, anthropic).
        method: HTTP method.
        endpoint: API endpoint.
        status_code: Response status code.
        duration_ms: Call duration in milliseconds.
        **extra: Additional fields.
    """
    logger.info(
        f"{method} {service}:{endpoint} -> {status_code}",
        extra={
            "service": service,
            "method": method,
            "endpoint": endpoint,
            "status_code": status_code,
            "duration_ms": duration_ms,
            **extra,
        },
    )


def log_llm_call(
    logger: logging.Logger,
    model: str,
    operation: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: float | None = None,
    **extra: Any,
) -> None:
    """Log an LLM API call.

    Args:
        logger: Logger to use.
        model: Model identifier.
        operation: Operation name (generate_prd, review_code, etc.).
        input_tokens: Input token count.
        output_tokens: Output token count.
        duration_ms: Call duration in milliseconds.
        **extra: Additional fields.
    """
    logger.info(
        f"LLM {operation} with {model}",
        extra={
            "llm_model": model,
            "operation": operation,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
            **extra,
        },
    )

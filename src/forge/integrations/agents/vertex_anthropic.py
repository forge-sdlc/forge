"""Translate LangChain native structured output for Claude on Vertex."""

from copy import deepcopy
from typing import Any

import httpx
from anthropic import APIConnectionError, APIStatusError, transform_schema
from langchain_anthropic.chat_models import convert_to_anthropic_tool
from langchain_core.outputs import ChatResult
from langchain_core.tools import BaseTool
from langchain_google_vertexai.model_garden import ChatAnthropicVertex
from pydantic import Field

_SERVER_TOOL_PREFIXES = (
    "web_search_",
    "web_fetch_",
    "code_execution_",
    "computer_",
    "bash_",
    "text_editor_",
    "tool_search_",
    "memory_",
)


class VertexResponseStopError(ValueError):
    """Claude stopped before a complete, usable response was available."""

    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason
        super().__init__(f"Vertex Claude response stop_reason={stop_reason}")


def _argument_structure(schema: Any) -> Any:
    """Retain schema features that determine accepted argument shapes."""
    if not isinstance(schema, dict):
        return schema
    properties = schema.get("properties")
    structure: dict[str, Any] = {
        key: schema[key]
        for key in ("type", "$ref", "required", "dependentRequired")
        if key in schema
    }
    if properties is not None:
        structure["properties"] = {
            name: _argument_structure(value) for name, value in properties.items()
        }
    for key in (
        "items",
        "additionalProperties",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
    ):
        if key in schema:
            structure[key] = _argument_structure(schema[key])
    if "additionalProperties" not in schema and properties:
        structure["additionalProperties"] = True
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if key in schema:
            structure[key] = [_argument_structure(item) for item in schema[key]]
    for key in ("$defs", "definitions", "patternProperties", "dependentSchemas"):
        if key in schema:
            structure[key] = {
                name: _argument_structure(value) for name, value in schema[key].items()
            }
    return structure


def _has_recursive_ref(schema: dict[str, Any]) -> bool:
    definitions = schema.get("$defs", {})

    def visit(value: Any, active: frozenset[str]) -> bool:
        if isinstance(value, list):
            return any(visit(item, active) for item in value)
        if not isinstance(value, dict):
            return False
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            if reference in active:
                return True
            target = definitions.get(reference.removeprefix("#/$defs/"))
            if target is not None and visit(target, active | {reference}):
                return True
        return any(visit(item, active) for key, item in value.items() if key != "$ref")

    return visit(schema, frozenset())


def _format_tool(tool: Any) -> dict[str, Any]:
    if (
        isinstance(tool, dict)
        and "type" in tool
        and tool["type"] != "function"
        and "input_schema" not in tool
    ):
        tool_type = tool["type"]
        if not (
            isinstance(tool_type, str)
            and tool_type.startswith(_SERVER_TOOL_PREFIXES)
            and isinstance(tool.get("name"), str)
        ):
            raise ValueError(f"Unsupported Vertex Claude tool: {tool_type!r}")
        return deepcopy(tool)

    formatted = dict(deepcopy(convert_to_anthropic_tool(tool)))
    if isinstance(tool, dict) and tool.get("type") == "function":
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("strict"), bool):
            formatted["strict"] = function["strict"]
    extras = tool.extras if isinstance(tool, BaseTool) else None
    if isinstance(extras, dict) and "strict" in extras:
        formatted["strict"] = extras["strict"]
    if formatted.get("strict") is True:
        original_schema = formatted.get("input_schema")
        if not isinstance(original_schema, dict):
            raise ValueError("Explicit strict tool needs an input schema")
        if _has_recursive_ref(original_schema):
            raise ValueError("Unsupported recursive strict tool input schema")
        try:
            adapted_schema = transform_schema(original_schema)
        except (TypeError, ValueError) as error:
            raise ValueError("Explicit strict tool has an unsupported input schema") from error
        if _argument_structure(original_schema) != _argument_structure(adapted_schema):
            raise ValueError("Explicit strict tool would change its input schema")
        formatted["input_schema"] = adapted_schema
    return formatted


def _explicitly_strict(tool: Any) -> bool:
    if isinstance(tool, dict):
        if tool.get("strict") is True:
            return True
        function = tool.get("function") if tool.get("type") == "function" else None
        return isinstance(function, dict) and function.get("strict") is True
    extras = tool.extras if isinstance(tool, BaseTool) else None
    return isinstance(extras, dict) and extras.get("strict") is True


class ForgeChatAnthropicVertex(ChatAnthropicVertex):
    """Keep LangChain's provider strategy on Anthropic's Vertex wire format."""

    tool_output_only: bool = Field(default=False, exclude=True)

    def __init__(self, **kwargs: Any) -> None:
        # Pydantic accepts configured model fields dynamically at runtime.
        super().__init__(**kwargs)

    def _format_output(self, data: Any, **kwargs: Any) -> ChatResult:
        stop_reason = data.stop_reason
        if stop_reason in {"max_tokens", "refusal"}:
            raise VertexResponseStopError(stop_reason)
        return super()._format_output(data, **kwargs)

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: dict[str, str] | str | None = None,
        strict: bool | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del strict  # LangChain's binding-wide strictness is not a per-tool opt-in.
        if self.tool_output_only and any(_explicitly_strict(tool) for tool in tools):
            raise ValueError("Explicit strict tools are incompatible with Vertex tool output mode")
        if sum(_explicitly_strict(tool) for tool in tools) > 20:
            raise ValueError("Claude on Vertex accepts at most 20 strict tools")
        formatted = [_format_tool(tool) for tool in tools]
        if self.tool_output_only and any(tool.get("strict") is True for tool in formatted):
            raise ValueError("Explicit strict tools are incompatible with Vertex tool output mode")
        if response_format is not None:
            if (
                not isinstance(response_format, dict)
                or response_format.get("type") != "json_schema"
                or not isinstance(response_format.get("json_schema"), dict)
                or not isinstance(response_format["json_schema"].get("schema"), dict)
            ):
                raise ValueError("Unsupported Vertex Claude response_format")
            schema = transform_schema(response_format["json_schema"]["schema"])
            native_format = {"type": "json_schema", "schema": schema}
            output_config = dict(kwargs.pop("output_config", {}) or {})
            if "format" in output_config and output_config["format"] != native_format:
                raise ValueError("Conflicting response_format and output_config.format")
            output_config["format"] = native_format
            kwargs["output_config"] = output_config
        if isinstance(tool_choice, dict):
            kwargs["tool_choice"] = tool_choice
        elif tool_choice in ("any", "auto"):
            kwargs["tool_choice"] = {"type": tool_choice}
        elif isinstance(tool_choice, str):
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}
        elif tool_choice is not None:
            raise ValueError(f"Unrecognized tool_choice: {tool_choice!r}")
        return self.bind(tools=formatted, **kwargs)


def is_unsupported_native_output_error(error: Exception) -> bool:
    """Require an explicit model-capability rejection before changing strategy."""
    if not isinstance(error, APIStatusError) or error.status_code != 400:
        return False
    body = error.body
    detail = body.get("error") if isinstance(body, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else None
    return isinstance(message, str) and (
        "output_config.format" in message.lower()
        and "not supported for this model" in message.lower()
    )


def is_transient_vertex_error(error: Exception) -> bool:
    """Retry only transport and provider rate/server errors on the same strategy."""
    if isinstance(error, APIStatusError):
        return error.status_code == 429 or error.status_code >= 500
    return isinstance(error, (APIConnectionError, httpx.TransportError))

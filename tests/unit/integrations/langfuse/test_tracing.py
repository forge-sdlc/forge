"""Tests for Langfuse tracing configuration.

Covers get_langfuse_config() metadata passthrough and
get_langfuse_context() metadata parameter.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from forge.integrations.langfuse.tracing import (
    AsyncLangfuseContext,
    get_langfuse_config,
    get_langfuse_context,
)


class TestGetLangfuseConfigMetadata:
    """get_langfuse_config() includes metadata in _langfuse_context."""

    def _call_with_handler(self, **kwargs: Any) -> dict[str, Any]:
        """Call get_langfuse_config with a mocked handler so it returns a config."""
        with patch(
            "forge.integrations.langfuse.tracing.get_langfuse_handler",
            return_value=MagicMock(),
        ):
            return get_langfuse_config(**kwargs)

    def test_metadata_included_in_langfuse_context(self) -> None:
        metadata = {"ticket_key": "PROJ-42", "retry_count": "3"}
        config = self._call_with_handler(metadata=metadata)

        assert config["_langfuse_context"]["metadata"] == metadata

    def test_metadata_none_when_not_provided(self) -> None:
        config = self._call_with_handler(trace_name="test-trace")
        assert config["_langfuse_context"]["metadata"] is None

    def test_metadata_merged_into_top_level_metadata(self) -> None:
        metadata = {"system_prompt_length": "4523"}
        config = self._call_with_handler(
            trace_name="test-trace",
            metadata=metadata,
        )
        assert config["metadata"]["langfuse_trace_name"] == "test-trace"
        assert config["metadata"]["system_prompt_length"] == "4523"

    def test_tags_and_metadata_both_passed_through(self) -> None:
        tags = ["PROJ-42", "Bug"]
        metadata = {"retry_count": "1"}
        config = self._call_with_handler(
            tags=tags,
            metadata=metadata,
            session_id="PROJ-42",
        )
        ctx = config["_langfuse_context"]
        assert ctx["tags"] == ["PROJ-42", "Bug"]
        assert ctx["metadata"] == {"retry_count": "1"}
        assert ctx["session_id"] == "PROJ-42"

    def test_returns_empty_dict_when_disabled(self) -> None:
        with patch(
            "forge.integrations.langfuse.tracing.get_langfuse_handler",
            return_value=None,
        ):
            config = get_langfuse_config(
                metadata={"key": "val"},
                tags=["tag"],
            )
        assert config == {}

    def test_all_context_params_present(self) -> None:
        config = self._call_with_handler(
            session_id="sess-1",
            user_id="user-1",
            tags=["t1"],
            metadata={"k": "v"},
        )
        ctx = config["_langfuse_context"]
        assert ctx["session_id"] == "sess-1"
        assert ctx["user_id"] == "user-1"
        assert ctx["tags"] == ["t1"]
        assert ctx["metadata"] == {"k": "v"}


class TestGetLangfuseContext:
    """get_langfuse_context() accepts metadata parameter."""

    def test_creates_context_with_metadata(self) -> None:
        ctx = get_langfuse_context(
            session_id="sess-1",
            tags=["tag"],
            metadata={"key": "val"},
        )
        assert isinstance(ctx, AsyncLangfuseContext)
        assert ctx.metadata == {"key": "val"}
        assert ctx.tags == ["tag"]
        assert ctx.session_id == "sess-1"

    def test_metadata_defaults_to_none(self) -> None:
        ctx = get_langfuse_context()
        assert ctx.metadata is None

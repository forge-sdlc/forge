"""Tests for Langfuse tracing field resolvers."""

import logging
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from forge.config import Settings
from forge.integrations.langfuse.fields import (
    TracingField,
    parse_trace_fields,
    resolve_field,
    resolve_trace_fields,
)


def _make_state(**overrides: Any) -> dict[str, Any]:
    """Build a minimal workflow state dict for testing."""
    base: dict[str, Any] = {
        "ticket_key": "PROJ-42",
        "ticket_type": "Bug",
        "current_node": "analyze_bug",
        "current_repo": "acme/widgets",
        "current_pr_number": 99,
        "ci_status": "passed",
        "event_type": "issue_updated",
        "retry_count": 3,
        "context": {"source": "jira"},
    }
    base.update(overrides)
    return base


class TestFieldResolvers:
    """Each TracingField resolver extracts the right value from state."""

    def test_ticket_key(self) -> None:
        assert resolve_field(TracingField.TICKET_KEY, _make_state()) == "PROJ-42"

    def test_ticket_key_missing(self) -> None:
        state = _make_state()
        del state["ticket_key"]
        assert resolve_field(TracingField.TICKET_KEY, state) is None

    def test_ticket_type(self) -> None:
        assert resolve_field(TracingField.TICKET_TYPE, _make_state()) == "Bug"

    def test_ticket_type_missing(self) -> None:
        state = _make_state()
        del state["ticket_type"]
        assert resolve_field(TracingField.TICKET_TYPE, state) is None

    def test_project_id(self) -> None:
        assert resolve_field(TracingField.PROJECT_ID, _make_state()) == "PROJ"

    def test_project_id_no_ticket_key(self) -> None:
        state = _make_state()
        del state["ticket_key"]
        assert resolve_field(TracingField.PROJECT_ID, state) is None

    def test_project_id_no_dash(self) -> None:
        assert resolve_field(TracingField.PROJECT_ID, _make_state(ticket_key="NODASH")) is None

    def test_workflow_step(self) -> None:
        assert resolve_field(TracingField.WORKFLOW_STEP, _make_state()) == "analyze_bug"

    def test_workflow_step_missing(self) -> None:
        state = _make_state()
        del state["current_node"]
        assert resolve_field(TracingField.WORKFLOW_STEP, state) is None

    def test_repo(self) -> None:
        assert resolve_field(TracingField.REPO, _make_state()) == "acme/widgets"

    def test_repo_from_short_key(self) -> None:
        state = _make_state()
        del state["current_repo"]
        state["repo"] = "acme/other"
        assert resolve_field(TracingField.REPO, state) == "acme/other"

    def test_repo_prefers_short_key(self) -> None:
        state = _make_state(repo="acme/preferred")
        assert resolve_field(TracingField.REPO, state) == "acme/preferred"

    def test_repo_missing(self) -> None:
        state = _make_state()
        del state["current_repo"]
        assert resolve_field(TracingField.REPO, state) is None

    def test_pr_number(self) -> None:
        assert resolve_field(TracingField.PR_NUMBER, _make_state()) == "99"

    def test_pr_number_from_short_key(self) -> None:
        state = _make_state()
        del state["current_pr_number"]
        state["pr_number"] = 42
        assert resolve_field(TracingField.PR_NUMBER, state) == "42"

    def test_pr_number_prefers_short_key(self) -> None:
        state = _make_state(pr_number=42)
        assert resolve_field(TracingField.PR_NUMBER, state) == "42"

    def test_pr_number_missing(self) -> None:
        state = _make_state()
        del state["current_pr_number"]
        assert resolve_field(TracingField.PR_NUMBER, state) is None

    def test_ci_status(self) -> None:
        assert resolve_field(TracingField.CI_STATUS, _make_state()) == "passed"

    def test_ci_status_missing(self) -> None:
        state = _make_state()
        del state["ci_status"]
        assert resolve_field(TracingField.CI_STATUS, state) is None

    def test_event_source(self) -> None:
        assert resolve_field(TracingField.EVENT_SOURCE, _make_state()) == "jira"

    def test_event_source_missing_context(self) -> None:
        assert resolve_field(TracingField.EVENT_SOURCE, _make_state(context={})) is None

    def test_event_source_no_context_key(self) -> None:
        state = _make_state()
        del state["context"]
        assert resolve_field(TracingField.EVENT_SOURCE, state) is None

    def test_event_type(self) -> None:
        assert resolve_field(TracingField.EVENT_TYPE, _make_state()) == "issue_updated"

    def test_event_type_missing(self) -> None:
        state = _make_state()
        del state["event_type"]
        assert resolve_field(TracingField.EVENT_TYPE, state) is None

    def test_retry_count(self) -> None:
        assert resolve_field(TracingField.RETRY_COUNT, _make_state()) == "3"

    def test_retry_count_missing(self) -> None:
        state = _make_state()
        del state["retry_count"]
        assert resolve_field(TracingField.RETRY_COUNT, state) is None

    def test_retry_count_zero(self) -> None:
        assert resolve_field(TracingField.RETRY_COUNT, _make_state(retry_count=0)) == "0"

    def test_system_prompt_length(self) -> None:
        state = _make_state(system_prompt_length=4523)
        assert resolve_field(TracingField.SYSTEM_PROMPT_LENGTH, state) == "4523"

    def test_system_prompt_length_missing(self) -> None:
        assert resolve_field(TracingField.SYSTEM_PROMPT_LENGTH, _make_state()) is None

    def test_llm_model(self) -> None:
        state = _make_state(llm_model="claude-sonnet-4-6-20250514")
        assert resolve_field(TracingField.LLM_MODEL, state) == "claude-sonnet-4-6-20250514"

    def test_llm_model_missing(self) -> None:
        assert resolve_field(TracingField.LLM_MODEL, _make_state()) is None


class TestTagEligibility:
    """Verify which fields are tag-eligible vs metadata-only."""

    @pytest.mark.parametrize(
        "field",
        [
            TracingField.TICKET_KEY,
            TracingField.TICKET_TYPE,
            TracingField.PROJECT_ID,
            TracingField.WORKFLOW_STEP,
            TracingField.REPO,
            TracingField.PR_NUMBER,
            TracingField.CI_STATUS,
            TracingField.EVENT_SOURCE,
            TracingField.EVENT_TYPE,
            TracingField.LLM_MODEL,
        ],
    )
    def test_tag_eligible_fields(self, field: TracingField) -> None:
        assert field.tag_eligible is True

    @pytest.mark.parametrize(
        "field",
        [
            TracingField.RETRY_COUNT,
            TracingField.SYSTEM_PROMPT_LENGTH,
        ],
    )
    def test_metadata_only_fields(self, field: TracingField) -> None:
        assert field.tag_eligible is False


class TestParseTraceFields:
    """Config string parsing and validation."""

    def test_valid_metadata_fields(self) -> None:
        fields = parse_trace_fields("ticket_key,ticket_type,retry_count", allow_tags=False)
        assert fields == [
            TracingField.TICKET_KEY,
            TracingField.TICKET_TYPE,
            TracingField.RETRY_COUNT,
        ]

    def test_valid_tag_fields(self) -> None:
        fields = parse_trace_fields("ticket_type,project_id", allow_tags=True)
        assert fields == [TracingField.TICKET_TYPE, TracingField.PROJECT_ID]

    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_trace_fields("", allow_tags=True) == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        assert parse_trace_fields("  ,  , ", allow_tags=True) == []

    def test_strips_whitespace(self) -> None:
        fields = parse_trace_fields(" ticket_key , ticket_type ", allow_tags=False)
        assert fields == [TracingField.TICKET_KEY, TracingField.TICKET_TYPE]

    def test_invalid_name_warns_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            fields = parse_trace_fields("ticket_key,foobar,ticket_type", allow_tags=False)
        assert fields == [TracingField.TICKET_KEY, TracingField.TICKET_TYPE]
        assert "foobar" in caplog.text
        assert "not a recognized field name" in caplog.text

    def test_tag_ineligible_field_in_tags_warns_and_skips(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            fields = parse_trace_fields("ticket_type,retry_count,project_id", allow_tags=True)
        assert fields == [TracingField.TICKET_TYPE, TracingField.PROJECT_ID]
        assert "retry_count" in caplog.text
        assert "not eligible for tags" in caplog.text

    def test_tag_ineligible_field_allowed_in_metadata(self) -> None:
        fields = parse_trace_fields("retry_count,system_prompt_length", allow_tags=False)
        assert fields == [TracingField.RETRY_COUNT, TracingField.SYSTEM_PROMPT_LENGTH]

    def test_all_invalid_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            fields = parse_trace_fields("bad1,bad2", allow_tags=True)
        assert fields == []

    def test_duplicate_fields_preserved(self) -> None:
        fields = parse_trace_fields("ticket_key,ticket_key", allow_tags=False)
        assert fields == [TracingField.TICKET_KEY, TracingField.TICKET_KEY]


def _make_settings(**overrides: Any) -> Settings:
    """Build a Settings instance with required fields and overrides."""
    defaults: dict[str, Any] = {
        "jira_base_url": "https://test.atlassian.net",
        "jira_api_token": "test",
        "jira_user_email": "test@example.com",
        "github_token": "test",
        "anthropic_api_key": "test",
        "langfuse_trace_tags": "",
        "langfuse_trace_metadata": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestSettingsTraceFields:
    """Settings properties parse and validate trace field config."""

    def test_trace_tag_fields_default_empty(self) -> None:
        settings = _make_settings()
        assert settings.trace_tag_fields == []

    def test_trace_metadata_fields_default_empty(self) -> None:
        settings = _make_settings()
        assert settings.trace_metadata_fields == []

    def test_trace_tag_fields_parsed(self) -> None:
        settings = _make_settings(langfuse_trace_tags="ticket_type,project_id")
        assert settings.trace_tag_fields == [TracingField.TICKET_TYPE, TracingField.PROJECT_ID]

    def test_trace_metadata_fields_parsed(self) -> None:
        settings = _make_settings(langfuse_trace_metadata="ticket_key,retry_count")
        assert settings.trace_metadata_fields == [
            TracingField.TICKET_KEY,
            TracingField.RETRY_COUNT,
        ]

    def test_tag_ineligible_field_rejected_from_tags(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            settings = _make_settings(langfuse_trace_tags="retry_count,ticket_type")
        assert settings.trace_tag_fields == [TracingField.TICKET_TYPE]

    def test_info_logged_when_fields_configured(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            settings = _make_settings(langfuse_trace_tags="ticket_type")
            _ = settings.trace_tag_fields
        assert "Langfuse trace tags configured: ticket_type" in caplog.text

    def test_no_info_logged_when_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            settings = _make_settings(langfuse_trace_tags="")
            _ = settings.trace_tag_fields
        assert "Langfuse trace tags configured" not in caplog.text

    def test_no_info_logged_when_all_invalid(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            settings = _make_settings(langfuse_trace_tags="bad1,bad2")
            _ = settings.trace_tag_fields
        assert "Langfuse trace tags configured" not in caplog.text


class TestResolveTraceFields:
    """Integration: resolve configured fields from workflow state."""

    def test_resolves_tags_and_metadata(self) -> None:
        state = _make_state()
        tag_fields = [TracingField.TICKET_TYPE, TracingField.PROJECT_ID, TracingField.WORKFLOW_STEP]
        metadata_fields = [TracingField.TICKET_KEY, TracingField.RETRY_COUNT]

        with (
            patch(
                "forge.config.get_settings"
            ) as mock_get_settings,
        ):
            mock_settings = mock_get_settings.return_value
            type(mock_settings).trace_tag_fields = PropertyMock(return_value=tag_fields)
            type(mock_settings).trace_metadata_fields = PropertyMock(return_value=metadata_fields)

            tags, metadata = resolve_trace_fields(state)

        assert tags == ["Bug", "PROJ", "analyze_bug"]
        assert metadata == {"ticket_key": "PROJ-42", "retry_count": "3"}

    def test_skips_missing_fields(self) -> None:
        state = _make_state()
        del state["current_repo"]
        tag_fields = [TracingField.TICKET_TYPE, TracingField.REPO]
        metadata_fields = [TracingField.PR_NUMBER]

        with patch(
            "forge.config.get_settings"
        ) as mock_get_settings:
            mock_settings = mock_get_settings.return_value
            type(mock_settings).trace_tag_fields = PropertyMock(return_value=tag_fields)
            type(mock_settings).trace_metadata_fields = PropertyMock(return_value=metadata_fields)

            tags, metadata = resolve_trace_fields(state)

        assert tags == ["Bug"]
        assert metadata == {"pr_number": "99"}

    def test_empty_config_returns_empty(self) -> None:
        with patch(
            "forge.config.get_settings"
        ) as mock_get_settings:
            mock_settings = mock_get_settings.return_value
            type(mock_settings).trace_tag_fields = PropertyMock(return_value=[])
            type(mock_settings).trace_metadata_fields = PropertyMock(return_value=[])

            tags, metadata = resolve_trace_fields(_make_state())

        assert tags == []
        assert metadata == {}

    def test_system_prompt_length_in_metadata(self) -> None:
        state = _make_state(system_prompt_length=4523)
        metadata_fields = [TracingField.SYSTEM_PROMPT_LENGTH]

        with patch(
            "forge.config.get_settings"
        ) as mock_get_settings:
            mock_settings = mock_get_settings.return_value
            type(mock_settings).trace_tag_fields = PropertyMock(return_value=[])
            type(mock_settings).trace_metadata_fields = PropertyMock(return_value=metadata_fields)

            tags, metadata = resolve_trace_fields(state)

        assert tags == []
        assert metadata == {"system_prompt_length": "4523"}

    def test_llm_model_in_tags(self) -> None:
        state = _make_state(llm_model="claude-sonnet-4-6-20250514")
        tag_fields = [TracingField.LLM_MODEL]

        with patch(
            "forge.config.get_settings"
        ) as mock_get_settings:
            mock_settings = mock_get_settings.return_value
            type(mock_settings).trace_tag_fields = PropertyMock(return_value=tag_fields)
            type(mock_settings).trace_metadata_fields = PropertyMock(return_value=[])

            tags, metadata = resolve_trace_fields(state)

        assert tags == ["claude-sonnet-4-6-20250514"]
        assert metadata == {}

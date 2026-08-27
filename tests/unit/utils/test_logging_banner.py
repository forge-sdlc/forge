"""Unit tests for startup configuration logging banner."""

import logging
from unittest.mock import patch

from forge.config import Settings
from forge.utils.logging import log_startup_banner


def test_log_startup_banner_standard_redis(caplog):
    """Test log_startup_banner with standard Redis URL and check output."""
    mock_settings = Settings(
        _env_file=None,
        jira_base_url="https://company.atlassian.net",
        jira_user_email="test@example.com",
        jira_api_token="fake-token",
        github_token="fake-github",
        llm_backend="anthropic",
        llm_model="claude-3-5-sonnet-20241022",
        anthropic_api_key="fake-key",
        redis_url="redis://localhost:6379/0",
    )

    with (
        patch("forge.config.get_settings", return_value=mock_settings),
        patch("forge.utils.logging.get_settings", return_value=mock_settings),
        caplog.at_level(logging.INFO),
    ):
        log_startup_banner("Test Component")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "INFO"
    assert "Forge Startup - Test Component" in record.message
    assert "Component      : Test Component" in record.message
    assert "Redis URL      : redis://localhost:6379/0" in record.message
    assert "Jira Base URL  : https://company.atlassian.net" in record.message


def test_log_startup_banner_redacted_redis(caplog):
    """Test log_startup_banner properly redacts Redis credentials."""
    mock_settings = Settings(
        _env_file=None,
        jira_base_url="https://company.atlassian.net",
        jira_user_email="test@example.com",
        jira_api_token="fake-token",
        github_token="fake-github",
        llm_backend="anthropic",
        llm_model="claude-3-5-sonnet-20241022",
        anthropic_api_key="fake-key",
        redis_url="redis://:secret_password@redis-host:6379/0",
    )

    with (
        patch("forge.config.get_settings", return_value=mock_settings),
        patch("forge.utils.logging.get_settings", return_value=mock_settings),
        caplog.at_level(logging.INFO),
    ):
        log_startup_banner("Test Component")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert "secret_password" not in record.message
    assert "Redis URL      : redis://:****@redis-host:6379/0" in record.message


def test_log_startup_banner_redacted_redis_with_user(caplog):
    """Test log_startup_banner properly redacts Redis user and credentials."""
    mock_settings = Settings(
        _env_file=None,
        jira_base_url="https://company.atlassian.net",
        jira_user_email="test@example.com",
        jira_api_token="fake-token",
        github_token="fake-github",
        llm_backend="anthropic",
        llm_model="claude-3-5-sonnet-20241022",
        anthropic_api_key="fake-key",
        redis_url="redis://admin:secret_password@redis-host:6379/0",
    )

    with (
        patch("forge.config.get_settings", return_value=mock_settings),
        patch("forge.utils.logging.get_settings", return_value=mock_settings),
        caplog.at_level(logging.INFO),
    ):
        log_startup_banner("Test Component")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert "secret_password" not in record.message
    assert "Redis URL      : redis://admin:****@redis-host:6379/0" in record.message

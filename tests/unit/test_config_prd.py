"""Tests for PRD approval configuration settings."""

from forge.config import Settings


class TestPrdApprovalConfig:
    def test_default_proposals_repo_is_empty(self) -> None:
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_repo == ""

    def test_default_proposals_path(self) -> None:
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_path == ""

    def test_proposals_repo_can_be_set_as_global_fallback(self) -> None:
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
            prd_proposals_repo="org/proposals",
        )
        assert settings.prd_proposals_repo == "org/proposals"


class TestTaskTakeoverConfig:
    def test_default_task_takeover_settings(self) -> None:
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.task_takeover.require_tests is True
        assert settings.task_takeover.review_max_attempts == 2

        # Verify default labels
        labels = settings.task_takeover.labels
        assert labels.pending == "forge:task-plan-pending"
        assert labels.approved == "forge:task-plan-approved"

    def test_override_task_takeover_settings(self) -> None:
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
            task_takeover={
                "labels": {
                    "pending": "custom-pending",
                    "approved": "custom-approved",
                },
                "require_tests": False,
                "review_max_attempts": 3,
            },
        )
        assert settings.task_takeover.require_tests is False
        assert settings.task_takeover.review_max_attempts == 3

        labels = settings.task_takeover.labels
        assert labels.pending == "custom-pending"
        assert labels.approved == "custom-approved"

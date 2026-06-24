"""Tests for PRD approval configuration settings."""

from forge.config import Settings


class TestPrdApprovalConfig:
    def test_default_proposals_repo_is_empty(self):
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_repo == ""

    def test_default_proposals_path(self):
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_path == ""

    def test_proposals_repo_can_be_set_as_global_fallback(self):
        settings = Settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
            prd_proposals_repo="org/proposals",
        )
        assert settings.prd_proposals_repo == "org/proposals"

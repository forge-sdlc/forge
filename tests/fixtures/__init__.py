"""Shared test fixtures for Forge test suite."""

from tests.fixtures.github_payloads import (
    API_GET_PR,
    WEBHOOK_CHECK_RUN_COMPLETED_FAILURE,
    WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS,
    WEBHOOK_PULL_REQUEST_REVIEW_APPROVED,
    make_check_run,
    make_github_pr,
)
from tests.fixtures.jira_payloads import (
    API_GET_ISSUE_BUG,
    API_GET_ISSUE_FEATURE,
    WEBHOOK_ISSUE_CREATED,
    WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED,
    WEBHOOK_ISSUE_UPDATED_LABEL_ADDED,
    make_jira_issue,
    make_jira_webhook,
)
from tests.fixtures.workflow_states import (
    STATE_CI_FAILED,
    STATE_COMPLETED,
    STATE_IMPLEMENTING,
    STATE_NEW_FEATURE,
    STATE_PLAN_APPROVED,
    STATE_PLAN_PENDING,
    STATE_PR_CREATED,
    STATE_PRD_APPROVED,
    STATE_PRD_PENDING,
    STATE_REVIEW_PENDING,
    STATE_SPEC_APPROVED,
    STATE_SPEC_PENDING,
    make_workflow_state,
)

__all__ = [
    # Jira payloads
    "WEBHOOK_ISSUE_CREATED",
    "WEBHOOK_ISSUE_UPDATED_LABEL_ADDED",
    "WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED",
    "API_GET_ISSUE_FEATURE",
    "API_GET_ISSUE_BUG",
    "make_jira_issue",
    "make_jira_webhook",
    # GitHub payloads
    "WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS",
    "WEBHOOK_CHECK_RUN_COMPLETED_FAILURE",
    "WEBHOOK_PULL_REQUEST_REVIEW_APPROVED",
    "API_GET_PR",
    "make_github_pr",
    "make_check_run",
    # Workflow states
    "STATE_NEW_FEATURE",
    "STATE_PRD_PENDING",
    "STATE_PRD_APPROVED",
    "STATE_SPEC_PENDING",
    "STATE_SPEC_APPROVED",
    "STATE_PLAN_PENDING",
    "STATE_PLAN_APPROVED",
    "STATE_IMPLEMENTING",
    "STATE_PR_CREATED",
    "STATE_CI_FAILED",
    "STATE_REVIEW_PENDING",
    "STATE_COMPLETED",
    "make_workflow_state",
]

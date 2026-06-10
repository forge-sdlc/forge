"""Pre-built WorkflowState snapshots for testing."""

from copy import deepcopy
from datetime import datetime
from typing import Any

from forge.models.workflow import TicketType
from forge.workflow.feature.state import FeatureState as WorkflowState


def _timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.utcnow().isoformat()


# Base state template
_BASE_STATE: WorkflowState = {
    "thread_id": "test-thread-001",
    "ticket_key": "TEST-123",
    "ticket_type": TicketType.FEATURE,
    "current_node": "start",
    "is_paused": False,
    "retry_count": 0,
    "last_error": None,
    "created_at": "2024-03-30T10:00:00",
    "updated_at": "2024-03-30T10:00:00",
    "prd_content": "",
    "spec_content": "",
    "epic_keys": [],
    "current_epic_key": None,
    "task_keys": [],
    "tasks_by_repo": {},
    "workspace_path": None,
    "pr_urls": [],
    "ci_status": None,
    "current_pr_url": None,
    "current_pr_number": None,
    "current_repo": None,
    "repos_to_process": [],
    "repos_completed": [],
    "implemented_tasks": [],
    "current_task_key": None,
    "parallel_execution_enabled": True,
    "parallel_branch_id": None,
    "parallel_total_branches": None,
    "ci_failed_checks": [],
    "ci_fix_attempts": 0,
    "ai_review_status": None,
    "ai_review_results": [],
    "human_review_status": None,
    "pr_merged": False,
    "tasks_completed": False,
    "epics_completed": False,
    "feature_completed": False,
    "rca_content": None,
    "bug_fix_implemented": False,
    "feedback_comment": None,
    "revision_requested": False,
    "messages": [],
    "context": {},
}


# STATE_NEW_FEATURE: Just created, no work done yet
STATE_NEW_FEATURE: WorkflowState = deepcopy(_BASE_STATE)

# STATE_PRD_PENDING: PRD generated, awaiting approval
STATE_PRD_PENDING: WorkflowState = {
    **deepcopy(_BASE_STATE),
    "current_node": "prd_approval_gate",
    "is_paused": True,
    "prd_content": """# Product Requirements Document

## Overview
This feature enables user authentication via email and password.

## User Stories
1. As a user, I want to log in with my credentials.
2. As a user, I want to receive clear error messages on failure.

## Success Criteria
- Login succeeds with valid credentials
- Clear error messages for invalid credentials
- Session persists across browser refresh
""",
}

# STATE_PRD_APPROVED: PRD approved, ready for spec generation
STATE_PRD_APPROVED: WorkflowState = {
    **deepcopy(STATE_PRD_PENDING),
    "current_node": "generate_spec",
    "is_paused": False,
}

# STATE_SPEC_PENDING: Spec generated, awaiting approval
STATE_SPEC_PENDING: WorkflowState = {
    **deepcopy(STATE_PRD_APPROVED),
    "current_node": "spec_approval_gate",
    "is_paused": True,
    "spec_content": """# Technical Specification

## User Scenarios

### US1: Login with Email/Password (P1)
**Given** a user with valid credentials
**When** they submit the login form
**Then** they are redirected to the dashboard

### US2: Login Failure Handling (P1)
**Given** a user with invalid credentials
**When** they submit the login form
**Then** they see an error message "Invalid email or password"

## Functional Requirements
- FR-001: Login endpoint accepts email and password
- FR-002: Passwords are validated against stored hash
- FR-003: Session token returned on success
""",
}

# STATE_SPEC_APPROVED: Spec approved, ready for epic decomposition
STATE_SPEC_APPROVED: WorkflowState = {
    **deepcopy(STATE_SPEC_PENDING),
    "current_node": "decompose_epics",
    "is_paused": False,
}

# STATE_PLAN_PENDING: Epics created, awaiting approval
STATE_PLAN_PENDING: WorkflowState = {
    **deepcopy(STATE_SPEC_APPROVED),
    "current_node": "plan_approval_gate",
    "is_paused": True,
    "epic_keys": ["TEST-124", "TEST-125", "TEST-126"],
}

# STATE_PLAN_APPROVED: Plan approved, ready for task generation
STATE_PLAN_APPROVED: WorkflowState = {
    **deepcopy(STATE_PLAN_PENDING),
    "current_node": "generate_tasks",
    "is_paused": False,
}

# STATE_IMPLEMENTING: Tasks generated, implementation in progress
STATE_IMPLEMENTING: WorkflowState = {
    **deepcopy(STATE_PLAN_APPROVED),
    "current_node": "implement_task",
    "task_keys": ["TEST-127", "TEST-128", "TEST-129", "TEST-130"],
    "tasks_by_repo": {
        "org/backend": ["TEST-127", "TEST-128"],
        "org/frontend": ["TEST-129", "TEST-130"],
    },
    "repos_to_process": ["org/backend", "org/frontend"],
    "current_repo": "org/backend",
    "current_task_key": "TEST-127",
    "workspace_path": "/tmp/forge-workspace-abc123",
}

# STATE_PR_CREATED: PR created, CI pending
STATE_PR_CREATED: WorkflowState = {
    **deepcopy(STATE_IMPLEMENTING),
    "current_node": "ci_evaluator",
    "workspace_path": None,
    "implemented_tasks": ["TEST-127", "TEST-128"],
    "repos_completed": ["org/backend"],
    "current_repo": "org/backend",
    "current_pr_url": "https://github.com/org/backend/pull/42",
    "current_pr_number": 42,
    "pr_urls": ["https://github.com/org/backend/pull/42"],
    "ci_status": "pending",
}

# STATE_CI_FAILED: CI failed, fix attempts remaining
STATE_CI_FAILED: WorkflowState = {
    **deepcopy(STATE_PR_CREATED),
    "current_node": "attempt_ci_fix",
    "ci_status": "failed",
    "ci_fix_attempts": 1,
    "ci_failed_checks": [
        {
            "name": "CI / Tests",
            "conclusion": "failure",
            "output": {
                "title": "2 tests failed",
                "summary": "test_login_validation, test_password_special_chars failed",
            },
        }
    ],
}

# STATE_REVIEW_PENDING: CI passed, AI review done, human review needed
STATE_REVIEW_PENDING: WorkflowState = {
    **deepcopy(STATE_PR_CREATED),
    "current_node": "human_review_gate",
    "is_paused": True,
    "ci_status": "passed",
    "ai_review_status": "approved",
    "ai_review_results": [
        {
            "reviewer": "forge-ai",
            "status": "approved",
            "comments": ["Code follows best practices", "Tests cover edge cases"],
        }
    ],
    "human_review_status": "pending",
}

# STATE_COMPLETED: Feature fully implemented and merged
STATE_COMPLETED: WorkflowState = {
    **deepcopy(STATE_REVIEW_PENDING),
    "current_node": "complete",
    "is_paused": False,
    "human_review_status": "approved",
    "pr_merged": True,
    "tasks_completed": True,
    "epics_completed": True,
    "feature_completed": True,
    "implemented_tasks": ["TEST-127", "TEST-128", "TEST-129", "TEST-130"],
    "repos_completed": ["org/backend", "org/frontend"],
    "pr_urls": [
        "https://github.com/org/backend/pull/42",
        "https://github.com/org/frontend/pull/43",
    ],
}


def make_workflow_state(
    ticket_key: str = "TEST-123",
    ticket_type: TicketType = TicketType.FEATURE,
    current_node: str = "start",
    is_paused: bool = False,
    base_state: WorkflowState | None = None,
    **overrides: Any,
) -> WorkflowState:
    """Factory function to create WorkflowState instances.

    Args:
        ticket_key: Jira ticket key.
        ticket_type: Type of ticket.
        current_node: Current graph node.
        is_paused: Whether workflow is paused.
        base_state: Optional base state to copy from.
        **overrides: Additional field overrides.

    Returns:
        WorkflowState dict.
    """
    state = deepcopy(base_state) if base_state else deepcopy(_BASE_STATE)

    state["ticket_key"] = ticket_key
    state["ticket_type"] = ticket_type
    state["current_node"] = current_node
    state["is_paused"] = is_paused
    state["updated_at"] = _timestamp()

    # Apply overrides
    for key, value in overrides.items():
        state[key] = value

    return state


# Bug workflow states
STATE_BUG_NEW: WorkflowState = {
    **deepcopy(_BASE_STATE),
    "ticket_key": "TEST-456",
    "ticket_type": TicketType.BUG,
    "current_node": "analyze_bug",
}

STATE_RCA_PENDING: WorkflowState = {
    **deepcopy(STATE_BUG_NEW),
    "current_node": "rca_approval_gate",
    "is_paused": True,
    "rca_content": """# Root Cause Analysis

## Problem
Login fails when password contains special characters ($@!).

## Root Cause
The password validation regex rejects valid special characters.
Located in: src/auth/validators.py:23

## Fix Options
1. Update regex to allow common special characters (recommended)
2. Escape special characters before validation

## Recommended Fix
Option 1: Update the VALID_PASSWORD_PATTERN constant to include $@!#%^&*

## Test Plan
1. Add test case with special character passwords
2. Verify existing tests still pass
""",
}

STATE_RCA_APPROVED: WorkflowState = {
    **deepcopy(STATE_RCA_PENDING),
    "current_node": "implement_bug_fix",
    "is_paused": False,
}

# New bug workflow redesign states

STATE_TRIAGE_PENDING: WorkflowState = {
    **deepcopy(STATE_BUG_NEW),
    "current_node": "triage_gate",
    "is_paused": True,
    "triage_passed": False,
    "triage_missing_fields": ["steps_to_reproduce", "affected_versions"],
}

STATE_RCA_OPTION_PENDING: WorkflowState = {
    **deepcopy(STATE_BUG_NEW),
    "current_node": "rca_option_gate",
    "is_paused": True,
    "triage_passed": True,
    "triage_missing_fields": [],
    "rca_content": """## Root Cause Analysis

The password validator rejects valid special characters ($@!) due to an overly
restrictive regex pattern in src/auth/validators.py:23.

Introduced in: commit abc1234 (PR #42, 2024-01-15)
Confidence: High — 90%
""",
    "rca_options": [
        {
            "title": "Update regex pattern",
            "description": "Extend VALID_PASSWORD_PATTERN to include $@! characters.",
            "tradeoffs": "Low risk; well-scoped to one constant. Must verify no other callers rely on old pattern.",
        },
        {
            "title": "Escape input before validation",
            "description": "Pre-process input to escape special characters before applying regex.",
            "tradeoffs": "Higher complexity; adds escaping step to auth pipeline.",
        },
    ],
}

STATE_BUG_PLAN_PENDING: WorkflowState = {
    **deepcopy(STATE_RCA_OPTION_PENDING),
    "current_node": "plan_approval_gate",
    "is_paused": True,
    "selected_fix_option": 0,  # 0-based index into rca_options
    "selected_fix_approach": {
        "title": "Update regex pattern",
        "description": "Extend VALID_PASSWORD_PATTERN to include $@! characters.",
        "tradeoffs": "Low risk; well-scoped to one constant.",
    },
    "plan_content": (
        "## Implementation Plan\n\n"
        "**Approach: Update regex pattern**\n\n"
        "### Changes\n"
        "1. `src/auth/validators.py` — update VALID_PASSWORD_PATTERN constant (repo:backend)\n"
        "2. `tests/auth/test_validators.py` — add regression test for special char passwords\n\n"
        "### Order of operations\n"
        "1. Update constant\n"
        "2. Run existing tests to confirm no regression\n"
        "3. Add new test verifying special chars accepted\n"
    ),
}

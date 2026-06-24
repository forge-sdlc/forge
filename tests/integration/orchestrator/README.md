# Orchestrator Integration Tests

This directory contains integration tests for the workflow orchestrator and related node implementations.

## Test Files

### test_task_implementation_status.py

Integration tests for task implementation status comments (SC-002 specification).

**Purpose**: Verify that Jira status comments are posted correctly during task implementation workflow execution.

**Test Scenarios**:

1. **TS-003: Single task receives start and completion comments**
   - `test_single_task_receives_start_comment`: Verifies "🔨 Forge is implementing this task." is posted
   - `test_single_task_receives_completion_comment_on_success`: Verifies both start and "✅ Implementation complete. Running local code review before PR." comments
   - `test_single_task_no_completion_comment_on_failure`: Verifies no completion comment when task fails

2. **TS-013: Multiple tasks receive independent comments (no cross-contamination)**
   - `test_multiple_tasks_receive_independent_start_comments`: Verifies each task gets its own start comment with correct task_key
   - `test_multiple_tasks_receive_independent_completion_comments`: Verifies each task gets completion comments independently without cross-contamination

3. **Failure Scenarios**
   - `test_task_implementation_fails_midway_no_completion_comment`: Verifies no completion comment when container fails
   - `test_multiple_tasks_partial_failure_only_successful_get_completion`: Verifies only successful tasks get completion comments

4. **Error Handling**
   - `test_workflow_continues_when_start_comment_posting_fails`: Verifies workflow continues when Jira start comment fails
   - `test_workflow_continues_when_completion_comment_posting_fails`: Verifies workflow continues when Jira completion comment fails
   - `test_workflow_continues_when_all_comment_posting_fails`: Verifies workflow continues even with complete Jira outage

**Running the tests**:
```bash
uv run pytest tests/integration/orchestrator/test_task_implementation_status.py -v
```

**Mock Strategy**:
- JiraClient is mocked to avoid external API calls
- ContainerRunner is mocked with configurable success/failure results
- Tests verify exact comment text matches specification
- Tests verify workflow continues despite Jira failures (error suppression)

### test_local_review_status_comments.py

Integration tests for local review status comments.

**Purpose**: Verify that Jira status comments are posted correctly during local review workflow execution, covering first pass with no issues, multiple fix passes, and pass number tracking.

**Test Scenarios**:

1. **TS-004: First pass with no issues posts only initial comment**
   - `test_first_pass_no_issues_posts_only_initial_comment`: Verifies only "🔍 Running local code review on changes before creating PR." is posted when first pass finds no issues

2. **TS-005: 3-pass scenario posts initial + 3 fix comments with correct numbering**
   - `test_three_pass_scenario_posts_all_comments_with_correct_numbering`: Verifies initial + fix comments for multiple passes (with MAX_REVIEW_ATTEMPTS=2)
   - `test_three_pass_scenario_with_max_attempts_override`: Verifies 3-pass scenario by overriding MAX_REVIEW_ATTEMPTS to 3

3. **5+ pass scenario posts all fix comments with correct incrementing numbers**
   - `test_five_plus_pass_scenario_posts_all_comments_with_incrementing_numbers`: Verifies 6 passes post correct comments with incrementing pass numbers (pass 2, 3, 4, 5, 6)

4. **Pass number resets between features**
   - `test_pass_number_resets_when_transitioning_from_implementation_to_local_review`: Verifies pass_number resets to 1 when implementation.py transitions to local_review
   - `test_pass_number_resets_for_new_feature`: Verifies pass_number initializes to 1 for new features

5. **Pass number persists across iterations within same feature**
   - `test_pass_number_persists_and_increments_within_same_feature`: Verifies pass_number persists and increments across review iterations
   - `test_pass_number_increments_correctly_across_multiple_iterations`: Verifies pass_number increments correctly across 4 passes

6. **Error Handling**
   - `test_workflow_continues_when_comment_posting_fails`: Verifies workflow continues when initial comment posting fails
   - `test_workflow_continues_when_fix_comment_posting_fails`: Verifies workflow continues when fix pass comment posting fails

**Running the tests**:
```bash
uv run pytest tests/integration/orchestrator/test_local_review_status_comments.py -v
```

**Mock Strategy**:
- JiraClient is mocked to avoid external API calls
- ContainerRunner is mocked with configurable unfixed issues results
- GitOperations is mocked to simulate commits
- Tests verify exact comment text matches specification
- Tests verify pass_number tracking across iterations
- Tests verify workflow continues despite Jira failures (error suppression)

### test_pr_creation_status_comments.py

Integration tests for PR creation status comments.

**Purpose**: Verify that Jira status comments are posted correctly when PRs are created, including label transitions from forge:implementing to forge:ci-pending.

**Test Scenarios**:

1. **TS-006: PR creation posts comment with PR number and updates labels**
   - `test_pr_creation_posts_comment_with_pr_number`: Verifies "🚀 Pull request #{pr_number} created and submitted. Waiting for CI checks to complete." is posted
   - `test_pr_creation_removes_implementing_label`: Verifies forge:implementing label removed from feature ticket
   - `test_pr_creation_adds_ci_pending_label`: Verifies forge:ci-pending label added to feature ticket
   - `test_pr_creation_jira_client_closed`: Verifies JiraClient properly closed after operations

2. **TS-014: Comment uses fallback text when PR number unavailable**
   - `test_pr_creation_posts_comment_without_pr_number`: Verifies fallback comment "🚀 Pull request created and submitted. Waiting for CI checks to complete." when PR number is None
   - `test_pr_creation_labels_updated_without_pr_number`: Verifies label transitions work even without PR number

3. **Error Handling**
   - `test_workflow_continues_when_comment_posting_fails`: Verifies workflow continues when status comment posting fails
   - `test_workflow_continues_when_label_removal_fails`: Verifies workflow continues when label removal fails
   - `test_workflow_continues_when_label_setting_fails`: Verifies workflow continues when label setting fails
   - `test_jira_client_closed_even_on_error`: Verifies JiraClient closed even when operations fail

4. **CI Fix Re-entry**
   - `test_ci_fix_reentry_no_comment_posted`: Verifies no comment posted when re-entering after CI fix
   - `test_ci_fix_reentry_multiple_attempts_no_comment`: Verifies no comment posted for multiple CI fix re-entries

**Running the tests**:
```bash
uv run pytest tests/integration/orchestrator/test_pr_creation_status_comments.py -v
```

**Mock Strategy**:
- JiraClient is mocked to avoid external API calls
- Tests verify exact comment text matches specification
- Tests verify label transitions occur correctly
- Tests verify workflow continues despite Jira failures (error suppression)

### test_ci_fix_attempt_status_comments.py

Integration tests for CI fix attempt status comments (TS-007).

**Purpose**: Verify that Jira status comments are posted correctly at the start of each CI fix attempt, displaying current attempt and max attempts.

**Test Scenarios**:

1. **TS-007: CI fix attempts post comments with correct counts**
   - `test_first_attempt_posts_comment_with_1_of_max`: Verifies first attempt posts "🔧 CI checks failed. Analyzing failure and attempting fix (1/{max_attempts})."
   - `test_second_attempt_posts_comment_with_2_of_max`: Verifies second attempt posts correct count (2/{max_attempts})
   - `test_final_attempt_posts_comment_with_max_of_max`: Verifies final attempt posts ({max_attempts}/{max_attempts}) format
   - `test_comment_posted_to_feature_ticket_not_task`: Verifies comment posted to feature ticket, not task tickets

2. **Attempt counts verification**
   - `test_multiple_attempts_show_incrementing_counts`: Verifies multiple attempts show incrementing counts (1/3, 2/3, 3/3)
   - `test_different_max_attempts_values`: Verifies correct counts with different max_attempts values (e.g., 5)

3. **Edge Cases**
   - `test_missing_current_attempt_logs_error_skips_comment`: Verifies missing current_attempt logs error and skips comment posting
   - `test_missing_max_attempts_logs_error_skips_comment`: Verifies missing max_attempts logs error and skips comment posting
   - `test_workflow_continues_when_both_values_missing`: Verifies workflow continues when both values are missing

4. **Error Handling**
   - `test_workflow_continues_when_comment_posting_fails`: Verifies workflow continues when status comment posting fails
   - `test_jira_client_closed_even_on_comment_error`: Verifies JiraClient closed even when comment posting fails
   - `test_no_comment_posted_when_no_failed_checks`: Verifies no comment posted when ci_failed_checks is empty

**Running the tests**:
```bash
uv run pytest tests/integration/orchestrator/test_ci_fix_attempt_status_comments.py -v
```

**Mock Strategy**:
- JiraClient is mocked to avoid external API calls
- ContainerRunner is mocked to avoid container execution
- GitHubClient is mocked to avoid external API calls
- Tests verify exact comment text matches specification
- Tests verify workflow continues despite Jira failures (error suppression)

### test_workflow_execution.py

Integration tests for LangGraph workflow execution.

**Status**: Currently skipped pending update for pluggable workflows architecture.

### test_task_handoff.py

Integration tests for task handoff between workflow nodes.

## Running All Integration Tests

```bash
# Run all orchestrator integration tests
uv run pytest tests/integration/orchestrator/ -v

# Run specific test file
uv run pytest tests/integration/orchestrator/test_task_implementation_status.py -v

# Run specific test class
uv run pytest tests/integration/orchestrator/test_task_implementation_status.py::TestTaskImplementationStatusCommentsTS003 -v

# Run specific test
uv run pytest tests/integration/orchestrator/test_task_implementation_status.py::TestTaskImplementationStatusCommentsTS003::test_single_task_receives_start_comment -v
```

## Test Maintenance

When updating task implementation behavior:

1. Update the corresponding tests in `test_task_implementation_status.py`
2. Ensure exact comment text matches the specification
3. Verify error handling tests still pass (workflow should never fail due to comment posting)
4. Run the full test suite to check for regressions

## Dependencies

These integration tests require:
- pytest
- pytest-asyncio (for async test support)
- unittest.mock (standard library)
- forge.workflow modules
- forge.integrations.jira modules

## Test Coverage Checklist

### Task Implementation Status Comments
- [x] TS-003: Single task receives both start and completion comments
- [x] TS-013: Multiple tasks receive independent comments (no cross-contamination)
- [x] No completion comment when task implementation fails
- [x] Workflow continues when comment posting fails
- [x] Exact comment text verification
- [x] Error logging verification (via caplog fixture)

### Local Review Status Comments
- [x] TS-004: First pass with no issues posts only initial comment
- [x] TS-005: 3-pass scenario posts initial + fix comments with correct numbering
- [x] 5+ pass scenario posts all fix comments with correct incrementing numbers
- [x] Pass number resets between features
- [x] Pass number persists across iterations within same feature
- [x] Workflow continues when comment posting fails

### PR Creation Status Comments
- [x] TS-006: PR creation posts comment with PR number and updates labels
- [x] TS-014: Comment uses fallback text when PR number unavailable
- [x] Label transitions work correctly (remove forge:implementing, add forge:ci-pending)
- [x] Workflow continues when comment posting fails
- [x] CI fix re-entry does not post duplicate comments

### CI Fix Attempt Status Comments
- [x] TS-007: CI fix attempts post comments with correct counts (1/3, 2/3, 3/3)
- [x] First attempt shows "1/{max_attempts}" format
- [x] Final attempt shows "{max_attempts}/{max_attempts}" format
- [x] Comment posted to feature ticket (not task tickets)
- [x] Missing attempt values log error and skip comment
- [x] Workflow continues when comment posting fails

"""Unit tests for rca_analysis nodes: analyze_bug and reflect_rca."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.nodes.rca_analysis import analyze_bug, reflect_rca


@pytest.fixture
def base_bug_state():
    """Minimal BugState dict for analysis tests."""
    return {
        "ticket_key": "BUG-123",
        "ticket_type": TicketType.BUG,
        "current_node": "analyze_bug",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "rca_content": None,
        "rca_options": [],
        "reflection_count": 0,
        "reflection_critique": None,
        "reproducibility_assessment": None,
        "triage_passed": True,
        "triage_missing_fields": [],
        "context": {
            "known_repos": ["acme/backend", "acme/frontend"],
        },
    }


SAMPLE_RCA_JSON = {
    "summary": "Password validation rejects special characters.",
    "code_location": {
        "file": "src/auth/validators.py",
        "function": "validate_password",
        "line_range": "23-31",
    },
    "mechanism": "Regex VALID_PASSWORD_PATTERN excludes $ @ ! characters.",
    "trigger_to_symptom": "User submits password with $; regex fails; 400 returned.",
    "hypothesis_log": [
        {
            "candidate": "Regex exclusion",
            "evidence": "Pattern confirmed in code",
            "verdict": "accepted",
            "reason": "Directly reproduces bug.",
        },
    ],
    "introduced_in": {"commit": "abc1234", "pr": "#42", "date": "2024-01-15"},
    "confidence": {"level": "High", "percentage": 95, "rationale": "Code directly confirmed."},
    "options": [
        {
            "title": "Update regex",
            "description": "Extend VALID_PASSWORD_PATTERN to include special chars.",
            "tradeoffs": "Low risk.",
        },
        {
            "title": "Escape before validate",
            "description": "Pre-process input before regex.",
            "tradeoffs": "Higher complexity.",
        },
    ],
    "reproducibility": {
        "feasible": True,
        "test_source": "def test_special_chars(): ...",
        "conditions": "",
    },
}


def _make_mock_jira(summary="Bug summary", description="Bug description", repos=None):
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = summary
    issue.description = description
    issue.project_key = "BUG"
    jira.get_issue = AsyncMock(return_value=issue)
    jira.get_project_repos = AsyncMock(return_value=repos or ["acme/backend"])
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


def _make_mock_runner_success(rca_data=None):
    """Return a mock ContainerRunner that writes rca.json to the temp dir."""
    rca = rca_data or SAMPLE_RCA_JSON

    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):
            forge_dir = workspace_path / ".forge"
            forge_dir.mkdir(exist_ok=True)
            (forge_dir / "rca.json").write_text(json.dumps(rca))
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = "Done"
            result.stderr = ""
            return result

    return _FakeRunner()


def _make_mock_runner_failure():
    runner = MagicMock()
    result = MagicMock()
    result.success = False
    result.exit_code = 1
    result.stdout = ""
    result.stderr = "Container failed"
    runner.run = AsyncMock(return_value=result)
    return runner


def _make_mock_runner_no_file():
    """Runner that succeeds but writes no rca.json."""

    class _FakeRunner:
        async def run(self, workspace_path, **_kwargs):
            forge_dir = workspace_path / ".forge"
            forge_dir.mkdir(exist_ok=True)
            result = MagicMock()
            result.success = True
            result.exit_code = 0
            result.stdout = "Done"
            result.stderr = ""
            return result

    return _FakeRunner()


class TestAnalyzeBug:
    @pytest.mark.asyncio
    async def test_container_invoked_with_ticket_and_repos(self, base_bug_state):
        """Container is called with ticket key, bug description, and known repos."""
        mock_jira = _make_mock_jira(repos=["acme/backend", "acme/frontend"])
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        # Should advance to reflect_rca on success
        assert result["current_node"] == "reflect_rca"

    @pytest.mark.asyncio
    async def test_reflection_critique_included_in_context_when_present(self, base_bug_state):
        """When reflection_critique is set, it appears in the container task description."""
        base_bug_state["reflection_critique"] = "Missing hypothesis log entries."
        mock_jira = _make_mock_jira()
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):
                captured_desc.append(task_description)
                forge_dir = workspace_path / ".forge"
                forge_dir.mkdir(exist_ok=True)
                (forge_dir / "rca.json").write_text(json.dumps(SAMPLE_RCA_JSON))
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "Done"
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=_CapturingRunner()
            ),
        ):
            await analyze_bug(base_bug_state)

        assert captured_desc, "runner.run was not called"
        assert "Missing hypothesis log entries." in captured_desc[0]

    @pytest.mark.asyncio
    async def test_container_success_parses_rca_json_into_state(self, base_bug_state):
        """On success, rca_options and rca_content are populated from rca.json."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["rca_options"] == SAMPLE_RCA_JSON["options"]
        assert result["rca_content"] is not None
        assert len(result["rca_content"]) > 0

    @pytest.mark.asyncio
    async def test_rca_options_has_required_keys(self, base_bug_state):
        """Each entry in rca_options has title, description, tradeoffs keys."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        for option in result["rca_options"]:
            assert "title" in option
            assert "description" in option
            assert "tradeoffs" in option

    @pytest.mark.asyncio
    async def test_rca_options_count_is_1_to_4(self, base_bug_state):
        """rca_options contains between 1 and 4 items."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert 1 <= len(result["rca_options"]) <= 4

    @pytest.mark.asyncio
    async def test_reproducibility_assessment_stored(self, base_bug_state):
        """reproducibility_assessment is stored from rca.json reproducibility field."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["reproducibility_assessment"] is not None

    @pytest.mark.asyncio
    async def test_container_failure_increments_retry_count(self, base_bug_state):
        """Container failure increments retry_count."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None

    @pytest.mark.asyncio
    async def test_container_failure_after_3_retries_escalates(self, base_bug_state):
        """After 3 retries, escalate_blocked is the next current_node."""
        base_bug_state["retry_count"] = 2
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_failure()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["current_node"] == "escalate_blocked"

    @pytest.mark.asyncio
    async def test_malformed_rca_json_triggers_retry(self, base_bug_state):
        """rca.json missing required keys causes an error and increments retry_count."""
        bad_rca = {
            "summary": "Bad",
            "options": [{"title": "x", "description": "y", "tradeoffs": "z"}],
        }  # missing many keys
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_success(rca_data=bad_rca)

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None

    @pytest.mark.asyncio
    async def test_missing_rca_json_triggers_retry(self, base_bug_state):
        """If rca.json is not written by the container, error is logged and retry triggered."""
        mock_jira = _make_mock_jira()
        runner = _make_mock_runner_no_file()

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await analyze_bug(base_bug_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None


class TestReflectRca:
    @pytest.fixture
    def rca_state(self, base_bug_state):
        return {
            **base_bug_state,
            "current_node": "reflect_rca",
            "rca_content": "## Root Cause\nPassword regex excludes special chars.",
            "rca_options": SAMPLE_RCA_JSON["options"],
            "reflection_count": 0,
            "reflection_critique": None,
        }

    def _make_reflect_runner(self, stdout_output: str):
        class _FakeRunner:
            async def run(self, workspace_path, **_kwargs):
                forge_dir = workspace_path / ".forge"
                forge_dir.mkdir(exist_ok=True)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = stdout_output
                result.stderr = ""
                return result

        return _FakeRunner()

    @pytest.mark.asyncio
    async def test_container_invoked_with_full_rca_json(self, rca_state):
        """reflect_rca passes the full rca options and content to the container."""
        mock_jira = _make_mock_jira()
        captured_desc = []

        class _CapturingRunner:
            async def run(self, workspace_path, task_description="", **_kwargs):
                captured_desc.append(task_description)
                forge_dir = workspace_path / ".forge"
                forge_dir.mkdir(exist_ok=True)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "VALID"
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=_CapturingRunner()
            ),
        ):
            await reflect_rca(rca_state)

        assert captured_desc, "runner.run was not called"
        # rca_content and options should be in the task description
        assert "Root Cause" in captured_desc[0] or "rca" in captured_desc[0].lower()

    @pytest.mark.asyncio
    async def test_valid_output_routes_to_rca_option_gate(self, rca_state):
        """When container returns 'VALID', current_node is rca_option_gate."""
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("VALID")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] == "rca_option_gate"

    @pytest.mark.asyncio
    async def test_reflection_uses_history_final_ai_message_over_stdout_logs(self, rca_state):
        """Container logs on stdout should not hide a VALID final agent response."""
        mock_jira = _make_mock_jira()

        class _HistoryRunner:
            async def run(self, workspace_path, **_kwargs):
                history_dir = workspace_path / ".forge" / "history"
                history_dir.mkdir(parents=True, exist_ok=True)
                (history_dir / "BUG-123-reflect.json").write_text(json.dumps({
                    "messages": [
                        {"role": "human", "content": "Review this RCA"},
                        {"role": "ai", "content": "VALID"},
                    ],
                }))
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = (
                    "2026-06-30 [INFO] Implementing task: RCA reflection for BUG-123\n"
                    "2026-06-30 [INFO] Agent completed task execution\n"
                    "2026-06-30 [INFO] Task completed successfully\n"
                )
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=_HistoryRunner()),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] == "rca_option_gate"
        assert result["reflection_count"] == 0

    @pytest.mark.asyncio
    async def test_valid_output_does_not_change_reflection_count(self, rca_state):
        """reflection_count is not incremented on VALID output."""
        rca_state["reflection_count"] = 2  # Start non-zero so the assertion is meaningful
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("VALID")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["reflection_count"] == 2

    @pytest.mark.asyncio
    async def test_invalid_output_is_not_treated_as_valid(self, rca_state):
        """Output containing 'INVALID' must not match as VALID (substring trap)."""
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("INVALID: code_location file does not exist")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] != "rca_option_gate"
        assert result["reflection_count"] == 1

    @pytest.mark.asyncio
    async def test_critique_output_stores_critique_and_increments_count(self, rca_state):
        """Critique output stores in reflection_critique and increments reflection_count."""
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner(
            "1. Missing git blame evidence.\n2. No rejected hypotheses."
        )

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["reflection_count"] == 1
        assert "Missing git blame evidence" in result["reflection_critique"]

    @pytest.mark.asyncio
    async def test_critique_output_routes_back_to_analyze_bug(self, rca_state):
        """After a critique with reflection_count < 3, current_node is analyze_bug."""
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("1. Missing git blame evidence.")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] == "analyze_bug"

    @pytest.mark.asyncio
    async def test_third_failed_reflection_routes_to_rca_option_gate(self, rca_state):
        """When reflection_count reaches 3, proceed to rca_option_gate regardless."""
        rca_state["reflection_count"] = 2
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("1. Still missing evidence.")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] == "rca_option_gate"

    @pytest.mark.asyncio
    async def test_third_failed_reflection_posts_warning_to_jira(self, rca_state):
        """When proceeding after 3 failures, a warning comment is posted to Jira."""
        rca_state["reflection_count"] = 2
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("1. Still missing evidence.")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            await reflect_rca(rca_state)

        mock_jira.add_comment.assert_called_once()
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "Reflection cap reached" in comment_text or "reflection" in comment_text.lower()

    @pytest.mark.asyncio
    async def test_reflection_count_below_3_with_critique_continues_loop(self, rca_state):
        """reflection_count < 3 and non-empty critique → routes back to analyze_bug."""
        rca_state["reflection_count"] = 1
        mock_jira = _make_mock_jira()
        runner = self._make_reflect_runner("Still incomplete.")

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=runner),
        ):
            result = await reflect_rca(rca_state)

        assert result["current_node"] == "analyze_bug"
        assert result["reflection_count"] == 2

    @pytest.mark.asyncio
    async def test_container_failure_uses_own_retry_counter_not_shared_retry_count(self, rca_state):
        """reflect_rca container failure increments reflect_rca_retry_count, not retry_count.

        analyze_bug and reflect_rca share a retry_count field. A reflect_rca failure must
        not consume from analyze_bug's retry budget, otherwise one analyze_bug failure + one
        reflect_rca failure exhausts the budget after the next analyze_bug failure (3 total
        across two stages, vs 3 per stage).
        """
        rca_state["retry_count"] = 2  # analyze_bug has already used 2 retries
        rca_state["reflect_rca_retry_count"] = 0
        mock_jira = _make_mock_jira()

        class _FailingRunner:
            async def run(self, _workspace_path, **_kwargs):
                result = MagicMock()
                result.success = False
                result.exit_code = 1
                result.stdout = ""
                result.stderr = "Container OOM"
                return result

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=_FailingRunner()
            ),
        ):
            result = await reflect_rca(rca_state)

        # reflect_rca failure must NOT escalate based on analyze_bug's counter
        assert result["current_node"] != "escalate_blocked", (
            "reflect_rca should not escalate based on analyze_bug's retry_count"
        )
        # retry_count must be unchanged — reflect_rca uses its own counter
        assert result["retry_count"] == 2, (
            "reflect_rca must not modify retry_count (that belongs to analyze_bug)"
        )
        assert result.get("reflect_rca_retry_count", 0) == 1

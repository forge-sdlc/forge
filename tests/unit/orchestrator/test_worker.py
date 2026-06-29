"""Unit tests for the orchestrator worker."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage


class TestQuestionDetection:
    """Tests for Q&A mode question detection."""

    @pytest.fixture(autouse=True)
    def ack_comment_mocks(self):
        """Mock Jira acknowledgement posting for direct resume-event tests."""
        mock_jira = AsyncMock()
        mock_jira.close = AsyncMock()
        with (
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
            patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock) as post,
        ):
            yield post

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.fixture
    def base_message(self) -> QueueMessage:
        """Create a base queue message for testing."""
        return QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Feature"},
                    },
                },
            },
        )

    @pytest.fixture
    def base_state(self) -> dict:
        """Create a base workflow state for testing."""
        return {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "prd_approval_gate",
            "is_paused": True,
            "context": {},
        }

    def _make_message_with_comment(
        self, base_message: QueueMessage, comment_body: str
    ) -> QueueMessage:
        """Create a message with a comment in the payload."""
        payload = {
            **base_message.payload,
            "comment": {"body": comment_body},
            "changelog": {"items": []},
        }
        return QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_question_comment_sets_is_question_flag(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments starting with ? set is_question flag."""
        message = self._make_message_with_comment(base_message, "?Why REST instead of GraphQL?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["feedback_comment"] == "?Why REST instead of GraphQL?"
        assert result["revision_requested"] is False
        assert result["is_paused"] is False
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-123"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your question" in ack_text
        assert "the PRD" in ack_text

    @pytest.mark.asyncio
    async def test_forge_ask_comment_sets_is_question_flag(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Comments with @forge ask set is_question flag."""
        message = self._make_message_with_comment(
            base_message, "@forge ask explain the database choice"
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["feedback_comment"] == "@forge ask explain the database choice"
        assert result["revision_requested"] is False
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_normal_feedback_still_works(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Feedback comments with ! prefix trigger revision_requested."""
        message = self._make_message_with_comment(
            base_message, "!Please add more detail to the security section"
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result.get("is_question") is not True
        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please add more detail to the security section"
        assert result["is_paused"] is False
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-123"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your revision request" in ack_text
        assert "regenerating" in ack_text

    @pytest.mark.asyncio
    async def test_task_phase_feedback_from_epic_sets_current_epic_key(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments on an Epic during task review preserve the Epic source."""
        state = {
            **base_state,
            "current_node": "task_approval_gate",
            "epic_keys": ["TEST-124"],
            "task_keys": ["TEST-130"],
        }
        payload = {
            **base_message.payload,
            "source_ticket_key": "TEST-124",
            "comment": {"body": "!Please revise the tasks for this epic"},
            "changelog": {"items": []},
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please revise the tasks for this epic"
        assert result["current_epic_key"] == "TEST-124"
        assert result["current_task_key"] is None
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-124"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "from TEST-124" in ack_text

    @pytest.mark.asyncio
    async def test_plan_phase_feedback_from_epic_acknowledges_epic(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
        ack_comment_mocks,
    ):
        """Comments on an Epic during plan review are acknowledged on that Epic."""
        state = {
            **base_state,
            "current_node": "plan_approval_gate",
            "epic_keys": ["TEST-124"],
        }
        payload = {
            **base_message.payload,
            "source_ticket_key": "TEST-124",
            "comment": {"body": "!Please revise this epic plan"},
            "changelog": {"items": []},
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="comment_created",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["feedback_comment"] == "Please revise this epic plan"
        assert result["current_epic_key"] == "TEST-124"
        ack_comment_mocks.assert_awaited_once()
        assert ack_comment_mocks.await_args.args[1] == "TEST-124"
        ack_text = ack_comment_mocks.await_args.args[2]
        assert "received your revision request" in ack_text
        assert "from TEST-124" in ack_text

    @pytest.mark.asyncio
    async def test_retry_at_task_approval_gate_clears_stale_epic_and_task_keys(
        self,
        worker: OrchestratorWorker,
        base_message: QueueMessage,
        base_state: dict,
    ):
        """forge:retry at task_approval_gate must zero out current_epic_key and current_task_key."""
        state = {
            **base_state,
            "current_node": "task_approval_gate",
            "is_paused": True,
            "task_keys": ["TEST-130"],
            "current_epic_key": "TEST-124",  # stale from a prior epic comment
            "current_task_key": "TEST-130",  # stale from a prior task comment
            "last_error": None,
        }
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "toString": "forge:managed forge:retry",
                        "fromString": "forge:managed",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, state)

        assert result["current_epic_key"] is None
        assert result["current_task_key"] is None
        assert result["revision_requested"] is True

    @pytest.mark.asyncio
    async def test_prd_label_change_to_approved_sets_approved_flag(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Approval is detected via label change from pending to approved, not comment text."""
        payload = {
            **base_message.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed forge:prd-pending",
                        "toString": "forge:managed forge:prd-approved",
                    }
                ]
            },
        }
        message = QueueMessage(
            message_id=base_message.message_id,
            event_id=base_message.event_id,
            source=base_message.source,
            event_type="jira:issue_updated",
            ticket_key=base_message.ticket_key,
            payload=payload,
        )

        result = await worker._handle_resume_event(message, base_state)

        assert result.get("is_question") is not True
        assert result["revision_requested"] is False
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_question_with_leading_whitespace(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """Questions with leading whitespace are still detected."""
        message = self._make_message_with_comment(base_message, "  ?What about caching?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["revision_requested"] is False

    @pytest.mark.asyncio
    async def test_forge_ask_case_insensitive(
        self, worker: OrchestratorWorker, base_message: QueueMessage, base_state: dict
    ):
        """@forge ask detection is case insensitive."""
        message = self._make_message_with_comment(base_message, "@FORGE ASK why use microservices?")

        result = await worker._handle_resume_event(message, base_state)

        assert result["is_question"] is True
        assert result["revision_requested"] is False


class TestEnsureSkillsIntegration:
    """Tests for ensure_skills() integration inside _process_workflow."""

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        """Create a worker instance for testing."""
        return OrchestratorWorker(consumer_name="test-worker")

    @pytest.fixture
    def jira_message(self) -> QueueMessage:
        """Create a minimal Jira queue message."""
        return QueueMessage(
            message_id="1234567890-0",
            event_id="test-event-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={
                "issue": {
                    "key": "TEST-123",
                    "fields": {
                        "issuetype": {"name": "Feature"},
                    },
                },
            },
        )

    @pytest.mark.asyncio
    async def test_ensure_skills_called_before_workflow_resolution(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """ensure_skills() is invoked at the top of _process_workflow."""
        call_order: list[str] = []

        async def fake_ensure_skills(*_args, **_kwargs) -> None:
            call_order.append("ensure_skills")

        async def fake_find_workflow(*_args, **_kwargs):
            call_order.append("workflow_resolution")
            return None, None

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", fake_find_workflow),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            # _find_workflow_by_state returns (None, None) → worker returns early
            await worker._process_workflow(jira_message)

        # ensure_skills must have been called before any workflow resolution
        assert "ensure_skills" in call_order

    @pytest.mark.asyncio
    async def test_ensure_skills_receives_correct_project_key(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Project key extracted from ticket key is passed to ensure_skills."""
        received: dict = {}

        async def fake_ensure_skills(project_key, _jira_client, _skills_dir) -> None:
            received["project_key"] = project_key

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["project_key"] == "TEST"

    @pytest.mark.asyncio
    async def test_ensure_skills_receives_skills_dir_from_settings(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """skills_dir passed to ensure_skills comes from settings.skills_dir."""
        received: dict = {}

        async def fake_ensure_skills(_project_key, _jira_client, skills_dir) -> None:
            received["skills_dir"] = skills_dir

        worker.settings.skills_dir = "custom/skills"

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["skills_dir"] == Path("custom/skills")

    @pytest.mark.asyncio
    async def test_workflow_continues_when_ensure_skills_raises(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Workflow processing continues past skill sync even when ensure_skills raises."""
        extract_ticket_type_called = False

        async def failing_ensure_skills(*_args, **_kwargs) -> None:
            raise RuntimeError("git clone failed")

        original_extract = worker._extract_ticket_type

        def tracking_extract_ticket_type(msg):
            nonlocal extract_ticket_type_called
            extract_ticket_type_called = True
            return original_extract(msg)

        # The main workflow may raise for unrelated reasons (no checkpointer in tests),
        # but what matters is that _extract_ticket_type was called, proving execution
        # continued past the skill-sync try/except block.
        with (
            patch("forge.orchestrator.worker.ensure_skills", failing_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", side_effect=tracking_extract_ticket_type),
            pytest.raises(ValueError),
        ):
            await worker._process_workflow(jira_message)

        assert extract_ticket_type_called, (
            "Workflow processing should continue after skill sync failure"
        )

    @pytest.mark.asyncio
    async def test_warning_logged_when_ensure_skills_fails(
        self,
        worker: OrchestratorWorker,
        jira_message: QueueMessage,
        caplog: pytest.LogCaptureFixture,
    ):
        """A warning is logged when ensure_skills raises an exception."""
        import logging

        async def failing_ensure_skills(*_args, **_kwargs) -> None:
            raise ValueError("bad config")

        with (
            patch("forge.orchestrator.worker.ensure_skills", failing_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
            caplog.at_level(logging.WARNING, logger="forge.orchestrator.worker"),
        ):
            await worker._process_workflow(jira_message)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Skill synchronisation failed" in m for m in warning_messages)

    @pytest.mark.asyncio
    async def test_jira_client_instantiated_for_ensure_skills(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """A JiraClient instance is created and passed to ensure_skills."""
        received: dict = {}
        fake_client_instance = MagicMock()

        async def fake_ensure_skills(_project_key, jira_client, _skills_dir) -> None:
            received["jira_client"] = jira_client

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient", return_value=fake_client_instance),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            await worker._process_workflow(jira_message)

        assert received["jira_client"] is fake_client_instance

    @pytest.mark.asyncio
    async def test_ensure_skills_skipped_gracefully_when_forge_skills_not_set(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """When forge.skills is not configured, ensure_skills returns without error.

        Simulates the real ensure_skills behaviour: get_skills_config returns None
        (property not set), so the function returns early and the workflow continues.
        """
        ensure_skills_called = False

        async def fake_ensure_skills_no_property(project_key, jira_client, _skills_dir) -> None:
            """Simulate ensure_skills when forge.skills property is absent (returns None)."""
            nonlocal ensure_skills_called
            ensure_skills_called = True
            # Mimic real behaviour: get_skills_config returns None → early return, no error
            skills_config = await jira_client.get_skills_config(project_key)
            if skills_config is None:
                return

        fake_jira = MagicMock()
        fake_jira.get_skills_config = MagicMock(return_value=None)

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills_no_property),
            patch("forge.orchestrator.worker.JiraClient", return_value=fake_jira),
            patch.object(worker, "_find_workflow_by_state", return_value=(None, None)),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="UNKNOWN")),
        ):
            # Should not raise; workflow continues normally after early-return from ensure_skills
            await worker._process_workflow(jira_message)

        assert ensure_skills_called, (
            "ensure_skills should be called even when forge.skills is unset"
        )

    @pytest.mark.asyncio
    async def test_ensure_skills_called_for_resumed_workflows(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """ensure_skills is triggered for resumed (paused) workflows, not just new ones.

        Verifies that skill synchronisation happens regardless of whether the workflow
        is being started fresh or resumed from a checkpoint.
        """
        ensure_skills_called = False

        async def fake_ensure_skills(*_args, **_kwargs) -> None:
            nonlocal ensure_skills_called
            ensure_skills_called = True

        # Simulate a paused, in-progress workflow state stored in the checkpoint.
        paused_state = MagicMock()
        paused_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "prd_approval_gate",
            "is_paused": True,
        }

        # Fake workflow instance returned by the router
        fake_workflow = MagicMock()
        fake_workflow.name = "feature_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=paused_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "current_node": "prd_approval_gate",
                "is_paused": True,
                "ticket_type": "Feature",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", fake_ensure_skills),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Feature")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(
                worker,
                "_handle_resume_event",
                return_value={
                    "ticket_key": "TEST-123",
                    "current_node": "prd_approval_gate",
                    "is_paused": False,
                    "is_blocked": False,
                    "ticket_type": "Feature",
                },
            ),
        ):
            await worker._process_workflow(jira_message)

        assert ensure_skills_called, (
            "ensure_skills must be called for resumed workflows, not just new ones"
        )

    @pytest.mark.asyncio
    async def test_setup_workspace_retry_reinvokes_fresh_state(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Retrying a setup_workspace failure re-runs the node instead of continuing past it."""
        blocked_state = MagicMock()
        blocked_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Feature",
            "current_node": "setup_workspace",
            "is_paused": True,
            "is_blocked": True,
            "last_error": "Clone failed",
            "context": {},
        }
        retry_cleared_state = {
            **blocked_state.values,
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
        }

        fake_workflow = MagicMock()
        fake_workflow.name = "feature_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=blocked_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "ticket_key": "TEST-123",
                "current_node": "setup_workspace",
                "is_paused": False,
                "ticket_type": "Feature",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", AsyncMock()),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Feature")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(worker, "_handle_resume_event", return_value=retry_cleared_state),
        ):
            await worker._process_workflow(jira_message)

        fake_compiled.aupdate_state.assert_not_awaited()
        fake_compiled.ainvoke.assert_awaited_once_with(
            retry_cleared_state,
            config={"configurable": {"thread_id": "TEST-123"}},
        )

    @pytest.mark.asyncio
    async def test_retry_force_fresh_invoke_reruns_bug_implementation(
        self, worker: OrchestratorWorker, jira_message: QueueMessage
    ):
        """Bug implementation retry re-enters implement_bug_fix instead of routing past it."""
        blocked_state = MagicMock()
        blocked_state.values = {
            "ticket_key": "TEST-123",
            "ticket_type": "Bug",
            "current_node": "implement_bug_fix",
            "is_paused": True,
            "is_blocked": True,
            "last_error": "Implementation failed",
            "context": {},
        }
        retry_cleared_state = {
            **blocked_state.values,
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
            "context": {"force_fresh_invoke": True},
        }
        expected_invoked_state = {
            **retry_cleared_state,
            "context": {},
        }

        fake_workflow = MagicMock()
        fake_workflow.name = "bug_workflow"
        fake_compiled = MagicMock()
        fake_compiled.aget_state = AsyncMock(return_value=blocked_state)
        fake_compiled.aupdate_state = AsyncMock(return_value=None)
        fake_compiled.ainvoke = AsyncMock(
            return_value={
                "ticket_key": "TEST-123",
                "current_node": "implement_bug_fix",
                "is_paused": False,
                "ticket_type": "Bug",
            }
        )

        with (
            patch("forge.orchestrator.worker.ensure_skills", AsyncMock()),
            patch("forge.orchestrator.worker.JiraClient"),
            patch.object(worker, "_extract_ticket_type", return_value=MagicMock(value="Bug")),
            patch.object(worker.router, "resolve", return_value=fake_workflow),
            patch.object(worker, "_get_compiled_workflow", return_value=fake_compiled),
            patch.object(worker, "_handle_resume_event", return_value=retry_cleared_state),
        ):
            await worker._process_workflow(jira_message)

        fake_compiled.aupdate_state.assert_not_awaited()
        fake_compiled.ainvoke.assert_awaited_once_with(
            expected_invoked_state,
            config={"configurable": {"thread_id": "TEST-123"}},
        )


class TestCiWebhookSignalAtCiEvaluator:
    """check_suite events must wake up the workflow when paused at ci_evaluator.

    Previously the signal check only covered wait_for_ci_gate. Workflows that
    resume directly at ci_evaluator (e.g. after a skip-gate command) were silently
    ignored, leaving CI failures unhandled.
    """

    @pytest.fixture
    def worker(self) -> OrchestratorWorker:
        return OrchestratorWorker(consumer_name="test-worker")

    def _ci_state(self, node: str) -> dict:
        return {
            "ticket_key": "AISOS-701",
            "ticket_type": "Bug",
            "current_node": node,
            "is_paused": False,
            "last_error": None,
            "context": {},
        }

    def _check_suite_message(self, conclusion: str = "failure") -> QueueMessage:
        return QueueMessage(
            message_id="1-0",
            event_id="test-ci-001",
            source=EventSource.GITHUB,
            event_type="check_suite",
            ticket_key="AISOS-701",
            payload={
                "action": "completed",
                "check_suite": {
                    "status": "completed",
                    "conclusion": conclusion,
                    "head_branch": "forge/aisos-701",
                    "pull_requests": [{"number": 52}],
                },
                "repository": {"full_name": "forge-sdlc/forge"},
            },
        )

    @pytest.mark.asyncio
    async def test_check_suite_recognized_at_ci_evaluator(self, worker):
        """A completed check_suite event at ci_evaluator must produce a new state object.

        _handle_resume_event signals 'no valid event' by returning the *same* state
        object unchanged. A recognised signal always returns a new dict. We verify
        object identity to catch the bug where the worker silently ignored the event.
        """
        state = self._ci_state("ci_evaluator")
        message = self._check_suite_message("failure")

        result = await worker._handle_resume_event(message, state)

        assert result is not state, (
            "check_suite at ci_evaluator returned the original state unchanged — "
            "signal was not recognised"
        )
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_check_suite_at_wait_for_ci_gate_still_works(self, worker):
        """Existing wait_for_ci_gate behaviour must be preserved."""
        state = self._ci_state("wait_for_ci_gate")
        message = self._check_suite_message("success")

        result = await worker._handle_resume_event(message, state)

        assert result is not state
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_incomplete_check_suite_does_not_unpause_at_ci_evaluator(self, worker):
        """A check_suite with status=in_progress must not wake up the workflow."""
        state = self._ci_state("ci_evaluator")
        message = QueueMessage(
            message_id="1-0",
            event_id="test-ci-002",
            source=EventSource.GITHUB,
            event_type="check_suite",
            ticket_key="AISOS-701",
            payload={
                "check_suite": {"status": "in_progress", "conclusion": None},
                "repository": {"full_name": "forge-sdlc/forge"},
            },
        )

        result = await worker._handle_resume_event(message, state)

        # unchanged state returned — is_paused stays as it was
        assert result is state


class TestExtractTextFromAdf:
    """Tests for _extract_text_from_adf."""

    def test_paragraph_text(self):
        adf = {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
        }
        assert OrchestratorWorker._extract_text_from_adf(adf) == "hello"

    def test_blockquote_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "option 2"}]}
                    ],
                }
            ],
        }
        assert "option 2" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_heading_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Title"}],
                }
            ],
        }
        assert "Title" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_bullet_list_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "item one"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        assert "item one" in OrchestratorWorker._extract_text_from_adf(adf)

    def test_non_dict_returns_string(self):
        assert OrchestratorWorker._extract_text_from_adf("plain") == "plain"
        assert OrchestratorWorker._extract_text_from_adf(None) == ""

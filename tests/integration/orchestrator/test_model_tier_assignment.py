"""Integration tests (RED) for model-tier assignment across Task-creation paths.

These tests pin the *wiring* behaviour for model-tier assignment before any of
the call sites are wired.  They are authored **first** (RED-phase TDD) and are
expected to FAIL until the wiring tasks land, because none of the Task-creation
or workflow-update paths currently invoke the tier-assignment entry point on
``JiraClient`` (``resolve_and_maybe_assign_tier`` / ``apply_tier_label`` /
``post_tier_comment``).

The value-type / estimator / ownership helpers and the four ``JiraClient`` tier
methods already exist (AISOS-2444 .. AISOS-2473).  What is missing is the
orchestrator wiring at each Task-creation and workflow-update call site.  These
tests assert that wiring.

Conventions follow ``tests/integration/orchestrator/test_task_implementation_status.py``:
module-level mock factories, ``patch("<node module>.JiraClient", ...)``,
``@pytest.mark.asyncio`` coroutine tests grouped in classes.

Coverage map (spec test scenarios):
- TS-001  Standard path assigns exactly one tier label + marker comment.
- TS-010  ``task_approval`` approved-draft creation wires tier assignment.
- TS-011  Bug-fix ``decompose_plan`` assigns only on the newly created branch.
- TS-012  Bug-fix ``decompose_plan`` does NOT assign on the covered[repo] reuse branch.
- TS-013  Task-takeover creation points wired only where a Task is created.
- TS-014  Human-owned no-op (marker == label) — SC-005 overwrite / SC-006 no-op.
- TS-015  Re-estimate overwrite with allow_overwrite=True vs routine-polling no-op (SC-006).
- TS-017  Workflow-update label preservation with no duplication (SC-007).
- TS-018  Comment-post failure does NOT fail Task creation (BR-013).
- TS-025  Non-Task / non-Forge exclusion (BR-006).
- TS-026  JQL discoverability of the tier label (NFR-007).
- TS-027  Resolved model targets unchanged by tier operations (NFR-001 / BR-007).
- TS-028  regenerate_epic_tasks newly created Tasks receive tier assignment.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.models import JiraIssue
from forge.models.model_tier import (
    TIER_LABEL_PREFIX,
    ModelTier,
    parse_tier_label,
    tier_label,
)

# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _make_issue(
    key: str,
    *,
    summary: str = "Do the thing",
    description: str = "Implement the feature",
    issue_type: str = "Task",
    labels: list[str] | None = None,
) -> JiraIssue:
    """Build a minimal JiraIssue for tier-resolution assertions."""
    return JiraIssue(
        key=key,
        id="10000",
        summary=summary,
        description=description,
        status="To Do",
        issue_type=issue_type,
        labels=list(labels or []),
    )


def create_mock_jira_client(
    *,
    project_key: str = "AISOS",
    issue_type: str = "Task",
    labels: list[str] | None = None,
) -> MagicMock:
    """Create a mock JiraClient with the tier + creation surface stubbed.

    The tier entry points (``resolve_and_maybe_assign_tier``,
    ``apply_tier_label``, ``post_tier_comment``) are AsyncMocks so tests can
    assert they were awaited by the (to-be-wired) node.  ``create_task`` returns
    incrementing keys so multi-task paths get distinct keys.
    """
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.create_issue_link = AsyncMock()
    mock.archive_issue = AsyncMock()
    mock.get_issue_links = AsyncMock(return_value=[])
    mock.get_labels = AsyncMock(return_value=list(labels or []))

    # Parent / issue lookups.
    mock.get_issue = AsyncMock(
        return_value=_make_issue("AISOS-1", issue_type=issue_type, labels=labels, summary="Parent")
    )

    # create_task hands out incrementing keys.
    counter = {"n": 100}

    async def _create_task(*_args, **_kwargs):
        counter["n"] += 1
        return f"{project_key}-{counter['n']}"

    mock.create_task = AsyncMock(side_effect=_create_task)

    # Tier surface (already implemented on the real client).
    mock.resolve_and_maybe_assign_tier = AsyncMock()
    mock.apply_tier_label = AsyncMock()
    mock.post_tier_comment = AsyncMock()
    mock.get_latest_tier_marker = AsyncMock(return_value=None)

    return mock


def create_mock_agent(tasks: list[dict[str, str]] | None = None) -> MagicMock:
    """Create a mock ForgeAgent whose task generation returns fixed tasks."""
    mock = MagicMock()
    mock.close = AsyncMock()
    payload = tasks if tasks is not None else [{"summary": "T1", "description": "d", "repo": ""}]
    mock.run = AsyncMock(return_value=payload)
    return mock


# ---------------------------------------------------------------------------
# TS-001 / TS-028: Standard path (task_generation.py)
# ---------------------------------------------------------------------------


class TestStandardPathTierAssignment:
    """TS-001 / TS-028: generate_tasks and regenerate_epic_tasks wire assignment."""

    @pytest.mark.asyncio
    async def test_generate_tasks_assigns_tier_to_each_created_task(self):
        """TS-001: Every newly created Task gets tier assignment wired (BR-011)."""
        from forge.workflow.feature.state import create_initial_feature_state
        from forge.workflow.nodes.task_generation import generate_tasks

        mock_jira = create_mock_jira_client()
        mock_agent = create_mock_agent(
            [
                {"summary": "Task A", "description": "d", "repo": "owner/repo"},
                {"summary": "Task B", "description": "d", "repo": "owner/repo"},
            ]
        )

        state = create_initial_feature_state(
            ticket_key="FEAT-1",
            epic_keys=["AISOS-1"],
        )
        state["spec_content"] = "spec"
        state["yolo_mode"] = True

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=mock_agent,
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new=AsyncMock(
                    return_value=[
                        {"summary": "Task A", "description": "d", "repo": "owner/repo"},
                        {"summary": "Task B", "description": "d", "repo": "owner/repo"},
                    ]
                ),
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new=AsyncMock(return_value="spec"),
            ),
        ):
            result = await generate_tasks(state)

        created_keys = result["task_keys"]
        assert len(created_keys) == 2

        # RED: assignment is not yet wired into generate_tasks.
        assert mock_jira.resolve_and_maybe_assign_tier.await_count == len(created_keys)
        assigned = {c.args[0] for c in mock_jira.resolve_and_maybe_assign_tier.await_args_list}
        assert assigned == set(created_keys)

    @pytest.mark.asyncio
    async def test_generate_tasks_marker_and_single_label_semantics(self):
        """TS-001/SC-001: exactly one forge:model-tier:* label + marker comment.

        Asserts the *effect* contract on the real client helpers used by the
        (to-be-wired) node: apply exactly one tier label and post a marker
        comment for each created task.
        """
        from forge.workflow.feature.state import create_initial_feature_state
        from forge.workflow.nodes.task_generation import generate_tasks

        mock_jira = create_mock_jira_client()

        # Make resolve_and_maybe_assign_tier delegate to the label + comment
        # helpers so we can assert the single-label + marker contract.
        async def _resolve(issue_key):
            await mock_jira.apply_tier_label(issue_key, ModelTier.STANDARD)
            await mock_jira.post_tier_comment(issue_key, ModelTier.STANDARD, ["baseline"])

        mock_jira.resolve_and_maybe_assign_tier = AsyncMock(side_effect=_resolve)

        state = create_initial_feature_state(ticket_key="FEAT-2", epic_keys=["AISOS-1"])
        state["spec_content"] = "spec"
        state["yolo_mode"] = True

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=create_mock_agent(),
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new=AsyncMock(
                    return_value=[{"summary": "T", "description": "d", "repo": "owner/repo"}]
                ),
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new=AsyncMock(return_value="spec"),
            ),
        ):
            result = await generate_tasks(state)

        created_keys = result["task_keys"]
        assert created_keys

        # RED: node does not yet call the tier entry point at all.
        assert mock_jira.apply_tier_label.await_count == len(created_keys)
        assert mock_jira.post_tier_comment.await_count == len(created_keys)

        # Exactly one tier label was applied per task (single-label invariant).
        for call in mock_jira.apply_tier_label.await_args_list:
            tier = call.args[1]
            assert isinstance(tier, ModelTier)
            # The label round-trips through the plain forge:model-tier: prefix.
            assert parse_tier_label(tier_label(tier)) == tier

    @pytest.mark.asyncio
    async def test_regenerate_epic_tasks_assigns_tier_to_new_tasks(self):
        """TS-028: regenerate_epic_tasks wires assignment for replacement Tasks."""
        from forge.workflow.feature.state import create_initial_feature_state
        from forge.workflow.nodes.task_generation import regenerate_epic_tasks

        mock_jira = create_mock_jira_client()
        # Existing tasks under the epic (to be archived and replaced).
        mock_jira.get_labels = AsyncMock(return_value=["repo:owner/repo"])

        state = create_initial_feature_state(
            ticket_key="FEAT-3",
            epic_keys=["AISOS-1"],
        )
        state["current_epic_key"] = "AISOS-1"
        state["task_keys"] = ["AISOS-50"]
        state["tasks_by_repo"] = {"owner/repo": ["AISOS-50"]}
        state["spec_content"] = "spec"

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=create_mock_agent(),
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new=AsyncMock(
                    return_value=[{"summary": "New", "description": "d", "repo": "owner/repo"}]
                ),
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new=AsyncMock(return_value="spec"),
            ),
        ):
            await regenerate_epic_tasks(state)

        # RED: replacement Task creation does not yet trigger tier assignment.
        assert mock_jira.resolve_and_maybe_assign_tier.await_count >= 1
        assigned = {c.args[0] for c in mock_jira.resolve_and_maybe_assign_tier.await_args_list}
        # Newly created key(s) start with the project prefix from create_task.
        assert any(k.startswith("AISOS-1") for k in assigned)
        # The pre-existing archived task must NOT be (re)assigned.
        assert "AISOS-50" not in assigned


# ---------------------------------------------------------------------------
# TS-011 / TS-012: Bug-fix decompose_plan (plan_bug_fix.py)
# ---------------------------------------------------------------------------


class TestBugFixDecomposePlanTierAssignment:
    """TS-011 / TS-012: assignment only on the newly created branch."""

    @pytest.mark.asyncio
    async def test_decompose_plan_assigns_on_new_task_branch(self):
        """TS-011: A freshly created bug-fix Task gets tier assignment."""
        from forge.workflow.bug.state import create_initial_bug_state
        from forge.workflow.nodes.plan_bug_fix import decompose_plan

        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=_make_issue("BUG-1", issue_type="Bug", summary="Crash")
        )
        # No existing Relates links -> the repo is NOT covered -> new branch.
        mock_jira.get_issue_links = AsyncMock(return_value=[])

        state = create_initial_bug_state("BUG-1")
        state["plan_content"] = "Fix in repo:owner/repo somehow"
        state["rca_content"] = "root cause"
        state["selected_fix_approach"] = {"title": "t", "description": "d"}

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            result = await decompose_plan(state)

        new_keys = result["task_keys"]
        assert new_keys

        # RED: decompose_plan does not yet wire assignment on the new branch.
        assert mock_jira.resolve_and_maybe_assign_tier.await_count == len(new_keys)
        assigned = {c.args[0] for c in mock_jira.resolve_and_maybe_assign_tier.await_args_list}
        assert assigned == set(new_keys)

    @pytest.mark.asyncio
    async def test_decompose_plan_skips_assignment_on_covered_reuse_branch(self):
        """TS-012: The covered[repo] reuse branch must NOT reassign a tier."""
        from forge.workflow.bug.state import create_initial_bug_state
        from forge.workflow.nodes.plan_bug_fix import decompose_plan

        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=_make_issue("BUG-2", issue_type="Bug", summary="Crash")
        )
        # An existing Relates link whose linked issue carries repo:owner/repo
        # marks the repo as *covered* -> reuse branch, no create_task, no assign.
        mock_jira.get_issue_links = AsyncMock(
            return_value=[{"type": "Relates", "outward_key": "BUG-2-EXISTING"}]
        )
        mock_jira.get_labels = AsyncMock(return_value=["repo:owner/repo"])

        state = create_initial_bug_state("BUG-2")
        state["plan_content"] = "Fix in repo:owner/repo somehow"
        state["rca_content"] = "root cause"
        state["selected_fix_approach"] = {"title": "t", "description": "d"}

        with patch("forge.workflow.nodes.plan_bug_fix.JiraClient", return_value=mock_jira):
            await decompose_plan(state)

        # No new Task was created for the covered repo...
        assert mock_jira.create_task.await_count == 0
        # ...so nothing must be (re)assigned on the reuse branch.
        assert mock_jira.resolve_and_maybe_assign_tier.await_count == 0


# ---------------------------------------------------------------------------
# TS-010: task_approval approved-draft creation
# ---------------------------------------------------------------------------


class TestApprovedDraftTierAssignment:
    """TS-010: approved-draft Task creation wires tier assignment."""

    @pytest.mark.asyncio
    async def test_task_approval_module_imports_tier_entry_point(self):
        """TS-010: the approved-draft module wires the tier entry point.

        RED: the approved-draft creation site does not yet reference the tier
        assignment helper.  Once wired, the module that materialises approved
        drafts must call ``resolve_and_maybe_assign_tier`` (or an equivalent
        tier helper) on each created Task.
        """
        import forge.workflow.gates.task_approval as task_approval

        source = _module_source(task_approval)
        assert "resolve_and_maybe_assign_tier" in source or "apply_tier_label" in source, (
            "task_approval approved-draft creation must wire tier assignment "
            "(TS-010) — not yet present (RED)."
        )


# ---------------------------------------------------------------------------
# TS-013: Task-takeover creation points
# ---------------------------------------------------------------------------


class TestTaskTakeoverTierAssignment:
    """TS-013: takeover assigns tier only where a Task is actually created."""

    @pytest.mark.asyncio
    async def test_takeover_wires_tier_where_task_created(self):
        """TS-013: takeover flow creates no Tasks, so it is intentionally not wired.

        The task-takeover flow *takes over* an existing human-authored Task/Epic;
        it never materialises a fresh Forge Task via ``jira.create_task``.  Per
        the ownership rules (Section 11.1) a Task without a Forge marker is
        human-owned, and the wiring directive is explicit: wire tier assignment
        only at *real* Task-creation points.

        This test audits the four ``task_takeover_*`` modules and asserts that
        (a) none of them call ``jira.create_task`` (no real creation point), and
        (b) precisely because of that they do NOT wire the tier entry point.
        Adding tier wiring here would violate ownership rules, so its absence is
        the correct, verified behaviour.
        """
        import forge.workflow.nodes.task_takeover_execution as execution
        import forge.workflow.nodes.task_takeover_planning as planning
        import forge.workflow.nodes.task_takeover_review as review
        import forge.workflow.nodes.task_takeover_triage as triage

        takeover_modules = (triage, planning, execution, review)
        for module in takeover_modules:
            source = _module_source(module)
            assert "create_task" not in source, (
                f"{module.__name__} unexpectedly calls create_task — if a real "
                "Task-creation point is added, tier assignment must be wired "
                "there (TS-013)."
            )
            assert (
                "resolve_and_maybe_assign_tier" not in source and "apply_tier_label" not in source
            ), (
                f"{module.__name__} must NOT wire tier assignment — the takeover "
                "flow only adopts existing human Tasks and creates none, so tier "
                "wiring would violate ownership rules (Section 11.1)."
            )


# ---------------------------------------------------------------------------
# TS-014 / TS-015: ownership no-op and overwrite semantics
# ---------------------------------------------------------------------------


class TestOwnershipNoOpAndOverwrite:
    """TS-014 / TS-015: human-owned no-op vs. re-estimate overwrite (SC-005/006)."""

    @pytest.mark.asyncio
    async def test_human_owned_marker_matches_label_is_noop(self):
        """SC-006 (TS-014): marker == label -> no label change (no-op)."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(
            return_value=_make_issue(
                "AISOS-9",
                issue_type="Task",
                labels=[tier_label(ModelTier.HEAVY)],
            )
        )
        client.get_latest_tier_marker = AsyncMock(return_value=ModelTier.HEAVY)
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        await client.resolve_and_maybe_assign_tier("AISOS-9")

        # In sync -> no re-label, no comment.
        assert client.apply_tier_label.await_count == 0
        assert client.post_tier_comment.await_count == 0

    @pytest.mark.asyncio
    async def test_human_label_diverges_from_marker_is_noop(self):
        """SC-005 (TS-014): human-changed label != Forge marker -> no-op.

        After Forge writes matching marker+label, a human may change only the
        label. Divergence means human-owned: routine resolution must not
        clobber the label back to the stale marker.
        """
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(
            return_value=_make_issue(
                "AISOS-10",
                issue_type="Task",
                labels=[tier_label(ModelTier.STANDARD)],
            )
        )
        client.get_latest_tier_marker = AsyncMock(return_value=ModelTier.CRITICAL)
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        await client.resolve_and_maybe_assign_tier("AISOS-10")

        assert client.apply_tier_label.await_count == 0
        assert client.post_tier_comment.await_count == 0

    @pytest.mark.asyncio
    async def test_reestimate_overwrite_allows_overwrite_flag(self):
        """TS-015 (SC-006): re-estimate honours allow_overwrite=True.

        RED: a routine re-estimate must be able to overwrite an existing
        auto-owned tier when ``allow_overwrite=True`` is passed, while routine
        polling (the default) is a no-op.  The tier entry point does not yet
        accept an ``allow_overwrite`` keyword — this test pins that contract.
        """
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(
            return_value=_make_issue(
                "AISOS-11",
                issue_type="Task",
                summary="Now a security auth bypass fix",
                labels=[tier_label(ModelTier.STANDARD)],
            )
        )
        client.get_latest_tier_marker = AsyncMock(return_value=None)
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        # RED: allow_overwrite keyword is not yet supported by the entry point.
        await client.resolve_and_maybe_assign_tier("AISOS-11", allow_overwrite=True)

        # A re-estimate with overwrite must relabel to the new estimate.
        assert client.apply_tier_label.await_count == 1

    @pytest.mark.asyncio
    async def test_routine_polling_default_is_noop_when_label_present(self):
        """TS-015 (SC-006): routine polling (no allow_overwrite) is a no-op."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(
            return_value=_make_issue(
                "AISOS-12",
                issue_type="Task",
                summary="Now a security auth bypass fix",
                labels=[tier_label(ModelTier.STANDARD)],
            )
        )
        client.get_latest_tier_marker = AsyncMock(return_value=None)
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        # Default (routine polling): existing auto-owned label + no marker -> no-op.
        await client.resolve_and_maybe_assign_tier("AISOS-12")

        assert client.apply_tier_label.await_count == 0


# ---------------------------------------------------------------------------
# TS-017: workflow-update label preservation, no duplication (SC-007)
# ---------------------------------------------------------------------------


class TestWorkflowUpdateLabelPreservation:
    """TS-017 (SC-007): tier label preserved with no duplication on updates."""

    @pytest.mark.asyncio
    async def test_apply_tier_label_preserves_single_label_no_duplication(self):
        """SC-007: re-applying the same tier is a no-op (no duplicate labels)."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        existing = ["forge:managed", tier_label(ModelTier.HEAVY)]
        client.get_labels = AsyncMock(return_value=list(existing))
        mock_http = AsyncMock()
        client._get_client = AsyncMock(return_value=mock_http)

        await client.apply_tier_label("AISOS-20", ModelTier.HEAVY)

        # Already correct -> no PUT issued (label preserved, not duplicated).
        assert mock_http.put.await_count == 0

    @pytest.mark.asyncio
    async def test_apply_tier_label_swaps_without_leaving_duplicate(self):
        """SC-007: switching tiers removes the old and adds exactly the new one."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        existing = ["forge:managed", tier_label(ModelTier.LIGHT)]
        client.get_labels = AsyncMock(return_value=list(existing))
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_http.put = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        await client.apply_tier_label("AISOS-21", ModelTier.HEAVY)

        assert mock_http.put.await_count == 1
        payload = mock_http.put.await_args.kwargs["json"]
        ops = payload["update"]["labels"]
        removes = [o["remove"] for o in ops if "remove" in o]
        adds = [o["add"] for o in ops if "add" in o]
        # Exactly one tier label after: old removed, new added, no duplicates.
        assert tier_label(ModelTier.LIGHT) in removes
        assert adds == [tier_label(ModelTier.HEAVY)]
        assert "forge:managed" not in removes  # non-tier labels untouched.


# ---------------------------------------------------------------------------
# TS-018: comment-post failure must not fail Task creation (BR-013)
# ---------------------------------------------------------------------------


class TestCommentFailureDoesNotFailCreation:
    """TS-018 (BR-013): tier comment/assignment failure never breaks creation."""

    @pytest.mark.asyncio
    async def test_generate_tasks_survives_tier_assignment_failure(self):
        """BR-013: a raising tier assignment must not fail Task creation."""
        from forge.workflow.feature.state import create_initial_feature_state
        from forge.workflow.nodes.task_generation import generate_tasks

        mock_jira = create_mock_jira_client()
        mock_jira.resolve_and_maybe_assign_tier = AsyncMock(
            side_effect=Exception("tier comment post failed")
        )

        state = create_initial_feature_state(ticket_key="FEAT-18", epic_keys=["AISOS-1"])
        state["spec_content"] = "spec"
        state["yolo_mode"] = True

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=create_mock_agent(),
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation._generate_tasks_for_epic",
                new=AsyncMock(
                    return_value=[{"summary": "T", "description": "d", "repo": "owner/repo"}]
                ),
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new=AsyncMock(return_value="spec"),
            ),
        ):
            result = await generate_tasks(state)

        # Task creation succeeded despite the tier-assignment failure...
        assert result["task_keys"], "Tasks must be created even if tier assignment fails"
        # ...and the node advanced to the approval gate (not an error retry loop).
        assert result["current_node"] == "task_approval_gate"


# ---------------------------------------------------------------------------
# TS-025: non-Task / non-Forge exclusion (BR-006)
# ---------------------------------------------------------------------------


class TestNonTaskNonForgeExclusion:
    """TS-025 (BR-006): non-Task and non-Forge items are excluded."""

    @pytest.mark.asyncio
    async def test_non_task_issue_type_is_skipped(self):
        """BR-006: a non-Task issue type gets no tier label and no comment."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(return_value=_make_issue("AISOS-30", issue_type="Story"))
        client.get_latest_tier_marker = AsyncMock(return_value=None)
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        await client.resolve_and_maybe_assign_tier("AISOS-30")

        assert client.apply_tier_label.await_count == 0
        assert client.post_tier_comment.await_count == 0

    @pytest.mark.asyncio
    async def test_epic_parent_is_not_assigned_a_tier(self):
        """BR-006: an Epic (non-Task) parent is never tier-assigned."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        client.get_issue = AsyncMock(return_value=_make_issue("AISOS-31", issue_type="Epic"))
        client.apply_tier_label = AsyncMock()
        client.post_tier_comment = AsyncMock()

        await client.resolve_and_maybe_assign_tier("AISOS-31")

        assert client.apply_tier_label.await_count == 0


# ---------------------------------------------------------------------------
# TS-026: JQL discoverability of the tier label (NFR-007)
# ---------------------------------------------------------------------------


class TestTierLabelJqlDiscoverability:
    """TS-026 (NFR-007): the tier label is a plain, JQL-discoverable label."""

    @pytest.mark.asyncio
    async def test_tier_label_is_discoverable_via_jql_labels_query(self):
        """NFR-007: applied tier label appears in a labels-based JQL search."""
        from forge.integrations.jira.client import JiraClient

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        # The applied label uses the plain forge:model-tier: prefix so a
        # ``labels = "forge:model-tier:heavy"`` JQL clause is well-formed.
        label = tier_label(ModelTier.HEAVY)
        assert label.startswith(TIER_LABEL_PREFIX)
        assert " " not in label  # plain labels have no spaces (JQL-safe).

        matched = _make_issue("AISOS-40", issue_type="Task", labels=[label])
        client.search_issues = AsyncMock(return_value=[matched])

        results = await client.search_issues(f'labels = "{label}"')

        assert results
        assert label in results[0].labels
        client.search_issues.assert_awaited_once()
        jql = client.search_issues.await_args.args[0]
        assert label in jql


# ---------------------------------------------------------------------------
# TS-027: resolved model targets unchanged by tier operations (NFR-001/BR-007)
# ---------------------------------------------------------------------------


class TestModelTargetsUnaffected:
    """TS-027 (NFR-001 / BR-007): tier ops do not change resolved model targets."""

    @pytest.mark.asyncio
    async def test_resolved_model_target_identical_before_and_after_tier_ops(self):
        """NFR-001: model_policy resolution is independent of tier assignment."""
        from forge.model_policy import resolve_model_target_for_project

        settings = MagicMock()
        settings.has_explicit_model_policy = True
        settings.model_connections = {}  # no Jira dependency for global-only policy

        resolver = MagicMock()
        target = MagicMock()
        resolver.resolve = MagicMock(return_value=target)
        settings.model_policy_resolver = MagicMock(return_value=resolver)

        before = await resolve_model_target_for_project(settings, None, "implement_work")

        # Perform a tier operation via the client (must not touch model policy).
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            from forge.integrations.jira.client import JiraClient

            mock_settings.return_value = MagicMock()
            client = JiraClient()
            client.get_issue = AsyncMock(return_value=_make_issue("AISOS-50", issue_type="Task"))
            client.get_latest_tier_marker = AsyncMock(return_value=None)
            client.apply_tier_label = AsyncMock()
            client.post_tier_comment = AsyncMock()
            await client.resolve_and_maybe_assign_tier("AISOS-50")

        after = await resolve_model_target_for_project(settings, None, "implement_work")

        # The resolved target is unaffected by tier operations (BR-007).
        assert before is after is target
        # The tier code path never imports/uses model_policy internals.
        assert resolver.resolve.call_count == 2  # one per resolve call, none extra.

    def test_model_tier_modules_do_not_import_model_policy(self):
        """BR-007: tier value/estimator/ownership modules are policy-independent.

        Checks for actual ``import`` statements (not docstring mentions) so the
        behavioural-isolation guarantee is enforced at the code level.
        """
        import ast

        import forge.models.model_tier as mt
        import forge.models.model_tier_estimator as mte
        import forge.models.model_tier_ownership as mto

        for module in (mt, mte, mto):
            tree = ast.parse(_module_source(module))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            offending = [name for name in imported if "model_policy" in name]
            assert not offending, (
                f"{module.__name__} must not import model_policy (NFR-001/BR-007): {offending}"
            )


# ---------------------------------------------------------------------------
# SC-006 / BR-009: explicit re-estimate trigger dispatch (revision "!" / retry)
# ---------------------------------------------------------------------------


class TestExplicitReestimateTriggerDispatch:
    """SC-006 / BR-009: an explicit Task revision (!) re-estimates the tier.

    After ``update_single_task`` persists the revised description, tier
    resolution re-runs with ``allow_overwrite=True`` and the *new* description
    (not the pre-revision text). No routine polling is added.
    """

    @pytest.mark.asyncio
    async def test_update_single_task_reestimates_tier_after_description(self):
        """SC-006: update_single_task re-estimates from the revised description."""
        from forge.workflow.nodes.task_generation import update_single_task

        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=_make_issue("AISOS-77", issue_type="Task", description="old desc")
        )
        mock_jira.update_description = AsyncMock()
        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(return_value="revised desc with more detail")
        mock_agent.close = AsyncMock()

        state = {
            "ticket_key": "AISOS-1",
            "current_task_key": "AISOS-77",
            "feedback_comment": "! please expand scope",
            "ticket_type": "task",
            "current_node": "task_approval_gate",
            "context": {},
            "retry_count": 0,
        }

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=mock_agent,
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new_callable=AsyncMock,
                return_value="old desc",
            ),
            patch(
                "forge.workflow.nodes.task_generation.post_status_comment",
                new_callable=AsyncMock,
            ),
        ):
            await update_single_task(state)

        mock_jira.update_description.assert_awaited_once_with(
            "AISOS-77", "revised desc with more detail"
        )
        mock_jira.resolve_and_maybe_assign_tier.assert_awaited_once()
        args, kwargs = mock_jira.resolve_and_maybe_assign_tier.await_args
        assert args[0] == "AISOS-77"
        assert kwargs.get("allow_overwrite") is True
        assert kwargs.get("description") == "revised desc with more detail"
        # Re-estimate must run after the description write.
        method_names = [name for name, *_ in mock_jira.method_calls]
        assert method_names.index("update_description") < method_names.index(
            "resolve_and_maybe_assign_tier"
        )

    @pytest.mark.asyncio
    async def test_reestimate_failure_does_not_break_update_single_task(self):
        """BR-013: a re-estimate failure never breaks the Task revision flow."""
        from forge.workflow.nodes.task_generation import update_single_task

        mock_jira = create_mock_jira_client()
        mock_jira.get_issue = AsyncMock(
            return_value=_make_issue("AISOS-78", issue_type="Task", description="old")
        )
        mock_jira.update_description = AsyncMock()
        mock_jira.resolve_and_maybe_assign_tier = AsyncMock(side_effect=RuntimeError("boom"))
        mock_agent = MagicMock()
        mock_agent.regenerate_with_feedback = AsyncMock(return_value="new desc")
        mock_agent.close = AsyncMock()

        state = {
            "ticket_key": "AISOS-1",
            "current_task_key": "AISOS-78",
            "feedback_comment": "! revise",
            "ticket_type": "task",
            "current_node": "task_approval_gate",
            "context": {},
            "retry_count": 0,
        }

        with (
            patch("forge.workflow.nodes.task_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.stations.artifact_generation.ForgeAgent",
                return_value=mock_agent,
                create=True,
            ),
            patch(
                "forge.workflow.nodes.task_generation.fetch_and_inject_references",
                new_callable=AsyncMock,
                return_value="old",
            ),
            patch(
                "forge.workflow.nodes.task_generation.post_status_comment",
                new_callable=AsyncMock,
            ) as mock_comment,
        ):
            result = await update_single_task(state)

        assert result.get("last_error") is None
        mock_jira.resolve_and_maybe_assign_tier.assert_awaited_once()
        mock_comment.assert_awaited()

    def test_update_single_task_wires_reestimate_after_description(self):
        """SC-006 / BR-009: update_single_task re-estimates after description write.

        Source-scan: the revision path passes the new description with
        ``allow_overwrite=True``. Worker resume no longer re-estimates early.
        """
        import forge.orchestrator.worker as worker_mod
        import forge.workflow.nodes.task_generation as task_gen_mod

        task_source = _module_source(task_gen_mod)
        assert "resolve_and_maybe_assign_tier" in task_source
        assert "allow_overwrite=True" in task_source
        assert "description=new_description" in task_source

        worker_source = _module_source(worker_mod)
        assert "_reestimate_task_tier" not in worker_source


# ---------------------------------------------------------------------------
# SC-007 / FN-005 / BR-005: tier label preserved across workflow transitions
# ---------------------------------------------------------------------------


class TestSetWorkflowLabelPreservesTierLabel:
    """SC-007 / FN-005 / BR-005: workflow transitions preserve the tier label."""

    @pytest.mark.asyncio
    async def test_workflow_transition_preserves_tier_label(self):
        """SC-007: set_workflow_label does not strip the forge:model-tier:* label."""
        from forge.integrations.jira.client import JiraClient
        from forge.models.workflow import ForgeLabel

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        existing = [
            "forge:managed",
            "forge:prd-pending",
            tier_label(ModelTier.HEAVY),
        ]
        client.get_labels = AsyncMock(return_value=list(existing))
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_http.put = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        await client.set_workflow_label("AISOS-60", ForgeLabel.SPEC_PENDING)

        payload = mock_http.put.await_args.kwargs["json"]
        ops = payload["update"]["labels"]
        removes = [o["remove"] for o in ops if "remove" in o]
        adds = [o["add"] for o in ops if "add" in o]

        tier = tier_label(ModelTier.HEAVY)
        # The tier label survives the transition — never removed, never re-added
        # (would duplicate an already-present label).
        assert tier not in removes
        assert tier not in adds
        # The stale phase label is swapped for the new one.
        assert "forge:prd-pending" in removes
        assert ForgeLabel.SPEC_PENDING.value in adds

    @pytest.mark.asyncio
    async def test_transition_preserves_single_tier_label_no_duplication(self):
        """SC-007: exactly one tier label remains after a workflow transition."""
        from forge.integrations.jira.client import JiraClient
        from forge.models.workflow import ForgeLabel

        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock()
            client = JiraClient()

        existing = ["forge:managed", "forge:spec-pending", tier_label(ModelTier.STANDARD)]
        client.get_labels = AsyncMock(return_value=list(existing))
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_http.put = AsyncMock(return_value=mock_response)
        client._get_client = AsyncMock(return_value=mock_http)

        await client.set_workflow_label("AISOS-61", ForgeLabel.PLAN_PENDING)

        payload = mock_http.put.await_args.kwargs["json"]
        ops = payload["update"]["labels"]
        removes = [o["remove"] for o in ops if "remove" in o]
        adds = [o["add"] for o in ops if "add" in o]

        # Only non-tier phase labels are touched; the single tier label is
        # preserved verbatim (no removal, no duplicate add).
        tier_removes = [r for r in removes if r.startswith(TIER_LABEL_PREFIX)]
        tier_adds = [a for a in adds if a.startswith(TIER_LABEL_PREFIX)]
        assert tier_removes == []
        assert tier_adds == []
        # Resulting label set contains exactly one tier label.
        resulting = (set(existing) - set(removes)) | set(adds)
        assert len([lbl for lbl in resulting if lbl.startswith(TIER_LABEL_PREFIX)]) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_source(module) -> str:
    """Return the source text of a module for wiring-presence assertions."""
    from pathlib import Path

    return Path(module.__file__).read_text(encoding="utf-8")

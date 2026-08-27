"""CI/CD evaluator node for monitoring and responding to CI results."""

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

from forge.api.routes.metrics import record_ci_fix_attempt
from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.integrations.source_control.contracts import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    RepositoryRef,
    SourceControlProvider,
)
from forge.integrations.source_control.errors import NotFoundError
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.code_review import run_post_change_review, sync_pr_description
from forge.workflow.nodes.error_handler import notify_error
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.pr_state import find_active_pull_request
from forge.workflow.utils import merge_review_exhaustion, update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    set_review_pending_label,
)
from forge.workflow.utils.source_control import get_adapter, identity_for
from forge.workspace.git_ops import GitOperations
from forge.workspace.handoff import capture_handoff
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def evaluate_ci_status(state: WorkflowState) -> WorkflowState:
    """Evaluate CI status for the current PR.

    This node:
    1. Checks CI status from GitHub
    2. Routes based on pass/fail
    3. On failure, attempts autonomous fix if retries remain

    Args:
        state: Current workflow state.

    Returns:
        Updated state with ci_status.
    """
    ticket_key = state["ticket_key"]
    current_pr_url = state.get("current_pr_url")
    pull_requests = state.get("pull_requests", {})
    _, active_pr = find_active_pull_request(
        pull_requests,
        state.get("current_repo", ""),
        state.get("current_pr_number"),
        current_pr_url,
    )
    if pull_requests:
        if not (
            isinstance(active_pr, dict)
            and active_pr.get("number") == state.get("current_pr_number")
            and current_pr_url
        ):
            logger.error("Cannot evaluate CI: active PR state is inconsistent for %s", ticket_key)
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "failed",
                    "current_node": "ci_evaluator",
                    "last_error": "Active pull request state is inconsistent",
                    "pending_ci_event": False,
                }
            )
        pr_urls = [current_pr_url]
    else:
        pr_urls = state.get("pr_urls", [])
    ci_fix_attempt = state.get("ci_fix_attempt", 0)
    ci_fix_max = state.get("ci_fix_max_attempts", 5)
    settings = get_settings()

    if not pr_urls:
        logger.info(f"No PRs to evaluate for {ticket_key}")
        return update_state_timestamp(
            {
                **state,
                "ci_status": "no_prs",
                "current_node": "complete",
                "pending_ci_event": False,
            }
        )

    logger.info(f"Evaluating CI status for {ticket_key}")

    try:
        # Checks whose name contains any of these substrings are treated as passing
        ci_skipped_checks = state.get("ci_skipped_checks", [])

        def _is_skipped(check: CheckRun) -> bool:
            return any(skip.lower() in check.name.lower() for skip in ci_skipped_checks)

        # Only *completed* non-skipped checks count toward pass/fail.
        # Pending checks (e.g. tide, which waits for merge labels) are ignored
        # once at least one real check has completed — they would block forever.
        # Merge-queue meta-checks that are permanently pending until labels are
        # added — not real CI, should not block CI evaluation.
        # Configurable via CI_IGNORED_CHECKS setting (default: "tide").
        _permanent_pending = settings.ignored_ci_checks

        all_passed = True
        _any_skipped = False
        any_still_running = False
        failed_checks: list[dict[str, Any]] = []

        repo_ref, adapter = get_adapter(state.get("current_repo", ""))
        identity = identity_for(repo_ref, state.get("current_pr_number"))
        change_request = await adapter.get_change_request(repo_ref, identity)
        checks = await adapter.get_checks(
            repo_ref, change_request.head_sha or change_request.source_branch
        )

        # If no checks exist yet, CI is still pending
        if not checks:
            logger.info(f"No CI checks registered yet for {ticket_key}, waiting for webhook")
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "pending",
                    "current_node": "ci_evaluator",  # Stay here, wait for webhook
                    "pending_ci_event": False,
                }
            )

        for check in checks:
            if _is_skipped(check):
                logger.info(f"CI check skipped by human override: {check.name}")
                _any_skipped = True
                continue

            # Ignore permanently-pending meta-checks (e.g. tide) — they
            # wait for merge labels, not for CI to pass, and would block
            # evaluation indefinitely.
            if check.status != CheckStatus.COMPLETED and any(
                p in check.name.lower() for p in _permanent_pending
            ):
                logger.info(f"Ignoring permanently-pending check: {check.name}")
                continue

            if check.status != CheckStatus.COMPLETED:
                # Real CI check still running — wait for it
                all_passed = False
                any_still_running = True
                logger.info(f"CI still running for {ticket_key}")
            elif check.conclusion not in (
                CheckConclusion.SUCCESS,
                CheckConclusion.SKIPPED,
                CheckConclusion.NEUTRAL,
            ):
                all_passed = False
                failed_checks.append(
                    {
                        "name": check.name,
                        "conclusion": check.conclusion.value,
                        "output": check.output,
                        "logs_ref": check.logs_url,
                    }
                )

        if all_passed:
            logger.info(f"All CI checks passed for {ticket_key}")
            jira = JiraClient()
            try:
                await set_review_pending_label(jira, ticket_key)
            finally:
                await jira.close()
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "passed",
                    "current_node": "human_review_gate",
                    "last_error": None,
                    "ci_fix_attempt": 0,
                    "pending_ci_event": False,
                }
            )

        # Some checks still running AND some have failed — wait for all to complete
        # before starting the fix pipeline. The fix agent needs the full failure list.
        if any_still_running and failed_checks:
            logger.info(
                f"CI partially complete for {ticket_key} "
                f"({len(failed_checks)} failed, more still running) — waiting"
            )
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "pending",
                    "current_node": "ci_evaluator",
                    "pending_ci_event": False,
                }
            )

        # Checks are still running but none have failed yet — wait for the next webhook.
        # This prevents the fix pipeline from firing while real CI jobs are in-progress.
        if not failed_checks:
            logger.info(f"CI checks still running for {ticket_key}, waiting for completion")
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "pending",
                    "current_node": "ci_evaluator",
                    "pending_ci_event": False,
                }
            )

        # CI failed - check if we can retry
        if ci_fix_attempt >= ci_fix_max:
            logger.warning(f"CI fix attempt limit ({ci_fix_max}) reached for {ticket_key}")
            record_ci_fix_attempt(repo=state.get("current_repo", "unknown"), result="exhausted")
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "failed",
                    "ci_failed_checks": failed_checks,
                    "current_node": "ci_evaluator",
                    "last_error": "CI fix attempt limit reached",
                    "pending_ci_event": False,
                }
            )

        next_attempt = ci_fix_attempt + 1
        logger.info(f"CI failed for {ticket_key}, attempt {next_attempt}/{ci_fix_max}")
        return update_state_timestamp(
            {
                **state,
                "ci_status": "fixing",
                "ci_failed_checks": failed_checks,
                "ci_fix_attempt": next_attempt,
                "current_node": "attempt_ci_fix",
                "pending_ci_event": False,
            }
        )

    except Exception as e:
        logger.error(f"CI evaluation failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "ci_evaluator",
            "retry_count": state.get("retry_count", 0) + 1,
            "pending_ci_event": False,
        }


async def attempt_ci_fix(state: WorkflowState) -> WorkflowState:
    """Attempt to autonomously fix CI failures.

    This node:
    1. Extracts error information from failed checks
    2. Invokes the configured LLM backend to generate a fix
    3. Applies the fix and pushes
    4. Routes back to CI evaluation

    Args:
        state: Current workflow state with ci_failed_checks.

    Returns:
        Updated state after fix attempt.
    """
    ticket_key = state["ticket_key"]
    failed_checks = state.get("ci_failed_checks", [])

    if not failed_checks:
        # Defensive: attempt_ci_fix is only routed to when ci_failed_checks is
        # non-empty. If it arrived here empty anyway (e.g. a concurrent/stale
        # state update cleared it before this node ran), re-verify against
        # live CI rather than asserting the checks passed.
        logger.warning(
            f"attempt_ci_fix entered with no ci_failed_checks for {ticket_key} "
            "— re-verifying CI status instead of assuming pass"
        )
        return update_state_timestamp(
            {
                **state,
                "current_node": "ci_evaluator",
                "pending_ci_event": False,
            }
        )

    ci_fix_attempt = state.get("ci_fix_attempt", 0)
    ci_fix_max = state.get("ci_fix_max_attempts", 5)

    jira = JiraClient()
    fix_started = False
    try:
        message = (
            f"🔧 CI checks failed. Analyzing failure attribution ({ci_fix_attempt}/{ci_fix_max})."
        )
        await post_status_comment(jira, ticket_key, message)
    finally:
        await jira.close()

    settings = get_settings()
    fork_owner = state.get("fork_owner", "")
    fork_repo = state.get("fork_repo", "")
    try:
        workspace_path, _ = await prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}
    except Exception as _setup_err:
        logger.error(f"Workspace setup failed for {ticket_key}: {_setup_err}")
        return {
            **state,
            "last_error": str(_setup_err),
            "current_node": "attempt_ci_fix",
            "pending_ci_event": False,
        }

    try:
        failures_file = Path(workspace_path) / ".forge" / "ci-failures.md"
        attribution_file = Path(workspace_path) / ".forge" / "ci-attribution.json"
        fix_plan_file = Path(workspace_path) / ".forge" / "fix-plan.md"
        logs_dir = Path(workspace_path) / ".forge" / "logs"
        failures_file.parent.mkdir(parents=True, exist_ok=True)

        repo_ref, adapter = get_adapter(state.get("current_repo", ""))
        await _fetch_ci_logs_and_artifacts(failed_checks, logs_dir, repo_ref, adapter)
        failures_file.write_text(_collect_error_info(failed_checks))

        # ── Phase 0: Attribution ─────────────────────────────────────────────
        logger.info(f"Phase 0: Attributing CI failures for {ticket_key}")
        attribution_prompt = load_prompt(
            "ci-attribution",
            failures_file_path=str(failures_file),
            base_branch=state.get("context", {}).get("default_branch", "main"),
        )
        # Workspaces are reused across CI cycles. Remove any previous verdict so
        # a runner that produces no output falls back to the safe attributable
        # default instead of silently reusing stale attribution.
        attribution_file.unlink(missing_ok=True)
        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Attribute CI failure (attempt {ci_fix_attempt})",
            task_description=attribution_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-ci-attribution",
            repo_name=state.get("current_repo", ""),
        )

        attributable = True  # fail-safe default
        attribution_reason = ""
        if attribution_file.exists():
            try:
                verdict = json.loads(attribution_file.read_text())
                attributable = verdict.get("attributable", True)
                attribution_reason = verdict.get("reason", "")
            except (ValueError, KeyError):
                logger.warning(
                    f"Could not parse ci-attribution.json for {ticket_key}, assuming attributable"
                )

        if not attributable:
            logger.info(f"CI failure attributed as external for {ticket_key}: {attribution_reason}")
            jira = JiraClient()
            try:
                check_names = ", ".join(c.get("name", "unknown") for c in failed_checks)
                await post_status_comment(
                    jira,
                    ticket_key,
                    f"⚠️ CI check(s) `{check_names}` are failing due to issues outside this PR "
                    f"({attribution_reason}). "
                    "The workflow will continue waiting. "
                    "Use `/forge skip-gate <name>` on the PR to bypass if needed.",
                )
            finally:
                await jira.close()
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "external_failure",
                    # evaluate_ci_status reserves an attempt before attribution.
                    # External failures must not consume the autonomous-fix budget.
                    "ci_fix_attempt": max(0, ci_fix_attempt - 1),
                    "current_node": "human_review_gate",
                    "pending_ci_event": False,
                    "last_error": None,
                }
            )

        # ── Phase 1: Analysis ────────────────────────────────────────────────
        logger.info(
            f"Phase 1: Analyzing CI failures for {ticket_key} (fix attempt {ci_fix_attempt}/{ci_fix_max})"
        )

        jira = JiraClient()
        try:
            await post_status_comment(
                jira,
                ticket_key,
                f"🔧 Attempting CI fix ({ci_fix_attempt}/{ci_fix_max}).",
            )
        finally:
            await jira.close()

        analysis_prompt = load_prompt(
            "analyze-ci",
            failures_file_path=str(failures_file),
            attempt=ci_fix_attempt,
        )
        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Analyze CI failures (attempt {ci_fix_attempt})",
            task_description=analysis_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-ci-analyze",
            repo_name=state.get("current_repo", ""),
            step_name="analyze_ci",
            policy_key="ci_analysis",
            skill_name="analyze-ci",
        )
        state = merge_review_exhaustion(state, result, ticket_key, "analyze_ci")

        if not fix_plan_file.exists():
            logger.warning(f"No fix plan written for {ticket_key} — skipping fix phase")
            return update_state_timestamp(
                {
                    **state,
                    "current_node": "human_review_gate",
                    "pending_ci_event": False,
                    "last_error": None,
                }
            )

        fix_plan = fix_plan_file.read_text()
        logger.info(f"Phase 1 complete: fix plan generated ({len(fix_plan)} chars)")

        # ── Phase 2: Fix ─────────────────────────────────────────────────────
        logger.info(f"Phase 2: Applying fixes for {ticket_key}")
        fix_prompt = load_prompt("fix-ci", fix_plan=fix_plan)
        runner = ContainerRunner(settings)
        fix_started = True
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Apply CI fix plan (attempt {ci_fix_attempt})",
            task_description=fix_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-ci-fix",
            repo_name=state.get("current_repo", ""),
            step_name="fix_ci",
            policy_key="ci_fix",
            skill_name="fix-ci",
        )
        state = merge_review_exhaustion(state, result, ticket_key, "fix_ci")

        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=state.get("current_repo", ""),
            branch_name=state.get("context", {}).get("branch_name", ""),
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace, await adapter.get_git_credentials(repo_ref))
        branch_name = state.get("context", {}).get("branch_name", "")

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address CI failures (attempt {ci_fix_attempt})")

        if fork_owner and fork_repo:
            git.add_fork_remote(fork_owner, fork_repo)
            remote_ref = f"fork/{branch_name}"
        else:
            remote_ref = f"origin/{branch_name}"

        unpushed = git._run_git(
            "log", f"{remote_ref}..HEAD", "--oneline", check=False
        ).stdout.strip()

        if not unpushed:
            logger.warning(f"Container made no changes for {ticket_key} (attempt {ci_fix_attempt})")
        else:
            _, review_result = await run_post_change_review(
                workspace_path=str(workspace_path),
                ticket_key=ticket_key,
                current_repo=state.get("current_repo", ""),
                branch_name=branch_name,
                spec_content=state.get("spec_content", ""),
                guardrails=state.get("context", {}).get("guardrails", ""),
                label=f"ci-fix-{ci_fix_attempt}",
            )
            if review_result is not None:
                state = merge_review_exhaustion(state, review_result, ticket_key, "code_review")
            if fork_owner and fork_repo:
                git.push_to_fork(force=False)
            else:
                logger.warning("Fork info not in state — pushing to origin instead")
                git.push(force=False)
            logger.info(f"CI fix pushed for {ticket_key} (attempt {ci_fix_attempt})")
            record_ci_fix_attempt(repo=state.get("current_repo", "unknown"), result="pushed")

            await sync_pr_description(
                state,
                git,
                current_repo=state.get("current_repo", ""),
                pr_number=state.get("current_pr_number"),
                attempt=ci_fix_attempt,
            )

        state = capture_handoff(
            workspace_path,
            state.get("current_repo", ""),
            f"{ticket_key}-ci-fix-{ci_fix_attempt}",
            state,
        )
        fix_started = False

        return update_state_timestamp(
            {
                **state,
                "current_node": "human_review_gate",
                "pending_ci_event": False,
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"CI fix failed for {ticket_key}: {e}")
        if fix_started:
            state = capture_handoff(
                workspace_path,
                state.get("current_repo", ""),
                f"{ticket_key}-ci-fix-{ci_fix_attempt}",
                state,
            )
        return {
            **state,
            "last_error": str(e),
            "current_node": "attempt_ci_fix",
            "pending_ci_event": False,
        }


async def escalate_to_blocked(state: WorkflowState) -> WorkflowState:
    """Escalate ticket to Blocked status after workflow failure.

    This handles various failure scenarios:
    - CI fix exhaustion
    - Workspace setup failures (invalid repo, clone failures)
    - Other workflow errors

    Args:
        state: Current workflow state.

    Returns:
        Updated state with ticket transitioned to Blocked.
    """
    ticket_key = state["ticket_key"]
    ci_fix_attempt = state.get("ci_fix_attempt", 0)
    failed_checks = state.get("ci_failed_checks", [])
    last_error = state.get("last_error", "")
    current_node = state.get("current_node", "")

    logger.info(f"Escalating {ticket_key} to Blocked status")

    jira = JiraClient()

    try:
        # Build escalation error message based on failure type
        if failed_checks:
            # CI failure scenario
            check_names = [c.get("name", "Unknown") for c in failed_checks]
            error_msg = (
                f"CI fixes exhausted after {ci_fix_attempt} attempts. "
                f"Failed checks: {', '.join(check_names)}. "
                "Manual intervention required."
            )
        elif last_error and (
            "repository" in last_error.lower() or "workspace" in last_error.lower()
        ):
            # Workspace/repository setup failure
            error_msg = (
                f"Repository configuration error: {last_error}. "
                "Ensure tasks have valid repo assignments (owner/repo format)."
            )
        else:
            # Generic failure
            error_msg = f"{last_error}. Manual intervention required."

        # Post error with @mentions for reporter and assignee
        await notify_error(state, error_msg, f"escalate_blocked ({current_node})")
        # Set blocked label instead of transitioning to custom status
        await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)

        logger.info(f"Ticket {ticket_key} escalated to Blocked")

        return update_state_timestamp(
            {
                **state,
                "is_blocked": True,
                "ci_status": "blocked",
                # current_node preserved — forge:retry resumes from the node that failed
                "generation_context": {},
                "qa_history": [],
            }
        )

    except Exception as e:
        logger.error(f"Escalation failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
        }
    finally:
        await jira.close()


async def _fetch_ci_logs_and_artifacts(
    failed_checks: list[dict[str, Any]],
    logs_dir: Path,
    repo_ref: RepositoryRef,
    adapter: SourceControlProvider,
) -> None:
    """Download job logs and run artifacts for all failed checks into logs_dir.

    Deduplicates artifact downloads across checks sharing the same run
    (``logs_ref``). Silently skips any individual download that fails so one
    bad log does not block the whole fix pipeline.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    fetched_run_refs: set[str] = set()

    for check_record in failed_checks:
        check_name = check_record.get("name", "unknown")
        logs_ref = check_record.get("logs_ref")
        safe_name = check_name.replace(" ", "-").replace("/", "-")
        check = CheckRun(
            name=check_name,
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.FAILURE,
            logs_url=logs_ref,
        )

        # Job log
        try:
            log_text = await adapter.get_check_logs(repo_ref, check)
            (logs_dir / f"{safe_name}.txt").write_text(log_text, errors="replace")
            logger.info(f"Downloaded job log for '{check_name}' ({len(log_text)} chars)")
        except NotFoundError:
            logger.warning(f"No logs found for '{check_name}'")
        except Exception as e:
            logger.warning(f"Could not download job log for '{check_name}': {e}")

        # Artifacts (once per run)
        if not logs_ref or logs_ref in fetched_run_refs:
            continue
        fetched_run_refs.add(logs_ref)

        try:
            for artifact_name, zip_bytes in await adapter.get_check_artifacts(repo_ref, check):
                try:
                    artifact_dir = logs_dir / artifact_name
                    artifact_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        zf.extractall(artifact_dir)
                    logger.info(f"Extracted artifact '{artifact_name}' to {artifact_dir}")
                except Exception as e:
                    logger.warning(f"Could not extract artifact '{artifact_name}': {e}")
        except Exception as e:
            logger.warning(f"Could not list artifacts for '{check_name}': {e}")


def _collect_error_info(failed_checks: list[dict[str, Any]]) -> str:
    """Collect and format error information from failed checks.

    Args:
        failed_checks: List of failed check data.

    Returns:
        Formatted error information.
    """
    parts = []

    for check in failed_checks:
        check_name = check.get("name", "unknown")
        parts.append(f"## {check.get('name', 'Unknown Check')}")
        parts.append(f"Result: {check.get('conclusion', 'failed')}")

        if check.get("logs_ref"):
            safe_name = check_name.replace(" ", "-").replace("/", "-")
            parts.append(f"Log file: `.forge/logs/{safe_name}.txt`")

        output = check.get("output", {})
        if output:
            title = output.get("title", "")
            summary = output.get("summary", "")
            text = output.get("text", "")

            if title:
                parts.append(f"Title: {title}")
            if summary:
                parts.append(f"Summary: {summary}")
            if text:
                parts.append(f"Details:\n{text[:2000]}")

        parts.append("")

    return "\n".join(parts)

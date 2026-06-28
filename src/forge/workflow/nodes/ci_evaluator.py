"""CI/CD evaluator node for monitoring and responding to CI results."""

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

from forge.api.routes.metrics import record_ci_fix_attempt
from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.code_review import run_post_change_review, sync_pr_description
from forge.workflow.nodes.error_handler import notify_error
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    remove_implementing_label,
    set_ci_pending_label,
)
from forge.workspace.git_ops import GitOperations
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
            }
        )

    logger.info(f"Evaluating CI status for {ticket_key}")

    github = GitHubClient()

    try:
        # Checks whose name contains any of these substrings are treated as passing
        ci_skipped_checks = state.get("ci_skipped_checks", [])

        def _is_skipped(check: dict) -> bool:
            name = check.get("name", "")
            return any(skip.lower() in name.lower() for skip in ci_skipped_checks)

        # Check each PR's CI status.
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
        failed_checks = []

        for pr_url in pr_urls:
            # Parse PR URL to get owner/repo/number
            parts = pr_url.rstrip("/").split("/")
            owner, repo = parts[-4], parts[-3]
            pr_number = int(parts[-1])

            # Get PR details for head SHA
            pr_data = await github.get_pull_request(owner, repo, pr_number)
            head_sha = pr_data.get("head", {}).get("sha", "")

            if not head_sha:
                continue

            # Get check runs for the commit
            check_runs = await github.get_check_runs(owner, repo, head_sha)

            # If no check runs exist yet, CI is still pending
            if not check_runs:
                logger.info(f"No CI checks registered yet for {pr_url}, waiting for webhook")
                return update_state_timestamp(
                    {
                        **state,
                        "ci_status": "pending",
                        "current_node": "ci_evaluator",  # Stay here, wait for webhook
                    }
                )

            for check in check_runs:
                if _is_skipped(check):
                    logger.info(f"CI check skipped by human override: {check.get('name')}")
                    _any_skipped = True
                    continue

                check_name = check.get("name", "")
                status = check.get("status")
                conclusion = check.get("conclusion")

                # Ignore permanently-pending meta-checks (e.g. tide) — they
                # wait for merge labels, not for CI to pass, and would block
                # evaluation indefinitely.
                if status != "completed" and any(
                    p in check_name.lower() for p in _permanent_pending
                ):
                    logger.info(f"Ignoring permanently-pending check: {check_name}")
                    continue

                if status != "completed":
                    # Real CI check still running — wait for it
                    all_passed = False
                    any_still_running = True
                    logger.info(f"CI still running for {pr_url}")
                elif conclusion not in ("success", "skipped", "neutral"):
                    all_passed = False
                    failed_checks.append(
                        {
                            "pr_url": pr_url,
                            "name": check_name,
                            "conclusion": conclusion,
                            "output": check.get("output", {}),
                            "log_url": check.get("html_url", ""),
                        }
                    )

        if all_passed:
            logger.info(f"All CI checks passed for {ticket_key}")
            return update_state_timestamp(
                {
                    **state,
                    "ci_status": "passed",
                    "current_node": "human_review_gate",
                    "last_error": None,
                    "ci_fix_attempt": 0,
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
            }
        )

    except Exception as e:
        logger.error(f"CI evaluation failed for {ticket_key}: {e}")
        await notify_error(state, str(e), "ci_evaluator")
        return {
            **state,
            "last_error": str(e),
            "current_node": "ci_evaluator",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await github.close()


async def attempt_ci_fix(state: WorkflowState) -> WorkflowState:
    """Attempt to autonomously fix CI failures.

    This node:
    1. Extracts error information from failed checks
    2. Invokes Claude to generate a fix
    3. Applies the fix and pushes
    4. Routes back to CI evaluation

    Args:
        state: Current workflow state with ci_failed_checks.

    Returns:
        Updated state after fix attempt.
    """
    ticket_key = state["ticket_key"]
    failed_checks = state.get("ci_failed_checks", [])
    workspace_path = state.get("workspace_path")

    if not failed_checks:
        return update_state_timestamp(
            {
                **state,
                "current_node": "ci_evaluator",
            }
        )

    logger.info(f"Attempting CI fix for {ticket_key}")

    # Post status comment to feature ticket at start of CI fix attempt
    ci_fix_attempt = state.get("ci_fix_attempt", 0)
    ci_fix_max = state.get("ci_fix_max_attempts", 5)

    jira = JiraClient()
    try:
        message = f"🔧 CI checks failed. Analyzing failure and attempting fix ({ci_fix_attempt}/{ci_fix_max})."
        await post_status_comment(jira, ticket_key, message)
    finally:
        await jira.close()

    settings = get_settings()
    fork_owner = state.get("fork_owner", "")
    fork_repo = state.get("fork_repo", "")
    attempt = ci_fix_attempt

    try:
        workspace_path, _ = prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}

    except Exception as _setup_err:
        logger.error(f"Workspace setup failed for {ticket_key}: {_setup_err}")
        await notify_error(state, str(_setup_err), "attempt_ci_fix")
        return {
            **state,
            "last_error": str(_setup_err),
            "current_node": "attempt_ci_fix",
        }

    try:
        # ── Phase 1: Analysis ────────────────────────────────────────────────
        # ForgeAgent with MCP access fetches the Prow logs and produces a
        # ── Phase 1: Analysis (container) ────────────────────────────────────
        # Runs in a container so the agent has gh CLI and shell access to fetch
        # real GitHub Actions / Prow logs. Writes the fix plan to a file so
        # Phase 2 can read it without hitting token limits.
        logger.info(f"Phase 1: Analyzing CI failures for {ticket_key} (attempt {attempt})")

        failures_file = Path(workspace_path) / ".forge" / "ci-failures.md"
        fix_plan_file = Path(workspace_path) / ".forge" / "fix-plan.md"
        logs_dir = Path(workspace_path) / ".forge" / "logs"
        failures_file.parent.mkdir(parents=True, exist_ok=True)

        # Download job logs and artifacts before the container starts so the
        # agent can read them directly without needing gh CLI authentication.
        await _fetch_ci_logs_and_artifacts(failed_checks, logs_dir, GitHubClient())

        failures_file.write_text(_collect_error_info(failed_checks))

        analysis_prompt = load_prompt(
            "analyze-ci",
            failures_file_path=str(failures_file),
            attempt=attempt,
        )

        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Analyze CI failures (attempt {attempt})",
            task_description=analysis_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-ci-analyze",
            repo_name=state.get("current_repo", ""),
        )

        if not fix_plan_file.exists():
            logger.warning(f"No fix plan written for {ticket_key} — skipping fix phase")
            return update_state_timestamp(
                {
                    **state,
                    "current_node": "wait_for_ci_gate",
                    "last_error": None,
                }
            )

        fix_plan = fix_plan_file.read_text()
        logger.info(f"Phase 1 complete: fix plan generated ({len(fix_plan)} chars)")

        # ── Phase 2: Fix ─────────────────────────────────────────────────────
        # Container applies the fix plan written by Phase 1.
        logger.info(f"Phase 2: Applying fixes for {ticket_key}")
        fix_prompt = load_prompt("fix-ci", fix_plan=fix_plan)

        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Apply CI fix plan (attempt {attempt})",
            task_description=fix_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-ci-fix",
            repo_name=state.get("current_repo", ""),
        )

        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=state.get("current_repo", ""),
            branch_name=state.get("context", {}).get("branch_name", ""),
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)

        branch_name = state.get("context", {}).get("branch_name", "")

        # Commit any files the container left uncommitted (safety net)
        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address CI failures (attempt {attempt})")

        # Check for changes before doing anything expensive
        if fork_owner and fork_repo:
            git.add_fork_remote(fork_owner, fork_repo)
            remote_ref = f"fork/{branch_name}"
        else:
            remote_ref = f"origin/{branch_name}"

        unpushed = git._run_git(
            "log", f"{remote_ref}..HEAD", "--oneline", check=False
        ).stdout.strip()

        if not unpushed:
            logger.warning(f"Container made no changes for {ticket_key} (attempt {attempt})")
        else:
            # Only run the expensive review pass when the fix actually changed code
            await run_post_change_review(
                workspace_path=str(workspace_path),
                ticket_key=ticket_key,
                current_repo=state.get("current_repo", ""),
                branch_name=branch_name,
                spec_content=state.get("spec_content", ""),
                guardrails=state.get("context", {}).get("guardrails", ""),
                label=f"ci-fix-{attempt}",
            )

            # Push all commits (CI fix + any review corrections)
            if fork_owner and fork_repo:
                git.push_to_fork(force=False)
            else:
                logger.warning("Fork info not in state — pushing to origin instead")
                git.push(force=False)
            logger.info(f"CI fix pushed for {ticket_key} (attempt {attempt})")
            record_ci_fix_attempt(repo=state.get("current_repo", "unknown"), result="pushed")

            # Sync PR description to reflect what actually changed
            _repo = state.get("current_repo", "/")
            _owner, _repo_name = (_repo.split("/") + [""])[:2]
            await sync_pr_description(
                state,
                git,
                owner=_owner,
                repo=_repo_name,
                pr_number=state.get("current_pr_number"),
                attempt=attempt,
            )

        return update_state_timestamp(
            {
                **state,
                "current_node": "wait_for_ci_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"CI fix failed for {ticket_key}: {e}")
        await notify_error(state, str(e), "attempt_ci_fix")
        return {
            **state,
            "last_error": str(e),
            "current_node": "attempt_ci_fix",  # preserved so retry resumes here,
        }


async def wait_for_ci_gate(state: WorkflowState) -> WorkflowState:
    """Pause the workflow until a GitHub CI webhook arrives.

    Inserted after PR creation and after each CI fix push. The workflow
    resumes when GitHub sends a check_run or check_suite webhook, at which
    point ci_evaluator re-checks the actual CI results without any
    polling delay.

    On initial entry (after PR creation), posts a status comment to the feature
    ticket and transitions labels from forge:implementing to forge:ci-pending.

    Args:
        state: Current workflow state.

    Returns:
        Updated state with is_paused=True.
    """
    ticket_key = state["ticket_key"]
    ci_fix_attempt = state.get("ci_fix_attempt", 0)

    # Detect initial entry (after PR creation) vs re-entry (after CI fix)
    is_initial_entry = ci_fix_attempt == 0

    if is_initial_entry:
        # Post status comment and update labels on initial entry
        jira = JiraClient()
        try:
            # Build status message with PR number if available
            pr_number = state.get("current_pr_number")
            if pr_number is not None:
                message = f"🚀 Pull request #{pr_number} created and submitted. Waiting for CI checks to complete."
            else:
                message = (
                    "🚀 Pull request created and submitted. Waiting for CI checks to complete."
                )

            # Post status comment to feature ticket
            await post_status_comment(jira, ticket_key, message)

            # Transition labels: remove forge:implementing, add forge:ci-pending
            await remove_implementing_label(jira, ticket_key)
            await set_ci_pending_label(jira, ticket_key)

        finally:
            await jira.close()

        logger.info(f"Pausing {ticket_key} after PR creation, waiting for GitHub CI webhook")
    else:
        logger.info(
            f"Pausing {ticket_key} after CI fix attempt {ci_fix_attempt}, "
            "waiting for GitHub CI webhook"
        )

    return update_state_timestamp(
        {
            **state,
            "is_paused": True,
            "current_node": "wait_for_ci_gate",
        }
    )


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


def _parse_run_info(log_url: str) -> tuple[str, str, str, str] | None:
    """Extract (owner, repo, run_id, job_id) from a GitHub Actions html_url.

    Expects: https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id}
    """
    parts = log_url.rstrip("/").split("/")
    if len(parts) < 10 or parts[5] != "actions" or parts[8] != "job":
        return None
    return parts[3], parts[4], parts[7], parts[9]


async def _fetch_ci_logs_and_artifacts(
    failed_checks: list[dict[str, Any]],
    logs_dir: Path,
    github: GitHubClient,
) -> None:
    """Download job logs and run artifacts for all failed checks into logs_dir.

    Deduplicates artifact downloads across checks sharing the same run_id.
    Silently skips any individual download that fails so one bad log does not
    block the whole fix pipeline.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    fetched_run_ids: set[str] = set()

    for check in failed_checks:
        log_url = check.get("log_url", "")
        check_name = check.get("name", "unknown")
        info = _parse_run_info(log_url)
        if not info:
            continue
        owner, repo, run_id, job_id = info
        safe_name = check_name.replace(" ", "-").replace("/", "-")

        # Job log
        try:
            log_text = await github.get_job_logs(owner, repo, job_id)
            (logs_dir / f"{safe_name}-{job_id}.txt").write_text(log_text, errors="replace")
            logger.info(f"Downloaded job log for '{check_name}' ({len(log_text)} chars)")
        except Exception as e:
            logger.warning(f"Could not download job log for '{check_name}': {e}")

        # Artifacts (once per run_id)
        if run_id in fetched_run_ids:
            continue
        fetched_run_ids.add(run_id)

        try:
            artifacts = await github.get_run_artifacts(owner, repo, run_id)
            for artifact in artifacts:
                artifact_id = artifact.get("id")
                artifact_name = artifact.get("name", str(artifact_id))
                try:
                    zip_bytes = await github.download_artifact_zip(owner, repo, artifact_id)
                    artifact_dir = logs_dir / artifact_name
                    artifact_dir.mkdir(exist_ok=True)
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        zf.extractall(artifact_dir)
                    logger.info(f"Extracted artifact '{artifact_name}' to {artifact_dir}")
                except Exception as e:
                    logger.warning(f"Could not download artifact '{artifact_name}': {e}")
        except Exception as e:
            logger.warning(f"Could not list artifacts for run {run_id}: {e}")


def _collect_error_info(failed_checks: list[dict[str, Any]]) -> str:
    """Collect and format error information from failed checks.

    Args:
        failed_checks: List of failed check data.

    Returns:
        Formatted error information.
    """
    parts = []

    for check in failed_checks:
        parts.append(f"## {check.get('name', 'Unknown Check')}")
        parts.append(f"Result: {check.get('conclusion', 'failed')}")

        log_url = check.get("log_url", "")
        check_name = check.get("name", "unknown")
        if log_url:
            parts.append(f"Log URL: {log_url}")
            info = _parse_run_info(log_url)
            if info:
                _, _, _, job_id = info
                safe_name = check_name.replace(" ", "-").replace("/", "-")
                parts.append(f"Log file: `.forge/logs/{safe_name}-{job_id}.txt`")

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

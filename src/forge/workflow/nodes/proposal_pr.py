"""Shared publication helpers for PRD and specification proposal PRs."""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient, pr_interaction_options
from forge.models.workflow import ForgeLabel
from forge.orchestrator.checkpointer import set_pr_ticket_index
from forge.workflow.utils.jira_status import post_status_comment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposalArtifact:
    """Artifact-specific text and state fields for the shared PR flow."""

    state_prefix: str
    branch_segment: str
    file_name: str
    title_name: str
    document_name: str
    pending_label: ForgeLabel
    published_name: str


PRD_PROPOSAL = ProposalArtifact(
    state_prefix="prd",
    branch_segment="prd",
    file_name="prd.md",
    title_name="PRD",
    document_name="PRD document",
    pending_label=ForgeLabel.PRD_PENDING,
    published_name="PRD",
)

SPEC_PROPOSAL = ProposalArtifact(
    state_prefix="spec",
    branch_segment="spec",
    file_name="design.md",
    title_name="Spec",
    document_name="specification",
    pending_label=ForgeLabel.SPEC_PENDING,
    published_name="Specification",
)


async def create_proposal_pr(
    *,
    artifact: ProposalArtifact,
    ticket_key: str,
    content: str,
    summary: str,
    proposals_repo: str,
    proposals_path: str,
) -> dict[str, Any]:
    """Publish an artifact branch to a fork and open its upstream PR."""
    upstream_owner, upstream_repo = proposals_repo.split("/", 1)
    branch = f"forge/{artifact.branch_segment}/{ticket_key.lower()}"
    file_path = "/".join(filter(None, [proposals_path, ticket_key, artifact.file_name]))

    gh = GitHubClient()
    jira = JiraClient()
    try:
        fork = await gh.get_or_create_fork(upstream_owner, upstream_repo)
        fork_owner = fork["owner"]["login"]
        fork_repo = fork["name"]
        upstream = await gh.get_repository(upstream_owner, upstream_repo)
        default_branch = upstream.get("default_branch") or "main"
        synced = await gh.sync_fork_with_upstream(fork_owner, fork_repo, branch=default_branch)
        if not synced:
            raise RuntimeError(
                f"Could not synchronize proposal fork {fork_owner}/{fork_repo} "
                f"(branch {default_branch}) with upstream"
            )

        await gh.create_branch(fork_owner, fork_repo, branch, base=default_branch)
        existing_file = await gh.get_file_contents(fork_owner, fork_repo, file_path, branch)
        await gh.create_or_update_file(
            owner=fork_owner,
            repo=fork_repo,
            path=file_path,
            content=content,
            message=f"Add {artifact.title_name} for {ticket_key}",
            branch=branch,
            sha=existing_file["sha"] if existing_file else None,
        )
        pr_body = (
            f"**{artifact.title_name} for [{ticket_key}]"
            f"(https://redhat.atlassian.net/browse/{ticket_key})**\n\n"
            f"The {artifact.document_name} is in [`{file_path}`](/{file_path}) "
            "on this branch.\n\n"
            "Review the file changes for the latest version. "
            "Leave comments on this PR to provide feedback — "
            f"Forge will regenerate the {artifact.title_name} and push updated commits."
        )
        pr_result = await gh.create_pull_request(
            owner=upstream_owner,
            repo=upstream_repo,
            title=f"[{ticket_key}] {artifact.title_name}: {summary}",
            body=pr_body,
            head=f"{fork_owner}:{branch}",
            base=default_branch,
        )

        pr_url = pr_result.pr["html_url"]
        pr_number = pr_result.pr["number"]
        await set_pr_ticket_index(pr_url, ticket_key)
        await jira.set_workflow_label(ticket_key, artifact.pending_label)
        await post_status_comment(
            jira,
            ticket_key,
            f"{artifact.published_name} published for review: "
            f"[GitHub PR]({pr_url})\n\n{pr_interaction_options(pr_url)}",
        )

        prefix = artifact.state_prefix
        return {
            f"{prefix}_pr_url": pr_url,
            f"{prefix}_pr_number": pr_number,
            f"{prefix}_pr_repo": proposals_repo,
            f"{prefix}_pr_fork_owner": fork_owner,
            f"{prefix}_pr_fork_repo": fork_repo,
            f"{prefix}_pr_branch": branch,
            f"{prefix}_pr_file_path": file_path,
        }
    finally:
        await gh.close()
        await jira.close()


def _git_blob_sha(content: str) -> str:
    """Compute the git blob SHA-1 for a string, matching how Git stores blobs."""
    content_bytes = content.encode()
    header = f"blob {len(content_bytes)}\0".encode()
    return hashlib.sha1(header + content_bytes).hexdigest()


async def update_proposal_pr(
    *,
    artifact: ProposalArtifact,
    ticket_key: str,
    content: str,
    state: dict[str, Any],
) -> bool:
    """Update a fork proposal branch, with fallback for legacy upstream state.

    Returns:
        True if the file was updated, False if the content was unchanged.
    """
    prefix = artifact.state_prefix
    upstream_owner, upstream_repo = state[f"{prefix}_pr_repo"].split("/", 1)
    owner = state.get(f"{prefix}_pr_fork_owner") or upstream_owner
    repo = state.get(f"{prefix}_pr_fork_repo") or upstream_repo
    branch = state[f"{prefix}_pr_branch"]
    pr_number = state[f"{prefix}_pr_number"]
    file_path = state[f"{prefix}_pr_file_path"]

    gh = GitHubClient()
    try:
        file_meta = await gh.get_file_contents(owner, repo, file_path, branch)
        if not file_meta:
            logger.warning(
                "Could not find %s file %s on branch %s",
                artifact.title_name,
                file_path,
                branch,
            )
            return False

        if _git_blob_sha(content) == file_meta["sha"]:
            logger.warning(
                "Regenerated %s for %s is unchanged — skipping commit",
                artifact.title_name,
                ticket_key,
            )
            await gh.create_issue_comment(
                upstream_owner,
                upstream_repo,
                pr_number,
                f"Forge reviewed the feedback but the regenerated "
                f"{artifact.published_name} was unchanged. The feedback may "
                f"require manual revision, or it may have already been "
                f"addressed in a previous revision.",
            )
            return False

        await gh.create_or_update_file(
            owner=owner,
            repo=repo,
            path=file_path,
            content=content,
            message=(f"Revise {artifact.title_name} for {ticket_key} based on feedback"),
            branch=branch,
            sha=file_meta["sha"],
        )
        await gh.create_issue_comment(
            upstream_owner,
            upstream_repo,
            pr_number,
            f"{artifact.published_name} has been revised based on feedback. "
            "Please review the updated version.",
        )
        return True
    finally:
        await gh.close()

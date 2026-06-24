"""Jira REST API client for CRUD operations on tickets."""

import asyncio
import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from forge.config import Settings, get_settings
from forge.integrations.jira.models import JiraComment, JiraIssue
from forge.models.workflow import ForgeLabel
from forge.skills.models import SkillEntry
from forge.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)

# Rate limit retry configuration
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

# Module-level cache for project properties (persists per worker lifetime)
_project_property_cache: dict[tuple[str, str], Any] = {}


class MissingProjectConfig(Exception):
    """Raised when a required Jira project property is absent or malformed."""


class JiraClient:
    """Async client for Jira REST API v3.

    Handles authentication, rate limiting, and common operations
    for Features, Epics, Tasks, and Comments.
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize the Jira client.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.base_url = f"{self.settings.jira_base_url}/rest/api/3"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with authentication."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                auth=(
                    self.settings.jira_user_email,
                    self.settings.jira_api_token.get_secret_value(),
                ),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a request with rate limit handling and exponential backoff.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            url: Request URL (relative to base_url).
            **kwargs: Additional arguments to pass to httpx request.

        Returns:
            httpx.Response on success.

        Raises:
            httpx.HTTPStatusError: If request fails after all retries.
        """
        client = await self._get_client()
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(MAX_RETRIES):
            response = await client.request(method, url, **kwargs)

            if response.status_code != 429:
                return response

            # Rate limited - apply exponential backoff
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_time = float(retry_after)
                except ValueError:
                    wait_time = backoff
            else:
                wait_time = backoff

            wait_time = min(wait_time, MAX_BACKOFF_SECONDS)

            logger.warning(
                f"Jira rate limited (attempt {attempt + 1}/{MAX_RETRIES}). "
                f"Waiting {wait_time:.1f}s before retry."
            )
            await asyncio.sleep(wait_time)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        # Final attempt
        response = await client.request(method, url, **kwargs)
        return response

    async def get_issue(self, issue_key: str) -> JiraIssue:
        """Fetch a Jira issue by key.

        Args:
            issue_key: The Jira issue key (e.g., "PROJ-123").

        Returns:
            JiraIssue with fields populated from the API response.
        """
        response = await self._request_with_retry("GET", f"/issue/{issue_key}")
        response.raise_for_status()
        data = response.json()
        return JiraIssue.from_api_response(data)

    async def update_description(self, issue_key: str, description: str) -> None:
        """Update the description field of a Jira issue.

        Args:
            issue_key: The Jira issue key.
            description: New description content (will be converted to ADF).
        """
        client = await self._get_client()
        adf_content = self._text_to_adf(description)
        response = await client.put(
            f"/issue/{issue_key}",
            json={"fields": {"description": adf_content}},
        )
        response.raise_for_status()
        logger.info(f"Updated description for {issue_key}")

    async def update_custom_field(self, issue_key: str, field_id: str, value: str) -> None:
        """Update a custom field on a Jira issue.

        Args:
            issue_key: The Jira issue key.
            field_id: The custom field ID (e.g., "customfield_10050").
            value: The new value for the field.
        """
        client = await self._get_client()
        response = await client.put(
            f"/issue/{issue_key}",
            json={"fields": {field_id: value}},
        )
        response.raise_for_status()
        logger.info(f"Updated {field_id} for {issue_key}")

    async def transition_issue(self, issue_key: str, transition_name: str) -> None:
        """Transition a Jira issue to a new status.

        Args:
            issue_key: The Jira issue key.
            transition_name: The name of the target status.
        """
        client = await self._get_client()

        # Get available transitions
        response = await client.get(f"/issue/{issue_key}/transitions")
        response.raise_for_status()
        transitions = response.json().get("transitions", [])

        # Find matching transition
        transition_id = None
        for t in transitions:
            if t.get("name", "").lower() == transition_name.lower():
                transition_id = t["id"]
                break
            if t.get("to", {}).get("name", "").lower() == transition_name.lower():
                transition_id = t["id"]
                break

        if transition_id is None:
            available = [t.get("name") for t in transitions]
            raise ValueError(f"Transition '{transition_name}' not found. Available: {available}")

        # Execute transition
        response = await client.post(
            f"/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        response.raise_for_status()
        logger.info(f"Transitioned {issue_key} to {transition_name}")

    async def create_epic(
        self,
        project_key: str,
        summary: str,
        description: str,
        parent_key: str,
        labels: list[str] | None = None,
    ) -> str:
        """Create a new Epic linked to a parent Feature.

        Args:
            project_key: The Jira project key.
            summary: Epic title/summary.
            description: Epic description (implementation plan).
            parent_key: Parent Feature key for linking.
            labels: Optional list of labels to add.

        Returns:
            The key of the created Epic.
        """
        client = await self._get_client()
        adf_content = self._text_to_adf(description)

        fields = {
            "project": {"key": project_key},
            "summary": summary,
            "description": adf_content,
            "issuetype": {"name": "Epic"},
            "parent": {"key": parent_key},
        }

        if labels:
            fields["labels"] = labels

        response = await client.post(
            "/issue",
            json={"fields": fields},
        )
        response.raise_for_status()
        epic_key = response.json()["key"]
        logger.info(f"Created Epic {epic_key} under {parent_key}")
        return epic_key

    async def create_task(
        self,
        project_key: str,
        summary: str,
        description: str,
        parent_key: str | None = None,
        labels: list[str] | None = None,
    ) -> str:
        """Create a new Task, optionally under a parent Epic.

        Note: parent_key must be an Epic or higher-level issue. Bug/Story/Task
        cannot be parents of Task in Jira's hierarchy — use create_issue_link
        to express peer relationships such as 'implements'.

        Args:
            project_key: The Jira project key.
            summary: Task title/summary.
            description: Task implementation details.
            parent_key: Optional parent Epic key. Omit for peer-linked tasks.
            labels: Optional labels (e.g., target repository).

        Returns:
            The key of the created Task.
        """
        client = await self._get_client()
        adf_content = self._text_to_adf(description)

        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "description": adf_content,
            "issuetype": {"name": "Task"},
        }
        if parent_key:
            fields["parent"] = {"key": parent_key}
        if labels:
            fields["labels"] = labels

        response = await client.post("/issue", json={"fields": fields})
        response.raise_for_status()
        task_key = response.json()["key"]
        logger.info(f"Created Task {task_key}" + (f" under {parent_key}" if parent_key else ""))
        return task_key

    async def delete_issue(self, issue_key: str, delete_subtasks: bool = True) -> None:
        """Delete a Jira issue.

        Args:
            issue_key: The Jira issue key.
            delete_subtasks: Whether to also delete subtasks.
        """
        client = await self._get_client()
        params = {"deleteSubtasks": str(delete_subtasks).lower()}
        response = await client.delete(f"/issue/{issue_key}", params=params)
        response.raise_for_status()
        logger.info(f"Deleted issue {issue_key}")

    async def archive_issue(self, issue_key: str, archive_subtasks: bool = True) -> None:
        """Archive a Jira issue natively and clean up its forge labels.

        The issue will be:
        1. Natively archived via the Jira archive endpoint (hidden from boards/backlogs)
        2. Marked with 'forge:archived' label and all other forge labels removed
        3. Unlinked from its parent (Epic/Feature)

        Args:
            issue_key: The Jira issue key.
            archive_subtasks: Whether to also archive subtasks/children.
        """
        client = await self._get_client()

        # First, archive any subtasks if requested
        if archive_subtasks:
            try:
                children = await self.get_epic_children(epic_key=issue_key)
                for child in children:
                    if child.key:
                        await self.archive_issue(child.key, archive_subtasks=False)
            except Exception as e:
                logger.debug(f"No children to archive for {issue_key}: {e}")

        # Get current issue to find existing forge labels
        issue = await self.get_issue(issue_key)
        current_labels = issue.labels or []
        forge_labels = [lbl for lbl in current_labels if lbl.startswith("forge:")]

        # Step 1: Update labels (remove forge workflow labels, add archived)
        label_update = {
            "update": {
                "labels": [
                    *[{"remove": label} for label in forge_labels],
                    {"add": "forge:archived"},
                ]
            }
        }

        try:
            response = await client.put(f"/issue/{issue_key}", json=label_update)
            response.raise_for_status()
            logger.info(f"Added forge:archived label to {issue_key}")
        except Exception as e:
            logger.warning(f"Failed to update labels for {issue_key}: {e}")

        # Step 2: Try to unlink from parent (may fail in some configs)
        try:
            parent_update = {"fields": {"parent": None}}
            response = await client.put(f"/issue/{issue_key}", json=parent_update)
            response.raise_for_status()
            logger.info(f"Unlinked {issue_key} from parent")
        except Exception as e:
            logger.debug(f"Could not unlink parent for {issue_key} (may not be supported): {e}")

        # Step 3: Natively archive the issue so it's hidden from boards and backlogs
        try:
            response = await client.put("/issue/archive", json={"issueIdsOrKeys": [issue_key]})
            response.raise_for_status()
            archive_result = response.json()
            archive_errors = archive_result.get("errors", {}) if archive_result else {}
            failed_error = next(
                (
                    error
                    for error in archive_errors.values()
                    if issue_key in error.get("issueIdsOrKeys", [])
                ),
                None,
            )
            if failed_error:
                raise RuntimeError(failed_error.get("message", "Unknown Jira archive error"))
            logger.info(f"Natively archived {issue_key}")
        except Exception as e:
            logger.warning(f"Failed to natively archive {issue_key}: {e}")

        logger.info(f"Archived issue {issue_key}")

    async def add_attachment(
        self,
        issue_key: str,
        filename: str,
        content: str | bytes,
        content_type: str = "text/markdown",
    ) -> dict[str, Any]:
        """Add an attachment to a Jira issue.

        Args:
            issue_key: The Jira issue key.
            filename: Name for the attachment file.
            content: File content (string or bytes).
            content_type: MIME type of the content.

        Returns:
            The attachment metadata from Jira API.
        """
        # Attachments require a separate client without JSON content-type
        async with httpx.AsyncClient(
            base_url=self.base_url,
            auth=(
                self.settings.jira_user_email,
                self.settings.jira_api_token.get_secret_value(),
            ),
            headers={
                "Accept": "application/json",
                "X-Atlassian-Token": "no-check",  # Required for attachments
            },
            timeout=60.0,
        ) as client:
            # Convert string to bytes if needed
            if isinstance(content, str):
                content = content.encode("utf-8")

            files = {"file": (filename, content, content_type)}
            response = await client.post(
                f"/issue/{issue_key}/attachments",
                files=files,
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Added attachment {filename} to {issue_key}")
            return data[0] if data else {}

    async def get_attachments(self, issue_key: str) -> list[dict[str, Any]]:
        """Get all attachments for a Jira issue.

        Args:
            issue_key: The Jira issue key.

        Returns:
            List of attachment metadata dicts with 'id', 'filename', 'size', etc.
        """
        client = await self._get_client()
        response = await client.get(
            f"/issue/{issue_key}",
            params={"fields": "attachment"},
        )
        response.raise_for_status()
        data = response.json()
        attachments = data.get("fields", {}).get("attachment", [])
        logger.debug(f"Found {len(attachments)} attachments on {issue_key}")
        return attachments

    async def delete_attachment(self, attachment_id: str) -> None:
        """Delete an attachment by ID.

        Args:
            attachment_id: The Jira attachment ID.
        """
        client = await self._get_client()
        response = await client.delete(f"/attachment/{attachment_id}")
        response.raise_for_status()
        logger.info(f"Deleted attachment {attachment_id}")

    async def delete_attachments_by_name(
        self,
        issue_key: str,
        filename: str,
    ) -> int:
        """Delete all attachments matching a filename.

        Args:
            issue_key: The Jira issue key.
            filename: Exact filename to match (e.g., "TEST-123-spec.md").

        Returns:
            Number of attachments deleted.
        """
        attachments = await self.get_attachments(issue_key)
        deleted = 0
        for attachment in attachments:
            if attachment.get("filename") == filename:
                await self.delete_attachment(attachment["id"])
                deleted += 1
        if deleted:
            logger.info(f"Deleted {deleted} attachment(s) named '{filename}' from {issue_key}")
        return deleted

    async def create_remote_link(self, issue_key: str, url: str, title: str) -> None:
        """Add a remote link (web link) to a Jira issue.

        Args:
            issue_key: The Jira issue key.
            url: The URL to link to.
            title: Display title for the link.
        """
        client = await self._get_client()
        response = await client.post(
            f"/issue/{issue_key}/remotelink",
            json={"object": {"url": url, "title": title}},
        )
        response.raise_for_status()
        logger.info(f"Added remote link to {issue_key}: {url}")

    async def create_issue_link(
        self,
        link_type: str,
        inward_key: str,
        outward_key: str,
    ) -> None:
        """Create a link between two Jira issues.

        Args:
            link_type: Link type name (e.g., "implements").
            inward_key: Key of the inward issue (the one that "does" the action).
            outward_key: Key of the outward issue (the one the action is done to).
        """
        client = await self._get_client()
        response = await client.post(
            "/issueLink",
            json={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
        )
        response.raise_for_status()
        logger.info(f"Created {link_type} link: {inward_key} → {outward_key}")

    async def get_issue_links(self, issue_key: str) -> list[dict[str, str | None]]:
        """Get all issue links for an issue.

        Returns a list of dicts with keys:
          type: normalized lowercase link type name
          inward_key: key of the inward issue relative to this issue (may be None)
          outward_key: key of the outward issue relative to this issue (may be None)

        Args:
            issue_key: The Jira issue key.

        Returns:
            List of link dicts.
        """
        client = await self._get_client()
        response = await client.get(f"/issue/{issue_key}", params={"fields": "issuelinks"})
        response.raise_for_status()
        data = response.json()
        links = data.get("fields", {}).get("issuelinks", [])
        result = []
        for link in links:
            link_type = link.get("type", {}).get("name", "").lower()
            inward = link.get("inwardIssue") or {}
            outward = link.get("outwardIssue") or {}
            result.append(
                {
                    "type": link_type,
                    "inward_key": inward.get("key"),
                    "outward_key": outward.get("key"),
                }
            )
        return result

    async def add_comment(self, issue_key: str, body: str) -> JiraComment:
        """Add a comment to a Jira issue.

        Args:
            issue_key: The Jira issue key.
            body: Comment text content.

        Returns:
            The created JiraComment.
        """
        client = await self._get_client()
        adf_content = self._text_to_adf(body)

        response = await client.post(
            f"/issue/{issue_key}/comment",
            json={"body": adf_content},
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"Added comment to {issue_key}")
        return JiraComment.from_api_response(data)

    async def add_error_comment(
        self,
        issue_key: str,
        error_message: str,
        node_name: str,
        mention_account_ids: list[str] | None = None,
    ) -> JiraComment:
        """Add an error notification comment with user mentions.

        Args:
            issue_key: The Jira issue key.
            error_message: The error message to include.
            node_name: The workflow node that failed.
            mention_account_ids: List of Jira account IDs to mention.

        Returns:
            The created JiraComment.
        """
        client = await self._get_client()

        # Build ADF content with mentions
        mention_nodes: list[dict[str, Any]] = []
        if mention_account_ids:
            for account_id in mention_account_ids:
                if account_id:  # Skip empty account IDs
                    mention_nodes.append(
                        {
                            "type": "mention",
                            "attrs": {
                                "id": account_id,
                                "text": f"@{account_id}",
                                "accessLevel": "",
                            },
                        }
                    )
                    mention_nodes.append({"type": "text", "text": " "})

        safe_error_message = redact_secrets(error_message)

        # Build the error message content
        error_paragraph: list[dict[str, Any]] = [
            {"type": "text", "text": "Workflow failed at ", "marks": []},
            {
                "type": "text",
                "text": node_name,
                "marks": [{"type": "strong"}],
            },
            {"type": "text", "text": f": {safe_error_message}"},
        ]

        adf_content: dict[str, Any] = {
            "version": 1,
            "type": "doc",
            "content": [
                # Mentions paragraph
                {
                    "type": "paragraph",
                    "content": mention_nodes
                    + [
                        {
                            "type": "text",
                            "text": "Forge Workflow Error",
                            "marks": [{"type": "strong"}],
                        },
                    ]
                    if mention_nodes
                    else [
                        {
                            "type": "text",
                            "text": "Forge Workflow Error",
                            "marks": [{"type": "strong"}],
                        },
                    ],
                },
                # Error details
                {"type": "paragraph", "content": error_paragraph},
            ],
        }

        response = await client.post(
            f"/issue/{issue_key}/comment",
            json={"body": adf_content},
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"Added error comment to {issue_key} mentioning {mention_account_ids}")
        return JiraComment.from_api_response(data)

    async def get_comments(self, issue_key: str) -> list[JiraComment]:
        """Get all comments for a Jira issue.

        Args:
            issue_key: The Jira issue key.

        Returns:
            List of JiraComment objects.
        """
        client = await self._get_client()
        response = await client.get(f"/issue/{issue_key}/comment")
        response.raise_for_status()
        data = response.json()
        return [JiraComment.from_api_response(c) for c in data.get("comments", [])]

    async def get_labels(self, issue_key: str) -> list[str]:
        """Get labels for a Jira issue.

        Args:
            issue_key: The Jira issue key.

        Returns:
            List of label strings.
        """
        client = await self._get_client()
        response = await client.get(f"/issue/{issue_key}?fields=labels")
        response.raise_for_status()
        data = response.json()
        return data.get("fields", {}).get("labels", [])

    async def add_labels(self, issue_key: str, labels: list[str]) -> None:
        """Add labels to a Jira issue.

        Args:
            issue_key: The Jira issue key.
            labels: Labels to add.
        """
        client = await self._get_client()
        response = await client.put(
            f"/issue/{issue_key}",
            json={"update": {"labels": [{"add": label} for label in labels]}},
        )
        response.raise_for_status()
        logger.info(f"Added labels {labels} to {issue_key}")

    async def remove_labels(self, issue_key: str, labels: list[str]) -> None:
        """Remove labels from a Jira issue.

        Args:
            issue_key: The Jira issue key.
            labels: Labels to remove.
        """
        client = await self._get_client()
        response = await client.put(
            f"/issue/{issue_key}",
            json={"update": {"labels": [{"remove": label} for label in labels]}},
        )
        response.raise_for_status()
        logger.info(f"Removed labels {labels} from {issue_key}")

    async def set_workflow_label(
        self,
        issue_key: str,
        new_label: ForgeLabel,
        remove_prefix: str = "forge:",
    ) -> None:
        """Set a workflow label, removing other forge: labels.

        This is used to track Forge workflow state via labels instead of
        custom Jira statuses. Only one workflow label should be active at a time.

        Args:
            issue_key: The Jira issue key.
            new_label: The new workflow label to set.
            remove_prefix: Prefix of labels to remove (default: "forge:").
        """
        # Get current labels
        current_labels = await self.get_labels(issue_key)

        # Find forge: labels to remove (except the new one and forge:managed)
        labels_to_remove = [
            label
            for label in current_labels
            if label.startswith(remove_prefix)
            and label != new_label.value
            and label != ForgeLabel.FORGE_MANAGED.value
        ]

        # Build update operations
        operations: list[dict[str, str]] = []
        for label in labels_to_remove:
            operations.append({"remove": label})
        operations.append({"add": new_label.value})

        # Ensure forge:managed is set
        if ForgeLabel.FORGE_MANAGED.value not in current_labels:
            operations.append({"add": ForgeLabel.FORGE_MANAGED.value})

        client = await self._get_client()
        response = await client.put(
            f"/issue/{issue_key}",
            json={"update": {"labels": operations}},
        )
        response.raise_for_status()
        logger.info(
            f"Set workflow label {new_label.value} on {issue_key} (removed: {labels_to_remove})"
        )

    async def add_structured_comment(
        self,
        issue_key: str,
        title: str,
        content: str,
        comment_type: str = "forge-artifact",
    ) -> JiraComment:
        """Add a structured comment with a marker for later retrieval.

        Used to store PRD/Spec content in comments when custom fields
        are not available.

        Args:
            issue_key: The Jira issue key.
            title: Title/header for the comment.
            content: Main content of the comment.
            comment_type: Type marker (e.g., 'prd', 'spec', 'plan').

        Returns:
            The created JiraComment.
        """
        # Format with markers for easy parsing
        formatted_body = (
            f"[FORGE:{comment_type.upper()}]\n"
            f"# {title}\n\n"
            f"{content}\n\n"
            f"[/FORGE:{comment_type.upper()}]"
        )
        return await self.add_comment(issue_key, formatted_body)

    async def get_structured_comment(
        self,
        issue_key: str,
        comment_type: str,
    ) -> str | None:
        """Get the latest structured comment of a specific type.

        Args:
            issue_key: The Jira issue key.
            comment_type: Type marker to search for.

        Returns:
            The comment content or None if not found.
        """
        comments = await self.get_comments(issue_key)
        marker_start = f"[FORGE:{comment_type.upper()}]"
        marker_end = f"[/FORGE:{comment_type.upper()}]"

        # Search in reverse order to get the latest
        for comment in reversed(comments):
            body = comment.body
            if marker_start in body and marker_end in body:
                # Extract content between markers
                start_idx = body.index(marker_start) + len(marker_start)
                end_idx = body.index(marker_end)
                return body[start_idx:end_idx].strip()

        return None

    async def search_issues(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int = 50,
    ) -> list[JiraIssue]:
        """Search for issues using JQL.

        Args:
            jql: JQL query string.
            fields: Optional list of fields to return.
            max_results: Maximum number of results (default: 50).

        Returns:
            List of matching JiraIssue objects.
        """
        client = await self._get_client()
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
        }
        if fields:
            params["fields"] = ",".join(fields)

        response = await client.get("/search", params=params)
        response.raise_for_status()
        data = response.json()

        return [JiraIssue.from_api_response(issue) for issue in data.get("issues", [])]

    async def get_epic_children(self, epic_key: str) -> list[JiraIssue]:
        """Get all child issues (Tasks/Stories) linked to an Epic.

        Args:
            epic_key: The Epic's Jira key.

        Returns:
            List of child JiraIssue objects.
        """
        # Use parent link - works in both classic and next-gen projects
        jql = f'"Parent" = {epic_key} OR "Epic Link" = {epic_key}'
        return await self.search_issues(
            jql=jql,
            fields=["summary", "status", "issuetype"],
        )

    async def get_project_property(self, project_key: str, property_key: str) -> Any | None:
        """Fetch a Jira project property value.

        Args:
            project_key: The Jira project key (e.g., "MYPROJ").
            property_key: The property key (e.g., "forge.repos").

        Returns:
            The property value, or None if the property is not set (404).
        """
        cache_key = (project_key, property_key)
        if cache_key in _project_property_cache:
            return _project_property_cache[cache_key]

        client = await self._get_client()
        response = await client.get(f"/project/{project_key}/properties/{property_key}")

        if response.status_code == 404:
            return None

        response.raise_for_status()
        value = response.json()["value"]
        _project_property_cache[cache_key] = value
        return value

    async def set_project_property(self, project_key: str, property_key: str, value: Any) -> None:
        """Set a Jira project property value.

        Args:
            project_key: The Jira project key (e.g., "MYPROJ").
            property_key: The property key (e.g., "forge.repos").
            value: The value to set (must be JSON-serialisable).
        """
        client = await self._get_client()
        response = await client.put(
            f"/project/{project_key}/properties/{property_key}",
            json=value,
        )
        response.raise_for_status()
        _project_property_cache.pop((project_key, property_key), None)

    async def delete_project_property(self, project_key: str, property_key: str) -> None:
        """Delete a Jira project property.

        Args:
            project_key: The Jira project key (e.g., "MYPROJ").
            property_key: The property key (e.g., "forge.prd_proposals_repo").
        """
        client = await self._get_client()
        response = await client.delete(
            f"/project/{project_key}/properties/{property_key}",
        )
        if response.status_code != 404:
            response.raise_for_status()
        _project_property_cache.pop((project_key, property_key), None)

    async def get_project_repos(self, project_key: str) -> list[str]:
        """Fetch the forge.repos project property.

        Args:
            project_key: The Jira project key.

        Returns:
            List of repo strings in "owner/repo" format.

        Raises:
            MissingProjectConfig: If the property is absent or malformed.
        """
        value = await self.get_project_property(project_key, "forge.repos")
        if value is None:
            raise MissingProjectConfig(f"forge.repos not set for project {project_key}")
        if not isinstance(value, list) or any(
            not isinstance(r, str) or "/" not in r for r in value
        ):
            raise MissingProjectConfig(
                f"forge.repos for project {project_key} is malformed: {value!r}"
            )
        logger.info(f"Project {project_key}: repos from Jira property: {value}")
        return value

    async def get_project_default_repo(self, project_key: str) -> str:
        """Fetch the forge.default_repo project property.

        Args:
            project_key: The Jira project key.

        Returns:
            Repo string in "owner/repo" format.

        Raises:
            MissingProjectConfig: If the property is absent or malformed.
        """
        value = await self.get_project_property(project_key, "forge.default_repo")
        if value is None:
            raise MissingProjectConfig(f"forge.default_repo not set for project {project_key}")
        if not isinstance(value, str) or "/" not in value:
            raise MissingProjectConfig(
                f"forge.default_repo for project {project_key} is malformed: {value!r}"
            )
        logger.info(f"Project {project_key}: default repo: {value}")
        return value

    async def get_prd_proposals_repo(self, project_key: str) -> str | None:
        """Fetch the forge.prd_proposals_repo project property.

        When set, enables PRD approval via GitHub PR for this project.
        The value is a GitHub repo in "owner/repo" format.

        Args:
            project_key: The Jira project key.

        Returns:
            Repo string in "owner/repo" format, or None if not configured.
        """
        value = await self.get_project_property(project_key, "forge.prd_proposals_repo")
        if value is None:
            return None
        if not isinstance(value, str) or "/" not in value:
            logger.warning(
                f"forge.prd_proposals_repo for project {project_key} is malformed: {value!r}"
            )
            return None
        logger.info(f"Project {project_key}: PRD proposals repo: {value}")
        return value

    async def get_skills_config(self, project_key: str) -> list[SkillEntry] | None:
        """Fetch and parse the forge.skills project property.

        Performs a fresh API call on every invocation — results are intentionally
        NOT cached so that each ticket receives an up-to-date skill configuration
        as required by SC-001.

        Args:
            project_key: The Jira project key (e.g., "MYPROJ").

        Returns:
            A list of :class:`~forge.skills.models.SkillEntry` objects when the
            property is set and valid, an empty list when the property value is
            present but cannot be parsed, or ``None`` when the property is not set
            (i.e. the API returns 404).
        """
        client = await self._get_client()
        response = await client.get(f"/project/{project_key}/properties/forge.skills")

        if response.status_code == 404:
            return None

        response.raise_for_status()
        value = response.json().get("value")

        # value may already be a parsed list (Jira sometimes auto-parses JSON
        # property values) or a raw JSON string.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                logger.warning(
                    f"forge.skills for project {project_key} contains invalid JSON; "
                    "returning empty list."
                )
                return []

        if not isinstance(value, list):
            logger.warning(
                f"forge.skills for project {project_key} is not a list "
                f"(got {type(value).__name__}); returning empty list."
            )
            return []

        entries: list[SkillEntry] = []
        for i, item in enumerate(value):
            try:
                entries.append(SkillEntry.model_validate(item))
            except (ValidationError, TypeError) as exc:
                logger.warning(
                    f"forge.skills[{i}] for project {project_key} failed validation: {exc}; "
                    "returning empty list."
                )
                return []

        return entries

    @staticmethod
    def _text_to_adf(text: str) -> dict[str, Any]:
        """Convert markdown text to Atlassian Document Format.

        Uses md-to-adf library for robust markdown parsing including
        tables, code blocks, lists, and inline formatting.

        Args:
            text: Markdown text content.

        Returns:
            ADF document structure.
        """
        from md_to_adf import convert

        if not text or not text.strip():
            return {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": []}],
            }

        try:
            return convert(text)
        except Exception as e:
            logger.warning(f"ADF conversion failed, using simple fallback: {e}")
            # Simple fallback - just paragraphs
            content = []
            for para in text.split("\n\n"):
                para = para.strip()
                if para:
                    content.append(
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": para}],
                        }
                    )
            return {
                "type": "doc",
                "version": 1,
                "content": content or [{"type": "paragraph", "content": []}],
            }

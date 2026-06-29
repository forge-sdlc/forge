"""Jira data models for API responses."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class JiraUser:
    """Represents a Jira user."""

    account_id: str
    display_name: str
    email: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any] | None) -> "JiraUser | None":
        """Create a JiraUser from an API response field."""
        if not data:
            return None
        return cls(
            account_id=data.get("accountId", ""),
            display_name=data.get("displayName", ""),
            email=data.get("emailAddress"),
        )


@dataclass
class JiraIssue:
    """Represents a Jira issue from the REST API."""

    key: str
    id: str
    summary: str
    description: str
    status: str
    issue_type: str
    parent_key: str | None = None
    labels: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)
    created: datetime | None = None
    updated: datetime | None = None
    reporter: JiraUser | None = None
    assignee: JiraUser | None = None

    @property
    def project_key(self) -> str:
        """Extract project key from issue key (e.g., 'AISOS' from 'AISOS-104')."""
        return self.key.rsplit("-", 1)[0] if "-" in self.key else self.key

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "JiraIssue":
        """Create a JiraIssue from an API response.

        Args:
            data: Raw API response dictionary.

        Returns:
            Populated JiraIssue instance.
        """
        fields = data.get("fields", {})

        # Extract description text from ADF
        description = ""
        desc_field = fields.get("description")
        if desc_field and isinstance(desc_field, dict):
            description = cls._extract_text_from_adf(desc_field)
        elif isinstance(desc_field, str):
            description = desc_field

        # Extract parent key if present
        parent_key = None
        parent = fields.get("parent")
        if parent:
            parent_key = parent.get("key")

        # Parse dates
        created = None
        if fields.get("created"):
            created = datetime.fromisoformat(fields["created"].replace("Z", "+00:00"))

        updated = None
        if fields.get("updated"):
            updated = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00"))

        # Collect custom fields
        custom_fields = {k: v for k, v in fields.items() if k.startswith("customfield_")}

        # Extract reporter and assignee
        reporter = JiraUser.from_api_response(fields.get("reporter"))
        assignee = JiraUser.from_api_response(fields.get("assignee"))

        return cls(
            key=data.get("key", ""),
            id=data.get("id", ""),
            summary=fields.get("summary", ""),
            description=description,
            status=fields.get("status", {}).get("name", ""),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            parent_key=parent_key,
            labels=fields.get("labels", []),
            custom_fields=custom_fields,
            created=created,
            updated=updated,
            reporter=reporter,
            assignee=assignee,
        )

    @staticmethod
    def _extract_text_from_adf(adf: dict[str, Any]) -> str:
        """Extract plain text from Atlassian Document Format.

        Args:
            adf: ADF document structure.

        Returns:
            Extracted plain text.
        """
        if not isinstance(adf, dict):
            return str(adf) if adf else ""

        # Jira does not provide a supported ADF-to-Markdown endpoint. Keep this
        # extractor local and conservative so workflow prompts receive readable
        # issue text without taking a dependency on an under-supported converter.
        # Add unsupported ADF nodes here with focused JiraIssue regression tests.
        # If a node cannot be parsed, preserve its raw representation instead of
        # dropping issue content from workflow prompts.
        def inline_text(nodes: list[dict[str, Any]]) -> str:
            parts = []
            for child in nodes:
                child_type = child.get("type")
                if child_type == "text":
                    parts.append(child.get("text", ""))
                elif child_type == "hardBreak":
                    parts.append("\n")
                else:
                    parts.append("\n".join(extract_blocks(child)))
            return "".join(parts)

        def extract_blocks(node: dict[str, Any]) -> list[str]:
            node_type = node.get("type")
            content = node.get("content", [])

            if node_type == "doc":
                return extract_children(content)
            if node_type == "paragraph":
                text = inline_text(content).strip()
                return [text] if text else []
            if node_type == "heading":
                text = inline_text(content).strip()
                if not text:
                    return []
                level = node.get("attrs", {}).get("level", 1)
                return [f"{'#' * int(level)} {text}"]
            if node_type == "text":
                text = node.get("text", "")
                return [text] if text else []
            if node_type == "codeBlock":
                text = inline_text(content)
                language = node.get("attrs", {}).get("language", "")
                fence = f"```{language}".rstrip()
                return [f"{fence}\n{text}\n```"]
            if node_type in ("bulletList", "orderedList"):
                items = []
                for index, item in enumerate(content, start=1):
                    item_text = "\n".join(extract_blocks(item)).strip()
                    if item_text:
                        prefix = f"{index}. " if node_type == "orderedList" else "- "
                        items.append(prefix + item_text.replace("\n", "\n  "))
                return items
            if node_type == "listItem":
                return extract_children(content)
            if node_type == "rule":
                return ["---"]

            blocks = extract_children(content)
            return blocks or [str(node)]

        def extract_children(nodes: list[dict[str, Any]]) -> list[str]:
            blocks = []
            for child in nodes:
                blocks.extend(extract_blocks(child))
            return [block for block in blocks if block]

        blocks = extract_blocks(adf)
        return "\n\n".join(blocks) if blocks else str(adf)


@dataclass
class JiraComment:
    """Represents a Jira comment from the REST API."""

    id: str
    body: str
    author_id: str
    author_name: str
    created: datetime | None = None
    updated: datetime | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "JiraComment":
        """Create a JiraComment from an API response.

        Args:
            data: Raw API response dictionary.

        Returns:
            Populated JiraComment instance.
        """
        # Extract body text from ADF
        body = ""
        body_field = data.get("body")
        if body_field and isinstance(body_field, dict):
            body = JiraIssue._extract_text_from_adf(body_field)
        elif isinstance(body_field, str):
            body = body_field

        author = data.get("author", {})

        # Parse dates
        created = None
        if data.get("created"):
            created = datetime.fromisoformat(data["created"].replace("Z", "+00:00"))

        updated = None
        if data.get("updated"):
            updated = datetime.fromisoformat(data["updated"].replace("Z", "+00:00"))

        return cls(
            id=data.get("id", ""),
            body=body,
            author_id=author.get("accountId", ""),
            author_name=author.get("displayName", ""),
            created=created,
            updated=updated,
        )

"""Utilities for state-backed draft review and comment rendering."""

import copy
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from forge.models.draft import DraftItem, ForgeDecompositionDraft

logger = logging.getLogger(__name__)


class DraftCommentPort(Protocol):
    """Minimal port needed to publish draft-review comments."""

    async def add_comment(self, issue_key: str, body: str) -> None: ...


class DraftManager:
    """Manages draft edits and state-backed review comments."""

    @staticmethod
    def _validate_item_params(
        params: dict[str, Any], target_item: dict[str, Any] | None = None
    ) -> None:
        """Validate the fields in draft item parameters strictly.

        Args:
            params: The parameters dictionary.
            target_item: Optional target item dictionary to merge with (for update command).

        Raises:
            ValueError: If a validation check fails.
        """
        from forge.models.draft import DraftItem

        if target_item is not None:
            full_item = {**target_item, **params}
        else:
            defaults = {
                "id": 1,
                "summary": "",
                "description": "",
                "repo": "",
                "acceptance_criteria": [],
                "excluded": False,
                "epic_key": None,
            }
            full_item = {**defaults, **params}

        try:
            DraftItem.model_validate(full_item, strict=True)
        except ValidationError as e:
            for error in e.errors():
                loc = error["loc"]
                if not loc:
                    continue
                field = str(loc[0])
                error_type = error["type"]
                if error_type == "extra_forbidden":
                    raise ValueError(f"Unknown field '{field}'")
                elif field in {"summary", "description", "repo"}:
                    val = (
                        params.get(field)
                        if field in params
                        else (target_item.get(field) if target_item else None)
                    )
                    raise ValueError(
                        f"Field '{field}' must be a string, got {type(val).__name__ if val is not None else 'None'}."
                    )
                elif field == "acceptance_criteria":
                    raise ValueError("Field 'acceptance_criteria' must be a list of strings.")
                elif field == "excluded":
                    raise ValueError("Field 'excluded' must be a boolean.")
                elif field == "epic_key":
                    raise ValueError("Field 'epic_key' must be a string or None.")
            raise ValueError(str(e))

    @staticmethod
    def apply_draft_modification(
        draft_json: list[dict[str, Any]],
        parsed_command: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply a direct mutation on a list of draft story or task JSON objects based on the command type.

        Args:
            draft_json: The current list of draft item dictionaries.
            parsed_command: The parsed comment command dictionary.

        Returns:
            The mutated list of draft item dictionaries.

        Raises:
            ValueError: If the command contains an error, the target ID is missing/not found,
                        or strict type validation fails.
        """
        if "error" in parsed_command:
            raise ValueError(f"Invalid command parameters: {parsed_command['error']}")

        command = parsed_command.get("command")
        if not command:
            raise ValueError("Command type is missing in parsed command.")

        mutated_list = copy.deepcopy(draft_json)

        if command == "remove":
            target_id = parsed_command.get("id")
            if target_id is None:
                raise ValueError("Missing ID for removal.")

            # Find and remove item
            found = False
            for i, item in enumerate(mutated_list):
                if item.get("id") == target_id:
                    mutated_list.pop(i)
                    found = True
                    break

            if not found:
                raise ValueError(f"Item with ID {target_id} not found for removal.")

            # Re-sequence remaining items
            for idx, item in enumerate(mutated_list):
                item["id"] = idx + 1

        elif command == "add":
            next_id = len(mutated_list) + 1
            params = parsed_command.get("params", {})

            # Strict type validation
            DraftManager._validate_item_params(params)

            # Build the new item using parsed parameters with defaults
            new_item = {
                "id": next_id,
                "summary": params.get("summary", ""),
                "description": params.get("description", ""),
                "repo": params.get("repo", ""),
                "acceptance_criteria": params.get("acceptance_criteria", []),
                "excluded": params.get("excluded", False),
                "epic_key": params.get("epic_key"),
            }

            mutated_list.append(new_item)

        elif command == "update":
            target_id = parsed_command.get("id")
            if target_id is None:
                raise ValueError("Missing ID for update.")

            # Find the item
            target_item = None
            for item in mutated_list:
                if item.get("id") == target_id:
                    target_item = item
                    break

            if not target_item:
                raise ValueError(f"Item with ID {target_id} not found for update.")

            params = parsed_command.get("params", {})

            # Strict type validation
            DraftManager._validate_item_params(params, target_item)

            # Apply updates
            for k, v in params.items():
                target_item[k] = v

        elif command == "exclude":
            target_id = parsed_command.get("id")
            if target_id is None:
                raise ValueError("Missing ID for exclude command.")

            # Find the item
            target_item = None
            for item in mutated_list:
                if item.get("id") == target_id:
                    target_item = item
                    break

            if not target_item:
                raise ValueError(f"Item with ID {target_id} not found for exclude.")

            # Flip the excluded boolean key
            target_item["excluded"] = not target_item.get("excluded", False)

        else:
            raise ValueError(f"Unsupported modification command type: '{command}'")

        return mutated_list

    @staticmethod
    def _truncate_to_jira_limit(text: str, limit: int = 32767) -> str:
        """Truncate text to fit within Jira's character limit and append a [truncated] suffix."""
        if len(text) <= limit:
            return text
        suffix = " [truncated]"
        if limit <= len(suffix):
            return text[:limit]
        return text[: limit - len(suffix)] + suffix

    @staticmethod
    def format_review_comment(draft: ForgeDecompositionDraft, limit: int = 32767) -> str:
        """Format a human-readable review comment for a draft."""
        from forge.models.workflow import ForgeLabel

        items = draft.items
        if draft.phase == "epics":
            phase_title = "Epics"
            phase_action = "decomposition"
            item_label = "Plan"
            approval_label = ForgeLabel.PLAN_APPROVED.value
        else:
            phase_title = "Tasks"
            phase_action = "implementation"
            item_label = "Description"
            approval_label = ForgeLabel.TASK_APPROVED.value

        header = f"### 📋 Proposed {phase_title} Draft\n\nThe following {phase_title} have been proposed for {phase_action}:\n\n"
        summary_list = ""
        for item in items:
            if item.excluded:
                summary = f"~~{item.summary}~~ *(excluded)*"
                repo = f"~~{item.repo or 'unknown'}~~"
            else:
                summary = item.summary
                repo = item.repo or "unknown"
            summary_list += f"- **{item.id}.** {summary} — Repo: `{repo}`\n"
        summary_list += "\n---\n\n"

        details = ""
        for item in items:
            heading_summary = f"~~{item.summary}~~ *(excluded)*" if item.excluded else item.summary
            details += f"#### {item.id}. {heading_summary} (Repo: {item.repo or 'unknown'})\n"
            if item.description:
                details += f"**{item_label}:**\n\n{item.description}\n\n"
            else:
                details += "\n"

        footer = (
            "## 🤖 Forge interaction options\n\n"
            f"- Approve:  add the `{approval_label}` label\n"
            f"- Revise:   comment starting with `!` (regenerates with your feedback)\n"
            f"- Add:      /forge add summary=... repo=...\n"
            f"- Update:   /forge update <ID> summary=... | description=... | repo=...\n"
            f"- Remove:   /forge remove <ID>\n"
            f"- Exclude:  /forge exclude <ID>\n"
            f"- Ask:      comment starting with `?`"
        )

        full_comment = header + summary_list + details + footer

        if len(full_comment) > limit or (draft.phase == "epics" and len(items) > 15):
            overflow_guidance = (
                "The complete plan is retained in Forge's workflow state; request a revision "
                "if the condensed summary needs more detail."
                if draft.phase == "epics"
                else "The complete task breakdown will be posted in ordered continuation comments."
            )
            condensed_header = (
                f"### 📋 Proposed {phase_title} Draft (Condensed)\n\n"
                "⚠️ **Warning:** The proposed plan exceeds character or size limits for detailed display in a comment. "
                f"{overflow_guidance}\n\n"
            )
            rows = []
            for item in items:
                if item.excluded:
                    summary = f"~~{item.summary}~~ *(excluded)*"
                    repo = f"~~{item.repo or 'unknown'}~~"
                else:
                    summary = item.summary
                    repo = item.repo or "unknown"
                rows.append(f"- **{item.id}.** {summary} — Repo: `{repo}`\n")

            full_condensed_comment = condensed_header + "".join(rows) + "\n" + footer

            if len(full_condensed_comment) > limit:
                allowed_rows: list[str] = []
                for i, row in enumerate(rows, start=1):
                    temp_warning = f"\n⚠️ Showing first {i} items in this comment.\n\n"
                    temp_comment = (
                        condensed_header + "".join(allowed_rows + [row]) + temp_warning + footer
                    )
                    if len(temp_comment) <= limit:
                        allowed_rows.append(row)
                    else:
                        break

                count = len(allowed_rows)
                warning_note = f"\n⚠️ Showing first {count} items in this comment.\n\n"
                condensed_comment = condensed_header + "".join(allowed_rows) + warning_note + footer
            else:
                condensed_comment = full_condensed_comment

            return DraftManager._truncate_to_jira_limit(condensed_comment, limit)

        return DraftManager._truncate_to_jira_limit(full_comment, limit)

    @staticmethod
    def chunk_text_by_limit(text: str, limit: int = 30000) -> list[str]:
        """Split text into chunks of at most 'limit' characters, splitting by lines if possible."""
        if len(text) <= limit:
            return [text]

        chunks = []
        lines = text.split("\n")
        current_chunk: list[str] = []
        current_length = 0

        for line in lines:
            if len(line) > limit:
                if current_chunk:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_length = 0
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                if line:
                    current_chunk = [line]
                    current_length = len(line)
                continue

            if current_length + len(line) + 1 > limit:
                if current_chunk:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_length = len(line)
            else:
                current_chunk.append(line)
                current_length += len(line) + 1

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    @staticmethod
    async def post_task_draft_review(
        jira_client: DraftCommentPort,
        feature_key: str,
        draft: ForgeDecompositionDraft,
    ) -> None:
        """Post sliced task draft review comments to Epic tickets, with continuation chunks for overflow, and a navigation link comment on Feature."""
        # 1. Slice draft by Epic key
        slices: dict[str, list[DraftItem]] = {}
        for item in draft.items:
            ek = item.epic_key
            if not ek:
                continue
            if ek not in slices:
                slices[ek] = []
            slices[ek].append(item)

        # 2. For each Epic, post the task draft review comment (supporting ordered continuation comments for overflow)
        for epic_key, items in slices.items():
            resequenced_items = []
            for idx, item in enumerate(items, start=1):
                cloned_item = item.model_copy()
                cloned_item.id = idx
                resequenced_items.append(cloned_item)

            epic_draft = ForgeDecompositionDraft(
                parent_key=epic_key,
                phase="tasks",
                items=resequenced_items,
                version=draft.version,
                created_at=draft.created_at,
                updated_at=datetime.now(UTC),
            )

            # Format the Epic's review comment
            # Keep every task detail visible. Jira's per-comment limit is
            # handled below with ordered continuation comments, so no JSON
            # attachment or condensed-only fallback is needed.
            epic_comment = DraftManager.format_review_comment(epic_draft, limit=10**9)

            # Support ordered continuation comments for overflow:
            chunks = DraftManager.chunk_text_by_limit(epic_comment, limit=30000)
            for i, chunk in enumerate(chunks):
                prefix = (
                    f"### 📋 Proposed Tasks Draft (Part {i + 1} of {len(chunks)})\n\n"
                    if len(chunks) > 1
                    else ""
                )
                await jira_client.add_comment(epic_key, prefix + chunk)

        # 3. On the Feature ticket, publish feature-level navigation links pointing to the Epics
        feature_comment = "### 📋 Proposed Tasks Drafts by Epic\n\nThe tasks have been proposed and distributed across the individual Epic tickets. Please review the detailed draft breakdown on each Epic:\n\n"
        for epic_key in slices:
            feature_comment += f"- 🔗 **Review Epic Tasks on:** {epic_key}\n"
        feature_comment += "\n---\n## 🤖 Feature-Level Approval\nApproving this Feature will provision all tasks across all Epics. Please add the `forge:task-approved` label to this Feature ticket when ready."

        await jira_client.add_comment(feature_key, feature_comment)

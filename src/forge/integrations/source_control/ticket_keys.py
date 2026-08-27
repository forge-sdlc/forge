"""Shared Jira ticket-key extraction for provider webhook routes.

Both the GitHub and GitLab webhook routes derive a ticket key the same way --
prefer the change request's title/branch, then fall back to branch names carried
in the raw payload -- differing only in which raw fields carry the branch. This
module holds the one regex and the shared traversal so provider behavior cannot
drift between the two routes.
"""

import re
from collections.abc import Iterable

from forge.integrations.source_control.contracts import NormalizedEvent

TICKET_PATTERN = re.compile(r"([A-Z][A-Z0-9]+-\d+)", re.IGNORECASE)


def extract_ticket_key(
    event: NormalizedEvent, *, fallback_branch_sources: Iterable[str] = ()
) -> str:
    """Extract a Jira ticket key from a NormalizedEvent.

    Prefers the change request's title/branch when one is present. Otherwise
    searches ``fallback_branch_sources`` -- the provider-specific raw payload
    fields (a push ref, a pipeline/check branch) that carry the branch when no
    change request is attached. Returns "" when nothing matches.
    """
    if event.change_request is not None:
        for text in (event.change_request.title, event.change_request.source_branch):
            match = TICKET_PATTERN.search(text or "")
            if match:
                return match.group(1).upper()
    for text in fallback_branch_sources:
        match = TICKET_PATTERN.search(str(text))
        if match:
            return match.group(1).upper()
    return ""

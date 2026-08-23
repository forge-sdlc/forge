"""Queue message models for webhook event processing."""

import json
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime
from typing import Any

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    Review,
    ReviewComment,
    ReviewState,
)
from forge.models.events import EventSource

# EventSource.SOURCE_CONTROL's value was renamed from "github" to
# "source_control". Retry/DLQ entries and unconsumed stream messages
# persisted before the rename still carry the old value in Redis; map it
# forward so they keep deserializing instead of raising ValueError.
_LEGACY_SOURCE_VALUES: dict[str, EventSource] = {"github": EventSource.SOURCE_CONTROL}


def _parse_event_source(value: str) -> EventSource:
    legacy = _LEGACY_SOURCE_VALUES.get(value)
    return legacy if legacy is not None else EventSource(value)


@dataclass
class QueueMessage:
    """Represents a message in the Redis Streams queue."""

    message_id: str
    event_id: str
    source: EventSource
    event_type: str
    ticket_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    normalized_event: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    retry_count: int = 0

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for Redis storage.

        Returns:
            Dictionary with string values for Redis.
        """
        return {
            "event_id": self.event_id,
            "source": self.source.value,
            "event_type": self.event_type,
            "ticket_key": self.ticket_key,
            "payload": json.dumps(self.payload),
            "normalized_event": (
                json.dumps(self.normalized_event) if self.normalized_event is not None else ""
            ),
            "timestamp": self.timestamp.isoformat(),
            "retry_count": str(self.retry_count),
        }

    @classmethod
    def from_redis(cls, message_id: str, data: dict[str, str]) -> "QueueMessage":
        """Create from Redis stream entry.

        Args:
            message_id: Redis stream message ID.
            data: Message data from Redis.

        Returns:
            Populated QueueMessage instance.
        """
        normalized_event_raw = data.get("normalized_event", "")
        return cls(
            message_id=message_id,
            event_id=data.get("event_id", ""),
            source=_parse_event_source(data.get("source", "jira")),
            event_type=data.get("event_type", ""),
            ticket_key=data.get("ticket_key", ""),
            payload=json.loads(data.get("payload", "{}")),
            normalized_event=json.loads(normalized_event_raw) if normalized_event_raw else None,
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.utcnow().isoformat())),
            retry_count=int(data.get("retry_count", "0")),
        )

    def increment_retry(self) -> "QueueMessage":
        """Return a new message with incremented retry count."""
        return dataclass_replace(self, retry_count=self.retry_count + 1)


def normalized_event_to_dict(event: NormalizedEvent) -> dict[str, Any]:
    """Serialize a NormalizedEvent for queue transport."""
    return {
        "id": event.id,
        "kind": event.kind.value,
        "repo_ref": {
            "id": event.repo_ref.id,
            "provider": event.repo_ref.provider.value,
            "connection": event.repo_ref.connection,
            "namespace": event.repo_ref.namespace,
            "default_branch": event.repo_ref.default_branch,
            "change_request_mode": event.repo_ref.change_request_mode,
        },
        "actor": {"login": event.actor.login, "is_bot": event.actor.is_bot},
        "received_at": event.received_at.isoformat(),
        "change_request": (
            {
                "identity": {
                    "connection": event.change_request.identity.connection,
                    "repository_id": event.change_request.identity.repository_id,
                    "native_id": event.change_request.identity.native_id,
                },
                "url": event.change_request.url,
                "title": event.change_request.title,
                "body": event.change_request.body,
                "state": event.change_request.state.value,
                "source_branch": event.change_request.source_branch,
                "target_branch": event.change_request.target_branch,
                "head_sha": event.change_request.head_sha,
                "draft": event.change_request.draft,
            }
            if event.change_request
            else None
        ),
        "comment": (_review_comment_to_dict(event.comment) if event.comment else None),
        "review": (
            {
                "id": event.review.id,
                "state": event.review.state.value,
                "body": event.review.body,
                "author": event.review.author,
                "comments": [_review_comment_to_dict(c) for c in event.review.comments],
            }
            if event.review
            else None
        ),
        "check": (
            {
                "name": event.check.name,
                "status": event.check.status.value,
                "conclusion": event.check.conclusion.value,
                "url": event.check.url,
                "logs_url": event.check.logs_url,
                "output": event.check.output,
            }
            if event.check
            else None
        ),
        "check_suite_status": (
            event.check_suite_status.value if event.check_suite_status else None
        ),
        "raw": event.raw,
    }


def _review_comment_to_dict(comment: ReviewComment) -> dict[str, Any]:
    """Serialize a ReviewComment for queue transport."""
    return {
        "id": comment.id,
        "body": comment.body,
        "author": comment.author,
        "path": comment.path,
        "line": comment.line,
        "resolved": comment.resolved,
        "in_reply_to": comment.in_reply_to,
    }


def _review_comment_from_dict(data: dict[str, Any]) -> ReviewComment:
    """Deserialize a ReviewComment from queue transport."""
    return ReviewComment(
        id=data["id"],
        body=data["body"],
        author=data["author"],
        path=data.get("path"),
        line=data.get("line"),
        resolved=data.get("resolved", False),
        in_reply_to=data.get("in_reply_to"),
    )


def normalized_event_from_dict(data: dict[str, Any]) -> NormalizedEvent:
    """Deserialize a NormalizedEvent from queue transport."""
    repo_ref_data = data["repo_ref"]
    repo_ref = RepositoryRef(
        id=repo_ref_data["id"],
        provider=Provider(repo_ref_data["provider"]),
        connection=repo_ref_data["connection"],
        namespace=repo_ref_data["namespace"],
        default_branch=repo_ref_data["default_branch"],
        change_request_mode=repo_ref_data["change_request_mode"],
    )
    actor = Actor(login=data["actor"]["login"], is_bot=data["actor"]["is_bot"])

    change_request = None
    if data.get("change_request") is not None:
        cr = data["change_request"]
        change_request = ChangeRequest(
            identity=ChangeRequestIdentity(
                connection=cr["identity"]["connection"],
                repository_id=cr["identity"]["repository_id"],
                native_id=cr["identity"]["native_id"],
            ),
            url=cr["url"],
            title=cr["title"],
            body=cr["body"],
            state=ChangeRequestState(cr["state"]),
            source_branch=cr["source_branch"],
            target_branch=cr["target_branch"],
            head_sha=cr.get("head_sha", ""),
            draft=cr["draft"],
        )

    comment = None
    if data.get("comment") is not None:
        comment = _review_comment_from_dict(data["comment"])

    review = None
    if data.get("review") is not None:
        r = data["review"]
        review = Review(
            id=r["id"],
            state=ReviewState(r["state"]),
            body=r["body"],
            author=r["author"],
            comments=[_review_comment_from_dict(c) for c in r.get("comments", [])],
        )

    check = None
    if data.get("check") is not None:
        ck = data["check"]
        check = CheckRun(
            name=ck["name"],
            status=CheckStatus(ck["status"]),
            conclusion=CheckConclusion(ck["conclusion"]),
            url=ck.get("url"),
            logs_url=ck.get("logs_url"),
            output=ck.get("output", {}),
        )

    check_suite_status_value = data.get("check_suite_status")

    return NormalizedEvent(
        id=data["id"],
        kind=EventKind(data["kind"]),
        repo_ref=repo_ref,
        actor=actor,
        received_at=datetime.fromisoformat(data["received_at"]),
        change_request=change_request,
        comment=comment,
        review=review,
        check=check,
        check_suite_status=(
            CheckStatus(check_suite_status_value) if check_suite_status_value else None
        ),
        raw=data.get("raw", {}),
    )

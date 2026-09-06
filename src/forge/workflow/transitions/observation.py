"""Provider-neutral application of normalized workflow observations.

This module owns the observation-to-state transition reducer.  The orchestrator
worker supplies the narrow runtime hooks used for external effects; it does not
own the event-specific state machine.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forge.domain import JsonValue
from forge.effects.jira import (
    JIRA_ATTACHMENT_REPLACE_OPERATION,
    JIRA_CUSTOM_FIELD_OPERATION,
    JIRA_DESCRIPTION_OPERATION,
    JIRA_LABEL_OPERATION,
    JIRA_STRUCTURED_COMMENT_OPERATION,
)
from forge.integrations.source_control.comment_identity import is_self_comment
from forge.integrations.source_control.contracts import (
    ChangeRequestState,
    CheckStatus,
    EventKind,
    ReviewState,
)
from forge.models.events import EventSource
from forge.models.workflow import ForgeLabel
from forge.orchestrator.command_handlers import (
    FeedbackKind,
    create_default_command_handler_registry,
)
from forge.orchestrator.event_adapters import (
    interpret_event,
)
from forge.utils.redaction import redact_secrets
from forge.workflow.pr_state import (
    activate_pull_request_for_event,
    all_pull_requests_merged,
    event_targets_pull_request,
    mark_active_pull_request_merged,
    save_active_pull_request,
)
from forge.workflow.utils.comment_classifier import CommentType, classify_comment
from forge.workflow.utils.review_decisions import (
    decision_matches_comment,
    merge_review_decisions,
)

# Keep the historical logger name so deployments and existing observability
# filters continue to receive transition diagnostics after extraction.
logger = logging.getLogger("forge.orchestrator.worker")


@dataclass(frozen=True)
class ObservationTransitionPolicy:
    """Identity of the workflow definition governing an observation.

    ``definition`` is intentionally opaque to this runtime.  Declarative
    workflow resolution can provide the concrete policy later without making
    this observation reducer depend on declarative models.
    """

    identifier: str = "default"
    definition: Mapping[str, Any] | None = None


def _validate_policy(policy: ObservationTransitionPolicy) -> frozenset[str] | None:
    """Validate a definition-selected policy and return its declared nodes."""
    if policy.identifier == "default" and policy.definition is None:
        return None  # Local harnesses have no published process artifact.
    if policy.identifier != "post-pr-v1":
        raise ValueError(f"unknown observation transition policy {policy.identifier!r}")
    if policy.definition is None:
        raise ValueError("a governed observation policy requires a pinned definition")
    spec = policy.definition.get("spec")
    if not isinstance(spec, Mapping):
        raise ValueError("checkpoint definition has no workflow specification")
    steps = spec.get("steps")
    if not isinstance(steps, Mapping):
        raise ValueError("checkpoint definition has no workflow steps")
    return frozenset(str(name) for name in steps)


def _validate_target(state: Mapping[str, Any], allowed_nodes: frozenset[str] | None) -> None:
    if allowed_nodes is None:
        return
    target = str(state.get("current_node") or "")
    if target and target not in allowed_nodes and target not in {"entry", "complete", "__end__"}:
        raise ValueError(f"observation policy targeted undeclared workflow node {target!r}")


def _flatten_review_threads(reviews: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "path": review.comments[-1].path or "",
            "line": review.comments[-1].line,
            "body": review.comments[-1].body,
        }
        for review in reviews
        if review.comments
    ]


def _reviews_to_raw_threads(reviews: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "thread_id": review.id,
            "path": review.comments[0].path if review.comments else None,
            "line": review.comments[0].line if review.comments else None,
            "comments": [
                {
                    "comment_id": int(comment.id) if comment.id.isdigit() else comment.id,
                    "body": comment.body,
                }
                for comment in review.comments
            ],
        }
        for review in reviews
    ]


def _is_workflow_errored(state: Mapping[str, Any]) -> bool:
    return not state.get("is_paused") and state.get("last_error") is not None


_PRD_GATE_NODES = ("prd_approval_gate", "generate_prd", "regenerate_prd")
_SPEC_GATE_NODES = ("spec_approval_gate", "generate_spec", "regenerate_spec")
_REVIEW_GATES = ("human_review_gate", "review_response_gate")
_MAX_AUTOMATED_REVIEW_REVISIONS = 3


def deserialize_observation_event(message: Any, adapted_event: Any | None = None) -> Any | None:
    """Return the normalized event carried by a source-control observation."""
    if message.normalized_event is None:
        return None
    return adapted_event.normalized_event if adapted_event is not None else None


def is_proposal_pull_request_event(
    message: Any, state: Mapping[str, Any], event: Any | None, *, artifact: str
) -> bool:
    """Match a normalized observation to the configured PRD/spec proposal PR."""
    if message.source is not EventSource.SOURCE_CONTROL or event is None:
        return False
    if event.change_request is None:
        return False
    prefix = "prd" if artifact == "prd" else "spec"
    number = state.get(f"{prefix}_pr_number")
    repo = state.get(f"{prefix}_pr_repo")
    return bool(
        number
        and repo
        and event.repo_ref.namespace == repo
        and event.change_request.identity.native_id == number
    )


async def apply_observation_transition(
    runtime: Any,
    message: Any,
    current_state: dict[str, Any],
    *,
    adapted_event: Any | None = None,
    command_decision: Any | None = None,
    policy: ObservationTransitionPolicy | None = None,
) -> dict[str, Any]:
    """Apply one normalized observation to the durable workflow state.

    ``runtime`` exposes only the effect and enrichment hooks required by the
    reducer.  ``policy`` identifies the governing workflow definition while
    keeping this runtime independent of declarative workflow models.
    """
    policy = policy or ObservationTransitionPolicy()
    if not policy.identifier.strip():
        raise ValueError("observation transition policy identifier must not be empty")
    allowed_nodes = _validate_policy(policy)
    logger.debug(
        "Applying observation under workflow policy %s (definition=%s)",
        policy.identifier,
        "supplied" if policy.definition is not None else "implicit",
    )
    adapted_event = adapted_event or runtime._event_adapter_registry().adapt(message)
    command_decision = command_decision or interpret_event(message, adapted_event, current_state)
    workflow_command = command_decision.command
    if command_decision.command is not None:
        logger.debug(
            "Interpreted %s as %s command %s",
            message.event_id,
            command_decision.command.command_type.value,
            command_decision.command.command_id,
        )
    else:
        logger.debug(
            "No workflow command derived from %s: %s",
            message.event_id,
            command_decision.reason,
        )
    if command_decision.status.value in {"duplicate", "stale", "invalid"}:
        return current_state

    event_obj = deserialize_observation_event(message, adapted_event)
    current_state = activate_pull_request_for_event(current_state, event_obj)
    targets_implementation_pr = event_targets_pull_request(current_state, event_obj)
    is_approved = False
    is_rejected = False
    is_question = False
    is_ci_webhook = False
    pr_merged = False
    feedback = None
    automated_review_revision_pending = None
    proposal_review_threads: list[dict[str, Any]] = []
    proposal_review_decisions: list[dict[str, Any]] = []
    implementation_pr_approved = False

    current_node = current_state.get("current_node", "")
    comment_ticket_key = None
    comment_ticket_type = None

    if workflow_command is not None:
        handlers = (
            getattr(runtime, "command_handlers", None) or create_default_command_handler_registry()
        )
        application = handlers.apply(workflow_command, current_state)
        if application is not None:
            feedback_request = application.feedback
            if feedback_request is not None:
                if feedback_request.kind is FeedbackKind.SKIP_GATE and event_obj is not None:
                    native_id = (
                        event_obj.change_request.identity.native_id
                        if event_obj.change_request
                        else None
                    )
                    await runtime._post_skip_gate_feedback(
                        ticket_key=message.ticket_key,
                        repo_ref=event_obj.repo_ref,
                        pr_number=int(native_id) if native_id is not None else None,
                        check_name=str(feedback_request.arguments["check_name"]),
                        sender=str(feedback_request.arguments.get("sender") or ""),
                        action=str(feedback_request.arguments["action"]),
                    )
                elif feedback_request.kind is FeedbackKind.REBASE and event_obj is not None:
                    native_id = (
                        event_obj.change_request.identity.native_id
                        if event_obj.change_request
                        else None
                    )
                    await runtime._post_rebase_feedback(
                        ticket_key=message.ticket_key,
                        repo_ref=event_obj.repo_ref,
                        pr_number=int(native_id) if native_id is not None else None,
                        sender=str(feedback_request.arguments.get("sender") or ""),
                    )
                elif feedback_request.kind is FeedbackKind.RETRY_ACKNOWLEDGEMENT:
                    await runtime._post_retry_acknowledgement(
                        message.ticket_key,
                        str(feedback_request.arguments["stage"]),
                    )
                elif feedback_request.kind is FeedbackKind.TERMINAL_ERROR:
                    await runtime._post_terminal_error_comment(
                        message.ticket_key,
                        str(feedback_request.arguments["message"]),
                    )
                elif feedback_request.kind is FeedbackKind.RESUME_ACKNOWLEDGEMENT:
                    source_ticket_key = feedback_request.arguments.get("source_ticket_key")
                    await runtime._post_resume_ack_comment(
                        message.ticket_key,
                        signal_type=str(feedback_request.arguments["signal_type"]),
                        current_node=str(feedback_request.arguments["stage"]),
                        source_ticket_key=(str(source_ticket_key) if source_ticket_key else None),
                    )
                elif feedback_request.kind is FeedbackKind.OPTION_RANGE:
                    maximum = int(feedback_request.arguments["maximum"])
                    await runtime._execute_required_comment(
                        message.ticket_key,
                        f"Please reply with >option N where N is between 1 and {maximum}.",
                        logical_action="invalid-option-range",
                        discriminator=message.event_id,
                    )
            return application.state

    # An inline reply at the review-response gate applies only to its thread.
    # Preserve unrelated contested threads and re-run review analysis so any
    # newly accepted item can proceed without globally clearing objections.
    if (
        event_obj is not None
        and event_obj.kind == EventKind.COMMENT_CREATED
        and event_obj.comment is not None
        and event_obj.comment.path is not None
        and current_node == "review_response_gate"
        and current_state.get("is_paused", True)
    ):
        reply = event_obj.comment
        sender_login = event_obj.actor.login
        if sender_login:
            forge_login = await runtime._get_forge_github_login(event_obj.repo_ref)
            settings = runtime._transition_settings()
            forge_bot_comment_prefix = settings.forge_bot_comment_prefix
            if is_self_comment(
                sender_login=sender_login,
                comment_body=reply.body,
                bot_login=forge_login,
                prefix=forge_bot_comment_prefix,
            ):
                logger.debug("Ignoring Forge's own inline review comment")
                return current_state
        in_reply_to_raw = reply.in_reply_to
        replied_to = (
            int(in_reply_to_raw)
            if in_reply_to_raw is not None and in_reply_to_raw.isdigit()
            else None
        )
        if replied_to is not None:
            contested = current_state.get("contested_comments", [])
            remaining = [
                item for item in contested if not decision_matches_comment(item, replied_to)
            ]
            return {
                **current_state,
                "is_paused": False,
                "revision_requested": True,
                "feedback_comment": reply.body,
                "contested_comments": remaining,
                "context": {
                    **current_state.get("context", {}),
                    "resume_event": message.event_type,
                    "observation_id": adapted_event.observation.observation_id,
                    "review_thread_comment_id": replied_to,
                },
            }
        own_id = int(reply.id) if reply.id and reply.id.isdigit() else None
        return {
            **current_state,
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": reply.body,
            "context": {
                **current_state.get("context", {}),
                "resume_event": message.event_type,
                "observation_id": adapted_event.observation.observation_id,
                "review_thread_comment_id": own_id,
            },
        }

    is_check_event = event_obj is not None and event_obj.kind == EventKind.CHECK_UPDATED
    if event_obj is not None and (
        current_node == "ci_evaluator" or (targets_implementation_pr and is_check_event)
    ):
        if is_check_event:
            suite_status = event_obj.check_suite_status
            if suite_status and suite_status != CheckStatus.COMPLETED:
                logger.info(
                    f"Ignoring {message.event_type} for {message.ticket_key}: "
                    f"check_suite not yet completed (status={suite_status!r})"
                )
            else:
                is_ci_webhook = True
                logger.info(f"Detected source-control CI webhook signal for {current_node}")
        elif not (
            event_obj.kind
            in (EventKind.COMMENT_CREATED, EventKind.REVIEW_SUBMITTED, EventKind.UNKNOWN)
            or (
                event_obj.change_request
                and event_obj.change_request.state == ChangeRequestState.MERGED
            )
        ):
            is_ci_webhook = True
            logger.info(f"Detected source-control CI webhook signal for {current_node}")

    # A human reply to a proposal review thread resumes only that thread's
    # feedback. Forge-authored replies are informational and must not loop.
    if (
        event_obj is not None
        and event_obj.kind == EventKind.COMMENT_CREATED
        and event_obj.comment is not None
        and event_obj.comment.path is not None
    ):
        reply = event_obj.comment
        in_reply_to_raw = reply.in_reply_to
        replied_to = (
            int(in_reply_to_raw)
            if in_reply_to_raw is not None and in_reply_to_raw.isdigit()
            else None
        )
        is_proposal_reply = (
            is_proposal_pull_request_event(message, current_state, event_obj, artifact="prd")
            and current_node in _PRD_GATE_NODES
        ) or (
            is_proposal_pull_request_event(message, current_state, event_obj, artifact="spec")
            and current_node in _SPEC_GATE_NODES
        )
        sender_login = event_obj.actor.login
        if is_proposal_reply and sender_login:
            forge_login = await runtime._get_forge_github_login(event_obj.repo_ref)
            settings = runtime._transition_settings()
            forge_bot_comment_prefix = settings.forge_bot_comment_prefix
            if is_self_comment(
                sender_login=sender_login,
                comment_body=reply.body,
                bot_login=forge_login,
                prefix=forge_bot_comment_prefix,
            ):
                return current_state
        if is_proposal_reply and replied_to:
            previous = current_state.get("proposal_review_decisions", [])
            matching = next(
                (item for item in previous if decision_matches_comment(item, replied_to)),
                None,
            )
            if matching:
                reply_body = reply.body.strip()
                reply_comment_id = int(reply.id) if reply.id.isdigit() else None
                decisions = [
                    {
                        **item,
                        "comment_id": (
                            reply_comment_id
                            if reply_comment_id is not None
                            else item.get("comment_id")
                        ),
                        "disposition": "accept",
                        "feedback": reply_body,
                        "status": "pending",
                    }
                    if item.get("thread_id") == matching.get("thread_id")
                    else item
                    for item in previous
                ]
                return {
                    **current_state,
                    "is_paused": False,
                    "revision_requested": True,
                    "feedback_comment": reply_body,
                    "proposal_review_decisions": decisions,
                    "automated_review_revision_count": 0,
                    "automated_review_revision_pending": False,
                }
            logger.debug(
                "Proposal reply target %s did not match a stored review decision",
                replied_to,
            )
        elif is_proposal_reply:
            body = reply.body.strip()
            if body and reply.id.isdigit():
                comment_id = int(reply.id)
                proposal_review_threads = [
                    {
                        "thread_id": f"comment-{comment_id}",
                        "path": reply.path or "",
                        "line": reply.line,
                        "comments": [
                            {
                                "comment_id": comment_id,
                                "body": body,
                                "author": sender_login,
                                "commit_sha": event_obj.raw.get("comment", {}).get("commit_id", ""),
                            }
                        ],
                    }
                ]
                is_rejected = True
                feedback = body
            else:
                logger.warning(
                    "Dropping proposal reply with empty body or non-numeric "
                    f"comment id (id={reply.id!r}) for {message.ticket_key}"
                )

    # GitHub events targeting the PRD proposals PR — handled at prd_approval_gate.
    # Merge = approval. Review with feedback = revision. Comment = feedback/question.
    if (
        is_proposal_pull_request_event(message, current_state, event_obj, artifact="prd")
        and current_node in _PRD_GATE_NODES
    ):
        if (
            event_obj is not None
            and event_obj.kind == EventKind.REVIEW_SUBMITTED
            and event_obj.review is not None
        ):
            pr_review = event_obj.review

            # Merge-only approval: review approval is intentionally ignored
            if pr_review.state in (ReviewState.CHANGES_REQUESTED, ReviewState.COMMENTED):
                repo_full = event_obj.repo_ref.namespace
                native_id = (
                    event_obj.change_request.identity.native_id
                    if event_obj.change_request
                    else None
                )
                pr_number = int(native_id) if native_id is not None else None
                spec_inline_comments: list[dict[str, Any]] = []
                if repo_full and pr_number:
                    _reviews = await runtime._review_enrichment().review_threads(
                        repo_full, pr_number
                    )
                    proposal_review_threads = _reviews_to_raw_threads(_reviews)
                    spec_inline_comments = _flatten_review_threads(_reviews)

                parts = []
                if pr_review.body.strip():
                    parts.append(pr_review.body.strip())
                if spec_inline_comments:
                    inline_text = "\n\n".join(
                        f"**{c['path']}** (line {c.get('line') or '?'}):\n{c['body']}"
                        for c in spec_inline_comments
                    )
                    parts.append(f"Inline comments:\n{inline_text}")

                if parts:
                    feedback = "\n\n".join(parts)
                    is_rejected = True
                    logger.info(
                        f"PRD PR review ({pr_review.state.value}) for {message.ticket_key}: "
                        f"body={'yes' if pr_review.body.strip() else 'no'}, "
                        f"inline={len(spec_inline_comments)}"
                    )
                else:
                    logger.info(
                        f"PRD PR review ({pr_review.state.value}) for {message.ticket_key} "
                        "with no content — ignoring"
                    )
                    return current_state

        elif (
            event_obj is not None
            and event_obj.change_request is not None
            and event_obj.change_request.state == ChangeRequestState.MERGED
        ):
            is_approved = True
            pr_merged = True
            logger.info(f"PRD PR merged for {message.ticket_key}")
            await runtime._execute_required_jira_effect(
                ticket_key=message.ticket_key,
                state=current_state,
                event_id=message.event_id,
                operation=JIRA_LABEL_OPERATION,
                payload={"label": ForgeLabel.PRD_APPROVED.value},
                logical_action="approve-prd",
            )
            prd_content = current_state.get("prd_content", "")
            if prd_content:
                await runtime._execute_required_jira_effect(
                    ticket_key=message.ticket_key,
                    state=current_state,
                    event_id=message.event_id,
                    operation=JIRA_DESCRIPTION_OPERATION,
                    payload={"description": prd_content},
                    logical_action="publish-approved-prd",
                )
                logger.info(f"Copied approved PRD to Jira description for {message.ticket_key}")

        elif (
            event_obj is not None
            and event_obj.kind == EventKind.COMMENT_CREATED
            and event_obj.comment is not None
            and event_obj.comment.path is None
        ):
            comment_body = (event_obj.comment.body or "").strip()
            sender_login = event_obj.actor.login

            if comment_body and sender_login:
                # Skip self-comments
                forge_login = await runtime._get_forge_github_login(event_obj.repo_ref)

                settings = runtime._transition_settings()
                forge_bot_comment_prefix = settings.forge_bot_comment_prefix
                if is_self_comment(
                    sender_login=sender_login,
                    comment_body=comment_body,
                    bot_login=forge_login,
                    prefix=forge_bot_comment_prefix,
                ):
                    logger.debug(f"Ignoring self-comment on PRD PR for {message.ticket_key}")
                    return current_state

                comment_type = classify_comment(comment_body)
                if comment_type == CommentType.QUESTION:
                    is_question = True
                    feedback = comment_body
                    logger.info(
                        f"PRD PR question for {message.ticket_key}: {comment_body[:100]}..."
                    )
                elif comment_type == CommentType.FEEDBACK:
                    is_rejected = True
                    feedback = re.sub(r"^\s*!\s*", "", comment_body)
                    logger.info(f"PRD PR feedback for {message.ticket_key}: {feedback[:100]}...")
                else:
                    logger.info(
                        f"Informational comment on PRD PR for {message.ticket_key}, "
                        f"ignoring: {comment_body[:100]}..."
                    )

    # GitHub events targeting the spec proposals PR — same pattern as PRD PR.
    if (
        is_proposal_pull_request_event(message, current_state, event_obj, artifact="spec")
        and current_node in _SPEC_GATE_NODES
    ):
        if (
            event_obj is not None
            and event_obj.kind == EventKind.REVIEW_SUBMITTED
            and event_obj.review is not None
        ):
            pr_review = event_obj.review

            if pr_review.state in (ReviewState.CHANGES_REQUESTED, ReviewState.COMMENTED):
                repo_full = event_obj.repo_ref.namespace
                native_id = (
                    event_obj.change_request.identity.native_id
                    if event_obj.change_request
                    else None
                )
                pr_number = int(native_id) if native_id is not None else None
                inline_comments: list[dict[str, Any]] = []
                if repo_full and pr_number:
                    _reviews = await runtime._review_enrichment().review_threads(
                        repo_full, pr_number
                    )
                    proposal_review_threads = _reviews_to_raw_threads(_reviews)
                    inline_comments = _flatten_review_threads(_reviews)

                parts = []
                if pr_review.body.strip():
                    parts.append(pr_review.body.strip())
                if inline_comments:
                    inline_text = "\n\n".join(
                        f"**{c['path']}** (line {c.get('line') or '?'}):\n{c['body']}"
                        for c in inline_comments
                    )
                    parts.append(f"Inline comments:\n{inline_text}")

                if parts:
                    feedback = "\n\n".join(parts)
                    is_rejected = True
                    logger.info(
                        f"Spec PR review ({pr_review.state.value}) for {message.ticket_key}: "
                        f"body={'yes' if pr_review.body.strip() else 'no'}, "
                        f"inline={len(inline_comments)}"
                    )
                else:
                    logger.info(
                        f"Spec PR review ({pr_review.state.value}) for {message.ticket_key} "
                        "with no content — ignoring"
                    )
                    return current_state

        elif (
            event_obj is not None
            and event_obj.change_request is not None
            and event_obj.change_request.state == ChangeRequestState.MERGED
        ):
            is_approved = True
            pr_merged = True
            logger.info(f"Spec PR merged for {message.ticket_key}")
            await runtime._execute_required_jira_effect(
                ticket_key=message.ticket_key,
                state=current_state,
                event_id=message.event_id,
                operation=JIRA_LABEL_OPERATION,
                payload={"label": ForgeLabel.SPEC_APPROVED.value},
                logical_action="approve-spec",
            )
            spec_content = current_state.get("spec_content", "")
            if spec_content:
                settings = runtime._transition_settings()
                if settings.jira_store_in_comments:
                    operation = JIRA_STRUCTURED_COMMENT_OPERATION
                    effect_payload: dict[str, JsonValue] = {
                        "title": "Technical Specification (Approved)",
                        "content": spec_content,
                        "comment_type": "spec",
                    }
                elif settings.jira_spec_custom_field:
                    operation = JIRA_CUSTOM_FIELD_OPERATION
                    effect_payload = {
                        "field": settings.jira_spec_custom_field,
                        "value": spec_content,
                    }
                else:
                    operation = JIRA_ATTACHMENT_REPLACE_OPERATION
                    effect_payload = {
                        "filename": f"{message.ticket_key}-spec.md",
                        "content": spec_content,
                        "content_type": "text/markdown",
                    }
                await runtime._execute_required_jira_effect(
                    ticket_key=message.ticket_key,
                    state=current_state,
                    event_id=message.event_id,
                    operation=operation,
                    payload=effect_payload,
                    logical_action="publish-approved-spec",
                )
                logger.info(
                    f"Copied approved spec to configured Jira storage for {message.ticket_key}"
                )

        elif (
            event_obj is not None
            and event_obj.kind == EventKind.COMMENT_CREATED
            and event_obj.comment is not None
            and event_obj.comment.path is None
        ):
            comment_body = (event_obj.comment.body or "").strip()
            sender_login = event_obj.actor.login

            if comment_body and sender_login:
                forge_login = await runtime._get_forge_github_login(event_obj.repo_ref)

                settings = runtime._transition_settings()
                forge_bot_comment_prefix = settings.forge_bot_comment_prefix
                if is_self_comment(
                    sender_login=sender_login,
                    comment_body=comment_body,
                    bot_login=forge_login,
                    prefix=forge_bot_comment_prefix,
                ):
                    logger.debug(f"Ignoring self-comment on spec PR for {message.ticket_key}")
                    return current_state

                comment_type = classify_comment(comment_body)
                if comment_type == CommentType.QUESTION:
                    is_question = True
                    feedback = comment_body
                    logger.info(
                        f"Spec PR question for {message.ticket_key}: {comment_body[:100]}..."
                    )
                elif comment_type == CommentType.FEEDBACK:
                    is_rejected = True
                    feedback = re.sub(r"^\s*!\s*", "", comment_body)
                    logger.info(f"Spec PR feedback for {message.ticket_key}: {feedback[:100]}...")
                else:
                    logger.info(
                        f"Informational comment on spec PR for {message.ticket_key}, "
                        f"ignoring: {comment_body[:100]}..."
                    )

    # Automated proposal reviewers often publish detailed suggestions even when
    # their overall verdict is satisfied. Semantically triage the complete review
    # before treating it as a revision request. Only a satisfied verdict stops;
    # ambiguous results retain the original feedback and revise within the cap.
    is_prd_review = (
        is_proposal_pull_request_event(message, current_state, event_obj, artifact="prd")
        and current_node in _PRD_GATE_NODES
    )
    is_spec_review = (
        is_proposal_pull_request_event(message, current_state, event_obj, artifact="spec")
        and current_node in _SPEC_GATE_NODES
    )
    if (
        is_rejected
        and proposal_review_threads
        and (is_prd_review or is_spec_review)
        and event_obj is not None
        and event_obj.actor.is_bot
    ):
        previous_decisions = {
            item.get("thread_id"): item
            for item in current_state.get("proposal_review_decisions", [])
            if item.get("thread_id")
        }
        proposal_review_threads = [
            thread
            for thread in proposal_review_threads
            if previous_decisions.get(thread["thread_id"], {}).get("comment_id")
            != thread["comments"][-1].get("comment_id")
        ]
        if proposal_review_threads:
            artifact_type = "PRD" if is_prd_review else "specification"
            artifact_content = current_state.get(
                "prd_content" if is_prd_review else "spec_content", ""
            )
            proposal_review_decisions = await runtime._review_enrichment().triage_threads(
                artifact_type=artifact_type,
                artifact_content=artifact_content,
                threads=proposal_review_threads,
                ticket_key=message.ticket_key,
            )
            repo_full = event_obj.repo_ref.namespace if event_obj is not None else ""
            native_id = (
                event_obj.change_request.identity.native_id
                if event_obj is not None and event_obj.change_request
                else None
            )
            pr_number = int(native_id) if native_id is not None else None
            if repo_full and pr_number:
                await runtime._review_enrichment().reply_to_decisions(
                    repo_full_name=repo_full,
                    pr_number=pr_number,
                    decisions=proposal_review_decisions,
                )
            actionable_feedback = [
                decision.get("feedback")
                or next(
                    (
                        thread["comments"][-1].get("body", "")
                        for thread in proposal_review_threads
                        if thread["thread_id"] == decision["thread_id"]
                    ),
                    "",
                )
                for decision in proposal_review_decisions
                if decision["disposition"] in ("accept", "uncertain")
            ]
            feedback = "\n\n".join(item for item in actionable_feedback if item)
            if not feedback:
                return {
                    **current_state,
                    "proposal_review_decisions": merge_review_decisions(
                        current_state.get("proposal_review_decisions", []),
                        proposal_review_decisions,
                    ),
                }

    if (
        is_rejected
        and feedback
        and (is_prd_review or is_spec_review)
        and event_obj is not None
        and event_obj.actor.is_bot
        and not proposal_review_decisions
    ):
        review_state = event_obj.review.state.value if event_obj.review else "comment"
        review_author = event_obj.actor.login or "unknown bot"
        artifact_type = "PRD" if is_prd_review else "specification"
        artifact_content = current_state.get("prd_content" if is_prd_review else "spec_content", "")
        decision = await runtime._review_enrichment().triage_automated(
            artifact_type=artifact_type,
            artifact_content=artifact_content,
            review_state=review_state,
            review_author=review_author,
            review_content=feedback,
            ticket_key=message.ticket_key,
        )
        logger.info(
            "Automated %s review triage for %s: %s (%s)",
            artifact_type,
            message.ticket_key,
            decision.verdict,
            decision.reason,
        )
        if decision.verdict == "satisfied":
            return current_state

        previous_count = current_state.get("automated_review_revision_count", 0)
        if previous_count >= _MAX_AUTOMATED_REVIEW_REVISIONS:
            logger.warning(
                "Automated review revision cap (%d) reached for %s; awaiting human review",
                _MAX_AUTOMATED_REVIEW_REVISIONS,
                message.ticket_key,
            )
            return current_state
        automated_review_revision_pending = True
        if decision.verdict == "blocking":
            feedback = decision.blocking_feedback

    # GitHub pull_request_review events — handled when paused at human_review_gate or review_response_gate.
    # A review submission is the primary signal for the human review stage.
    if (
        event_obj is not None
        and event_obj.kind == EventKind.REVIEW_SUBMITTED
        and event_obj.review is not None
        and (current_node in _REVIEW_GATES or targets_implementation_pr)
        and (current_state.get("is_paused", True) or current_state.get("pending_ci_event"))
    ):
        review = event_obj.review
        sender_login = review.author
        if sender_login:
            forge_login = await runtime._get_forge_github_login(event_obj.repo_ref)
            settings = runtime._transition_settings()
            forge_bot_comment_prefix = settings.forge_bot_comment_prefix
            if is_self_comment(
                sender_login=sender_login,
                comment_body=review.body,
                bot_login=forge_login,
                prefix=forge_bot_comment_prefix,
            ):
                logger.debug("Ignoring Forge's own pull request review")
                return current_state

        if review.state == ReviewState.APPROVED:
            if targets_implementation_pr:
                implementation_pr_approved = True
            is_approved = True
            logger.info(f"Detected PR review approval for {message.ticket_key}")
        elif review.state in (ReviewState.CHANGES_REQUESTED, ReviewState.COMMENTED):
            # Always fetch inline comments so the agent gets the full picture,
            # regardless of whether a summary body is also present.
            repo_full = event_obj.repo_ref.namespace
            pr_number = (
                event_obj.change_request.identity.native_id if event_obj.change_request else None
            )
            inline_comments = []
            if repo_full and pr_number:
                review_id = int(review.id) if review.id else None
                review_comments = await runtime._review_enrichment().review_comments(
                    repo_full, int(pr_number), review_id
                )
                inline_comments = [
                    {"path": c.path, "line": c.line, "body": c.body} for c in review_comments
                ]

            parts = []
            if review.body.strip():
                parts.append(review.body.strip())
            if inline_comments:
                inline_text = "\n\n".join(
                    f"**{c['path']}** (line {c.get('line') or '?'}):\n{c['body']}"
                    for c in inline_comments
                )
                parts.append(f"Inline comments:\n{inline_text}")

            if parts:
                feedback = "\n\n".join(parts)
                is_rejected = True
                logger.info(
                    f"Detected PR review ({review.state.value}) for {message.ticket_key}: "
                    f"body={'yes' if review.body.strip() else 'no'}, "
                    f"inline comments={len(inline_comments)}"
                )
            else:
                logger.info(
                    f"Detected PR review ({review.state.value}) for {message.ticket_key} "
                    f"with no body and no inline comments — ignoring"
                )
                return current_state

    # GitHub pull_request:closed + merged — PR was actually merged
    if (
        event_obj is not None
        and event_obj.change_request is not None
        and event_obj.change_request.state == ChangeRequestState.MERGED
        and targets_implementation_pr
    ):
        is_approved = True
        pr_merged = True
        logger.info(f"Detected PR merge for {message.ticket_key}")

    # Build updated state — do NOT set is_paused=False here.
    # Each branch below sets it explicitly when a valid signal is detected.
    # Unrecognized events (wrong-stage approval, unrelated label changes, etc.)
    # must not unpause the workflow — they return current_state unchanged.
    updated_state = {
        **current_state,
        "context": {
            **current_state.get("context", {}),
            "resume_event": message.event_type,
            "observation_id": adapted_event.observation.observation_id,
        },
    }
    if targets_implementation_pr and is_ci_webhook and current_node != "human_review_gate":
        updated_state["current_node"] = "ci_evaluator"
    elif targets_implementation_pr and (
        (event_obj is not None and event_obj.kind == EventKind.REVIEW_SUBMITTED) or pr_merged
    ):
        updated_state["current_node"] = "human_review_gate"

    was_errored = _is_workflow_errored(current_state)

    # Check if workflow is at a terminal state (complete)
    terminal_states = ("complete",)
    is_terminal = current_node in terminal_states

    if is_ci_webhook:
        # GitHub CI event — unpause the gate and let ci_evaluator check the results
        updated_state["is_paused"] = False

        if current_node == "human_review_gate":
            # Keep current_node as human_review_gate so review webhooks arriving
            # during the CI cycle are still accepted from the queue.
            updated_state["pending_ci_event"] = True

    elif is_approved:
        updated_state["is_paused"] = implementation_pr_approved
        updated_state["revision_requested"] = False
        updated_state["feedback_comment"] = None
        updated_state["last_error"] = None
        if implementation_pr_approved:
            updated_state["human_review_status"] = "approved"
        if pr_merged:
            updated_state["pr_merged"] = True
            if event_targets_pull_request(updated_state, event_obj):
                updated_state = mark_active_pull_request_merged(updated_state)
                updated_state["pr_merged"] = all_pull_requests_merged(updated_state)
                if not updated_state["pr_merged"]:
                    updated_state["is_paused"] = True
            if is_prd_review:
                # Specification review is a separate artifact cycle and must
                # receive its own automated revision budget.
                updated_state["automated_review_revision_count"] = 0
                updated_state["automated_review_revision_pending"] = False
                updated_state["proposal_review_decisions"] = []
    elif is_question:
        # Unpause so answer_question node runs, it will re-pause after answering
        updated_state["is_paused"] = False
        updated_state["is_question"] = True
        updated_state["feedback_comment"] = feedback
        updated_state["revision_requested"] = False
        await runtime._post_resume_ack_comment(
            message.ticket_key,
            signal_type="question",
            current_node=current_node,
            source_ticket_key=comment_ticket_key,
            event_id=message.event_id,
        )
    elif is_rejected and feedback:
        updated_state["is_paused"] = False
        updated_state["revision_requested"] = True
        updated_state["feedback_comment"] = feedback
        if proposal_review_decisions:
            updated_state["proposal_review_decisions"] = merge_review_decisions(
                current_state.get("proposal_review_decisions", []),
                proposal_review_decisions,
            )
        if automated_review_revision_pending is not None:
            updated_state["automated_review_revision_pending"] = True
        elif is_prd_review or is_spec_review:
            # A human-requested proposal revision starts a fresh automated
            # review cycle after that revision is published.
            updated_state["automated_review_revision_count"] = 0
            updated_state["automated_review_revision_pending"] = False
        if current_node == "review_response_gate":
            updated_state["contested_comments"] = []
        if comment_ticket_key and comment_ticket_type == "epic":
            updated_state["current_epic_key"] = comment_ticket_key
            updated_state["current_task_key"] = None
        elif comment_ticket_key and comment_ticket_type == "task":
            updated_state["current_task_key"] = comment_ticket_key
            updated_state["current_epic_key"] = None
        else:
            updated_state["current_task_key"] = None
            updated_state["current_epic_key"] = None
        await runtime._post_resume_ack_comment(
            message.ticket_key,
            signal_type="revision",
            current_node=current_node,
            source_ticket_key=comment_ticket_key,
            event_id=message.event_id,
        )
    elif was_errored:
        # Workflow has an error — auto-resume up to MAX_AUTO_RETRIES times,
        # then require an explicit forge:retry label.
        # Terminal states always require explicit retry regardless of count.
        MAX_AUTO_RETRIES = 3
        retry_count = current_state.get("retry_count", 0)
        cap_reached = retry_count >= MAX_AUTO_RETRIES

        if is_terminal or cap_reached:
            last_error = current_state.get("last_error", "Unknown error")
            reason = "terminal state" if is_terminal else f"retry cap ({MAX_AUTO_RETRIES}) reached"
            if cap_reached and current_state.get("auto_retry_cap_notified"):
                logger.info(
                    f"Workflow for {message.ticket_key} is already blocked after "
                    f"auto-retry cap at '{current_node}'"
                )
                return current_state

            logger.warning(
                f"Workflow for {message.ticket_key} at '{current_node}' requires "
                f"forge:retry ({reason})"
            )
            await runtime._post_terminal_error_comment(message.ticket_key, last_error)
            if cap_reached:
                updated_state["is_paused"] = True
                updated_state["is_blocked"] = True
                updated_state["auto_retry_cap_notified"] = True
                return updated_state
            return current_state
        else:
            # Transient failure — auto-resume and let the node retry
            prev_error = current_state.get("last_error", "")
            safe_prev_error = redact_secrets(prev_error) if prev_error else None
            logger.info(
                f"Auto-resuming {message.ticket_key} after error at '{current_node}' "
                f"(attempt {retry_count + 1}/{MAX_AUTO_RETRIES}): "
                f"{safe_prev_error[:100] if safe_prev_error else 'unknown'}"
            )
            updated_state["is_paused"] = False
            updated_state["last_error"] = None
    else:
        # Nodes that wait for specific external events should not auto-proceed.
        _signal_required_nodes = (
            "ci_evaluator",
            "attempt_ci_fix",
            "human_review_gate",
            "review_response_gate",
        )
        if not current_state.get("is_paused", True) and current_node not in _signal_required_nodes:
            # Workflow is unpaused at an execution node — let it run.
            # Covers checkpoint patches and nodes that don't need a signal.
            logger.info(
                f"Workflow for {message.ticket_key} is unpaused at {current_node} "
                f"— proceeding without explicit signal"
            )
            updated_state["is_paused"] = False
        else:
            # Paused gate with no recognized signal — do not unpause.
            # Covers wrong-stage approvals, unrelated label changes, etc.
            logger.info(
                f"No valid signal detected for {message.ticket_key} "
                f"at {current_node} — ignoring event, workflow state unchanged"
            )
            return current_state

    result = save_active_pull_request(updated_state)
    _validate_target(result, allowed_nodes)
    return result

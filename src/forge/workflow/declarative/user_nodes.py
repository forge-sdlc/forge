"""Safe, versioned templates for project-authored workflow nodes."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import create_model

from forge.workflow.base import merge_node_results
from forge.workflow.declarative.models import NodeTemplate


def select_inputs(template: NodeTemplate, state: dict[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    fields = {
        "artifact.prd": "prd_content",
        "artifact.spec": "spec_content",
        "artifact.rca": "rca_content",
        "artifact.plan": "plan_content",
    }
    for selector in template.inputs:
        value = state.get("ticket_key") if selector == "ticket.key" else state.get(fields[selector])
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"required input '{selector}' is unavailable")
        selected[selector] = value[:16000]
    return selected


async def execute_node(template: NodeTemplate, state: dict[str, Any], step: str) -> dict[str, Any]:
    from forge.workflow.declarative.predicates import evaluate_predicate

    if template.type == "decision-v1":
        outcome = next(
            (rule.outcome for rule in template.rules if evaluate_predicate(rule.when, state)),
            template.otherwise,
        )
        summary = None
    else:
        from forge.config import get_settings
        from forge.integrations.agents.agent import ForgeAgent
        from forge.model_policy import resolve_model_target_for_project

        inputs = select_inputs(template, state)
        project = str(
            state.get("workflow_project_key") or state.get("ticket_key", "").split("-", 1)[0]
        ).upper()
        settings = get_settings()
        target = await resolve_model_target_for_project(settings, project, "user_node_assessment")
        model = ForgeAgent(settings)._create_model(model_target=target)
        Response = create_model(
            "NodeAssessment", outcome=(Literal[tuple(template.outcomes)], ...), summary=(str, ...)
        )
        prompt = json.dumps(
            {
                "instruction": template.instruction,
                "inputs": inputs,
                "allowed_outcomes": template.outcomes,
            }
        )
        response = await model.with_structured_output(Response).ainvoke(prompt)
        parsed = Response.model_validate(response)
        outcome, summary = parsed.outcome, parsed.summary
        if len(summary) > 1000:
            raise ValueError("assessment summary exceeds 1000 characters")
    branch = state.get("parallel_branch_id")
    attempts = state.get("workflow_node_attempts") or {}
    key = f"{step}:{branch if branch is not None else 'main'}"
    record = {
        "step": step,
        "branch": branch,
        "attempt": int(attempts.get(step, 0)) + 1,
        "outcome": outcome,
        "summary": summary,
    }
    # The compiler evaluates outgoing cases before LangGraph applies reducers.
    # Supply the complete outcome view here as well as in the checkpoint.
    return {
        **state,
        "current_node": step,
        "node_results": merge_node_results(state.get("node_results"), {key: record}),
    }

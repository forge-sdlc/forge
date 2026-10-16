"""Evaluation fixtures must provide actual stage inputs, not unresolved templates."""

import pytest

from forge.prompts import load_prompt
from forge.prompts.evaluation import build_evaluation_prompt


def test_rendering_does_not_substitute_inside_untrusted_input():
    rendered = load_prompt(
        "generate-prd", raw_requirements="Keep {context} literal", context="other"
    )
    assert "Keep {context} literal" in rendered
    assert "Additional context:\nother" in rendered


def test_strict_render_rejects_missing_input_but_preserves_literal_json():
    with pytest.raises(ValueError, match="prd_content"):
        load_prompt("generate-spec", strict=True, context="repo")
    rendered = load_prompt(
        "ci-attribution",
        strict=True,
        failures_file_path="failures.json",
        base_branch="release/2026",
    )
    assert '"attributable": true' in rendered


@pytest.mark.parametrize("skill", ["generate-prd", "generate-spec", "decompose-epics"])
def test_primary_input_reaches_each_production_template(skill):
    rendered = build_evaluation_prompt(
        skill, "SPECIFIC FEATURE INPUT", "TEST", "Small feature", available_repos=["owner/repo"]
    )
    assert "SPECIFIC FEATURE INPUT" in rendered
    assert "owner/repo" in rendered
    assert "{prd_content}" not in rendered
    assert "{spec_content}" not in rendered


def test_task_generation_requires_spec_and_accepts_complete_fixture():
    with pytest.raises(ValueError, match="spec_content"):
        build_evaluation_prompt(
            "generate-tasks", "Epic plan", "TEST", "Epic", available_repos=["owner/repo"]
        )
    rendered = build_evaluation_prompt(
        "generate-tasks",
        "Epic plan",
        "TEST",
        "Epic",
        available_repos=["owner/repo"],
        prompt_inputs={"spec_content": "Approved spec"},
    )
    assert "Approved spec" in rendered and "Epic plan" in rendered


def test_repository_selection_fixture_cannot_silently_omit_repositories():
    with pytest.raises(ValueError, match="available_repos"):
        build_evaluation_prompt("generate-prd", "Input", "TEST", "Feature")

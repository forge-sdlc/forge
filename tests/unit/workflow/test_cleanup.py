"""Cleanup and integration tests — verifies no legacy artifacts remain."""

import subprocess
from pathlib import Path

# Anchor all path lookups to the repo root (two levels up from this test file)
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


class TestFixBugRetired:
    """fix-bug.md has been removed and no code references it."""

    def test_fix_bug_prompt_file_does_not_exist(self):
        """fix-bug.md must not exist in src/forge/prompts/v1/."""
        path = _REPO_ROOT / "src/forge/prompts/v1/fix-bug.md"
        assert not path.exists(), "fix-bug.md must be deleted as part of cleanup"

    def test_no_source_references_to_fix_bug(self):
        """No Python source file references the 'fix-bug' prompt string."""
        result = subprocess.run(
            ["grep", "-r", "--include=*.py", "fix-bug", str(_REPO_ROOT / "src")],
            capture_output=True,
            text=True,
        )
        # Exit code 1 = no matches (desired); 0 = matches found (fail); 2 = error
        assert result.returncode == 1, (
            f"'fix-bug' still referenced in Python source files (exit={result.returncode}):\n"
            f"{result.stdout or result.stderr}"
        )


class TestRequiredPromptsExist:
    """All 8 prompt files for the bug workflow redesign exist."""

    def _prompt_path(self, name: str) -> Path:
        return _REPO_ROOT / f"src/forge/prompts/v1/{name}.md"

    def test_triage_bug_prompt_exists(self):
        assert self._prompt_path("triage-bug").exists()

    def test_analyze_bug_prompt_exists(self):
        assert self._prompt_path("analyze-bug").exists()

    def test_reflect_rca_prompt_exists(self):
        assert self._prompt_path("reflect-rca").exists()

    def test_plan_bug_fix_prompt_exists(self):
        assert self._prompt_path("plan-bug-fix").exists()

    def test_regenerate_plan_prompt_exists(self):
        assert self._prompt_path("regenerate-plan").exists()

    def test_local_review_bug_prompt_exists(self):
        assert self._prompt_path("local-review-bug").exists()

    def test_post_merge_summary_prompt_exists(self):
        assert self._prompt_path("post-merge-summary").exists()


class TestRouteEntryCompleteness:
    """route_entry maps all current_node values that nodes in the graph can set."""

    def _route(self, node: str):

        from forge.workflow.bug.graph import route_entry
        return route_entry({"current_node": node})

    def test_all_new_pipeline_nodes_mapped(self):
        """Every node from the new pipeline is in route_entry."""
        new_nodes = {
            "triage_check": "triage_check",
            "triage_gate": "triage_gate",
            "analyze_bug": "analyze_bug",
            "reflect_rca": "reflect_rca",
            "regenerate_rca": "analyze_bug",  # loops back
            "rca_option_gate": "rca_option_gate",
            "plan_bug_fix": "plan_bug_fix",
            "plan_approval_gate": "plan_approval_gate",
            "regenerate_plan": "regenerate_plan",
            "decompose_plan": "decompose_plan",
            "post_merge_summary": "post_merge_summary",
        }
        for node, expected in new_nodes.items():
            result = self._route(node)
            assert result == expected, (
                f"route_entry('{node}') = '{result}', expected '{expected}'"
            )

    def test_backward_compat_rca_approval_gate(self):
        """Old rca_approval_gate checkpoint maps to rca_option_gate."""
        assert self._route("rca_approval_gate") == "rca_option_gate"

    def test_existing_nodes_still_mapped(self):
        """All pre-redesign node mappings are preserved."""
        from langgraph.graph import END
        preserved = {
            "setup_workspace": "setup_workspace",
            "implement_bug_fix": "implement_bug_fix",
            "local_review": "local_review",
            "create_pr": "create_pr",
            "teardown_workspace": "teardown_workspace",
            "ci_evaluator": "ci_evaluator",
            "attempt_ci_fix": "ci_evaluator",
            "wait_for_ci_gate": "ci_evaluator",
            "ai_review": "human_review_gate",
            "human_review_gate": "human_review_gate",
            "implement_review": "implement_review",
            "review_response_gate": "review_response_gate",
            "escalate_blocked": "escalate_blocked",
            "complete": END,
        }
        for node, expected in preserved.items():
            result = self._route(node)
            assert result == expected, (
                f"route_entry('{node}') = '{result}', expected '{expected}'"
            )

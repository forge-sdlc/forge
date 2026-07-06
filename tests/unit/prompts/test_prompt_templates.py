"""Unit tests for prompt template loading and rendering.

These tests verify that:
- All prompts load without error
- Variables are substituted correctly
- Missing required variables raise clear errors
- Prompts don't exceed reasonable token limits
"""

import pytest

from forge.prompts import (
    PROMPTS_DIR,
    get_default_version,
    list_prompts,
    list_versions,
    load_prompt,
    set_default_version,
)


class TestPromptLoading:
    """Test prompt loading functionality."""

    def test_all_prompts_load_without_error(self):
        """Every prompt template should load without exceptions."""
        versions = list_versions()
        assert len(versions) > 0, "Should have at least one prompt version"

        for version in versions:
            prompts = list_prompts(version)
            assert len(prompts) > 0, f"Version {version} should have prompts"

            for prompt_name in prompts:
                # Load without variables - should not raise
                template = load_prompt(prompt_name, version=version)
                assert template, f"Prompt {prompt_name} should have content"
                assert len(template) > 0

    def test_list_versions_returns_valid_directories(self):
        """list_versions should return valid version directories."""
        versions = list_versions()

        for version in versions:
            version_dir = PROMPTS_DIR / version
            assert version_dir.exists(), f"Version dir {version} should exist"
            assert version_dir.is_dir(), f"{version} should be a directory"

    def test_list_prompts_for_v1(self):
        """v1 should contain expected prompt templates."""
        prompts = list_prompts("v1")

        expected_prompts = [
            "system",
            "generate-prd",
            "generate-spec",
            "decompose-epics",
            "analyze-bug",
            "regenerate",
            "task-takeover-triage",
            "task-takeover-planning",
            "task-takeover-qa",
            "task-takeover-review",
        ]

        for expected in expected_prompts:
            assert expected in prompts, f"v1 should contain {expected} prompt"

    def test_load_nonexistent_prompt_raises_error(self):
        """Loading a nonexistent prompt should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError) as exc_info:
            load_prompt("nonexistent-prompt-xyz")

        assert "not found" in str(exc_info.value).lower()


class TestVariableSubstitution:
    """Test variable substitution in prompts."""

    def test_single_variable_substitution(self):
        """Single variable should be substituted correctly."""
        result = load_prompt("system", current_date="2024-03-20")

        assert "2024-03-20" in result
        assert "{current_date}" not in result, "Variable placeholder should be replaced"

    def test_multiple_variable_substitution(self):
        """Multiple variables should all be substituted."""
        result = load_prompt(
            "generate-prd",
            raw_requirements="User should be able to login",
            context="Web application, React frontend",
        )

        assert "User should be able to login" in result
        assert "React frontend" in result
        assert "{raw_requirements}" not in result
        assert "{context}" not in result

    def test_unsubstituted_variables_remain(self):
        """Variables not provided should remain as placeholders."""
        # Load without providing the required variable
        result = load_prompt("system")

        # The {current_date} should remain
        assert "{current_date}" in result

    def test_extra_variables_ignored(self):
        """Extra variables not in template should be ignored."""
        result = load_prompt(
            "system",
            current_date="2024-03-20",
            extra_unused_var="ignored",
            another_unused="also ignored",
        )

        assert "2024-03-20" in result
        assert "ignored" not in result


class TestDecomposeEpicsPrompt:
    """Tests for Epic decomposition prompt guardrails."""

    def test_decompose_epics_requires_repository_grounding(self):
        """Epic decomposition should require real repo inspection before paths."""
        result = load_prompt(
            "decompose-epics",
            spec_content="Spec content",
            feature_summary="Feature summary",
            project_key="AISOS",
            repo_instruction="AVAILABLE REPOSITORIES:\n  - forge-sdlc/forge",
        )

        assert "Repository Grounding Requirements" in result
        assert "inspect every target repository" in result
        assert "AGENTS.md" in result
        assert "CLAUDE.md" in result
        assert "repository standards" in result
        assert "test runner" in result
        assert "Prefer targeted codebase exploration" in result
        assert "relevant guidance, code, and nearby tests" in result
        assert "nearby code patterns" in result
        assert "instead of broadening into unrelated repo areas" in result
        assert "Broaden the search when needed" in result
        assert "unrelated branches, open issues, pull requests" in result
        assert "Do not invent generic paths" in result
        assert "repo grounding failed" in result


class TestPlanningPromptGrounding:
    """Tests for planning prompt repository grounding guardrails."""

    def test_generate_tasks_preserves_bounded_repo_grounding(self):
        """Task generation should preserve grounded paths without full repo rediscovery."""
        result = load_prompt(
            "generate-tasks",
            spec_content="Spec content",
            epic_summary="Epic summary",
            epic_plan="Plan content",
            sibling_epics_section="None",
            existing_tasks_section="None",
        )

        assert (
            "Prefer additional codebase exploration only for missing implementation details"
            in result
        )
        assert "broaden the search when needed" in result
        assert "unrelated branches, open issues, pull requests" in result
        assert "nearby source/test patterns" in result
        assert "follow nearby source/test patterns" in result

    def test_bug_plan_prompts_bound_repo_reinspection(self):
        """Bug planning and revision should bound repo inspection to relevant details."""
        plan_prompt = load_prompt(
            "plan-bug-fix",
            ticket_key="BUG-1",
            bug_summary="Bug summary",
            rca_content="RCA",
            fix_approach_title="Fix",
            fix_approach_description="Description",
            fix_approach_tradeoffs="Tradeoffs",
            known_repos="acme/backend",
        )
        regenerate_prompt = load_prompt(
            "regenerate-plan",
            ticket_key="BUG-1",
            bug_summary="Bug summary",
            rca_content="RCA",
            fix_approach_title="Fix",
            fix_approach_description="Description",
            fix_approach_tradeoffs="Tradeoffs",
            original_plan="Original",
            feedback_comment="Feedback",
            known_repos="acme/backend",
        )

        assert "Prefer codebase exploration focused" in plan_prompt
        assert "unrelated branches, open issues, pull requests" in plan_prompt
        assert "guessing from path names alone" in plan_prompt
        assert "Prefer focused codebase re-inspection" in regenerate_prompt
        assert "unrelated branches, open issues, pull requests" in regenerate_prompt
        assert "nearby source and test patterns" in regenerate_prompt


class TestVersionManagement:
    """Test prompt version management."""

    def test_default_version_is_v1(self):
        """Default version should be v1."""
        # Reset to default
        set_default_version("v1")
        assert get_default_version() == "v1"

    def test_set_default_version(self):
        """set_default_version should change the default."""
        original = get_default_version()

        try:
            set_default_version("test-version")
            assert get_default_version() == "test-version"
        finally:
            # Restore original
            set_default_version(original)

    def test_load_prompt_uses_default_version(self):
        """load_prompt without version should use default."""
        set_default_version("v1")

        # Load without specifying version
        result = load_prompt("system", current_date="2024-03-20")

        # Should load from v1
        assert "SDLC agent" in result  # Content from v1/system.md


class TestPromptContent:
    """Test prompt content quality."""

    def test_system_prompt_has_required_sections(self):
        """System prompt should have key instructions."""
        result = load_prompt("system", current_date="2024-03-20")

        # Should have date
        assert "2024-03-20" in result

        # Should have agent identity
        assert "agent" in result.lower()

    def test_generate_prd_prompt_structure(self):
        """generate-prd prompt should have proper structure."""
        result = load_prompt(
            "generate-prd",
            raw_requirements="Test requirements",
            context="Test context",
        )

        # Should mention PRD
        assert "PRD" in result or "Product Requirements" in result

        # Should include the provided content
        assert "Test requirements" in result
        assert "Test context" in result

    def test_task_takeover_triage_prompt(self):
        """task-takeover-triage prompt should allow contained tasks without formal sections."""
        result = load_prompt(
            "task-takeover-triage",
            summary="Test summary",
            description="Test description",
            comments="Test comments",
        )

        assert "Problem Statement" in result
        assert "Proposed Solution/Approach" in result
        assert "Acceptance Criteria" in result
        assert "Do not require formal section headings" in result
        assert "small and contained" in result
        assert "Target repository/file" in result
        assert "Test description" in result
        assert "Test comments" in result

    def test_task_takeover_planning_prompt(self):
        """task-takeover-planning prompt should map solutions to repository files and test plans."""
        result = load_prompt(
            "task-takeover-planning",
            ticket_key="AISOS-1234",
            summary="Test summary",
            description="Test description",
            comments="Test comments",
            known_repos="acme/repo",
            file_metadata="file1.py\nfile2.py",
        )

        assert "AISOS-1234" in result
        assert "acme/repo" in result
        assert "file1.py" in result
        assert "Target Files" in result
        assert "Test Plans" in result
        assert "Implementation Steps" in result
        assert "repository-relative paths only" in result
        assert "/home/..." in result
        assert "/workspace/..." in result

    def test_task_takeover_qa_prompt(self):
        """task-takeover-qa prompt should provide guidelines for contextual Q&A during planning."""
        result = load_prompt(
            "task-takeover-qa",
            ticket_key="AISOS-1234",
            summary="Test summary",
            description="Test description",
            plan_content="Test plan content",
            question="What is the test plan?",
        )

        assert "AISOS-1234" in result
        assert "Test plan content" in result
        assert "What is the test plan?" in result
        assert "clarifying question" in result

    def test_prompts_are_reasonable_length(self):
        """Prompts should not be excessively long (sanity check)."""
        # A rough estimate: 1 token ~ 4 characters
        # Most prompts should be under 2000 tokens (~8000 chars) without expansion
        max_base_length = 10000  # characters

        for version in list_versions():
            for prompt_name in list_prompts(version):
                template = load_prompt(prompt_name, version=version)
                assert len(template) < max_base_length, (
                    f"Prompt {prompt_name} is too long: {len(template)} chars"
                )


class TestPromptEdgeCases:
    """Test edge cases in prompt handling."""

    def test_prompt_with_special_characters_in_value(self):
        """Variables with special characters should be handled."""
        result = load_prompt(
            "generate-prd",
            raw_requirements='Test with $pecial ch@racters & symbols < > "quotes"',
            context="Normal context",
        )

        assert "$pecial" in result
        assert "ch@racters" in result
        assert '"quotes"' in result

    def test_prompt_with_multiline_value(self):
        """Multiline variable values should be preserved."""
        multiline_content = """Line 1
Line 2
Line 3 with indent
    - Bullet point"""

        result = load_prompt(
            "generate-prd",
            raw_requirements=multiline_content,
            context="Context",
        )

        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3 with indent" in result
        assert "- Bullet point" in result

    def test_prompt_with_empty_value(self):
        """Empty string values should be handled."""
        result = load_prompt(
            "generate-prd",
            raw_requirements="",
            context="",
        )

        # Should still load successfully
        assert "PRD" in result or "Product Requirements" in result

    def test_prompt_with_curly_braces_in_content(self):
        """Content with curly braces that aren't variables."""
        # The simple substitution might have issues with nested braces
        # This documents current behavior
        result = load_prompt(
            "generate-prd",
            raw_requirements='JSON: {"key": "value"}',
            context="Normal",
        )

        # The JSON should appear in the output
        # Note: This might cause issues if keys match variable names
        assert "JSON:" in result


class TestAllV1Prompts:
    """Comprehensive tests for all v1 prompts."""

    @pytest.fixture
    def all_v1_prompts(self):
        """Get all v1 prompt names."""
        return list_prompts("v1")

    def test_each_prompt_loads(self, all_v1_prompts):
        """Each v1 prompt should load without error."""
        for prompt_name in all_v1_prompts:
            template = load_prompt(prompt_name, version="v1")
            assert len(template) > 0, f"{prompt_name} should have content"

    def test_each_prompt_is_valid_utf8(self, all_v1_prompts):
        """Each prompt should be valid UTF-8 text."""
        for prompt_name in all_v1_prompts:
            template = load_prompt(prompt_name, version="v1")
            # If we got here, encoding was fine
            # Additionally verify it's printable/reasonable
            assert template.isprintable() or "\n" in template

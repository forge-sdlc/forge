"""Unit tests for artifact models."""

from datetime import datetime

from forge.models.artifacts import Epic, Feature, Task


class TestFeature:
    """Tests for Feature dataclass."""

    def test_create_feature(self):
        """Create a Feature artifact."""
        feature = Feature(
            jira_key="TEST-123",
            labels=["forge:managed"],
            prd_content="# PRD\n\nTest content.",
        )
        assert feature.jira_key == "TEST-123"
        assert "forge:managed" in feature.labels

    def test_feature_has_prd(self):
        """Feature correctly reports PRD presence."""
        feature_with_prd = Feature(
            jira_key="TEST-123",
            prd_content="# PRD content",
        )
        feature_without_prd = Feature(
            jira_key="TEST-124",
            prd_content="",
        )

        assert feature_with_prd.has_prd is True
        assert feature_without_prd.has_prd is False

    def test_feature_has_spec(self):
        """Feature correctly reports spec presence."""
        feature_with_spec = Feature(
            jira_key="TEST-123",
            spec_content="# Spec content",
        )
        feature_without_spec = Feature(
            jira_key="TEST-124",
            spec_content="   ",  # Whitespace only
        )

        assert feature_with_spec.has_spec is True
        assert feature_without_spec.has_spec is False

    def test_feature_is_forge_managed(self):
        """Feature correctly reports Forge management."""
        managed = Feature(
            jira_key="TEST-123",
            labels=["forge:managed", "other"],
        )
        unmanaged = Feature(
            jira_key="TEST-124",
            labels=["other"],
        )

        assert managed.is_forge_managed is True
        assert unmanaged.is_forge_managed is False

    def test_feature_tracks_epics(self):
        """Feature tracks related epic keys."""
        feature = Feature(
            jira_key="TEST-123",
            epic_keys=["TEST-124", "TEST-125"],
        )

        assert len(feature.epic_keys) == 2
        assert "TEST-124" in feature.epic_keys

    def test_feature_has_timestamps(self):
        """Feature has created_at and updated_at."""
        feature = Feature(jira_key="TEST-123")

        assert feature.created_at is not None
        assert feature.updated_at is not None
        assert isinstance(feature.created_at, datetime)


class TestEpic:
    """Tests for Epic dataclass."""

    def test_create_epic(self):
        """Create an Epic artifact."""
        epic = Epic(
            jira_key="TEST-124",
            feature_key="TEST-123",
            summary="Backend Authentication",
            plan_content="# Implementation Plan",
        )
        assert epic.jira_key == "TEST-124"
        assert epic.feature_key == "TEST-123"

    def test_epic_has_plan(self):
        """Epic correctly reports plan presence."""
        epic_with_plan = Epic(
            jira_key="TEST-124",
            feature_key="TEST-123",
            plan_content="# Plan content",
        )
        epic_without_plan = Epic(
            jira_key="TEST-125",
            feature_key="TEST-123",
            plan_content="",
        )

        assert epic_with_plan.has_plan is True
        assert epic_without_plan.has_plan is False

    def test_epic_tracks_tasks(self):
        """Epic tracks related task keys."""
        epic = Epic(
            jira_key="TEST-124",
            feature_key="TEST-123",
            task_keys=["TEST-126", "TEST-127", "TEST-128"],
        )

        assert len(epic.task_keys) == 3


class TestTask:
    """Tests for Task dataclass."""

    def test_create_task(self):
        """Create a Task artifact."""
        task = Task(
            jira_key="TEST-126",
            epic_key="TEST-124",
            summary="Implement login endpoint",
            description="Create POST /api/login endpoint",
            target_repo="org/backend",
        )
        assert task.jira_key == "TEST-126"
        assert task.epic_key == "TEST-124"
        assert task.target_repo == "org/backend"

    def test_task_has_implementation_details(self):
        """Task correctly reports implementation detail presence."""
        task_with_details = Task(
            jira_key="TEST-126",
            epic_key="TEST-124",
            description="Implementation details here",
            target_repo="org/backend",
        )
        task_without_details = Task(
            jira_key="TEST-127",
            epic_key="TEST-124",
            description="",
            target_repo="",
        )

        assert task_with_details.has_implementation_details is True
        assert task_without_details.has_implementation_details is False

    def test_task_tracks_acceptance_criteria(self):
        """Task tracks acceptance criteria."""
        task = Task(
            jira_key="TEST-126",
            epic_key="TEST-124",
            acceptance_criteria=[
                "Endpoint returns 200 with valid credentials",
                "Endpoint returns 401 with invalid credentials",
            ],
        )

        assert len(task.acceptance_criteria) == 2

    def test_task_tracks_pr_url(self):
        """Task tracks PR URL."""
        task = Task(
            jira_key="TEST-126",
            epic_key="TEST-124",
            pr_url="https://github.com/org/repo/pull/42",
        )

        assert task.pr_url is not None
        assert "pull/42" in task.pr_url

    def test_task_pr_url_optional(self):
        """Task PR URL is optional."""
        task = Task(
            jira_key="TEST-126",
            epic_key="TEST-124",
        )

        assert task.pr_url is None

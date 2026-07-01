"""Unit tests for metrics endpoint."""

import pytest
from httpx import ASGITransport, AsyncClient

from forge.main import app


class TestMetricsEndpoint:
    """Tests for /metrics endpoint."""

    @pytest.mark.asyncio
    async def test_metrics_returns_200(self):
        """Metrics endpoint returns 200."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/metrics")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_returns_prometheus_format(self):
        """Metrics endpoint returns Prometheus format."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/metrics")

        content_type = response.headers.get("content-type", "")
        assert "text/plain" in content_type or "openmetrics" in content_type

    @pytest.mark.asyncio
    async def test_metrics_includes_forge_metrics(self):
        """Metrics includes forge-related counters."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/metrics")

        body = response.text
        # Should include forge metrics
        assert "forge" in body

    @pytest.mark.asyncio
    async def test_metrics_includes_workflow_metrics(self):
        """Metrics includes workflow-related counters."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/metrics")

        body = response.text
        # Should include forge workflow metrics
        assert "forge_workflows" in body


class TestMetricRegistration:
    """Tests for metric registration."""

    def test_webhook_counter_exists(self):
        """Webhook received counter is registered."""
        from forge.api.routes.metrics import WEBHOOKS_RECEIVED

        assert WEBHOOKS_RECEIVED is not None

    def test_webhook_counter_has_labels(self):
        """Webhook counter has source and event_type labels."""
        from forge.api.routes.metrics import WEBHOOKS_RECEIVED

        # Counter should be labelable
        labeled = WEBHOOKS_RECEIVED.labels(source="jira", event_type="issue_updated")
        assert labeled is not None

    def test_workflow_histogram_exists(self):
        """Agent duration histogram is registered."""
        from forge.api.routes.metrics import AGENT_DURATION

        assert AGENT_DURATION is not None

    def test_increment_webhook_counter(self):
        """Can increment webhook counter."""
        from forge.api.routes.metrics import WEBHOOKS_RECEIVED

        # Should not raise
        WEBHOOKS_RECEIVED.labels(source="github", event_type="check_run").inc()


class TestReviewCycleMetrics:
    """Tests for review cycle metrics registration and recording."""

    def test_review_cycles_counter_exists(self):
        """Review cycles counter is registered."""
        from forge.api.routes.metrics import REVIEW_CYCLES

        assert REVIEW_CYCLES is not None

    def test_review_cycles_counter_has_labels(self):
        """Review cycles counter has skill and step labels."""
        from forge.api.routes.metrics import REVIEW_CYCLES

        labeled = REVIEW_CYCLES.labels(skill="implement-task", step="implementation")
        assert labeled is not None

    def test_review_verdicts_counter_exists(self):
        """Review verdicts counter is registered."""
        from forge.api.routes.metrics import REVIEW_VERDICTS

        assert REVIEW_VERDICTS is not None

    def test_review_verdicts_counter_has_labels(self):
        """Review verdicts counter has skill, step, and verdict labels."""
        from forge.api.routes.metrics import REVIEW_VERDICTS

        labeled = REVIEW_VERDICTS.labels(
            skill="implement-task", step="implementation", verdict="approved"
        )
        assert labeled is not None

    def test_review_duration_histogram_exists(self):
        """Review duration histogram is registered."""
        from forge.api.routes.metrics import REVIEW_DURATION

        assert REVIEW_DURATION is not None

    def test_review_duration_histogram_has_labels(self):
        """Review duration histogram has skill and step labels."""
        from forge.api.routes.metrics import REVIEW_DURATION

        labeled = REVIEW_DURATION.labels(skill="fix-ci", step="ci_fix")
        assert labeled is not None

    def test_record_review_cycle_helper(self):
        """record_review_cycle helper increments counter."""
        from forge.api.routes.metrics import REVIEW_CYCLES, record_review_cycle

        # Get initial value
        initial = REVIEW_CYCLES.labels(skill="test-skill", step="test-step")._value.get()

        record_review_cycle(skill="test-skill", step="test-step")

        # Counter should be incremented
        final = REVIEW_CYCLES.labels(skill="test-skill", step="test-step")._value.get()
        assert final == initial + 1

    def test_record_review_verdict_helper(self):
        """record_review_verdict helper increments counter."""
        from forge.api.routes.metrics import REVIEW_VERDICTS, record_review_verdict

        # Get initial value
        initial = REVIEW_VERDICTS.labels(
            skill="test-skill", step="test-step", verdict="approved"
        )._value.get()

        record_review_verdict(skill="test-skill", step="test-step", verdict="approved")

        # Counter should be incremented
        final = REVIEW_VERDICTS.labels(
            skill="test-skill", step="test-step", verdict="approved"
        )._value.get()
        assert final == initial + 1

    def test_record_review_verdict_rejected(self):
        """record_review_verdict helper handles rejected verdict."""
        from forge.api.routes.metrics import REVIEW_VERDICTS, record_review_verdict

        # Get initial value
        initial = REVIEW_VERDICTS.labels(
            skill="task-skill", step="task-step", verdict="rejected"
        )._value.get()

        record_review_verdict(skill="task-skill", step="task-step", verdict="rejected")

        # Counter should be incremented
        final = REVIEW_VERDICTS.labels(
            skill="task-skill", step="task-step", verdict="rejected"
        )._value.get()
        assert final == initial + 1

    def test_observe_review_duration_helper(self):
        """observe_review_duration helper observes histogram."""
        from forge.api.routes.metrics import REVIEW_DURATION, observe_review_duration

        # Get initial sum
        initial_sum = REVIEW_DURATION.labels(
            skill="duration-skill", step="duration-step"
        )._sum.get()

        observe_review_duration(skill="duration-skill", step="duration-step", duration=45.5)

        # Sum should increase by observed value
        final_sum = REVIEW_DURATION.labels(
            skill="duration-skill", step="duration-step"
        )._sum.get()
        # Just check that something was observed (sum increased)
        assert final_sum >= initial_sum + 45.5

    @pytest.mark.asyncio
    async def test_review_metrics_in_endpoint_output(self):
        """Review metrics appear in /metrics endpoint output."""
        from forge.api.routes.metrics import (
            observe_review_duration,
            record_review_cycle,
            record_review_verdict,
        )

        # Record some metrics first
        record_review_cycle(skill="endpoint-skill", step="endpoint-step")
        record_review_verdict(skill="endpoint-skill", step="endpoint-step", verdict="approved")
        observe_review_duration(skill="endpoint-skill", step="endpoint-step", duration=30.0)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/metrics")

        body = response.text
        assert "forge_review_cycles_total" in body
        assert "forge_review_verdicts_total" in body
        assert "forge_review_duration_seconds" in body

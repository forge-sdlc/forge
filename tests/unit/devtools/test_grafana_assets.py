"""Tests for checked-in Grafana dashboard assets."""

import json
from pathlib import Path

GRAFANA_DIR = Path("devtools/grafana")
DASHBOARD_DIR = GRAFANA_DIR / "dashboards"
EXPECTED_DASHBOARDS = {
    "forge-agent-performance.json",
    "forge-business.json",
    "forge-ci-review.json",
    "forge-cost.json",
    "forge-engineering.json",
    "forge-issue-detail.json",
    "forge-model-usage.json",
    "forge-observability-health.json",
    "forge-operations.json",
    "forge-ticket-execution.json",
    "forge-workflow-funnel.json",
}


def test_grafana_dashboard_json_is_valid() -> None:
    dashboards = sorted(DASHBOARD_DIR.glob("*.json"))
    assert dashboards, "expected checked-in Grafana dashboards"
    assert {dashboard.name for dashboard in dashboards} == EXPECTED_DASHBOARDS

    for dashboard in dashboards:
        data = json.loads(dashboard.read_text())
        assert data["kind"] == "Dashboard"
        assert data["metadata"]["name"] == dashboard.stem
        assert data["spec"]["title"]
        assert data["spec"]["elements"], f"{dashboard} should define panels"


def test_dashboards_use_configurable_trace_fields() -> None:
    joined = "\n".join(path.read_text() for path in sorted(DASHBOARD_DIR.glob("*.json")))

    assert "OSASINFRA" not in joined
    assert "OSPA" not in joined
    assert "tags[1]" not in joined
    assert "tags[3]" not in joined
    assert "project_id=~" not in joined
    assert "metadata['project_id']" in joined
    assert "metadata['ticket_type']" in joined
    assert "metadata['workflow_step']" in joined


def test_dashboards_cover_expected_datasources() -> None:
    joined = "\n".join(path.read_text() for path in sorted(DASHBOARD_DIR.glob("*.json")))

    assert "langfuse-clickhouse" in joined
    assert "forge-prometheus" in joined
    assert "forge-redis" in joined


def test_grafana_provisioning_files_exist() -> None:
    assert (GRAFANA_DIR / "provisioning/dashboards/dashboards.yml").is_file()
    assert (GRAFANA_DIR / "provisioning/datasources/clickhouse.yml").is_file()
    assert (GRAFANA_DIR / "provisioning/datasources/prometheus.yml").is_file()
    assert (GRAFANA_DIR / "provisioning/datasources/redis.yml").is_file()
    assert (GRAFANA_DIR / "compose.grafana.yml").is_file()
    assert (GRAFANA_DIR / "compose.langfuse-network.yml").is_file()

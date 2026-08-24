"""Unit tests to verify version bump for AISOS-2423."""

import os
import tomllib

import yaml  # type: ignore

from forge import __version__
from forge.observability.config import configure_tracing


def test_package_version() -> None:
    """Verify that the package version has been bumped to 1.0.0."""
    assert __version__ == "1.0.0"


def test_pyproject_version() -> None:
    """Verify that pyproject.toml version has been bumped to 1.0.0."""
    pyproject_path = os.path.join(os.path.dirname(__file__), "../..", "pyproject.toml")
    assert os.path.exists(pyproject_path), f"pyproject.toml not found at {pyproject_path}"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert data["project"]["version"] == "1.0.0"


def test_observability_version() -> None:
    """Verify that the tracing resource metadata service version has been bumped to 1.0.0."""
    provider = configure_tracing(service_name="test-forge-version-test")
    resource = provider.resource
    assert resource.attributes.get("service.version") == "1.0.0"


def test_helm_chart_version() -> None:
    """Verify that Helm Chart.yaml version and appVersion have been bumped to 1.0.0."""
    chart_path = os.path.join(os.path.dirname(__file__), "../..", "charts/forge/Chart.yaml")
    assert os.path.exists(chart_path), f"Chart.yaml not found at {chart_path}"

    with open(chart_path) as f:
        data = yaml.safe_load(f)

    assert data["version"] == "1.0.0"
    assert data["appVersion"] == "1.0.0"

"""Sandbox driver registry and factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.sandbox.driver import SandboxDriver

if TYPE_CHECKING:
    from forge.config import Settings

__all__ = ["create_driver"]


def create_driver(settings: Settings) -> SandboxDriver:
    """Create a sandbox driver based on settings.

    Raises:
        ValueError: If the configured driver name is unknown.
        RuntimeError: If the driver's runtime is not available.
    """
    driver_name = settings.sandbox_driver

    match driver_name:
        case "podman":
            from forge.sandbox.drivers.podman import PodmanDriver

            driver = PodmanDriver(settings)
        case "kubernetes":
            from forge.sandbox.drivers.kubernetes import KubernetesDriver

            driver = KubernetesDriver(settings)
        case _:
            raise ValueError(
                f"Unknown sandbox driver: {driver_name!r}. Valid options: podman, kubernetes"
            )

    if not driver.is_available():
        raise RuntimeError(
            f"Sandbox driver {driver_name!r} is not available. "
            f"Check that the required runtime is installed and accessible."
        )

    return driver

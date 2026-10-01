"""Tests for mounting isolated skills in agent containers."""

from pathlib import Path
from types import SimpleNamespace

from forge.sandbox.runner import ContainerRunner


def test_get_skill_mounts_includes_committed_and_fetched_skills(tmp_path: Path) -> None:
    """Container agents receive every skill layer in resolution order."""
    committed = tmp_path / "committed-skills"
    installed = tmp_path / "skills"
    expected = [committed / "default", committed / "proj", installed / "proj"]
    for skill_dir in expected:
        skill_dir.mkdir(parents=True)

    runner = object.__new__(ContainerRunner)
    runner.settings = SimpleNamespace(
        committed_skills_dir=committed,
        skills_install_dir=installed,
    )

    mounts, container_paths = runner._get_skill_mounts("PROJ-123")

    assert [host_path for host_path, _ in mounts] == expected
    assert [container_path for _, container_path in mounts] == [
        "/skills/skill_0",
        "/skills/skill_1",
        "/skills/skill_2",
    ]
    assert container_paths == "/skills/skill_0/,/skills/skill_1/,/skills/skill_2/"

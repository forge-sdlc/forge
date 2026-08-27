from pathlib import Path
from types import SimpleNamespace

from forge.sandbox.runner import ContainerRunner


def test_skill_mounts_use_shared_selinux_label(tmp_path: Path) -> None:
    """Concurrent sandboxes must retain access to shared skill directories."""
    runner = object.__new__(ContainerRunner)
    runner.settings = SimpleNamespace(llm_backend="openai")
    workspace = tmp_path / "workspace"
    task_file = tmp_path / "task.json"
    skill_dir = tmp_path / "skills"

    mounts = runner._build_volume_mounts(
        workspace,
        task_file,
        None,
        [(skill_dir, "/skills/skill_0")],
    )

    assert (skill_dir, "/skills/skill_0", "ro,z") in mounts
    assert (skill_dir, "/skills/skill_0", "ro,Z") not in mounts

"""Deterministic sandbox entrypoint used by the Kind cluster gate."""

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--max-retries")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()

    task = json.loads(Path(args.task_file).read_text())
    exit_code = int(os.environ.get("FIXTURE_EXIT_CODE", "0"))
    if exit_code == 0:
        Path("/workspace/kind-result.txt").write_text(
            f"{task['task_key']}:KUBERNETES_DRIVER_OK", encoding="utf-8"
        )
    print(f"fixture task={task['task_key']} exit_code={exit_code}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Forge skill test runner — simulates Forge's agent context locally.

Uses Forge's own prompt templates (src/forge/prompts/) to reproduce the
exact system prompt and user message format, without needing Jira, GitHub,
or the hosted beta.

Usage:
    python3 devtools/test-skill/run.py \
        --skill generate-prd \
        --skill-dir skills/osac/generate-prd \
        --input test-case.yaml \
        --output output/

    python3 devtools/test-skill/run.py \
        --skill generate-prd \
        --skill-dir skills/osac/generate-prd \
        --dataset eval/dataset/cases/ \
        --output output/
"""

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

# Add Forge source to path so we can import forge.prompts
FORGE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(FORGE_ROOT / "src"))

import yaml

from forge.prompts import load_prompt

try:
    import mlflow
    import mlflow.anthropic

    HAS_MLFLOW = True
except ImportError:
    HAS_MLFLOW = False

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_anthropic import ChatAnthropic as LCChatAnthropic
from langgraph.checkpoint.memory import MemorySaver

try:
    from langchain_google_vertexai.model_garden import (
        ChatAnthropicVertex as LCChatAnthropicVertex,
    )
except ImportError:
    LCChatAnthropicVertex = None


SCRIPT_DIR = Path(__file__).parent


def load_config():
    with open(SCRIPT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def _format_references(references: list[dict]) -> str:
    if not references:
        return ""
    lines = ["\n\n## Reference Documentation\n"]
    for ref in references:
        title = ref.get("title", "Untitled")
        url = ref.get("url", "")
        tags = ref.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- [{title}]({url}){tag_str}\n")
    return "".join(lines)



def build_user_message(
    skill_name: str,
    requirements: str,
    project_key: str,
    summary: str,
) -> str:
    prompt_name = skill_name  # e.g., "generate-prd"
    context_str = str({"project_key": project_key, "summary": summary})
    try:
        return load_prompt(
            prompt_name,
            raw_requirements=requirements,
            context=context_str,
        )
    except FileNotFoundError:
        return f"Please complete the following task:\n\n{requirements}"


def setup_workspace(
    skill_dir: Path,
    skill_name: str,
    project: str,
    repo_dirs: list[Path] | None = None,
) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="forge-test-"))
    skill_target = workspace / "opt" / "forge" / "skills" / project / skill_name
    skill_target.mkdir(parents=True)
    shutil.copytree(skill_dir, skill_target, dirs_exist_ok=True)
    user_dir = workspace / "home" / "user"
    user_dir.mkdir(parents=True)
    if repo_dirs:
        for repo_path in repo_dirs:
            repo_path = Path(repo_path).resolve()
            if not repo_path.is_dir():
                print(f"  Warning: repo dir not found, skipping: {repo_path}")
                continue
            target = user_dir / repo_path.name
            shutil.copytree(
                repo_path, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "node_modules", ".venv", "vendor",
                ),
            )
            print(f"  Repo: {repo_path.name} -> {target}")
    return workspace


def build_system_prompt_text(
    ticket_key: str,
    project_key: str,
    references: list[dict] | None = None,
) -> str:
    system_text = load_prompt("system", current_date=str(date.today()))
    system_text += f"\n\nContext:\n- ticket_key: {ticket_key}\n- project_key: {project_key}\n"
    system_text += _format_references(references or [])
    return system_text


async def run_agent_deepagents(
    system_prompt: str,
    user_message: str,
    workspace: Path,
    config: dict,
    _skill_name: str,
    project: str,
) -> dict:
    vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    vertex_region = os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5")
    model_name = config.get("model", "claude-opus-4-6")
    max_tokens = config.get("max_tokens", 16384)

    if vertex_project:
        if LCChatAnthropicVertex is None:
            raise RuntimeError(
                "ANTHROPIC_VERTEX_PROJECT_ID is set but langchain-google-vertexai is not installed. "
                "Install with: pip install langchain-google-vertexai"
            )
        model = LCChatAnthropicVertex(
            model_name=model_name,
            project=vertex_project,
            location=vertex_region,
            max_tokens=max_tokens,
        )
    else:
        model = LCChatAnthropic(
            model=model_name,
            max_tokens=max_tokens,
        )

    backend = FilesystemBackend(root_dir=str(workspace), virtual_mode=True)

    skill_paths = [f"/opt/forge/skills/{project}/"]

    checkpointer = MemorySaver()
    agent = create_deep_agent(
        model=model,
        backend=backend,
        skills=skill_paths,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
    )

    thread_id = str(uuid.uuid4())
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config={"configurable": {"thread_id": thread_id}},
    )

    messages = result.get("messages", []) if isinstance(result, dict) else []
    trace = []
    total_input = 0
    total_output = 0
    ai_iteration = 0

    for msg in messages:
        msg_type = type(msg).__name__
        if msg_type not in ("AIMessage", "AIMessageChunk"):
            continue

        ai_iteration += 1
        content = msg.content
        text_blocks = []

        if isinstance(content, str):
            if content.strip():
                text_blocks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_blocks.append(block.get("text", ""))

        tool_calls = [
            {"name": tc.get("name", ""), "input": tc.get("args", {})}
            for tc in getattr(msg, "tool_calls", [])
        ]

        usage = getattr(msg, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        output_tokens = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
        total_input += input_tokens
        total_output += output_tokens

        resp_meta = getattr(msg, "response_metadata", {}) or {}
        stop_reason = resp_meta.get("stop_reason", "")

        trace.append({
            "iteration": ai_iteration,
            "stop_reason": stop_reason,
            "text": text_blocks,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

        if tool_calls:
            tc_names = [tc["name"] for tc in tool_calls]
            print(f"    deepagents: {', '.join(tc_names)}")

    final_text = ""
    for entry in trace:
        for t in entry.get("text", []):
            if t.strip():
                final_text = t

    return {
        "trace": trace,
        "final_text": final_text,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "iterations": ai_iteration,
    }


def collect_output_files(
    workspace: Path,
    repo_dirs: list[Path] | None = None,
    written_paths: set[str] | None = None,
) -> dict[str, str]:
    """Collect files the agent wrote during execution.

    When *written_paths* is provided (a set of virtual paths the agent
    passed to write_file), only those files are collected — this avoids
    capturing pre-existing repo and skill files.  Falls back to the
    heuristic exclude-list when the set is not available.
    """
    repo_names = {Path(r).resolve().name for r in (repo_dirs or [])}
    files = {}
    for search_root in [workspace / "home" / "user", workspace / "opt" / "forge"]:
        if not search_root.exists():
            continue
        for fpath in search_root.rglob("*"):
            if fpath.is_file() and fpath.suffix != ".pyc":
                rel = str(fpath.relative_to(search_root))
                if written_paths is not None:
                    virt = "/" + str(fpath.relative_to(workspace))
                    if virt not in written_paths:
                        continue
                else:
                    top_dir = rel.split("/")[0] if "/" in rel else ""
                    if top_dir in repo_names or top_dir == "skills":
                        continue
                if rel not in files:
                    with contextlib.suppress(UnicodeDecodeError, PermissionError):
                        files[rel] = fpath.read_text()
    return files


def _setup_mlflow(tracking_uri: str, experiment_name: str):
    """Configure MLflow tracking and Anthropic auto-instrumentation."""
    if not HAS_MLFLOW:
        print("Warning: mlflow not installed, skipping MLflow integration")
        return False
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)
    mlflow.anthropic.autolog()
    print(f"MLflow: tracking to {tracking_uri}, experiment '{experiment_name}'")
    return True


def _save_outputs(result, workspace, output_dir, config, repo_dirs=None):
    """Save output files and trace JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)

    output_files = collect_output_files(workspace, repo_dirs=repo_dirs)
    for rel_path, content in output_files.items():
        out_path = output_dir / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content)
        print(f"Output: {out_path}")

    final_text = result.get("final_text", "")
    skill_name = result.get("_skill_name", "")
    filename = "design.md" if "spec" in skill_name else "prd.md"
    artifact_path = output_dir / filename
    if not artifact_path.exists() and final_text.strip():
        artifact_path.write_text(final_text)
        print(f"Output (inline): {artifact_path}")

    trace_path = output_dir / "trace.json"
    with open(trace_path, "w") as f:
        json.dump(
            {
                "ticket_key": result["_ticket_key"],
                "skill": result["_skill_name"],
                "model": config.get("model"),
                "elapsed_seconds": result["_elapsed"],
                "iterations": result["iterations"],
                "total_input_tokens": result["total_input_tokens"],
                "total_output_tokens": result["total_output_tokens"],
                "trace": result["trace"],
            },
            f,
            indent=2,
        )
    print(f"Trace: {trace_path}")


def _log_mlflow_metrics(result, config, output_dir):
    """Log metrics and artifacts to the current active MLflow run."""
    mlflow.set_tag("model", config.get("model", "claude-opus-4-6"))

    mlflow.log_metric("elapsed_seconds", result["_elapsed"])
    mlflow.log_metric("iterations", result["iterations"])
    mlflow.log_metric("input_tokens", result["total_input_tokens"])
    mlflow.log_metric("output_tokens", result["total_output_tokens"])
    total = result["total_input_tokens"] + result["total_output_tokens"]
    mlflow.log_metric("total_tokens", total)
    cost = (result["total_input_tokens"] * 15 + result["total_output_tokens"] * 75) / 1e6
    mlflow.log_metric("cost_usd", round(cost, 2))

    try:
        trace_path = output_dir / "trace.json"
        if trace_path.exists():
            mlflow.log_artifact(str(trace_path), "trace")
        for f in output_dir.rglob("prd.md"):
            if "skills" not in str(f):
                mlflow.log_artifact(str(f), "generated")
                break
    except Exception:
        pass


def _run_agent(
    system_text: str,
    user_message: str,
    workspace: Path,
    config: dict,
    skill_name: str,
    project: str,
) -> dict:
    return asyncio.run(
        run_agent_deepagents(system_text, user_message, workspace, config, skill_name, project)
    )


def run_single_case(
    skill_name: str,
    skill_dir: Path,
    input_path: Path,
    output_dir: Path,
    config: dict,
    repo_dirs: list[Path] | None = None,
):
    with open(input_path) as f:
        input_data = yaml.safe_load(f)

    ticket_key = input_data.get("jira_key", input_data.get("ticket_key", "TEST-0000"))
    project_key = config.get("project", "default").upper()
    project = config.get("project", "default")
    summary = input_data.get("title", input_data.get("summary", ""))
    requirements = input_data.get("prompt", input_data.get("requirements", ""))

    if not requirements:
        print(f"Error: No requirements found in {input_path}")
        return

    prd_file = input_path.parent / "gold-prd.md"
    if prd_file.exists():
        prd_content = prd_file.read_text()
        requirements += f"\n\n## Approved PRD\n\n{prd_content}"
        print(f"  PRD: loaded {prd_file.name} ({len(prd_content)} chars)")

    print(f"\n{'='*60}")
    print(f"Running: {ticket_key} — {summary}")
    print(f"Skill: {skill_name} from {skill_dir}")
    print(f"{'='*60}")

    workspace = setup_workspace(skill_dir, skill_name, project, repo_dirs=repo_dirs)
    print(f"Workspace: {workspace}")

    references = config.get("references", [])
    system_text = build_system_prompt_text(ticket_key, project_key, references)
    user_message = build_user_message(skill_name, requirements, project_key, summary)

    def _execute():
        return _run_agent(
            system_text, user_message,
            workspace, config, skill_name, project,
        )

    if HAS_MLFLOW and config.get("mlflow_enabled"):
        with mlflow.start_run(run_name=f"{ticket_key} — {summary}"):
            mlflow.set_tag("case", ticket_key)
            mlflow.set_tag("feature", summary)
            mlflow.set_tag("skill", skill_name)

            start = time.time()
            result = _execute()
            elapsed = round(time.time() - start, 1)

            result["_ticket_key"] = ticket_key
            result["_skill_name"] = skill_name
            result["_elapsed"] = elapsed

            _save_outputs(result, workspace, output_dir, config, repo_dirs=repo_dirs)
            _log_mlflow_metrics(result, config, output_dir)

            print(f"\nDone in {elapsed}s — {result['iterations']} iterations")
            print(f"Tokens: {result['total_input_tokens']} input, "
                  f"{result['total_output_tokens']} output")
            print(f"MLflow: logged run for {ticket_key}")
    else:
        start = time.time()
        result = _execute()
        elapsed = round(time.time() - start, 1)

        result["_ticket_key"] = ticket_key
        result["_skill_name"] = skill_name
        result["_elapsed"] = elapsed

        _save_outputs(result, workspace, output_dir, config)

        print(f"\nDone in {elapsed}s — {result['iterations']} iterations")
        print(f"Tokens: {result['total_input_tokens']} input, "
              f"{result['total_output_tokens']} output")

    shutil.rmtree(workspace)


def main():
    parser = argparse.ArgumentParser(description="Forge skill test runner")
    parser.add_argument("--skill", required=True, help="Skill name (e.g., generate-prd)")
    parser.add_argument("--skill-dir", required=True, help="Path to skill directory")
    parser.add_argument("--input", help="Path to a single input.yaml test case")
    parser.add_argument("--dataset", help="Path to dataset directory (runs all cases)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--model", help="Override model from config")
    parser.add_argument(
        "--mlflow",
        metavar="URI",
        help="MLflow tracking URI (e.g., http://host:5000). Enables auto-tracing of all API calls.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default="forge-skill-eval",
        help="MLflow experiment name (default: forge-skill-eval)",
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        metavar="DIR",
        help="Local repo directories to copy into the workspace (e.g., /path/to/myproject /path/to/enhancement-proposals). "
             "Gives the agent codebase access via read/grep tools.",
    )
    parser.add_argument(
        "--project",
        help="Project name for skill path (e.g., osac). Overrides config.yaml project setting.",
    )
    parser.add_argument(
        "--references",
        metavar="FILE",
        help="JSON file with reference documentation (same format as forge.references project property).",
    )
    args = parser.parse_args()

    config = load_config()
    if args.model:
        config["model"] = args.model
    if args.project:
        config["project"] = args.project
    if args.references:
        refs_path = Path(args.references)
        if not refs_path.exists():
            print(f"Error: references file not found: {refs_path}")
            sys.exit(1)
        with open(refs_path) as f:
            config["references"] = json.load(f)

    if args.mlflow:
        import logging
        logging.getLogger("mlflow.tracing.export").setLevel(logging.ERROR)
        config["mlflow_enabled"] = _setup_mlflow(args.mlflow, args.mlflow_experiment)
    else:
        config["mlflow_enabled"] = False

    skill_dir = Path(args.skill_dir).resolve()
    if not (skill_dir / "SKILL.md").exists():
        print(f"Error: No SKILL.md found in {skill_dir}")
        sys.exit(1)

    output_dir = Path(args.output).resolve()

    repo_dirs = [Path(r) for r in args.repos] if args.repos else None

    if args.input:
        run_single_case(args.skill, skill_dir, Path(args.input), output_dir, config, repo_dirs=repo_dirs)
    elif args.dataset:
        dataset_dir = Path(args.dataset)
        for case_dir in sorted(dataset_dir.iterdir()):
            if not case_dir.is_dir():
                continue
            input_yaml = case_dir / "input.yaml"
            if not input_yaml.exists():
                continue
            case_output = output_dir / case_dir.name
            run_single_case(args.skill, skill_dir, input_yaml, case_output, config, repo_dirs=repo_dirs)
    else:
        print("Error: Provide either --input or --dataset")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Command-line interface for Forge SDLC Orchestrator."""

import argparse
import asyncio
import logging
import sys
from typing import Any

from forge.config import get_settings


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI usage."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


async def _get_compiled_workflow_for_ticket(ticket_key: str):
    """Helper to get compiled workflow for a ticket (used by CLI commands).

    Args:
        ticket_key: The ticket key to get workflow for.

    Returns:
        Tuple of (compiled_workflow, checkpointer).
    """
    from forge.integrations.jira.client import JiraClient
    from forge.models.workflow import TicketType
    from forge.orchestrator.checkpointer import get_checkpointer
    from forge.workflow.registry import create_default_router

    # Fetch ticket to determine type
    jira = JiraClient()
    try:
        issue = await jira.get_issue(ticket_key)
        ticket_type_str = issue.issue_type
        try:
            ticket_type = TicketType(ticket_type_str)
        except ValueError:
            ticket_type = TicketType.FEATURE  # Default for unknown types
    finally:
        await jira.close()

    # Resolve workflow
    router = create_default_router()
    workflow_instance = router.resolve(
        ticket_type=ticket_type,
        labels=[],
        event={},
    )

    if workflow_instance is None:
        raise ValueError(f"No workflow found for ticket type: {ticket_type}")

    # Build and compile
    checkpointer = await get_checkpointer()
    graph = workflow_instance.build_graph()
    compiled_workflow = graph.compile(checkpointer=checkpointer)

    return compiled_workflow, checkpointer


async def cmd_run(args: argparse.Namespace) -> int:
    """Run workflow for a single ticket."""
    from forge.orchestrator.worker import run_single_ticket

    try:
        result = await run_single_ticket(args.ticket)
        print("\nWorkflow completed!")
        print(f"  Final node: {result.get('current_node')}")
        print(f"  Paused: {result.get('is_paused', False)}")
        if result.get("last_error"):
            print(f"  Error: {result.get('last_error')}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_worker(args: argparse.Namespace) -> int:
    """Start the orchestrator worker."""
    from forge.orchestrator.worker import OrchestratorWorker

    worker = OrchestratorWorker(consumer_name=args.name)
    await worker.start()
    return 0


async def cmd_test_node(args: argparse.Namespace) -> int:
    """Test a single workflow node."""
    from forge.integrations.jira.client import JiraClient

    # Import all nodes from workflow module
    from forge.workflow.nodes import (
        bug_workflow,
        epic_decomposition,
        prd_generation,
        spec_generation,
        task_generation,
    )

    node_map = {
        "generate_prd": prd_generation.generate_prd,
        "regenerate_prd": prd_generation.regenerate_prd_with_feedback,
        "generate_spec": spec_generation.generate_spec,
        "regenerate_spec": spec_generation.regenerate_spec_with_feedback,
        "decompose_epics": epic_decomposition.decompose_epics,
        "generate_tasks": task_generation.generate_tasks,
        "analyze_bug": bug_workflow.analyze_bug,
    }

    node_name = args.node
    if node_name not in node_map:
        print(f"Unknown node: {node_name}", file=sys.stderr)
        print(f"Available nodes: {', '.join(node_map.keys())}")
        return 1

    # Build initial state
    jira = JiraClient()
    try:
        issue = await jira.get_issue(args.ticket)
        ticket_type = issue.issue_type
    finally:
        await jira.close()

    state: dict[str, Any] = {
        "ticket_key": args.ticket,
        "ticket_type": ticket_type,
        "event_type": "test",
        "context": {},
        "current_node": node_name,
        "is_paused": False,
        "retry_count": 0,
    }

    print(f"Running node: {node_name}")
    print(f"Ticket: {args.ticket} ({ticket_type})")

    try:
        node_func = node_map[node_name]
        result = await node_func(state)
        print("\nNode completed!")
        print(f"  Next node: {result.get('current_node')}")
        if result.get("last_error"):
            print(f"  Error: {result.get('last_error')}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_check_ticket(args: argparse.Namespace) -> int:
    """Check ticket status and labels."""
    from forge.integrations.jira.client import JiraClient
    from forge.models.workflow import get_workflow_phase

    jira = JiraClient()
    try:
        issue = await jira.get_issue(args.ticket)
        labels = await jira.get_labels(args.ticket)

        print(f"Ticket: {issue.key}")
        print(f"  Summary: {issue.summary}")
        print(f"  Type: {issue.issue_type}")
        print(f"  Status: {issue.status}")
        print(f"  Labels: {', '.join(labels) if labels else '(none)'}")

        phase = get_workflow_phase(labels)
        print(f"  Workflow Phase: {phase or '(not managed by Forge)'}")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        await jira.close()


async def cmd_set_label(args: argparse.Namespace) -> int:
    """Set a workflow label on a ticket."""
    from forge.integrations.jira.client import JiraClient
    from forge.models.workflow import ForgeLabel

    # Find matching label
    label_name = args.label.upper().replace("-", "_")
    try:
        label = ForgeLabel[label_name]
    except KeyError:
        print(f"Unknown label: {args.label}", file=sys.stderr)
        print("Available labels:")
        for label_item in ForgeLabel:
            print(f"  {label_item.name.lower().replace('_', '-')}: {label_item.value}")
        return 1

    jira = JiraClient()
    try:
        await jira.set_workflow_label(args.ticket, label)
        print(f"Set label {label.value} on {args.ticket}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        await jira.close()


async def cmd_approve(args: argparse.Namespace) -> int:
    """Approve PRD/Spec and continue workflow."""
    from forge.integrations.jira.client import JiraClient
    from forge.models.workflow import ForgeLabel

    jira = JiraClient()
    try:
        # Get current labels to determine stage
        labels = await jira.get_labels(args.ticket)

        if ForgeLabel.PRD_PENDING.value in labels:
            # Approve PRD -> move to spec generation
            await jira.set_workflow_label(args.ticket, ForgeLabel.PRD_APPROVED)
            print(f"PRD approved for {args.ticket}")
        elif ForgeLabel.SPEC_PENDING.value in labels:
            # Approve Spec -> move to epic decomposition
            await jira.set_workflow_label(args.ticket, ForgeLabel.SPEC_APPROVED)
            print(f"Spec approved for {args.ticket}")
        elif ForgeLabel.PLAN_PENDING.value in labels:
            # Approve Plan -> move to task generation
            await jira.set_workflow_label(args.ticket, ForgeLabel.PLAN_APPROVED)
            print(f"Plan approved for {args.ticket}")
        else:
            print(f"No pending approval found for {args.ticket}")
            print(f"Current labels: {labels}")
            return 1

        # Resume workflow
        workflow, _ = await _get_compiled_workflow_for_ticket(args.ticket)

        config = {"configurable": {"thread_id": args.ticket}}

        # Get current state and update it
        state = await workflow.aget_state(config)
        if state and state.values:
            updated_state = {
                **state.values,
                "is_paused": False,
                "revision_requested": False,
                "feedback_comment": None,
            }

            result = await workflow.ainvoke(updated_state, config=config)
            print(f"Workflow resumed, now at: {result.get('current_node')}")
            if result.get("is_paused"):
                print("Workflow paused again, waiting for next approval")
        else:
            print("No saved workflow state found, run the workflow again")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        await jira.close()


async def cmd_reject(args: argparse.Namespace) -> int:
    """Reject PRD/Spec with feedback and regenerate."""
    from forge.integrations.jira.client import JiraClient
    from forge.models.workflow import ForgeLabel

    if not args.feedback:
        print("Error: --feedback is required for rejection", file=sys.stderr)
        return 1

    jira = JiraClient()
    try:
        # Get current labels to determine stage
        labels = await jira.get_labels(args.ticket)

        if ForgeLabel.PRD_PENDING.value in labels:
            stage = "PRD"
        elif ForgeLabel.SPEC_PENDING.value in labels:
            stage = "Spec"
        elif ForgeLabel.PLAN_PENDING.value in labels:
            stage = "Plan"
        else:
            print(f"No pending approval found for {args.ticket}")
            print(f"Current labels: {labels}")
            return 1

        # Add feedback as comment
        await jira.add_comment(args.ticket, f"**Revision Requested**\n\n{args.feedback}")
        print(f"{stage} rejected for {args.ticket}")
        print(f"Feedback: {args.feedback}")

        # Resume workflow with rejection
        workflow, _ = await _get_compiled_workflow_for_ticket(args.ticket)

        config = {"configurable": {"thread_id": args.ticket}}

        # Get current state and update it
        state = await workflow.aget_state(config)
        if state and state.values:
            updated_state = {
                **state.values,
                "is_paused": False,
                "revision_requested": True,
                "feedback_comment": args.feedback,
            }

            result = await workflow.ainvoke(updated_state, config=config)
            print(f"Workflow resumed for regeneration, now at: {result.get('current_node')}")
            if result.get("is_paused"):
                print("Regeneration complete, waiting for approval")
        else:
            print("No saved workflow state found, run the workflow again")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        await jira.close()


async def cmd_clear_checkpoint(args: argparse.Namespace) -> int:
    """Clear checkpoint state for a ticket."""
    from forge.orchestrator.checkpointer import clear_checkpoint

    try:
        cleared = await clear_checkpoint(args.ticket)
        if cleared:
            print(f"Checkpoint cleared for {args.ticket}")
        else:
            print(f"No checkpoint found for {args.ticket}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_list(_args: argparse.Namespace) -> int:
    """List active workflows."""
    from forge.orchestrator.checkpointer import get_redis_client

    redis_client = await get_redis_client()
    try:
        # Scan for workflow checkpoints
        cursor = 0
        workflows: list[dict[str, Any]] = []

        while True:
            cursor, keys = await redis_client.scan(
                cursor=cursor,
                match="langgraph:checkpoint:*",
                count=100,
            )

            for key in keys:
                # Extract ticket ID from key
                key_str = key.decode() if isinstance(key, bytes) else key
                parts = key_str.split(":")
                if len(parts) >= 3:
                    ticket_id = parts[2]
                    # Get checkpoint data
                    data = await redis_client.get(key)
                    if data:
                        workflows.append(
                            {
                                "ticket": ticket_id,
                                "key": key_str,
                            }
                        )

            if cursor == 0:
                break

        if not workflows:
            print("No active workflows found.")
            return 0

        # Filter and display based on status flag
        print(f"\nActive Workflows ({len(workflows)} total):\n")
        print(f"{'Ticket':<20} {'Checkpoint Key'}")
        print("-" * 60)

        for wf in sorted(workflows, key=lambda x: x["ticket"]):
            print(f"{wf['ticket']:<20} {wf['key'][:40]}...")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_retry(args: argparse.Namespace) -> int:
    """Retry a failed or blocked workflow."""
    try:
        workflow, _ = await _get_compiled_workflow_for_ticket(args.ticket)
        config = {"configurable": {"thread_id": args.ticket}}

        # Get current state
        state = await workflow.aget_state(config)
        if not state or not state.values:
            print(f"No workflow state found for {args.ticket}")
            return 1

        current_state = state.values
        print(f"Current node: {current_state.get('current_node')}")
        print(f"Retry count: {current_state.get('retry_count', 0)}")

        # Reset retry count and error state
        updated_state = {
            **current_state,
            "retry_count": 0,
            "last_error": None,
            "is_paused": False,
        }

        # Resume workflow
        result = await workflow.ainvoke(updated_state, config=config)
        print(f"\nWorkflow retried, now at: {result.get('current_node')}")
        if result.get("is_paused"):
            print("Workflow paused, waiting for approval")
        if result.get("last_error"):
            print(f"Error: {result.get('last_error')}")

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_logs(args: argparse.Namespace) -> int:
    """View logs for a ticket workflow."""
    from forge.orchestrator.checkpointer import get_redis_client

    redis_client = await get_redis_client()
    try:
        # Look for workflow logs in Redis
        log_key = f"forge:logs:{args.ticket}"
        logs = await redis_client.lrange(log_key, 0, args.limit - 1)

        if not logs:
            # Try to get checkpoint state for any info
            checkpoint_key = f"langgraph:checkpoint:{args.ticket}"
            data = await redis_client.get(checkpoint_key)
            if data:
                print(f"No logs found, but checkpoint exists for {args.ticket}")
                print("Use 'forge check {args.ticket}' to see current state")
            else:
                print(f"No logs or checkpoint found for {args.ticket}")
            return 0

        print(f"\nLogs for {args.ticket} (last {len(logs)} entries):\n")
        print("-" * 80)

        for log_entry in reversed(logs):
            entry = log_entry.decode() if isinstance(log_entry, bytes) else log_entry
            print(entry)

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cmd_skills_install(args: argparse.Namespace) -> int:
    """Install a skill from a Git URL or local path."""
    from forge.skills.cli_handlers import cmd_skills_install as _handler

    return await _handler(args)


async def cmd_skills_list(_args: argparse.Namespace) -> int:
    """List installed skills."""
    from forge.skills.cli_handlers import cmd_skills_list as _handler

    return await _handler(_args)


async def cmd_skills_update(_args: argparse.Namespace) -> int:
    """Update installed skills."""
    from forge.skills.cli_handlers import cmd_skills_update as _handler

    return await _handler(_args)


async def cmd_project_setup(args: argparse.Namespace) -> int:
    """Configure Jira project properties for Forge."""
    import json

    from forge.integrations.jira.client import JiraClient

    project_key = args.project_key.upper()
    jira = JiraClient()

    try:
        # forge.repos
        if args.repo:
            invalid = [r for r in args.repo if "/" not in r]
            if invalid:
                print(
                    f"Error: invalid repo format (expected owner/repo): {invalid}",
                    file=sys.stderr,
                )
                return 1
            await jira.set_project_property(project_key, "forge.repos", args.repo)
            print(f"[OK] forge.repos = {args.repo}")

        # forge.default_repo
        if args.default_repo:
            if "/" not in args.default_repo:
                print(
                    f"Error: --default-repo must be owner/repo, got: {args.default_repo!r}",
                    file=sys.stderr,
                )
                return 1
            await jira.set_project_property(project_key, "forge.default_repo", args.default_repo)
            print(f"[OK] forge.default_repo = {args.default_repo!r}")

        # forge.prd_proposals_repo — opt-in / opt-out for PRD approval via GitHub PR
        if args.prd_proposals_repo is not None:
            if args.prd_proposals_repo == "":
                await jira.delete_project_property(project_key, "forge.prd_proposals_repo")
                print("[OK] forge.prd_proposals_repo removed (PRD approval via Jira labels)")
            else:
                if "/" not in args.prd_proposals_repo:
                    print(
                        f"Error: --prd-proposals-repo must be owner/repo, got: {args.prd_proposals_repo!r}",
                        file=sys.stderr,
                    )
                    return 1
                await jira.set_project_property(
                    project_key, "forge.prd_proposals_repo", args.prd_proposals_repo
                )
                print(f"[OK] forge.prd_proposals_repo = {args.prd_proposals_repo!r}")

        # forge.prd_proposals_path — base directory for enhancement folders
        if args.prd_proposals_path is not None:
            if args.prd_proposals_path == "":
                await jira.delete_project_property(project_key, "forge.prd_proposals_path")
                print("[OK] forge.prd_proposals_path removed (reset to default: repo root)")
            else:
                path = args.prd_proposals_path.strip("/")
                await jira.set_project_property(project_key, "forge.prd_proposals_path", path)
                print(f"[OK] forge.prd_proposals_path = {path!r}")

        # forge.skills — built from --add-skill flags and/or --skills-config JSON
        skill_entries: list[dict] = []

        if args.skills_config:
            try:
                from_json = json.loads(args.skills_config)
            except json.JSONDecodeError as exc:
                print(f"Error: --skills-config is not valid JSON: {exc}", file=sys.stderr)
                return 1
            if not isinstance(from_json, list):
                print("Error: --skills-config must be a JSON array", file=sys.stderr)
                return 1
            skill_entries.extend(from_json)

        for raw in args.add_skill or []:
            # Format: source=<url>[,ref=<ref>][,path=<path>]
            #         source=<url>[,ref=<ref>],mapping=<name>:<repo_path>[,mapping=...]
            entry: dict = {}
            mappings: dict[str, str] = {}
            for part in raw.split(","):
                if "=" not in part:
                    print(
                        f"Error: --add-skill part {part!r} missing '=' (expected key=value)",
                        file=sys.stderr,
                    )
                    return 1
                key, _, val = part.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "mapping":
                    skill_name, _, repo_path = val.partition(":")
                    mappings[skill_name] = repo_path
                else:
                    entry[key] = val

            if not entry.get("source"):
                print("Error: --add-skill requires source=<git-url>", file=sys.stderr)
                return 1
            if mappings:
                entry["skill_mapping"] = mappings
            elif "path" not in entry:
                entry["path"] = ""
            skill_entries.append(entry)

        if skill_entries:
            from forge.skills.models import SkillEntry

            validated = []
            for i, raw_entry in enumerate(skill_entries):
                try:
                    validated.append(SkillEntry(**raw_entry).model_dump(exclude_none=True))
                except Exception as exc:
                    print(f"Error: skills entry {i} is invalid: {exc}", file=sys.stderr)
                    return 1
            skill_entries = validated

            await jira.set_project_property(project_key, "forge.skills", skill_entries)
            print(f"[OK] forge.skills = {len(skill_entries)} entries")

        if not any(
            [
                args.repo,
                args.default_repo,
                args.prd_proposals_repo is not None,
                args.prd_proposals_path is not None,
                args.skills_config,
                args.add_skill,
            ]
        ):
            print(
                "Nothing to set — specify at least one of: "
                "--repo, --default-repo, --prd-proposals-repo, "
                "--prd-proposals-path, --skills-config, --add-skill"
            )
            return 1

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        await jira.close()


async def cmd_health(_args: argparse.Namespace) -> int:
    """Check system health."""
    from forge.orchestrator.checkpointer import get_redis_client

    print("Checking system health...\n")

    # Check settings
    try:
        settings = get_settings()
        print("[OK] Configuration loaded")
        print(f"     Jira: {settings.jira_base_url}")
        print(f"     Use labels: {settings.jira_use_labels}")
        print(f"     Store in comments: {settings.jira_store_in_comments}")
    except Exception as e:
        print(f"[FAIL] Configuration: {e}")
        return 1

    # Check Redis
    try:
        redis_client = await get_redis_client()
        await redis_client.ping()
        print(f"[OK] Redis connected: {settings.redis_url}")
    except Exception as e:
        print(f"[FAIL] Redis: {e}")
        return 1

    # Check Jira (if token configured)
    if settings.jira_api_token.get_secret_value() != "your-jira-api-token":
        try:
            from forge.integrations.jira.client import JiraClient

            jira = JiraClient()
            # Try to get projects (simple API call)
            await jira.close()
            print("[OK] Jira credentials configured")
        except Exception as e:
            print(f"[WARN] Jira: {e}")
    else:
        print("[SKIP] Jira: API token not configured")

    # Check Anthropic/Vertex
    if settings.use_vertex_ai:
        print(f"[OK] Using Vertex AI: {settings.anthropic_vertex_project_id}")
    elif settings.anthropic_api_key.get_secret_value():
        print("[OK] Using direct Anthropic API")
    else:
        print("[WARN] No Claude API configured")

    print("\nHealth check complete!")
    return 0


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge SDLC Orchestrator CLI",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run workflow for a ticket",
    )
    run_parser.add_argument("ticket", help="Jira ticket key (e.g., AISOS-103)")

    # worker command
    worker_parser = subparsers.add_parser(
        "worker",
        help="Start the orchestrator worker",
    )
    worker_parser.add_argument(
        "--name",
        help="Worker name (auto-generated if not provided)",
    )

    # test-node command
    test_parser = subparsers.add_parser(
        "test-node",
        help="Test a single workflow node",
    )
    test_parser.add_argument("node", help="Node name to test")
    test_parser.add_argument("ticket", help="Jira ticket key")

    # check command
    check_parser = subparsers.add_parser(
        "check",
        help="Check ticket status and labels",
    )
    check_parser.add_argument("ticket", help="Jira ticket key")

    # set-label command
    label_parser = subparsers.add_parser(
        "set-label",
        help="Set a workflow label on a ticket",
    )
    label_parser.add_argument("ticket", help="Jira ticket key")
    label_parser.add_argument("label", help="Label name (e.g., prd-pending)")

    # approve command
    approve_parser = subparsers.add_parser(
        "approve",
        help="Approve PRD/Spec/Plan and continue workflow",
    )
    approve_parser.add_argument("ticket", help="Jira ticket key")

    # reject command
    reject_parser = subparsers.add_parser(
        "reject",
        help="Reject PRD/Spec/Plan with feedback",
    )
    reject_parser.add_argument("ticket", help="Jira ticket key")
    reject_parser.add_argument(
        "--feedback",
        "-f",
        required=True,
        help="Feedback explaining why rejected and what to change",
    )

    # clear-checkpoint command
    clear_parser = subparsers.add_parser(
        "clear-checkpoint",
        help="Clear checkpoint state for a ticket (allows workflow restart)",
    )
    clear_parser.add_argument("ticket", help="Jira ticket key")

    # health command
    subparsers.add_parser(
        "health",
        help="Check system health",
    )

    # list command
    subparsers.add_parser(
        "list",
        help="List active workflows",
    )

    # retry command
    retry_parser = subparsers.add_parser(
        "retry",
        help="Retry a failed or blocked workflow",
    )
    retry_parser.add_argument("ticket", help="Jira ticket key")

    # logs command
    logs_parser = subparsers.add_parser(
        "logs",
        help="View logs for a ticket workflow",
    )
    logs_parser.add_argument("ticket", help="Jira ticket key")
    logs_parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=50,
        help="Number of log entries to show (default: 50)",
    )

    # skills subparser group
    skills_parser = subparsers.add_parser(
        "skills",
        help="Manage Forge skills",
    )
    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command",
        help="Skills commands",
    )

    # skills install subcommand
    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install a skill from a Git URL or local path",
    )
    skills_install_parser.add_argument(
        "source",
        help="Git URL or local path to the skill",
    )
    skills_install_parser.add_argument(
        "--project",
        metavar="PROJECT_KEY",
        help=(
            "Project key to install the skill under (e.g. MYPROJ). "
            "Exactly one of --project or --default must be provided."
        ),
    )
    skills_install_parser.add_argument(
        "--default",
        action="store_true",
        help=(
            "Install the skill to skills/default/ (shared across all projects). "
            "Exactly one of --project or --default must be provided."
        ),
    )
    skills_install_parser.add_argument(
        "--ref",
        metavar="REF",
        help="Git ref (tag, branch, or SHA) to check out",
    )

    # skills list subcommand
    skills_subparsers.add_parser(
        "list",
        help="List installed skills",
    )

    # skills update subcommand
    skills_update_parser = skills_subparsers.add_parser(
        "update",
        help="Re-fetch skill packages listed in the local lock file",
        description=(
            "Re-fetch and reinstall skill packages recorded in skills/skills.lock. "
            "For each Git-sourced package the current commit SHA is resolved; "
            "packages whose SHA has not changed are skipped. "
            "NOTE: this command reads the LOCAL lock file only – it does NOT "
            "consult any Jira property (e.g. forge.skills). "
            "Use 'forge skills install' to add new packages."
        ),
    )
    skills_update_parser.add_argument(
        "--project",
        metavar="PROJECT_KEY",
        help=(
            "Only update packages installed under this project key "
            "(i.e. entries with target == PROJECT_KEY in the lock file). "
            "When omitted all entries in the lock file are processed."
        ),
    )

    # project-setup command
    setup_parser = subparsers.add_parser(
        "project-setup",
        help="Configure Jira project properties for Forge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Configure Forge metadata on a Jira project.

Examples:
  # Set GitHub repos and default
  forge project-setup MYPROJ \\
    --repo owner/repo1 --repo owner/repo2 \\
    --default-repo owner/repo1

  # Add a skills package (path mode)
  forge project-setup MYPROJ \\
    --add-skill source=https://github.com/acme/skills,ref=v1.0,path=

  # Add a skills package (skill_mapping mode)
  forge project-setup MYPROJ \\
    --add-skill source=https://github.com/acme/docs,ref=main,mapping=generate-prd:docs/prompts/prd

  # Set skills from a JSON array
  forge project-setup MYPROJ \\
    --skills-config '[{"source":"https://github.com/acme/skills","ref":"v1.0","path":""}]'
""",
    )
    setup_parser.add_argument("project_key", help="Jira project key (e.g., MYPROJ)")
    setup_parser.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/REPO",
        help="GitHub repo in owner/repo format (repeatable, sets forge.repos)",
    )
    setup_parser.add_argument(
        "--default-repo",
        metavar="OWNER/REPO",
        help="Primary GitHub repo (sets forge.default_repo)",
    )
    setup_parser.add_argument(
        "--prd-proposals-repo",
        metavar="OWNER/REPO",
        default=None,
        help=(
            "Enhancement proposals repo for PR-based PRD approval "
            "(sets forge.prd_proposals_repo). Pass empty string to disable."
        ),
    )
    setup_parser.add_argument(
        "--prd-proposals-path",
        metavar="PATH",
        default=None,
        help=(
            "Base directory in the proposals repo for enhancement folders "
            "(sets forge.prd_proposals_path). Default is repo root. "
            "Pass empty string to reset to default."
        ),
    )
    setup_parser.add_argument(
        "--add-skill",
        action="append",
        metavar="source=URL[,ref=REF][,path=PATH|,mapping=NAME:PATH]",
        help=(
            "Add a skill package entry (repeatable, appended to --skills-config if also given). "
            "Use path= for path mode or mapping=name:path for skill_mapping mode."
        ),
    )
    setup_parser.add_argument(
        "--skills-config",
        metavar="JSON",
        help="Full forge.skills value as a JSON array of SkillEntry objects",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        return 0

    # Handle skills subcommands
    if args.command == "skills":
        skills_handlers = {
            "install": cmd_skills_install,
            "list": cmd_skills_list,
            "update": cmd_skills_update,
        }
        skills_cmd = getattr(args, "skills_command", None)
        if skills_cmd is None:
            skills_parser.print_help()
            return 0
        skills_handler = skills_handlers.get(skills_cmd)
        if skills_handler:
            return asyncio.run(skills_handler(args))
        skills_parser.print_help()
        return 0

    # Map commands to async handlers
    handlers = {
        "run": cmd_run,
        "worker": cmd_worker,
        "test-node": cmd_test_node,
        "check": cmd_check_ticket,
        "set-label": cmd_set_label,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "clear-checkpoint": cmd_clear_checkpoint,
        "health": cmd_health,
        "list": cmd_list,
        "retry": cmd_retry,
        "logs": cmd_logs,
        "project-setup": cmd_project_setup,
    }

    handler = handlers.get(args.command)
    if handler:
        return asyncio.run(handler(args))

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

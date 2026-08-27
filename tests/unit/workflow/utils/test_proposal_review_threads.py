from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.utils.proposal_review_threads import (
    parse_proposal_thread_decisions,
    reply_to_proposal_decisions,
    triage_proposal_review_threads,
)


def _threads():
    return [
        {
            "thread_id": "thread-1",
            "path": "prd.md",
            "line": 10,
            "comments": [{"comment_id": 101, "body": "Clarify authorization."}],
        },
        {
            "thread_id": "thread-2",
            "path": "prd.md",
            "line": 20,
            "comments": [{"comment_id": 202, "body": "Rename the product."}],
        },
    ]


def test_parses_independent_thread_decisions() -> None:
    output = """[
      {"thread_id":"thread-1","comment_id":999,"disposition":"accept",
       "feedback":"Clarify authorization.","response":"","reason":"Valid"},
      {"thread_id":"thread-2","comment_id":999,"disposition":"reply",
       "feedback":"","response":"The name is externally defined.","reason":"Invalid"}
    ]"""

    decisions = parse_proposal_thread_decisions(output, _threads())

    assert decisions[0]["disposition"] == "accept"
    assert decisions[0]["comment_id"] == 101
    assert decisions[1]["disposition"] == "reply"
    assert decisions[1]["comment_id"] == 202


def test_missing_decision_conservatively_revises_original_feedback() -> None:
    decisions = parse_proposal_thread_decisions("not json", _threads())

    assert [item["disposition"] for item in decisions] == ["uncertain", "uncertain"]
    assert decisions[0]["feedback"] == "Clarify authorization."


def test_empty_comment_threads_are_ignored() -> None:
    decisions = parse_proposal_thread_decisions(
        "not json", [{"thread_id": "empty", "comments": []}, *_threads()]
    )

    assert [item["thread_id"] for item in decisions] == ["thread-1", "thread-2"]


@pytest.mark.asyncio
async def test_reply_skips_missing_repo_coordinates() -> None:
    with patch("forge.integrations.github.client.GitHubClient") as github:
        await reply_to_proposal_decisions(
            repo_full_name="",
            pr_number=7,
            decisions=[{"disposition": "reply", "comment_id": 10, "response": "No."}],
            dispositions={"reply"},
        )

    github.assert_not_called()


@pytest.mark.asyncio
async def test_triage_records_each_decision_for_monitoring() -> None:
    agent = MagicMock()
    agent.run_task = AsyncMock(
        return_value=(
            '[{"thread_id":"thread-1","comment_id":101,"disposition":"accept",'
            '"feedback":"Clarify authorization.","response":"","reason":"Valid"},'
            '{"thread_id":"thread-2","comment_id":202,"disposition":"reply",'
            '"feedback":"","response":"No.","reason":"Invalid"}]'
        )
    )
    agent._strip_preamble.side_effect = lambda value: value
    agent.close = AsyncMock()

    with (
        patch("forge.workflow.stations.agent_operation.ForgeAgent", return_value=agent),
        patch(
            "forge.workflow.utils.proposal_review_threads.record_proposal_review_decision"
        ) as record,
    ):
        await triage_proposal_review_threads(
            artifact_type="PRD",
            artifact_content="# PRD",
            threads=_threads(),
            ticket_key="TEST-233",
        )

    assert record.call_args_list[0].args == ("prd", "accept")
    assert record.call_args_list[1].args == ("prd", "reply")

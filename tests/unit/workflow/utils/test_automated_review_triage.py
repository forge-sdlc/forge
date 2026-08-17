from forge.workflow.utils.automated_review_triage import (
    is_bot_sender,
    parse_automated_review_decision,
)


def test_bot_sender_uses_github_account_type() -> None:
    assert is_bot_sender({"sender": {"login": "anything", "type": "Bot"}})
    assert not is_bot_sender({"sender": {"login": "someone[bot]", "type": "User"}})


def test_parse_blocking_decision() -> None:
    decision = parse_automated_review_decision(
        '```json\n{"verdict":"blocking","blocking_feedback":"Fix auth","reason":"Required"}\n```'
    )
    assert decision.verdict == "blocking"
    assert decision.blocking_feedback == "Fix auth"


def test_parse_failure_is_uncertain() -> None:
    assert parse_automated_review_decision("Verdict: PASS").verdict == "uncertain"
    assert (
        parse_automated_review_decision(
            '{"verdict":"blocking","blocking_feedback":"","reason":"Missing"}'
        ).verdict
        == "uncertain"
    )

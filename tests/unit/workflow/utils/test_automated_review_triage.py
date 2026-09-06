from forge.workflow.utils.automated_review_triage import is_bot_sender


def test_bot_sender_uses_github_account_type() -> None:
    assert is_bot_sender({"sender": {"login": "anything", "type": "Bot"}})
    assert not is_bot_sender({"sender": {"login": "someone[bot]", "type": "User"}})

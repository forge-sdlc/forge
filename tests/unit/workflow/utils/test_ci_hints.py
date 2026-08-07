"""Unit tests for /forge hint helpers."""

from forge.workflow.utils.ci_hints import (
    append_hint,
    format_hints_for_fix,
    hint_entry,
    mark_hints_consumed,
    parse_forge_hint,
    unconsumed_hints,
)


def test_parse_forge_hint_extracts_text():
    assert parse_forge_hint("/forge hint use the ipv6 job logs") == "use the ipv6 job logs"
    assert parse_forge_hint("/forge HINT restart openvswitch") == "restart openvswitch"


def test_parse_forge_hint_rejects_empty_and_oversized():
    assert parse_forge_hint("/forge hint") is None
    assert parse_forge_hint("/forge skip-gate lint") is None
    assert parse_forge_hint("/forge hint " + ("x" * 2001)) is None


def test_append_hint_dedupes_by_comment_id():
    first = hint_entry(
        text="look at unit tests",
        actor="alice",
        comment_id=42,
        repository="org/repo",
        pr_number=7,
    )
    second = hint_entry(
        text="look at unit tests again",
        actor="alice",
        comment_id=42,
        repository="org/repo",
        pr_number=7,
    )
    hints = append_hint([], first)
    hints = append_hint(hints, second)
    assert len(hints) == 1
    assert hints[0]["text"] == "look at unit tests"


def test_consume_and_format_hints():
    entry = hint_entry(
        text="service X must be running",
        actor="bob",
        comment_id=9,
        repository="org/repo",
        pr_number=3,
    )
    hints = [entry]
    assert len(unconsumed_hints(hints)) == 1
    rendered = format_hints_for_fix(unconsumed_hints(hints))
    assert "service X must be running" in rendered
    assert "@bob" in rendered
    consumed = mark_hints_consumed(hints)
    assert all(h["consumed"] for h in consumed)
    assert unconsumed_hints(consumed) == []

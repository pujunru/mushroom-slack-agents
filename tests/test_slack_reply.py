"""Tests for reply formatting."""

from __future__ import annotations

from mushroom_agent.slack_client import MAX_REPLY_CHARS, format_reply


def test_short_output_passes_through() -> None:
    assert format_reply("All done.") == "All done."


def test_empty_output_gets_a_placeholder() -> None:
    assert "no output" in format_reply("")
    assert "no output" in format_reply("   \n ")


def test_long_output_is_truncated_with_a_notice() -> None:
    """Slack rejects an oversized block, so the reply must be trimmed."""
    text = "x" * (MAX_REPLY_CHARS + 500)
    out = format_reply(text)
    assert len(out) < len(text)
    assert "truncated" in out
    assert str(len(text)) in out


def test_output_exactly_at_the_limit_is_not_truncated() -> None:
    text = "y" * MAX_REPLY_CHARS
    assert format_reply(text) == text

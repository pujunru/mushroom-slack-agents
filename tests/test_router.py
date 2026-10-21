"""Tests for the event routing policy."""

from __future__ import annotations

import pytest
from mushroom_core.models import SlackEnvelope
from mushroom_core.router import (
    DEFAULT_AGENT,
    clean_prompt,
    extract_agent,
    route,
)


def envelope(**event: object) -> SlackEnvelope:
    """Build an envelope shaped like a real Slack event_callback."""
    return SlackEnvelope(
        event_id=str(event.pop("event_id", "Ev123")),
        team_id="T123",
        event=dict(event),  # type: ignore[arg-type]
    )


# --- prompt cleaning -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<@U0BOT> summarise this", "summarise this"),
        ("<@U0BOT>   leading space", "leading space"),
        ("no mention here", "no mention here"),
        ("<@U0BOT>", ""),
        ("", ""),
        ("<@U0BOT> <@U0OTHER> two mentions", "two mentions"),
    ],
)
def test_clean_prompt_strips_mentions(raw: str, expected: str) -> None:
    assert clean_prompt(raw) == expected


@pytest.mark.parametrize(
    ("raw", "agent", "remainder"),
    [
        ("!review check my PR", "review", "check my PR"),
        ("!triage", "triage", ""),
        ("no directive", DEFAULT_AGENT, "no directive"),
        ("!UPPER not a directive", DEFAULT_AGENT, "!UPPER not a directive"),
        ("", DEFAULT_AGENT, ""),
    ],
)
def test_extract_agent(raw: str, agent: str, remainder: str) -> None:
    assert extract_agent(raw) == (agent, remainder)


# --- routing decisions -----------------------------------------------------


def test_app_mention_becomes_a_job() -> None:
    decision = route(
        envelope(
            type="app_mention", text="<@U0BOT> do the thing", channel="C1", user="U1", ts="1.0"
        )
    )
    assert decision.should_enqueue
    assert decision.job is not None
    assert decision.job.prompt == "do the thing"
    assert decision.job.agent == DEFAULT_AGENT
    assert decision.job.slack.channel == "C1"
    assert decision.job.slack.thread_ts == "1.0"


def test_agent_directive_selects_an_agent() -> None:
    decision = route(
        envelope(
            type="app_mention",
            text="<@U0BOT> !review look at this",
            channel="C1",
            user="U1",
            ts="1.0",
        )
    )
    assert decision.should_enqueue
    assert decision.job is not None
    assert decision.job.agent == "review"
    assert decision.job.prompt == "look at this"


def test_unknown_agent_falls_back_rather_than_dropping() -> None:
    decision = route(
        envelope(type="app_mention", text="<@U0BOT> !nope do it", channel="C1", ts="1.0"),
        allowed_agents=frozenset({"general", "review"}),
    )
    assert decision.should_enqueue
    assert decision.job is not None
    assert decision.job.agent == DEFAULT_AGENT
    # The directive stays in the prompt so the request is not silently altered.
    assert decision.job.prompt == "!nope do it"


def test_bot_authored_event_is_never_enqueued() -> None:
    """The loop guard: an agent must not answer its own message."""
    decision = route(envelope(type="message", text="I am the bot", bot_id="B1", ts="1.0"))
    assert not decision.should_enqueue
    assert decision.reason == "bot_authored"


def test_bot_message_subtype_is_never_enqueued() -> None:
    decision = route(envelope(type="message", subtype="bot_message", text="hi", ts="1.0"))
    assert not decision.should_enqueue
    assert decision.reason == "bot_authored"


@pytest.mark.parametrize("subtype", ["message_changed", "channel_join", "message_deleted"])
def test_noise_subtypes_are_ignored(subtype: str) -> None:
    decision = route(envelope(type="message", subtype=subtype, text="x", ts="1.0"))
    assert not decision.should_enqueue
    assert decision.reason.startswith("ignored_subtype")


def test_unactionable_event_type_is_ignored() -> None:
    decision = route(envelope(type="reaction_added", text=""))
    assert not decision.should_enqueue
    assert "event_type_not_actionable" in decision.reason


def test_empty_prompt_is_ignored() -> None:
    decision = route(envelope(type="app_mention", text="<@U0BOT>", channel="C1", ts="1.0"))
    assert not decision.should_enqueue
    assert decision.reason == "empty_prompt"


def test_url_verification_is_not_a_job() -> None:
    env = SlackEnvelope(event_id="n/a", type="url_verification")
    decision = route(env)
    assert not decision.should_enqueue
    assert decision.reason == "url_verification"


def test_thread_ts_prefers_thread_root_over_message_ts() -> None:
    """A reply must land in the thread, not as a new channel message."""
    decision = route(
        envelope(type="app_mention", text="<@U0BOT> hi", channel="C1", ts="2.0", thread_ts="1.0")
    )
    assert decision.job is not None
    assert decision.job.slack.thread_ts == "1.0"


# --- thread-aware follow-ups ----------------------------------------------


def test_app_mention_never_needs_a_known_thread() -> None:
    """An explicit mention is always ours, thread or not."""
    decision = route(
        envelope(type="app_mention", text="<@U0BOT> do it", channel="C1", ts="1.0"),
        in_known_thread=False,
    )
    assert decision.should_enqueue


def test_plain_message_outside_a_thread_is_ignored() -> None:
    """Ordinary channel chatter must not become a job.

    Subscribing to message.channels means Slack delivers every message in
    every channel the bot is in. Without this check the agent would answer
    all of it — one model call per message.
    """
    decision = route(
        envelope(type="message", text="just talking to a colleague", channel="C1", ts="2.0"),
        in_known_thread=True,
    )
    assert not decision.should_enqueue
    assert decision.reason == "message_not_in_thread"


def test_plain_message_in_an_unknown_thread_is_ignored() -> None:
    """A thread the bot has never worked in is somebody else's conversation."""
    decision = route(
        envelope(type="message", text="follow up", channel="C1", ts="2.0", thread_ts="1.0"),
        in_known_thread=False,
    )
    assert not decision.should_enqueue
    assert decision.reason == "message_in_unknown_thread"


def test_plain_message_in_a_known_thread_becomes_a_job() -> None:
    """The point of the feature: a follow-up needs no second @-mention."""
    decision = route(
        envelope(
            type="message", text="can you print the pwd?", channel="C1", ts="2.0", thread_ts="1.0"
        ),
        in_known_thread=True,
    )
    assert decision.should_enqueue
    assert decision.job is not None
    assert decision.job.prompt == "can you print the pwd?"
    # The reply must stay in the same thread.
    assert decision.job.slack.thread_ts == "1.0"


def test_bot_authored_thread_reply_is_still_dropped() -> None:
    """The loop guard outranks the thread rule.

    Mushroom's own replies land in a thread it is known to work in, so
    without the bot check first, every answer would trigger another job.
    """
    decision = route(
        envelope(
            type="message",
            text="my own reply",
            channel="C1",
            ts="3.0",
            thread_ts="1.0",
            bot_id="B1",
        ),
        in_known_thread=True,
    )
    assert not decision.should_enqueue
    assert decision.reason == "bot_authored"


def test_thread_history_shape_for_the_agent() -> None:
    """History is replayed as roles the model APIs already understand.

    'user' and 'assistant' are the vocabulary of every chat completion API,
    so storing those strings means the transcript needs no translation on the
    way to the model.
    """
    from mushroom_core.store import SESSION_NAMESPACE, _session_key

    # Sessions are scoped per agent: a different agent profile is a different
    # conversation even in the same Slack thread.
    assert _session_key("general", "C1", "1.0") != _session_key("review", "C1", "1.0")
    assert _session_key("general", "C1", "1.0") == "general:C1:1.0"
    assert SESSION_NAMESPACE == "opencode_session"

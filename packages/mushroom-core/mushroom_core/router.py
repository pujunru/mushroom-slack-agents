"""Decide whether a Slack event becomes a job, and which agent takes it.

Routing is a pure function of the stored event, kept separate from both HTTP
handling and queue mechanics so the policy is testable on its own and so
changing it never touches the ingest path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job, SlackContext, SlackEnvelope

# Event types we act on. Anything else is stored and marked ignored, which
# keeps the audit trail complete without creating work.
ACTIONABLE_EVENT_TYPES = frozenset(
    {
        "app_mention",
        "message",
        "assistant_thread_started",
        "assistant_thread_context_changed",
    }
)

# `message` subtypes that are channel noise rather than something to answer.
IGNORED_MESSAGE_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "thread_broadcast",
        "file_share",
    }
)

# `<@U123ABC>` — a leading bot mention, stripped before the text becomes a
# prompt so the agent is not told to reason about its own user id.
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")

# A bang-prefixed first word selects an agent explicitly: "!review this PR".
_AGENT_DIRECTIVE_RE = re.compile(r"^!([a-z][a-z0-9_-]{0,31})\s*")

DEFAULT_AGENT = "general"


@dataclass(frozen=True)
class RoutingDecision:
    """Why an event was or was not turned into a job."""

    should_enqueue: bool
    reason: str
    job: Job | None = None


def clean_prompt(text: str) -> str:
    """Turn Slack message text into an agent instruction."""
    without_mentions = _MENTION_RE.sub("", text or "")
    return without_mentions.strip()


def extract_agent(text: str) -> tuple[str, str]:
    """Split an optional `!agent` directive off the front of the text.

    Returns `(agent_name, remaining_text)`, defaulting to the general agent.
    """
    stripped = (text or "").lstrip()
    match = _AGENT_DIRECTIVE_RE.match(stripped)
    if not match:
        return DEFAULT_AGENT, stripped
    return match.group(1), stripped[match.end() :].lstrip()


def route(
    envelope: SlackEnvelope,
    *,
    allowed_agents: frozenset[str] | None = None,
    in_known_thread: bool = False,
) -> RoutingDecision:
    """Decide the fate of one Slack delivery.

    `in_known_thread` says whether this message continues a thread the bot has
    already participated in. The caller determines it, because answering it
    needs the event store. It only affects plain `message` events; an
    `app_mention` is always ours.

    The order of these checks is deliberate: the bot-authored check comes
    first, because an agent replying in a channel it also watches would
    otherwise answer itself forever.
    """
    if envelope.is_bot_event:
        return RoutingDecision(False, "bot_authored")

    if envelope.type == "url_verification":
        return RoutingDecision(False, "url_verification")

    event_type = envelope.event_type
    if event_type not in ACTIONABLE_EVENT_TYPES:
        return RoutingDecision(False, f"event_type_not_actionable:{event_type}")

    subtype = envelope.event_subtype
    if event_type == "message" and subtype in IGNORED_MESSAGE_SUBTYPES:
        return RoutingDecision(False, f"ignored_subtype:{subtype}")

    # A plain `message` is only ours when it continues a thread we are already
    # part of. Subscribing to message.channels means Slack delivers *every*
    # message in every channel the bot is in, so without this the agent would
    # answer all channel chatter — a model call per message, and a bot that
    # interrupts conversations it was not invited into.
    #
    # `in_known_thread` is supplied by the caller, which looks up whether we
    # have handled this thread before. It is None when the caller could not
    # determine it, which is treated as "not ours" so a lookup failure stays
    # quiet rather than becoming noisy.
    if event_type == "message":
        if not envelope.event.get("thread_ts"):
            # Not a threaded message at all: a top-level channel post.
            return RoutingDecision(False, "message_not_in_thread")
        if not in_known_thread:
            return RoutingDecision(False, "message_in_unknown_thread")

    text = clean_prompt(envelope.text)
    agent, remainder = extract_agent(text)

    if allowed_agents is not None and agent not in allowed_agents:
        # An unknown directive falls back to the general agent with the
        # directive left in the prompt, rather than dropping the request.
        agent, remainder = DEFAULT_AGENT, text

    if not remainder:
        return RoutingDecision(False, "empty_prompt")

    job = Job(
        event_id=envelope.event_id,
        agent=agent,
        prompt=remainder,
        slack=SlackContext(
            team_id=envelope.team_id,
            channel=envelope.channel,
            user=envelope.user,
            thread_ts=envelope.thread_ts,
            event_type=event_type,
        ),
    )
    return RoutingDecision(True, "ok", job)

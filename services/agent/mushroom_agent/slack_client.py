"""Post agent results back into Slack.

Kept behind a tiny interface so the worker can be tested without a Slack
token, and so a failure to reply never fails the job: the work is done and
recorded, and a dropped reply is a notification problem, not a reason to run
an expensive agent again.
"""

from __future__ import annotations

from mushroom_core.logging import get_logger

log = get_logger(__name__)

# Slack rejects a message block over 3000 characters; 2900 leaves room for the
# code fence and a truncation notice.
MAX_REPLY_CHARS = 2900


def format_reply(text: str, *, max_chars: int = MAX_REPLY_CHARS) -> str:
    """Trim an agent's output to something Slack will accept."""
    cleaned = (text or "").strip() or "(the agent produced no output)"
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[:max_chars].rstrip()
    return f"{head}\n\n_…output truncated ({len(cleaned)} characters total)._"


class SlackReplier:
    """Posts into a thread, or logs the reply when no token is configured."""

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._client = None
        if not bot_token:
            return
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as exc:
            # The async client needs aiohttp, an optional slack-sdk extra. A
            # missing dependency must degrade to "cannot reply" rather than
            # crash the worker loop: the agent run already succeeded and is
            # recorded, so taking the worker down would turn a notification
            # problem into an outage.
            log.error(
                "slack.client.unavailable",
                error=str(exc),
                detail="install slack-sdk[optional] or aiohttp to enable replies",
            )
            return
        self._client = AsyncWebClient(token=bot_token)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def post(self, *, channel: str, text: str, thread_ts: str | None = None) -> bool:
        """Send a message. Returns False on failure rather than raising."""
        if self._client is None:
            log.info("slack.reply.skipped", channel=channel, reason="no_bot_token")
            return False
        try:
            await self._client.chat_postMessage(
                channel=channel,
                text=format_reply(text),
                thread_ts=thread_ts,
                # Keep the agent's reply in the thread rather than the channel.
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            log.warning("slack.reply.failed", channel=channel, error=str(exc))
            return False
        log.info("slack.reply.sent", channel=channel, thread_ts=thread_ts)
        return True

"""Slack request signature verification.

Implemented directly rather than pulled from the SDK so the gateway can verify
a request without constructing a Bolt app, and so the rule is auditable in one
place. The algorithm is Slack's documented v0 scheme:

    basestring = "v0:{timestamp}:{raw_body}"
    signature  = "v0=" + HMAC_SHA256(signing_secret, basestring).hexdigest()

compared against the `X-Slack-Signature` header in constant time.

Two failure modes matter and both are rejected here: a wrong signature (not
from Slack, or the wrong app), and a stale timestamp (a captured request being
replayed).
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Slack's header names, spelled as Slack sends them. HTTP headers are
# case-insensitive; callers should look them up case-insensitively.
SIGNATURE_HEADER = "X-Slack-Signature"
TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
RETRY_NUM_HEADER = "X-Slack-Retry-Num"
RETRY_REASON_HEADER = "X-Slack-Retry-Reason"

_VERSION = "v0"


class SignatureError(Exception):
    """Raised when a request cannot be attributed to Slack."""


def compute_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    """Return the expected `v0=...` signature for a request."""
    basestring = b"%s:%s:%s" % (_VERSION.encode(), timestamp.encode(), body)
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"{_VERSION}={digest}"


def verify_request(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    max_age_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Verify a Slack request, raising `SignatureError` if it fails.

    Raises rather than returning a bool so a caller cannot accidentally ignore
    the result.
    """
    if not signing_secret:
        raise SignatureError("signing secret is not configured")
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("malformed timestamp header") from exc

    current = int(now if now is not None else time.time())
    # Guard both directions: an old captured request, and one with a
    # far-future timestamp.
    if abs(current - ts) > max_age_seconds:
        raise SignatureError("timestamp outside the accepted window")

    expected = compute_signature(signing_secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature mismatch")


def retry_info(headers: dict[str, str]) -> tuple[int, str | None]:
    """Extract Slack's retry metadata from a request.

    Slack sets these when it is redelivering an event we failed to ack. We log
    them, but never use them to skip work: the event store's primary key is
    what makes a retry idempotent.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get(RETRY_NUM_HEADER.lower())
    try:
        num = int(raw) if raw is not None else 0
    except ValueError:
        num = 0
    return num, lowered.get(RETRY_REASON_HEADER.lower())

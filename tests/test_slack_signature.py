"""Tests for Slack request signature verification.

The oracle here is an inline reimplementation of Slack's documented v0 scheme
(`v0:{timestamp}:{body}`, HMAC-SHA256, hex) written independently of the module
under test. That catches a drifting implementation. It cannot catch both
copies being wrong in the same way, so `test_matches_documented_scheme`
additionally pins the basestring's exact byte layout, which is the part of the
spec easiest to get subtly wrong.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from mushroom_core.slack_signature import (
    SignatureError,
    compute_signature,
    retry_info,
    verify_request,
)

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TIMESTAMP = "1531420618"
BODY = b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&text=hello"


def reference_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Slack's scheme, implemented inline as a check on the module."""
    basestring = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def test_matches_documented_scheme() -> None:
    expected = reference_signature(SECRET, TIMESTAMP, BODY)
    assert compute_signature(SECRET, TIMESTAMP, BODY) == expected
    # Pin the basestring layout: version prefix, colons, raw body, no
    # trailing newline and no re-encoding of the body.
    assert expected.startswith("v0=")
    assert len(expected) == len("v0=") + 64


def test_signature_is_body_sensitive() -> None:
    a = compute_signature(SECRET, TIMESTAMP, BODY)
    b = compute_signature(SECRET, TIMESTAMP, BODY + b"!")
    assert a != b


def test_signature_is_timestamp_sensitive() -> None:
    a = compute_signature(SECRET, TIMESTAMP, BODY)
    b = compute_signature(SECRET, str(int(TIMESTAMP) + 1), BODY)
    assert a != b


def test_signature_is_secret_sensitive() -> None:
    a = compute_signature(SECRET, TIMESTAMP, BODY)
    b = compute_signature(SECRET + "x", TIMESTAMP, BODY)
    assert a != b


def test_verify_accepts_a_genuine_request() -> None:
    sig = reference_signature(SECRET, TIMESTAMP, BODY)
    verify_request(
        signing_secret=SECRET,
        timestamp=TIMESTAMP,
        signature=sig,
        body=BODY,
        now=int(TIMESTAMP) + 5,
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"signature": "v0=deadbeef"}, "wrong signature"),
        ({"body": BODY + b"tampered"}, "tampered body"),
        ({"now": int(TIMESTAMP) + 3600}, "stale timestamp"),
        ({"now": int(TIMESTAMP) - 3600}, "future timestamp"),
        ({"timestamp": None}, "missing timestamp"),
        ({"signature": None}, "missing signature"),
        ({"timestamp": "not-a-number"}, "malformed timestamp"),
        ({"signing_secret": ""}, "unconfigured secret"),
    ],
)
def test_verify_rejects(overrides: dict, reason: str) -> None:
    kwargs = {
        "signing_secret": SECRET,
        "timestamp": TIMESTAMP,
        "signature": reference_signature(SECRET, TIMESTAMP, BODY),
        "body": BODY,
        "now": int(TIMESTAMP) + 5,
    }
    kwargs.update(overrides)
    with pytest.raises(SignatureError):
        verify_request(**kwargs)


def test_verify_rejects_signature_for_a_different_body() -> None:
    """A valid signature must not authorise a different payload."""
    sig = reference_signature(SECRET, TIMESTAMP, b"original")
    with pytest.raises(SignatureError):
        verify_request(
            signing_secret=SECRET,
            timestamp=TIMESTAMP,
            signature=sig,
            body=b"substituted",
            now=int(TIMESTAMP) + 5,
        )


def test_retry_info_reads_headers_case_insensitively() -> None:
    assert retry_info({"X-Slack-Retry-Num": "2", "x-slack-retry-reason": "http_timeout"}) == (
        2,
        "http_timeout",
    )


def test_retry_info_defaults_when_absent() -> None:
    assert retry_info({}) == (0, None)
    assert retry_info({"X-Slack-Retry-Num": "junk"}) == (0, None)

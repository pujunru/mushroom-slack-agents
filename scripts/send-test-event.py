#!/usr/bin/env python
"""Post a correctly-signed fake Slack event to a running gateway.

Lets the whole pipeline be exercised — ingest, queue, agent, reply — without a
Slack workspace or a public tunnel. The signature is computed the same way
Slack computes it, so the gateway's verification is genuinely tested rather
than bypassed.

    ./scripts/send-test-event.py "summarise the readme"
    ./scripts/send-test-event.py --url http://localhost:8080 --retry 1 "hi"
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def build_body(text: str, *, event_id: str, channel: str, user: str, event_type: str) -> bytes:
    now = int(time.time())
    return json.dumps(
        {
            "token": "verification-token-unused",
            "team_id": "T0TEST",
            "api_app_id": "A0TEST",
            "type": "event_callback",
            "event_id": event_id,
            "event_time": now,
            "event": {
                "type": event_type,
                "text": text,
                "user": user,
                "channel": channel,
                "ts": f"{now}.000100",
                "channel_type": "channel",
            },
        }
    ).encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="<@U0BOT> hello from the test script")
    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL", "http://localhost:8080"))
    parser.add_argument(
        "--secret",
        default=os.environ.get("SLACK_SIGNING_SECRET", ""),
        help="defaults to $SLACK_SIGNING_SECRET",
    )
    parser.add_argument("--event-id", default=None, help="reuse one to test deduplication")
    parser.add_argument("--channel", default="C0TEST")
    parser.add_argument("--user", default="U0TEST")
    parser.add_argument("--type", dest="event_type", default="app_mention")
    parser.add_argument("--retry", type=int, default=0, help="set Slack's retry headers")
    parser.add_argument(
        "--bad-signature",
        action="store_true",
        help="send a deliberately invalid signature; the gateway should answer 401",
    )
    args = parser.parse_args()

    if not args.secret:
        print(
            "No signing secret. Set SLACK_SIGNING_SECRET or pass --secret.\n"
            "It must match the gateway's, or the request is correctly rejected.",
            file=sys.stderr,
        )
        return 2

    event_id = args.event_id or f"Ev{uuid.uuid4().hex[:16].upper()}"
    body = build_body(
        args.text,
        event_id=event_id,
        channel=args.channel,
        user=args.user,
        event_type=args.event_type,
    )
    timestamp = str(int(time.time()))
    signature = "v0=" + "0" * 64 if args.bad_signature else sign(args.secret, timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }
    if args.retry:
        headers["X-Slack-Retry-Num"] = str(args.retry)
        headers["X-Slack-Retry-Reason"] = "http_timeout"

    url = args.url.rstrip("/") + "/slack/events"
    print(f"POST {url}")
    print(f"  event_id {event_id}  type {args.event_type}")

    if not url.startswith(("http://", "https://")):
        print(f"--url must be http(s), got {args.url!r}", file=sys.stderr)
        return 2

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        # Scheme is validated above; this is a developer tool pointed at a
        # gateway address the operator supplies.
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            print(f"  {response.status} {response.read().decode()}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"  {exc.code} {exc.read().decode()}")
        # A 401 is the expected result with --bad-signature.
        return 0 if args.bad_signature and exc.code == 401 else 1
    except urllib.error.URLError as exc:
        print(f"  could not reach the gateway: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e packages/mushroom-core -e services/gateway -e services/agent
pip install pytest pytest-asyncio httpx ruff
pytest -q
```

The unit suite needs no services — Postgres, Slack and opencode are all faked.

## Running the real thing

```bash
cp .env.example .env     # fill in Slack credentials
./scripts/dev.sh up
./scripts/dev.sh itest   # the pgmq integration suite, against real Postgres
```

## Before opening a PR

```bash
ruff check . && ruff format --check .
pytest -q
./scripts/dev.sh itest                     # if you touched SQL or the queue client
helm lint deploy/helm/mushroom             # if you touched the chart
```

## Things worth knowing

**The pgmq client is the sharp edge.** pgmq overloads its functions, and the
overloads differ in *return shape*, not just argument types — `pgmq.delete`
returns `BOOLEAN` for a single `msg_id` and `SETOF BIGINT` for a `msg_ids`
array. Named arguments are used deliberately to pin the overload. If you
change a call in `queue.py`, run the integration suite; the unit tests fake
the queue and will not catch it.

**Idempotency lives in one place.** `slack_events.event_id` is the primary key
and that is the whole deduplication mechanism. Slack Bolt does not dedupe
retries. Do not add a second mechanism; fix this one if it is wrong.

**The 3-second budget is real.** Slack retries deliveries it does not get an
ack for, and eventually disables the subscription. Nothing slow belongs in the
ingest handler — no Slack API calls, no model calls. Put it in a job.

**Never ack before the work is recorded.** A worker deletes its queue message
only after `complete_job`. Moving that earlier turns a crash into lost work.

**The self-reply guard matters.** An agent that posts into a channel it also
watches will answer itself forever. `router.route()` drops bot-authored events
first, before anything else. There is a test for it; keep it passing.

## Adding an agent profile

1. Add the name to the router's allowed set, or pass `allowed_agents`.
2. Give the worker a branch on `job.agent` to vary the prompt, model or
   workspace.
3. Add a test in `tests/test_router.py` for the directive.

Users select it with `@bot !yourprofile do the thing`.

## Version pins

Three upstream artifacts are stale in ways that look authoritative. Do not
"update" these to a floating tag without reading
[docs/verification.md](docs/verification.md):

- pgmq is pinned to `v1.13.1`; its README quickstart still shows v1.10.0.
- opencode is pinned to `@opencode/cli@2.1.1`; `ghcr.io/anomalyco/opencode:latest`
  still serves the v1 line.
- ESO manifests use `external-secrets.io/v1`; `v1beta1` is no longer served.

## Commit style

Short imperative subject, body explaining *why*. Reference an issue when there
is one.

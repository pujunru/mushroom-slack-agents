"""Run opencode as a subprocess.

Targets **opencode v2** (`@opencode/cli`, org `anomalyco`). Three v2 specifics
drive this module:

- `--standalone` runs a private foreground server for the invocation instead
  of using the shared background service. Without it, the provider credential
  from this process's environment never reaches the process that makes model
  requests, and the run fails to authenticate. This is also what upstream's
  own CI recipe uses.
- `--format json` emits newline-delimited JSON events rather than prose. The
  event schema is not documented on the CLI page, so parsing here is
  defensive: known shapes are extracted, anything unrecognised is retained as
  raw text rather than dropped.
- There is no official Python SDK for v2, so the CLI is the integration point.

The agent runs unattended, so approval prompts must never block it. Approval
mode is configured in the image's `cli.json` (`session.permissions:
autoaccept`); see `images/agent/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field

from mushroom_core.config import AgentConfig
from mushroom_core.logging import get_logger

log = get_logger(__name__)


@dataclass
class OpencodeResult:
    """Outcome of one opencode invocation."""

    exit_code: int
    # Human-readable text to send back to Slack.
    text: str
    # Raw stdout, kept for debugging a surprising result.
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    events: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class OpencodeError(Exception):
    """Raised when opencode cannot be invoked at all."""


def build_command(
    prompt: str,
    cfg: AgentConfig,
    *,
    session_id: str | None = None,
    json_output: bool = True,
) -> list[str]:
    """Assemble the argv for a headless run.

    The prompt is passed as a single argv element, never through a shell, so
    message text cannot be interpreted as shell syntax.
    """
    cmd = [cfg.opencode_binary, "run", "--standalone"]
    if json_output:
        cmd += ["--format", "json"]
    if cfg.opencode_model:
        cmd += ["--model", cfg.opencode_model]
    if session_id:
        # Continue a prior conversation, so a Slack thread maps to a session.
        cmd += ["--session", session_id]
    cmd.append(prompt)
    return cmd


def parse_json_events(stdout: str) -> tuple[list[dict], str]:
    """Parse newline-delimited JSON events into (events, assistant_text).

    The v2 event schema is not documented on the CLI page, so this tolerates
    unknown shapes: each line that parses as a JSON object is kept, and text is
    harvested from the field names opencode is observed to use. Lines that are
    not JSON are treated as plain output, which means a format change degrades
    to "show the user the raw output" rather than losing the answer.
    """
    events: list[dict] = []
    text_parts: list[str] = []
    plain_lines: list[str] = []

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            plain_lines.append(line)
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            plain_lines.append(line)
            continue
        if not isinstance(event, dict):
            plain_lines.append(line)
            continue

        events.append(event)
        text_parts.extend(_extract_text(event))

    if text_parts:
        return events, "\n".join(text_parts).strip()
    # No recognised text field: fall back to whatever was printed.
    return events, "\n".join(plain_lines).strip()


def _extract_text(event: dict) -> list[str]:
    """Pull assistant-visible text out of one event, defensively."""
    found: list[str] = []

    # Shape A: {"type": "...", "text": "..."}
    text = event.get("text")
    if isinstance(text, str) and text.strip():
        found.append(text)

    # Shape B: {"part": {"type": "text", "text": "..."}}
    part = event.get("part")
    if isinstance(part, dict):
        part_text = part.get("text")
        if isinstance(part_text, str) and part_text.strip():
            found.append(part_text)

    # Shape C: {"message": {"content": [{"type": "text", "text": "..."}]}}
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            found.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_text = block.get("text")
                    if isinstance(block_text, str) and block_text.strip():
                        found.append(block_text)

    return found


def _subprocess_env(cfg: AgentConfig) -> dict[str, str]:
    """Environment for the child, carrying the model credential.

    v2 reads a supported provider variable from the process that serves model
    requests and does not persist it, which is exactly what we want in a
    container: no `auth.json`, no `opencode auth login` (that needs a TTY).
    """
    env = os.environ.copy()
    key = cfg.model_api_key.get_secret_value()
    if key:
        # The variable *name* is configuration, not a constant: opencode reads
        # a provider-specific one, so a model change from openrouter/... to
        # anthropic/... also changes which variable must be set.
        env[cfg.model_api_key_env] = key
    # Point HOME at the writable workspace, where the entrypoint wrote
    # opencode's config.
    #
    # This is an unconditional override, not a setdefault. The container image
    # creates its user with --no-create-home, so HOME is already set to a
    # directory that does not exist — a setdefault silently keeps that value,
    # opencode finds no config (including `permissions: autoaccept`), and the
    # run hangs waiting for an approval prompt that can never be answered.
    env["HOME"] = cfg.workspace_root
    return env


async def run_opencode(
    prompt: str,
    cfg: AgentConfig,
    *,
    cwd: str | None = None,
    session_id: str | None = None,
    timeout_seconds: int | None = None,
) -> OpencodeResult:
    """Invoke opencode once and collect its output.

    On timeout the process group is killed and `timed_out` is set, so a hung
    model call cannot hold a queue message's visibility lease forever.
    """
    if shutil.which(cfg.opencode_binary) is None:
        raise OpencodeError(f"opencode binary not found on PATH: {cfg.opencode_binary}")

    cmd = build_command(prompt, cfg, session_id=session_id)
    timeout = timeout_seconds or cfg.job_timeout_seconds
    workdir = cwd or cfg.workspace_root

    log.info(
        "opencode.start",
        model=cfg.opencode_model,
        cwd=workdir,
        timeout_seconds=timeout,
        prompt_chars=len(prompt),
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        # stdin MUST be closed, not inherited. opencode keeps reading stdin
        # after it has finished answering, so an inherited descriptor makes it
        # hang forever — and because it has already written its output to the
        # pipe, the symptom is a silent timeout with zero bytes on both
        # streams rather than an error. This was the whole of a production
        # hang; `opencode run ... </dev/null` returns in ~3s.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=_subprocess_env(cfg),
        # New process group, so a timeout kills opencode's children too.
        start_new_session=True,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        await _terminate(process)
        log.warning("opencode.timeout", timeout_seconds=timeout)
        return OpencodeResult(
            exit_code=124,
            text=f"The agent run exceeded its {timeout}s budget and was stopped.",
            timed_out=True,
        )

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    events, text = parse_json_events(stdout)
    exit_code = process.returncode if process.returncode is not None else -1

    if exit_code != 0:
        log.warning(
            "opencode.failed",
            exit_code=exit_code,
            stderr_tail=stderr[-2000:],
        )
    else:
        log.info("opencode.finished", exit_code=exit_code, events=len(events))

    return OpencodeResult(
        exit_code=exit_code,
        text=text or stderr.strip() or "(no output)",
        stdout=stdout,
        stderr=stderr,
        events=events,
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a process, escalating to SIGKILL if it ignores SIGTERM."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()

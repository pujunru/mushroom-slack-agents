"""Tests for the opencode v2 runner.

The event schema of `--format json` is not documented upstream, so the parser
is written defensively and these tests pin that behaviour: recognised shapes
are extracted, unrecognised output is preserved rather than lost.
"""

from __future__ import annotations

import json

import pytest
from mushroom_agent.opencode import build_command, parse_json_events
from mushroom_core.config import AgentConfig


def cfg(**kw) -> AgentConfig:
    defaults = {
        "opencode_binary": "opencode",
        "opencode_model": "openrouter/deepseek/deepseek-v4-flash-0731",
    }
    defaults.update(kw)
    return AgentConfig(**defaults)


# --- command construction -------------------------------------------------


def test_command_uses_standalone_for_unattended_runs() -> None:
    """--standalone is what gets the provider credential to the model process."""
    cmd = build_command("hello", cfg())
    assert cmd[:3] == ["opencode", "run", "--standalone"]


def test_command_requests_json_output_and_model() -> None:
    cmd = build_command("hello", cfg())
    assert "--format" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "openrouter/deepseek/deepseek-v4-flash-0731"


def test_prompt_is_the_final_argv_element() -> None:
    """Passed as one argv element, so message text is never shell-parsed."""
    prompt = "rm -rf / ; echo $(whoami) `id` && :"
    cmd = build_command(prompt, cfg())
    assert cmd[-1] == prompt
    assert len([a for a in cmd if a == prompt]) == 1


def test_session_continuation_is_optional() -> None:
    assert "--session" not in build_command("x", cfg())
    cmd = build_command("x", cfg(), session_id="ses_123")
    assert cmd[cmd.index("--session") + 1] == "ses_123"


def test_json_output_can_be_disabled() -> None:
    assert "--format" not in build_command("x", cfg(), json_output=False)


# --- event parsing --------------------------------------------------------


def test_parses_top_level_text_events() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "text", "text": "First line."}),
            json.dumps({"type": "text", "text": "Second line."}),
        ]
    )
    events, text = parse_json_events(stdout)
    assert len(events) == 2
    assert text == "First line.\nSecond line."


def test_parses_part_wrapped_text() -> None:
    stdout = json.dumps({"type": "part.updated", "part": {"type": "text", "text": "Hi"}})
    events, text = parse_json_events(stdout)
    assert text == "Hi"
    assert len(events) == 1


def test_parses_message_content_blocks() -> None:
    stdout = json.dumps(
        {
            "message": {
                "content": [
                    {"type": "text", "text": "Block A"},
                    {"type": "text", "text": "Block B"},
                ]
            }
        }
    )
    _, text = parse_json_events(stdout)
    assert text == "Block A\nBlock B"


def test_parses_message_content_string() -> None:
    stdout = json.dumps({"message": {"content": "Plain content"}})
    _, text = parse_json_events(stdout)
    assert text == "Plain content"


def test_unrecognised_json_is_kept_as_an_event_without_text() -> None:
    """A schema change must not crash the worker."""
    stdout = json.dumps({"type": "tool.invoked", "tool": {"name": "bash"}})
    events, text = parse_json_events(stdout)
    assert len(events) == 1
    assert text == ""


def test_non_json_output_is_preserved_as_text() -> None:
    """If --format json is unsupported or output is prose, show the user the
    output rather than silently returning nothing."""
    stdout = "I could not parse this as JSON\nbut it is still the answer"
    events, text = parse_json_events(stdout)
    assert events == []
    assert "still the answer" in text


def test_mixed_json_and_plain_prefers_recognised_text() -> None:
    stdout = "\n".join(
        [
            "warning: something on stderr-ish stdout",
            json.dumps({"type": "text", "text": "The real answer"}),
        ]
    )
    _, text = parse_json_events(stdout)
    assert text == "The real answer"


def test_malformed_json_line_does_not_discard_other_output() -> None:
    stdout = "\n".join(['{"type": "text", "text": "kept"}', '{"truncated'])
    events, text = parse_json_events(stdout)
    assert len(events) == 1
    assert "kept" in text


def test_blank_and_empty_output() -> None:
    assert parse_json_events("") == ([], "")
    assert parse_json_events("\n  \n") == ([], "")


def test_json_array_line_is_not_treated_as_an_event() -> None:
    stdout = json.dumps(["not", "an", "object"])
    events, text = parse_json_events(stdout)
    assert events == []
    assert "not" in text


def test_whitespace_only_text_fields_are_ignored() -> None:
    stdout = json.dumps({"type": "text", "text": "   "})
    events, text = parse_json_events(stdout)
    assert len(events) == 1
    assert text == ""


# --- missing binary -------------------------------------------------------


async def test_missing_binary_raises_opencode_error() -> None:
    from mushroom_agent.opencode import OpencodeError, run_opencode

    with pytest.raises(OpencodeError, match="not found on PATH"):
        await run_opencode("hi", cfg(opencode_binary="definitely-not-installed-xyz"))


# --- provider credential wiring -------------------------------------------


def test_credential_is_exported_under_the_configured_variable() -> None:
    """The variable name is configuration, not a constant.

    opencode reads a provider-specific variable, so switching the model from
    openrouter/... to anthropic/... must also change which variable carries
    the key. Hardcoding one provider's name is what this guards against.
    """
    from mushroom_agent.opencode import _subprocess_env
    from pydantic import SecretStr

    env = _subprocess_env(
        cfg(
            model_api_key=SecretStr("sk-or-v1-example"),
            model_api_key_env="OPENROUTER_API_KEY",
        )
    )
    assert env["OPENROUTER_API_KEY"] == "sk-or-v1-example"
    assert "ANTHROPIC_API_KEY" not in env


def test_credential_variable_follows_the_provider() -> None:
    from mushroom_agent.opencode import _subprocess_env
    from pydantic import SecretStr

    env = _subprocess_env(
        cfg(
            opencode_model="anthropic/claude-sonnet-4.5",
            model_api_key=SecretStr("sk-ant-example"),
            model_api_key_env="ANTHROPIC_API_KEY",
        )
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-example"
    assert "OPENROUTER_API_KEY" not in env


def test_no_credential_variable_when_unset() -> None:
    """An empty key must not export an empty variable, which would look
    configured to opencode and fail with a confusing auth error."""
    from mushroom_agent.opencode import _subprocess_env

    env = _subprocess_env(cfg())
    assert "OPENROUTER_API_KEY" not in env


def test_home_is_overridden_not_defaulted() -> None:
    """HOME must be forced to the workspace, even when already set.

    The image creates its user with --no-create-home, so the container starts
    with HOME pointing at a directory that does not exist. A setdefault keeps
    that value, opencode then finds none of its config — including the
    unattended approval mode — and the run hangs on a prompt nobody can
    answer. This is what that looked like in production.
    """
    import os

    from mushroom_agent.opencode import _subprocess_env

    os.environ["HOME"] = "/home/does-not-exist"
    try:
        env = _subprocess_env(cfg(workspace_root="/workspace"))
    finally:
        os.environ.pop("HOME", None)
    assert env["HOME"] == "/workspace"


async def test_stdin_is_closed_not_inherited(monkeypatch) -> None:
    """opencode must be spawned with stdin at DEVNULL.

    It keeps reading stdin after answering, so an inherited descriptor hangs
    the run forever. The output has already been written by then, so the
    failure looks like a silent timeout with zero bytes on both streams — not
    an error anyone can diagnose from the logs. This is what took a
    production debugging session.
    """
    import asyncio

    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b'{"type":"text","text":"ok"}', b""

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/opencode")

    from mushroom_agent.opencode import run_opencode

    result = await run_opencode("hi", cfg())

    assert captured["stdin"] is asyncio.subprocess.DEVNULL
    assert result.ok


# --- session continuity ----------------------------------------------------


def test_session_id_is_extracted_from_the_event_stream() -> None:
    """opencode reports its session on every event; we need it to resume."""
    import json

    from mushroom_agent.opencode import extract_session_id, parse_json_events

    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_abc123"}),
            json.dumps({"type": "text", "text": "hi", "sessionID": "ses_abc123"}),
        ]
    )
    events, _ = parse_json_events(stdout)
    assert extract_session_id(events) == "ses_abc123"


def test_session_id_found_inside_a_part() -> None:
    """Some events carry it only on the nested part."""
    from mushroom_agent.opencode import extract_session_id

    assert extract_session_id([{"part": {"sessionID": "ses_nested"}}]) == "ses_nested"


def test_no_session_id_is_not_an_error() -> None:
    from mushroom_agent.opencode import extract_session_id

    assert extract_session_id([{"type": "text", "text": "x"}]) is None
    assert extract_session_id([]) is None


def test_resuming_passes_the_session_flag() -> None:
    """A follow-up must continue the thread's conversation, not start one."""
    cmd = build_command("follow up", cfg(), session_id="ses_prior")
    assert cmd[cmd.index("--session") + 1] == "ses_prior"


def test_first_turn_has_no_session_flag() -> None:
    assert "--session" not in build_command("first", cfg())

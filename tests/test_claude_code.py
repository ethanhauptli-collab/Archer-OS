"""--provider claude-code: Claude through Claude Code, on the user's Pro/Max plan.

A fake `claude` program stands in for Claude Code. It records its command
lines and environment, so the tests can check that no API key ever reaches
it (Claude Code would bill an API key instead of the subscription).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import CANNED_PLAN

from roughcut.cli import main
from roughcut.providers import ImagePart, ProviderAuthError, ProviderError, TextPart, make_provider
from roughcut.providers.claude_code import ClaudeCodeProvider

FAKE_CLAUDE = r'''#!{python}
import json, os, sys
state = json.load(open(os.environ["FAKE_CLAUDE_STATE"]))
with open(os.environ["FAKE_CLAUDE_STATE"] + ".log", "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "api_key": "ANTHROPIC_API_KEY" in os.environ,
                          "stdin": "" if sys.argv[1:3] == ["auth", "status"] else sys.stdin.read()}}) + "\n")
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps(state["auth"]))
    sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
if "clips" in schema.get("properties", {{}}):
    out = {{"clips": []}}
else:
    out = state["plan"]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                  "structured_output": out, "usage": {{"input_tokens": 1200, "output_tokens": 300}}}}))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "claude-state.json"
    state.write_text(json.dumps({"auth": {"loggedIn": True, "authMethod": "claude.ai"}, "plan": CANNED_PLAN}))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-would-bill-the-api-not-the-plan")

    def calls():
        log = Path(str(state) + ".log")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def set_auth(**auth):
        data = json.loads(state.read_text())
        data["auth"] = auth
        state.write_text(json.dumps(data))

    return type("Fake", (), {"calls": staticmethod(calls), "set_auth": staticmethod(set_auth), "path": str(exe)})


def test_build_runs_on_the_subscription(fake_claude, footage, tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["build", str(footage), "--provider", "claude-code", "--model", "Sonnet 5.5", "--transcriber", "none",
                 "-o", str(out), "--cache-dir", str(tmp_path / "cache")])
    assert code == 0, capsys.readouterr()
    calls = fake_claude.calls()
    assert calls[0]["argv"] == ["auth", "status", "--json"]
    assert not any(c["api_key"] for c in calls)  # never billed to an API key
    plan_call = next(c for c in calls if "--json-schema" in c["argv"] and '"sections"' in c["argv"][c["argv"].index("--json-schema") + 1])
    argv = plan_call["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == ""  # planning needs no tools
    assert "--no-session-persistence" in argv and "--system-prompt" in argv
    assert "V01.S001" in plan_call["stdin"]  # transcripts go in on stdin, not the command line
    assert "Mountain Day" in (out / "edit_report.md").read_text()


def test_signed_out_or_api_key_sign_in_stops_before_analysis(fake_claude):
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    fake_claude.set_auth(loggedIn=False)
    with pytest.raises(ProviderAuthError, match="/login"):
        provider.verify()
    fake_claude.set_auth(loggedIn=True, authMethod="api_key")
    with pytest.raises(ProviderAuthError, match="bill API credit"):
        provider.verify()
    fake_claude.set_auth(loggedIn=True, authMethod="oauth_token")  # `claude setup-token`
    assert "Claude Code" in provider.verify()


def test_images_are_written_out_for_claude_code_to_read(fake_claude):
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    schema = {"type": "object", "properties": {"clips": {"type": "array"}}}
    provider.complete_json("sys", [TextPart("Clip V02:"), ImagePart(b"\xff\xd8jpeg", "image/jpeg")], schema, schema_name="clip_log", purpose="visual log")
    call = fake_claude.calls()[-1]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "Read" and "--add-dir" in argv
    assert "[image: " in call["stdin"] and ".jpg]" in call["stdin"]


@pytest.mark.parametrize(
    "result,error",
    [
        ("Invalid API key · Please run /login", ProviderAuthError),
        ("Claude usage limit reached. Your limit will reset at 5pm.", ProviderError),
        ("Something else went wrong", ProviderError),
    ],
)
def test_failures_are_explained(result, error):
    class Proc:
        returncode = 1
        stdout = json.dumps({"type": "result", "is_error": True, "result": result})
        stderr = ""

    provider = ClaudeCodeProvider(binary="/bin/true", run=lambda *a, **k: Proc())
    with pytest.raises(error) as e:
        provider.complete_json("s", [TextPart("x")], {"type": "object"}, schema_name="plan", purpose="edit plan")
    if "limit" in result:
        assert "usage limit" in str(e.value)


def test_plain_text_json_reply_still_parses():
    class Proc:
        returncode = 0
        stdout = json.dumps({"type": "result", "is_error": False, "result": '```json\n{"title": "x"}\n```'})
        stderr = ""

    provider = ClaudeCodeProvider(binary="/bin/true", run=lambda *a, **k: Proc())
    assert provider.complete_json("s", [TextPart("x")], {"type": "object"}, schema_name="plan", purpose="edit plan") == {"title": "x"}
    assert provider.usage.calls[0].model.endswith("(subscription)")  # no API cost estimate


def test_missing_claude_code_says_how_to_install(monkeypatch):
    monkeypatch.setattr("roughcut.providers.claude_code.find_claude", lambda: None)
    with pytest.raises(ProviderError, match="isn't installed"):
        make_provider("claude-code")

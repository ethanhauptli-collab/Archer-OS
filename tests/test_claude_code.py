"""--provider claude-code: Claude through Claude Code, on the user's Pro/Max plan.

A fake `claude` program stands in for Claude Code. It records its command
lines and environment, so the tests can check that no API key ever reaches
it (Claude Code would bill an API key instead of the subscription).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import CANNED_PLAN

from roughcut.cli import main
from roughcut.providers import ImagePart, ProviderAuthError, ProviderError, TextPart, make_provider
from roughcut.providers.claude_code import ClaudeCodeProvider, parse_version, picture_args, version_problem

FAKE_CLAUDE = r'''#!{python}
import json, os, re, sys
state = json.load(open(os.environ["FAKE_CLAUDE_STATE"]))
quiet = sys.argv[1:3] == ["auth", "status"] or sys.argv[1:] == ["--version"]
stdin = "" if quiet else sys.stdin.read()
images = re.findall(r"^\[image: (.*)\]$", stdin, re.M)
with open(os.environ["FAKE_CLAUDE_STATE"] + ".log", "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "api_key": "ANTHROPIC_API_KEY" in os.environ, "stdin": stdin, "cwd": os.getcwd(),
                          "images": [{{"path": p, "exists": os.path.exists(p), "real": os.path.realpath(p) == p, "bytes": os.path.getsize(p) if os.path.exists(p) else None}} for p in images],
                          "staged": sorted(os.listdir(os.getcwd()))}}) + "\n")
if sys.argv[1:] == ["--version"]:
    print(state.get("version", "2.1.299") + " (Claude Code)")
    sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps(state["auth"]))
    sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
if "clips" in schema.get("properties", {{}}):
    out = {{"clips": []}}
else:
    out = state["plan"]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                  "structured_output": out, "usage": {{"input_tokens": 1200, "output_tokens": 300}},
                  "permission_denials": state.get("denials", [])}}))
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

    def set_state(**fields):
        data = json.loads(state.read_text())
        data.update(fields)
        state.write_text(json.dumps(data))

    return type("Fake", (), {"calls": staticmethod(calls), "set_auth": staticmethod(set_auth), "set_state": staticmethod(set_state), "path": str(exe)})


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


SCHEMA = {"type": "object", "properties": {"clips": {"type": "array"}}}


def _ask_with_a_picture(provider, data=b"\xff\xd8jpeg", media_type="image/jpeg"):
    return provider.complete_json("sys", [TextPart("Clip V02:"), ImagePart(data, media_type)], SCHEMA, schema_name="clip_log", purpose="visual log")


def test_images_are_written_out_for_claude_code_to_read(fake_claude):
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    _ask_with_a_picture(provider)
    call = fake_claude.calls()[-1]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == "Read"
    assert "[image: " in call["stdin"] and ".jpg]" in call["stdin"]
    (image,) = call["images"]
    assert image["exists"] and image["bytes"] == len(b"\xff\xd8jpeg")  # the picture's own bytes
    assert call["staged"] == ["image-000.jpg"]  # and nothing else is in the folder Claude Code runs in
    assert os.path.dirname(image["path"]) == call["cwd"]


def test_a_picture_call_can_read_only_the_pictures_folder(fake_claude):
    """The finding: `--allowedTools Read` approved a read of any path, so a picture that told Claude to open
    another file could send that file's text to the model. The call is now boxed in to the folder the pictures
    are in, with controls that refuse (never ask) and that the owner's own settings can't widen."""
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    _ask_with_a_picture(provider)
    call = fake_claude.calls()[-1]
    argv = call["argv"]
    assert "--allowedTools" not in argv and "--allowed-tools" not in argv  # an allow rule is not a boundary
    assert "--add-dir" not in argv  # the working directory is the pictures' folder, nothing is added to it
    assert "--restricted" in argv  # file tools confined to the working directory; user, project and local settings ignored
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"  # what would ask is denied...
    assert argv[argv.index("--permission-prompts") + 1] == "none"  # ...and nobody is asked
    assert "Bash" in argv[argv.index("--disallowedTools") + 1].split(",")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert "Read(~/.ssh/**)" in settings["permissions"]["deny"] and "allow" not in settings["permissions"]
    assert "Read only those picture files" in call["stdin"]  # and the prompt says so


def test_text_only_calls_keep_their_tools_off_and_their_command_line(fake_claude):
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    provider.complete_json("sys", [TextPart("plan this")], SCHEMA, schema_name="plan", purpose="edit plan")
    (call,) = [c for c in fake_claude.calls() if "--json-schema" in c["argv"]]
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert not {"--restricted", "--settings", "--permission-mode", "--add-dir", "--allowedTools"} & set(argv)
    assert not [c for c in fake_claude.calls() if c["argv"] == ["--version"]]  # no version probe: nothing to confine


def test_pictures_are_staged_by_real_path_so_the_folder_check_accepts_them(fake_claude, tmp_path, monkeypatch):
    """macOS temp folders sit behind /var -> /private/var, and Claude Code refuses a picture reached through such a link."""
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    _ask_with_a_picture(provider)
    (image,) = fake_claude.calls()[-1]["images"]
    assert image["real"] and image["path"].startswith(str(real.resolve()))


def test_picture_args_name_every_control_and_skip_a_secret_place_the_pictures_are_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".config" / "tmp").mkdir(parents=True)
    inside_config = str(tmp_path / "home" / ".config" / "tmp")
    elsewhere = str(tmp_path / "scratch")
    os.mkdir(elsewhere)
    deny = json.loads(picture_args(elsewhere)[picture_args(elsewhere).index("--settings") + 1])["permissions"]["deny"]
    assert {"Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.claude/**)", "Read(~/.config/**)", "Read(~/Library/**)", "Read(~/.claude.json)", "Read(~/.netrc)"} <= set(deny)
    # a pictures folder that happens to be under one of them isn't locked out of its own files
    args = picture_args(inside_config)
    assert "Read(~/.config/**)" not in json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]
    assert "Read(~/.ssh/**)" in json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]


@pytest.mark.parametrize(
    "text,version",
    [("2.1.299 (Claude Code)\n", (2, 1, 299)), ("2.1.259", (2, 1, 259)), ("claude 2.1.300-beta.2", (2, 1, 300)), ("", None), ("unknown", None)],
)
def test_claude_code_versions_are_read(text, version):
    assert parse_version(text) == version


def test_the_boundary_needs_the_minimum_version_and_says_so():
    assert version_problem((2, 1, 259)) is None and version_problem((2, 2, 0)) is None and version_problem((3, 0, 0)) is None
    assert "2.1.258" in version_problem((2, 1, 258)) and "claude update" in version_problem((2, 1, 258))
    assert "claude update" in version_problem(None)  # a version it can't read is not trusted either


def test_an_older_claude_code_is_shown_no_pictures(fake_claude, capsys):
    fake_claude.set_state(version="2.1.200")
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    assert provider.supports_vision is False and provider.supports_vision is False
    assert "older than 2.1.259" in capsys.readouterr().err  # said once, in the log
    with pytest.raises(ProviderError, match="claude update"):
        _ask_with_a_picture(provider)
    assert len([c for c in fake_claude.calls() if c["argv"] == ["--version"]]) == 1  # looked up once
    assert not [c for c in fake_claude.calls() if "--json-schema" in c["argv"]]  # no request went out, with or without the flags
    # words alone still go through
    assert provider.complete_json("sys", [TextPart("plan this")], {"type": "object", "properties": {}}, schema_name="plan", purpose="edit plan")


def test_a_version_that_cant_be_read_fails_closed(fake_claude):
    fake_claude.set_state(version="not a version")
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    assert provider.supports_vision is False
    with pytest.raises(ProviderError, match="which version"):
        _ask_with_a_picture(provider)


def test_the_minimum_version_gets_pictures(fake_claude):
    fake_claude.set_state(version="2.1.259")
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    assert provider.supports_vision is True
    _ask_with_a_picture(provider)


def test_refused_reads_are_reported_not_fatal(fake_claude, capsys):
    """Claude Code lists what it refused as `permission_denials`: the editor is told, the answer is still used."""
    outside = "/home/someone/.ssh/id_rsa"
    fake_claude.set_state(denials=[{"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": outside}}])
    provider = ClaudeCodeProvider(binary=fake_claude.path)
    _ask_with_a_picture(provider)
    assert provider.refused_reads == [outside]
    err = capsys.readouterr().err
    assert "refused 1 read outside the pictures" in err and outside in err and "visual log" in err


def test_a_refused_picture_is_reported_as_unseen(fake_claude, capsys):
    def run(cmd, **kw):
        proc = subprocess.run(cmd, **kw)
        if "--json-schema" in cmd:  # Claude Code says it would not open the picture it was given
            data = json.loads(proc.stdout)
            data["permission_denials"] = [{"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"file_path": os.path.join(kw["cwd"], "image-000.jpg")}}]
            proc.stdout = json.dumps(data)
        return proc

    provider = ClaudeCodeProvider(binary=fake_claude.path, run=run)
    _ask_with_a_picture(provider)
    err = capsys.readouterr().err
    assert "would not open 1 of the 1 pictures" in err and "without seeing them" in err
    assert "outside the pictures" not in err


def test_doctor_says_what_claude_code_version_it_found(fake_claude):
    from roughcut.cli import _claude_code_detail, claude_code_report

    report = claude_code_report(offline=True)
    assert report["claude_code_version"] == "2.1.299" and report["claude_code_pictures"] is True
    assert "pictures" not in _claude_code_detail({**report, "claude_code_auth": "claude.ai"})
    fake_claude.set_state(version="2.1.100")
    old = claude_code_report(offline=True)
    assert old["claude_code_pictures"] is False
    detail = _claude_code_detail({**old, "claude_code_auth": "claude.ai"})
    assert "2.1.100" in detail and "2.1.259" in detail and "claude update" in detail
    assert "not installed" in _claude_code_detail({"claude_code": None}) or "install" in _claude_code_detail({"claude_code": None})


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

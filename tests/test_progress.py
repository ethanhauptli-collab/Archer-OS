"""The JSON-lines contract the Mac app depends on (--progress-json, doctor --json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeProvider

from roughcut import pipeline, progress
from roughcut.cli import main


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]  # every stdout line must be JSON
    assert all("event" in e for e in events)
    return events


def test_build_progress_stream(footage, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--progress-json"])
    assert code == 0
    events = _events(capsys)
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    order = [s for s in progress.STAGES if s in stages]
    assert order == ["scan", "probe", "silence", "transcribe", "vision", "plan", "cut", "write"]
    # Stages only move forward (the app ticks them off in order).
    idx = [progress.STAGES.index(s) for s in stages]
    assert idx == sorted(idx)
    tr = [e for e in events if e.get("stage") == "transcribe"]
    assert tr[-1]["current"] == tr[-1]["total"]
    assert any(e["event"] == "log" for e in events)

    done = events[-1]
    assert done["event"] == "done"
    r = done["result"]
    assert Path(r["fcpxml"]).is_file() and Path(r["report"]).is_file()
    assert r["title"] == "Mountain Day" and r["sections"][:3] == ["Cold open", "Heading out", "Wrap"]
    assert r["rough_seconds"] > 0 and r["stringout_seconds"] > 0 and r["edits"] > 0 and r["broll"] >= 1
    assert r["fell_back"] is False and any("V09.S001" in w for w in r["warnings"])


def test_render_progress_stream(footage, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 0
    capsys.readouterr()
    assert main(["render", str(out), "--style", "tight", "--format", "vertical", "--progress-json"]) == 0
    events = _events(capsys)
    assert [e["stage"] for e in events if e["event"] == "stage"] == ["cut", "write"]
    assert events[-1]["event"] == "done" and events[-1]["result"]["fcpxml"].endswith(".fcpxml")


def test_errors_are_reported_as_events(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", str(empty), "--provider", "none", "--transcriber", "none", "--progress-json"]) == 1
    events = _events(capsys)
    assert events[-1]["event"] == "error" and "No media files" in events[-1]["message"]

    assert main(["render", str(tmp_path), "--progress-json"]) == 1
    events = _events(capsys)
    assert events[-1]["event"] == "error" and "output folder" in events[-1]["message"]


def test_plain_mode_prints_no_json(footage, tmp_path, capsys):
    assert main(["build", str(footage), "-o", str(tmp_path / "o"), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 0
    out = capsys.readouterr().out
    assert "Rough cut:" in out and '"event"' not in out


def test_doctor_json(capsys, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    for key in ("version", "python", "executable", "ffmpeg", "ffprobe", "transcriber", "anthropic_key", "anthropic_key_status",
                "anthropic_key_message", "openai_key", "openai_installed"):
        assert key in report
    assert report["anthropic_key_status"] == "missing" and "console.anthropic.com" in report["anthropic_key_message"]


def test_doctor_offline_does_not_call_anthropic(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # would fail if contacted
    assert main(["doctor", "--json", "--offline"]) == 0
    assert json.loads(capsys.readouterr().out)["anthropic_key_status"] == "not checked"


def test_swift_fixture_matches_the_cli_contract():
    """mac/Tests/.../progress-sample.jsonl is decoded by the Swift app's tests.

    If the CLI's done/stage payloads change shape, this fails here (where
    Python runs) before the Mac app silently stops understanding them.
    """
    from roughcut.pipeline import Result, result_summary

    fixture = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "progress-sample.jsonl"
    events = [json.loads(ln) for ln in fixture.read_text().splitlines() if ln.strip()]
    for e in events:
        if e["event"] == "stage":
            assert e["stage"] in progress.STAGES
    done = events[-1]
    assert done["event"] == "done"

    class _TL:
        seconds = 1.0
        spine = []
        connected = []
        section_starts = []

    class _Plan:
        title = "t"
        source = "s"

    live = result_summary(Result(Path("/o"), Path("/o/x.fcpxml"), Path("/o/r.md"), _Plan(), _TL(), None))
    assert set(done["result"]) == set(live)

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
    # The settings a re-cut keeps unless it changes them (the app's Re-cut starts from these).
    assert r["cut"] == {"style": "medium", "format": None, "keep_fillers": False, "stringout": True, "punch_in": False}
    # result.json in the output folder is the same payload, for a recent-runs list in the app.
    assert json.loads((out / "result.json").read_text()) == r and Path(r["report_html"]).is_file()


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
                "anthropic_key_message", "openai_key", "openai_installed", "venture_studio"):
        assert key in report
    assert report["anthropic_key_status"] == "missing" and "console.anthropic.com" in report["anthropic_key_message"]
    # What the app's Settings row decodes (StudioStatus): no Studio on this machine, so no row.
    studio = report["venture_studio"]
    assert studio["installed"] is False and studio["available"] is False and studio["reason"] == "not installed"
    assert isinstance(studio["summary"], str) and studio["notes"] == []


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


def test_swift_revise_fixture_matches_the_cli_contract():
    """progress-revise-sample.jsonl is a real `roughcut revise --progress-json` stream, kept so the app's tests decode
    a done event whose `changes` is not null (the build sample's is). If the CLI's shape drifts this fails here first."""
    from roughcut.pipeline import Result, result_summary

    fixture = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "progress-revise-sample.jsonl"
    events = [json.loads(ln) for ln in fixture.read_text().splitlines() if ln.strip()]
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    assert stages and all(s in progress.STAGES for s in stages)
    assert stages == sorted(stages, key=progress.STAGES.index)  # only ever forward
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
    changes = done["result"]["changes"]
    assert set(changes) == {"from_version", "items", "unchanged"}  # RunChanges in the app
    assert isinstance(changes["from_version"], int) and done["result"]["version"] == changes["from_version"] + 1
    assert changes["items"] and all(isinstance(i, str) for i in changes["items"]) and changes["unchanged"] is False


def test_done_payload_additions_keep_their_shape():
    """Keys added after the app's first version: what the Swift side should decode (all optional there)."""
    fixture = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "progress-sample.jsonl"
    r = [json.loads(ln) for ln in fixture.read_text().splitlines() if ln.strip()][-1]["result"]
    assert isinstance(r["version"], int) and r["project"].startswith("Trip - Rough Cut v") and r["event"].startswith("Trip (")
    assert r["graphics"] is None or set(r["graphics"]) - {"pending"} == {"planned", "placed", "rendered", "note"}  # "pending" only when above 0
    assert set(r["visual_log"]) == {"described", "total"}
    assert set(r["skipped"]) == {"files", "plan_refs"}
    assert set(r["usage"]) == {"calls", "dollars", "by_purpose", "calls_by_purpose", "subscription_calls", "unpriced_calls"}
    assert r["cut"] == {"style": "medium", "format": None, "keep_fillers": False, "stringout": True, "punch_in": False}  # CutSettings in the app
    assert r["changes"] is None or set(r["changes"]) == {"from_version", "items", "unchanged"}  # RunChanges in the app: null for a first build


def test_swift_fixtures_for_a_stopped_revise_and_for_graphics_that_keep_an_earlier_look_match_the_cli_contract():
    """progress-stopped-revise-sample.jsonl (an `error` event whose `written` names the partial cut a stopped revise left as the
    newest version) and progress-pending-revise-sample.jsonl (a `done` event whose `graphics` has a `pending` count and a note) are
    real `roughcut revise --progress-json` streams, kept for the app to decode: both keys are optional, add-only."""
    from roughcut.pipeline import Result, result_summary

    folder = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures"
    stopped = [json.loads(ln) for ln in (folder / "progress-stopped-revise-sample.jsonl").read_text().splitlines() if ln.strip()]
    assert [e["stage"] for e in stopped if e["event"] == "stage"][0] == "plan" and all(
        e["stage"] in progress.STAGES for e in stopped if e["event"] == "stage"
    )
    error = stopped[-1]
    assert error["event"] == "error" and set(error) == {"event", "message", "resume", "written"}  # no "kind": a crash, not a cancel or a limit
    assert error["resume"].startswith("roughcut render ")
    assert set(error["written"]) == {"version", "fcpxml", "project", "partial"}
    assert error["written"]["partial"] is True and isinstance(error["written"]["version"], int) and error["written"]["fcpxml"].endswith(".fcpxml")

    pending = [json.loads(ln) for ln in (folder / "progress-pending-revise-sample.jsonl").read_text().splitlines() if ln.strip()]
    done = pending[-1]
    assert done["event"] == "done"

    class _TL:
        seconds = 1.0
        spine = []
        connected = []
        section_starts = []

    class _Plan:
        title = "t"
        source = "s"

    assert set(done["result"]) == set(result_summary(Result(Path("/o"), Path("/o/x.fcpxml"), Path("/o/r.md"), _Plan(), _TL(), None)))
    row = done["result"]["graphics"]
    assert set(row) == {"planned", "placed", "rendered", "note", "pending"}
    assert row["rendered"] == 2 and row["pending"] == 2 and "keep their earlier clip" in row["note"]


def test_swift_fixtures_for_a_usage_limit_and_the_saved_results_match_the_cli_contract():
    """The app reads what a stopped run left in two ways, both optional and add-only: the `error` event's `written`
    (progress-limit-revise-sample.jsonl: the usage limit after version 2 was written) and the folder's result.json
    (result-partial-revise-sample.json, a cut written before its graphics; result-pending-revise-sample.json, the finished
    write of that limit). All three are real `roughcut revise --progress-json` output, decoded by the Swift tests."""
    from roughcut.pipeline import Result, result_summary

    folder = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures"
    limit = [json.loads(ln) for ln in (folder / "progress-limit-revise-sample.jsonl").read_text().splitlines() if ln.strip()]
    stages = [e["stage"] for e in limit if e["event"] == "stage"]
    assert stages and all(s in progress.STAGES for s in stages) and stages == sorted(stages, key=progress.STAGES.index)
    error = limit[-1]
    assert error["event"] == "error" and error["kind"] == "usage_limit" and "resume" not in error  # the cut is written: plain render would only repeat it
    assert set(error) == {"event", "message", "written", "kind"}
    assert set(error["written"]) == {"version", "fcpxml", "project", "partial", "design_missing"}
    assert error["written"]["partial"] is False and error["written"]["design_missing"] is True and error["written"]["version"] == 2
    assert "--design-missing" in error["message"]

    class _TL:
        seconds = 1.0
        spine = []
        connected = []
        section_starts = []

    class _Plan:
        title = "t"
        source = "s"

    live = set(result_summary(Result(Path("/o"), Path("/o/x.fcpxml"), Path("/o/r.md"), _Plan(), _TL(), None)))
    partial = json.loads((folder / "result-partial-revise-sample.json").read_text())
    assert set(partial) == live | {"partial"} and partial["partial"] is True  # `partial` is in result.json only, never in a done event
    assert partial["version"] == 2 and partial["fcpxml"] == error["written"]["fcpxml"]
    assert set(partial["graphics"]) == {"planned", "placed", "rendered", "note"}
    assert partial["graphics"]["planned"] > 0 and partial["graphics"]["rendered"] == 0  # nothing made yet: the app offers Finish graphics
    assert partial["graphics"]["note"] == "not made yet: this cut was written before its graphics"
    assert partial["changes"]["items"] == ["Graphics: none in this cut yet; it was written before they were made."]  # not what became of each

    pending = json.loads((folder / "result-pending-revise-sample.json").read_text())
    assert set(pending) == live and "partial" not in pending
    assert set(pending["graphics"]) == {"planned", "placed", "rendered", "note", "pending"} and pending["graphics"]["pending"] == 2
    assert pending["changes"]["items"][0].startswith("Redesigning the graphics: no new design came back")  # not "designed again"
    assert pending["fcpxml"] == error["written"]["fcpxml"] and pending["version"] == error["written"]["version"]


def test_the_apps_keep_both_sentence_is_the_one_the_paper_edit_and_the_terminal_say():
    """RunResult.sameNamesHint (the result screen's import hint for a version after the first) repeats report.SAME_NAMES."""
    from roughcut.report import SAME_NAMES

    swift = (Path(__file__).parents[1] / "mac" / "Sources" / "RoughcutKit" / "StoppedRun.swift").read_text()
    assert f'sameNamesHint = "{SAME_NAMES}"' in swift


def test_every_run_leaves_a_log_that_says_how_it_ended(footage, tmp_path):
    """A run that never finished looked like a finished one (old files, no graphics): the log tells them apart."""
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 0
    text = (out / "roughcut.log").read_text()
    assert "roughcut build started" in text and "FINISHED" in text

    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", str(empty), "-o", str(tmp_path / "out2"), "--provider", "none", "--transcriber", "none"]) == 1
    assert "STOPPED: No media files" in (tmp_path / "out2" / "roughcut.log").read_text()

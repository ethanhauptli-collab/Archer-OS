"""Run lifecycle: saved plans, versions, revise, summaries, stopping cleanly."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd

from roughcut import graphics, pipeline, progress
from roughcut.cli import main
from roughcut.pipeline import Options, RoughcutError
from roughcut.providers import ProviderAuthError

GRAPHIC = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


def _opts(footage, out, cache, **kw) -> Options:
    return Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw)


def _rough(projects: dict) -> dict:
    return next(v for k, v in projects.items() if "Rough Cut" in k)


def _graphics_plan() -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [GRAPHIC]
    return plan


def _events(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------- N2: the paid plan is never lost


def test_plan_is_saved_before_graphics_and_render_finishes_without_the_model(footage, tmp_path, monkeypatch, capsys):
    provider = FakeProvider(_graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def crash(*a, **k):
        raise RuntimeError("the renderer crashed")

    monkeypatch.setattr(graphics, "render", crash)
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics", "--progress-json"])
    assert code == 1
    events = _events(capsys.readouterr().out)
    assert events[-1]["event"] == "error" and events[-1]["resume"] == f"roughcut render {out}"

    saved = json.loads((out / "plan.json").read_text())
    assert saved["plan"]["title"] == "Mountain Day" and saved["analysis_digest"]
    assert saved["settings"]["graphics"] is True and saved["settings"]["style"] == "medium"
    cut = next(out.glob("*.fcpxml"))  # the cut without graphics, written before rendering started
    validate_dtd(cut)
    check_invariants(cut)
    assert "The edit plan was saved" in (out / "roughcut.log").read_text()

    planner_calls = len(provider.calls)
    monkeypatch.setattr(graphics, "render", lambda *a, **k: {})
    again = pipeline.rerender(out, Options(inputs=[]))
    assert len(provider.calls) == planner_calls  # no model calls to finish it
    validate_dtd(again.fcpxml)
    assert again.plan.title == "Mountain Day"


def test_stop_after_the_plan_prints_how_to_finish(footage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "make_timelines", broken)
    out = tmp_path / "my out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 1
    err = capsys.readouterr().err
    assert f"roughcut render '{out}'" in err  # quoted: the path has a space


def test_a_failed_rebuild_leaves_the_previous_plan_working(footage, tmp_path, monkeypatch, fake):
    """IDs follow recording order: an earlier clip renumbers everything after it."""
    src = tmp_path / "footage"
    shutil.copytree(footage, src)
    out, cache = tmp_path / "out", tmp_path / "c"
    pipeline.build(_opts(src, out, cache))
    before = (out / "analysis.json").read_text()

    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-metadata", "creation_time=2026-08-12T07:00:00Z", str(src / "early.mp4"))

    class KeyRevoked(FakeProvider):
        def complete_json(self, *a, **k):
            if k.get("schema_name") == "edit_plan":
                raise ProviderAuthError("rejected")
            return super().complete_json(*a, **k)

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: KeyRevoked())
    with pytest.raises(ProviderAuthError):
        pipeline.build(_opts(src, out, cache))
    assert (out / "analysis.json").read_text() == before  # nothing touched before a new plan existed
    assert pipeline.rerender(out, Options(inputs=[])).plan.title == "Mountain Day"


def test_render_refuses_a_plan_made_for_other_clips(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    data = json.loads((out / "analysis.json").read_text())
    data["clips"][0]["media"]["fingerprint"] = "a-different-file"
    (out / "analysis.json").write_text(json.dumps(data))
    with pytest.raises(RoughcutError, match="different set of clips"):
        pipeline.rerender(out, Options(inputs=[]))


def test_render_keeps_the_builds_cut_settings(footage, tmp_path, fake):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"),
                 "--style", "tight", "--no-stringout"]) == 0
    tight = _rough(check_invariants(next(out.glob("*.fcpxml"))))["seconds"]

    assert main(["render", str(out)]) == 0  # no flags: still tight, still no stringout
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] == tight and not any("Stringout" in k for k in projects)

    assert main(["render", str(out), "--style", "medium", "--stringout"]) == 0
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] > tight and any("Stringout" in k for k in projects)
    assert json.loads((out / "plan.json").read_text())["settings"]["style"] == "medium"  # the next render starts here


def test_a_new_build_without_graphics_drops_the_old_ones(footage, tmp_path, monkeypatch, fake):
    out = tmp_path / "out"
    out.mkdir()
    (out / "graphics.json").write_text(json.dumps({"style": "", "graphics": [{"id": "G01", "index": 0, **GRAPHIC, "html": "graphics/G01-title-card.html"}]}))
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    assert not (out / "graphics.json").exists()

    def never(*a, **k):
        raise AssertionError("old graphics re-attached")

    monkeypatch.setattr(graphics, "make", never)
    pipeline.rerender(out, Options(inputs=[]))


def test_progress_stream_with_graphics_only_moves_forward(footage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def fake_make(provider, specs, tl, analysis, out_dir, **k):
        progress.stage("graphics", "Designing 1 graphics")
        return [], [], []

    monkeypatch.setattr(graphics, "make", fake_make)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics", "--progress-json"]) == 0
    events = _events(capsys.readouterr().out)
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    idx = [progress.STAGES.index(s) for s in stages]
    assert idx == sorted(idx) and stages[-1] == "write" and "graphics" in stages
    assert events[-1]["event"] == "done"


# ---------------------------------------------------------------- N5: versions, history, stopping honestly


def _top_fcpxml(out: Path) -> list[str]:
    return sorted(p.name for p in out.glob("*.fcpxml"))


def test_every_run_is_a_new_version_with_its_own_project_name(footage, tmp_path, fake):
    out = tmp_path / "out"
    first = pipeline.build(_opts(footage, out, tmp_path / "c"))
    assert (first.version, first.project) == (1, "footage - Rough Cut v1 (medium, fake-model)")
    assert _top_fcpxml(out) == ["footage v1.fcpxml"]

    second = pipeline.rerender(out, Options(inputs=[], style="tight"))
    assert (second.version, second.project) == (2, "footage - Rough Cut v2 (tight, fake-model)")
    assert set(check_invariants(second.fcpxml)) == {"footage - Rough Cut v2 (tight, fake-model)", "footage - Stringout v2"}
    assert _top_fcpxml(out) == ["footage v2.fcpxml"]  # the newest cut is the only one on top

    v1 = next((out / "history").glob("v1 *"))
    assert {p.name for p in v1.iterdir()} >= {"footage v1.fcpxml", "plan.json", "edit_report.md", "prompt.md"}
    validate_dtd(v1 / "footage v1.fcpxml")  # still importable, untouched
    assert "Rough Cut v1 (medium" in (v1 / "footage v1.fcpxml").read_text()

    summary = pipeline.result_summary(second)
    assert summary["version"] == 2 and summary["project"] == second.project and summary["event"].startswith("footage (")
    listed = json.loads((out / "versions.json").read_text())["versions"]
    assert [(v["n"], v["command"], v["style"]) for v in listed] == [(1, "build", "medium"), (2, "render", "tight")]


def test_history_keeps_the_last_five_versions(footage, tmp_path):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "f", "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 0
    for _ in range(6):
        assert main(["render", str(out)]) == 0
    kept = sorted(int(p.name.split()[0][1:]) for p in (out / "history").iterdir())
    assert kept == [2, 3, 4, 5, 6] and _top_fcpxml(out) == ["f v7.fcpxml"]
    listed = json.loads((out / "versions.json").read_text())["versions"]
    assert listed[0]["n"] == 1 and listed[0].get("pruned") and "snapshot" not in listed[0]


def test_graphics_used_by_a_kept_version_are_never_deleted(tmp_path):
    from roughcut import versions

    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    (out / "history" / "v1 2026-10-01 10.00").mkdir(parents=True)
    for name in ("G01-stat-aaaaaa.mov", "G01-stat-bbbbbb.mov", "G02-quote-cccccc.mov", "G03-list-dddddd.partial.mov", "G01-stat.html", "G09-old.html"):
        (out / "graphics" / name).write_bytes(b"x")
    uri = (out / "graphics").as_uri()
    (out / "f v2.fcpxml").write_text(f'<media-rep src="{uri}/G01-stat-bbbbbb.mov"/>')
    (out / "history" / "v1 2026-10-01 10.00" / "f v1.fcpxml").write_text(f'<media-rep src="{uri}/G01-stat-aaaaaa.mov"/>')
    (out / "graphics.json").write_text(json.dumps({"graphics": [{"id": "G01", "html": "graphics/G01-stat.html"}]}))
    removed = versions.prune_graphics(out)
    assert sorted(removed) == ["G02-quote-cccccc.mov", "G03-list-dddddd.partial.mov", "G09-old.html"]
    assert sorted(p.name for p in (out / "graphics").iterdir()) == ["G01-stat-aaaaaa.mov", "G01-stat-bbbbbb.mov", "G01-stat.html"]


def test_a_failed_write_leaves_the_previous_version_intact(footage, tmp_path, monkeypatch, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}

    def broken(*a, **k):
        raise RuntimeError("report failed")

    monkeypatch.setattr(pipeline, "build_report", broken)
    with pytest.raises(RuntimeError):
        pipeline.rerender(out, Options(inputs=[], style="tight"))
    after = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}
    assert after == before and not (out / "history").exists()


def test_atomic_write_never_leaves_a_half_written_file(tmp_path, monkeypatch):
    import os

    from roughcut.versions import atomic_write

    target = tmp_path / "plan.json"
    atomic_write(target, "old")

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, "new")
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["plan.json"]


def test_render_works_on_a_folder_from_before_versions(footage, tmp_path, fake):
    """Output folders written by earlier roughcut releases: one <name>.fcpxml, plan.json without settings."""
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", style="tight"))
    # Turn it into the old layout by hand.
    (out / "versions.json").unlink()
    (out / "footage v1.fcpxml").rename(out / "footage.fcpxml")
    saved = json.loads((out / "plan.json").read_text())
    (out / "plan.json").write_text(json.dumps({"name": saved["name"], "plan": saved["plan"], "raw": saved["raw"]}))

    assert main(["render", str(out)]) == 0
    assert _top_fcpxml(out) == ["footage v2.fcpxml"]
    old = next((out / "history").glob("v1 *"))
    assert (old / "footage.fcpxml").is_file() and (old / "plan.json").is_file()
    projects = check_invariants(out / "footage v2.fcpxml")
    assert "footage - Rough Cut v2 (medium, fake-model)" in projects  # no saved settings: the defaults
    validate_dtd(out / "footage v2.fcpxml")


def test_a_run_that_never_finished_is_reported_next_time(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    log = out / "roughcut.log"
    log.write_text(log.read_text() + "2025-01-02 14:02:00  === roughcut build started (output: x)\n2025-01-02 14:02:01  found 5 media files\n")
    assert main(["render", str(out)]) == 0
    assert "previous run here (started 2025-01-02 14:02) never finished" in log.read_text()
    assert pipeline.unfinished_run(log) is None  # the render itself ended with FINISHED


def test_open_hands_the_cut_to_final_cut(monkeypatch, tmp_path):
    import sys

    from roughcut import cli

    calls = []

    def run(cmd, **k):
        calls.append(cmd)
        return type("P", (), {"returncode": 1 if "-a" in cmd else 0})()

    monkeypatch.setattr(sys, "platform", "darwin")
    said = cli.open_in_final_cut(tmp_path / "f v2.fcpxml", run=run)
    assert calls == [["open", "-a", "Final Cut Pro", str(tmp_path / "f v2.fcpxml")], ["open", str(tmp_path / "f v2.fcpxml")]]
    assert "Opened" in said
    monkeypatch.setattr(sys, "platform", "linux")
    assert "only works on a Mac" in cli.open_in_final_cut(tmp_path / "x.fcpxml", run=run)


def test_build_open_flag_opens_the_new_cut(footage, tmp_path, monkeypatch, capsys):
    from roughcut import cli

    opened = []
    monkeypatch.setattr(cli, "open_in_final_cut", lambda path: opened.append(path) or "Opened in Final Cut Pro.")
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--open"]) == 0
    assert opened == [out / "footage v1.fcpxml"]
    printed = capsys.readouterr().out
    assert "Opened in Final Cut Pro." in printed and 'the project "footage - Rough Cut v1 (medium, no AI)" in the event "footage (' in printed


FAKE_SLOW_CLAUDE = r'''#!{python}
import json, os, sys, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai"}}))
    sys.exit(0)
open(os.environ["FAKE_CLAUDE_PID"], "w").write(str(os.getpid()))
time.sleep(120)
'''


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie nobody reaped yet has stopped running
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except OSError:
        return True


def test_cancel_from_the_app_stops_claude_and_logs_it(footage, tmp_path):
    """The app cancels with SIGTERM; `claude -p` mid-request must not keep running (and billing) on its own."""
    import os
    import signal
    import stat
    import subprocess
    import sys
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_SLOW_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    pid_file = tmp_path / "claude.pid"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_CLAUDE_PID": str(pid_file)}
    out = tmp_path / "out"
    proc = subprocess.Popen(
        [sys.executable, "-m", "roughcut", "build", str(footage), "--provider", "claude-code", "--model", "sonnet", "--no-vision",
         "--transcriber", "none", "-o", str(out), "--cache-dir", str(tmp_path / "c"), "--progress-json"],
        cwd=Path(__file__).parents[1], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not pid_file.exists() or not pid_file.read_text():
            assert proc.poll() is None and time.monotonic() < deadline, "roughcut never asked Claude Code for a plan"
            time.sleep(0.1)
        claude_pid = int(pid_file.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == -signal.SIGTERM
        deadline = time.monotonic() + 10
        while _alive(claude_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(claude_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert (out / "roughcut.log").read_text().rstrip().endswith("STOPPED: cancelled")


# ---------------------------------------------------------------- N6: revise from notes


def _revised_plan() -> dict:
    """The canned plan minus 'It's going to be a long day.', plus an unknown ID the model invented."""
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["sections"][1]["items"].remove("V01.S004")
    plan["sections"][1]["broll"][0]["over"] = ["V01.S003"]
    plan["sections"][2]["items"].append("V07.S001")
    plan["cut_notes"] = "Dropped the long-day line."
    return plan


def test_revise_replans_from_notes_with_the_builds_cached_prompt(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", context="Family trip", target_seconds=60))
    build_call = next(c for c in fake.calls if c["purpose"] == "edit plan")
    fake.plan = _revised_plan()

    result = pipeline.revise(out, "Drop the long-day line.", Options(inputs=[]))
    call = fake.calls[-1]
    assert call["purpose"] == "revision" and call["schema_name"] == "edit_plan"
    # Same system prompt and byte-identical cached media block: the revision pays only for the new part.
    assert call["system"] == build_call["system"]
    assert call["parts"][0].text == build_call["parts"][0].text and call["parts"][0].cache is True
    request = call["parts"][1].text
    assert not call["parts"][1].cache
    assert "<editor_notes>\nDrop the long-day line.\n</editor_notes>" in request
    assert "<previous_plan>" in request and 'V01.S004 "It\'s going to be a long day."' in request
    assert "Family trip" in request and "1:00" in request  # the build's brief still applies

    assert result.version == 2 and result.project == "footage - Rough Cut v2 (medium, fake-model)"
    assert result.stringout is None  # one new Rough Cut; the stringout didn't change
    assert _top_fcpxml(out) == ["footage v2.fcpxml"]
    validate_dtd(result.fcpxml)
    assert set(check_invariants(result.fcpxml)) == {"footage - Rough Cut v2 (medium, fake-model)"}
    assert "V01.S004" not in [i.seg_id for s in result.plan.sections for i in s.items]
    assert any("V07.S001" in w for w in result.warnings)  # still checked by normalize()

    report = result.report.read_text()
    assert report.index("## Revision notes") < report.index("## Paper edit")
    assert "> Drop the long-day line." in report and 'Removed **V01.S004** "It\'s going to be a long day."' in report
    assert "Changes from v1:" in report and "- Length: 0:15 → 0:" in report
    saved = json.loads((out / "plan.json").read_text())
    assert saved["notes_history"] == [{"version": 2, "notes": "Drop the long-day line.", "keep": [], "avoid": []}]
    assert saved["settings"]["context"] == "Family trip" and saved["revision_of"] == 1


def test_revise_keeps_earlier_notes_in_play(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    pipeline.revise(out, "More B-roll in the wrap.", Options(inputs=[]))
    request = fake.calls[-1]["parts"][1].text
    assert "<earlier_notes>\n- Tighter hook.\n</earlier_notes>" in request and "More B-roll in the wrap." in request
    assert "Earlier notes, still applied:" in (out / "edit_report.md").read_text()
    assert _top_fcpxml(out) == ["footage v3.fcpxml"]


def test_revise_enforces_avoid_and_flags_missing_keeps(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    code = main(["revise", str(out), "Less of the photo.", "--avoid", "V01.S003,V02", "--avoid", "V03@0-2", "--keep", "V01.S002"])
    assert code == 0
    plan = json.loads((out / "plan.json").read_text())["plan"]
    items = [i for s in plan["sections"] for i in s["items"]]
    assert "V01.S003" not in items and not any(i.startswith("V03@") for i in items)
    assert not any(b["clip"] == "V02" for s in plan["sections"] for b in s["broll"])
    assert {"ref": "V01.S005", "note": "You asked to keep V01.S002 (--keep), but the revision left it out."} in plan["flags"]  # marked on the next line kept
    report = (out / "edit_report.md").read_text()
    assert "**You asked to keep V01.S002, but the revision left it out.**" in report
    xml = next(out.glob("*.fcpxml")).read_text()
    assert "You asked to keep V01.S002" in xml  # a to-do marker in Final Cut
    validate_dtd(next(out.glob("*.fcpxml")))


def test_revise_works_on_a_folder_from_before_saved_settings(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", context="Family trip to the lake", target_seconds=90))
    saved = json.loads((out / "plan.json").read_text())
    (out / "plan.json").write_text(json.dumps({"name": saved["name"], "plan": saved["plan"], "raw": saved["raw"]}))
    (out / "versions.json").unlink()
    (out / "footage v1.fcpxml").rename(out / "footage.fcpxml")

    result = pipeline.revise(out, "Tighter.", Options(inputs=[]))
    request = fake.calls[-1]["parts"][1].text
    assert "Family trip to the lake" in request and "1:30" in request  # recovered from prompt.md
    assert result.version == 2 and _top_fcpxml(out) == ["footage v2.fcpxml"]
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)


def test_a_failed_revision_changes_nothing(footage, tmp_path, monkeypatch, fake):
    from roughcut.providers import ProviderError

    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}

    def overloaded(*a, **k):
        raise ProviderError("Anthropic API error 529: overloaded")

    monkeypatch.setattr(fake, "complete_json", overloaded)
    with pytest.raises(ProviderError, match="Nothing changed: v1 is still the latest cut"):
        pipeline.revise(out, "Tighter.", Options(inputs=[]))
    after = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}
    assert after == before


def test_revise_progress_stream_for_the_app(footage, tmp_path, fake, capsys):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    capsys.readouterr()
    assert main(["revise", str(out), "Tighter hook.", "--progress-json"]) == 0
    events = _events(capsys.readouterr().out)
    assert [e["stage"] for e in events if e["event"] == "stage"] == ["plan", "cut", "write"]
    done = events[-1]["result"]
    assert done["version"] == 2 and done["stringout_seconds"] is None and done["project"].startswith("footage - Rough Cut v2")


def test_revise_needs_notes(tmp_path, capsys):
    assert main(["revise", str(tmp_path)]) == 1
    assert "Say what to change" in capsys.readouterr().err


def test_revise_reuses_unchanged_graphics_and_designs_only_new_ones(footage, tmp_path, monkeypatch):
    plan = _graphics_plan()
    plan["graphics"].append({"kind": "stat", "segment": "V01.S005", "words": "for weeks", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""})
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    # The designs are what's under test: skip the browser, keep everything else.
    monkeypatch.setattr(graphics, "_render_one", lambda job: {"id": job["id"], "error": "not rendered in this test", "console": []})
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", graphics=True))
    manifest = json.loads((out / "graphics.json").read_text())
    title = next(g for g in manifest["graphics"] if g["kind"] == "title_card")
    (out / title["html"]).write_text((out / title["html"]).read_text() + "<!-- hand edit -->")

    revised = json.loads(json.dumps(plan))
    revised["graphics"][1]["text"] = "Three Weeks"  # changed: needs a new design
    provider.plan = revised
    designs_before = sum(1 for c in provider.calls if c["schema_name"] == "graphics")
    pipeline.revise(out, "Say three weeks.", Options(inputs=[]))

    design_calls = [c for c in provider.calls if c["schema_name"] == "graphics"][designs_before:]
    assert len(design_calls) == 1
    request = design_calls[0]["parts"][-1].text
    assert "Three Weeks" in request and "Heading Out" not in request.split("<graphics>")[-1]
    assert "Match the graphics already made for this video" in request
    after = json.loads((out / "graphics.json").read_text())
    kept = next(g for g in after["graphics"] if g["kind"] == "title_card")
    assert "<!-- hand edit -->" in (out / kept["html"]).read_text()  # the saved (hand-edited) design, not a new one
    assert (next((out / "history").glob("v1 *")) / title["html"]).is_file()  # v1's designs are kept in history

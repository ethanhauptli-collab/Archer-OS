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


@pytest.fixture(scope="module")
def shared_cache(tmp_path_factory):
    """One analysis cache for these tests: probes, silence maps and frames are keyed by file, so results don't change."""
    return tmp_path_factory.mktemp("analysis-cache")


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


def test_plan_is_saved_before_graphics_and_render_finishes_without_the_model(footage, tmp_path, monkeypatch, capsys, shared_cache):
    provider = FakeProvider(_graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def crash(*a, **k):
        raise RuntimeError("the renderer crashed")

    monkeypatch.setattr(graphics, "render", crash)
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(shared_cache), "--graphics", "--progress-json"])
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


def test_stop_after_the_plan_prints_how_to_finish(footage, tmp_path, monkeypatch, capsys, shared_cache):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "make_timelines", broken)
    out = tmp_path / "my out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(shared_cache)]) == 1
    err = capsys.readouterr().err
    assert f"roughcut render '{out}'" in err  # quoted: the path has a space


def test_a_failed_rebuild_leaves_the_previous_plan_working(footage, tmp_path, monkeypatch, fake, shared_cache):
    """IDs follow recording order: an earlier clip renumbers everything after it."""
    src = tmp_path / "footage"
    shutil.copytree(footage, src)
    out, cache = tmp_path / "out", shared_cache
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


def test_render_refuses_a_plan_made_for_other_clips(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    data = json.loads((out / "analysis.json").read_text())
    data["clips"][0]["media"]["fingerprint"] = "a-different-file"
    (out / "analysis.json").write_text(json.dumps(data))
    with pytest.raises(RoughcutError, match="different set of clips"):
        pipeline.rerender(out, Options(inputs=[]))


def test_render_keeps_the_builds_cut_settings(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(shared_cache),
                 "--style", "tight", "--no-stringout"]) == 0
    tight = _rough(check_invariants(next(out.glob("*.fcpxml"))))["seconds"]

    assert main(["render", str(out)]) == 0  # no flags: still tight, still no stringout
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] == tight and not any("Stringout" in k for k in projects)

    assert main(["render", str(out), "--style", "medium", "--stringout"]) == 0
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] > tight and any("Stringout" in k for k in projects)
    assert json.loads((out / "plan.json").read_text())["settings"]["style"] == "medium"  # the next render starts here


def test_a_new_build_without_graphics_drops_the_old_ones(footage, tmp_path, monkeypatch, fake, shared_cache):
    out = tmp_path / "out"
    out.mkdir()
    (out / "graphics.json").write_text(json.dumps({"style": "", "graphics": [{"id": "G01", "index": 0, **GRAPHIC, "html": "graphics/G01-title-card.html"}]}))
    pipeline.build(_opts(footage, out, shared_cache))
    assert not (out / "graphics.json").exists()

    def never(*a, **k):
        raise AssertionError("old graphics re-attached")

    monkeypatch.setattr(graphics, "make", never)
    pipeline.rerender(out, Options(inputs=[]))


def test_progress_stream_with_graphics_only_moves_forward(footage, tmp_path, monkeypatch, capsys, shared_cache):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def fake_make(provider, specs, tl, analysis, out_dir, **k):
        progress.stage("graphics", "Designing 1 graphics")
        return [], [], []

    monkeypatch.setattr(graphics, "make", fake_make)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(shared_cache), "--graphics", "--progress-json"]) == 0
    events = _events(capsys.readouterr().out)
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    idx = [progress.STAGES.index(s) for s in stages]
    assert idx == sorted(idx) and stages[-1] == "write" and "graphics" in stages
    assert events[-1]["event"] == "done"


# ---------------------------------------------------------------- N5: versions, history, stopping honestly


def _top_fcpxml(out: Path) -> list[str]:
    return sorted(p.name for p in out.glob("*.fcpxml"))


def test_every_run_is_a_new_version_with_its_own_project_name(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    first = pipeline.build(_opts(footage, out, shared_cache))
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


def test_history_keeps_the_last_five_versions(footage, tmp_path, shared_cache):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "f", "--provider", "none", "--transcriber", "none", "--cache-dir", str(shared_cache)]) == 0
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
    for name in ("G01-stat-aaaaaa.mov", "G01-stat-bbbbbb.mov", "G02-quote-cccccc.mov", "G03-list-dddddd.partial.mov", "G01-stat.html", "G09-old.html", "preview.html"):
        (out / "graphics" / name).write_bytes(b"x")
    (out / "graphics" / "previews").mkdir()
    (out / "graphics" / "previews" / "G01.mp4").write_bytes(b"x")
    uri = (out / "graphics").as_uri()
    (out / "f v2.fcpxml").write_text(f'<media-rep src="{uri}/G01-stat-bbbbbb.mov"/>')
    (out / "history" / "v1 2026-10-01 10.00" / "f v1.fcpxml").write_text(f'<media-rep src="{uri}/G01-stat-aaaaaa.mov"/>')
    (out / "graphics.json").write_text(json.dumps({"graphics": [{"id": "G01", "html": "graphics/G01-stat.html", "file": "graphics/G01-stat-bbbbbb.mov"}]}))
    removed = versions.prune_graphics(out)
    assert sorted(removed) == ["G02-quote-cccccc.mov", "G03-list-dddddd.partial.mov", "G09-old.html"]
    assert sorted(p.name for p in (out / "graphics").iterdir()) == ["G01-stat-aaaaaa.mov", "G01-stat-bbbbbb.mov", "G01-stat.html", "preview.html", "previews"]
    # A version without graphics: the preview page would show an earlier cut's, so it goes; the clips stay.
    (out / "graphics.json").unlink()
    assert sorted(versions.prune_graphics(out)) == ["G01-stat.html", "preview.html"]
    assert sorted(p.name for p in (out / "graphics").iterdir()) == ["G01-stat-aaaaaa.mov", "G01-stat-bbbbbb.mov"]


def test_a_failed_write_leaves_the_previous_version_intact(footage, tmp_path, monkeypatch, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
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


def test_render_works_on_a_folder_from_before_versions(footage, tmp_path, fake, shared_cache):
    """Output folders written by earlier roughcut releases: one <name>.fcpxml, plan.json without settings."""
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, style="tight"))
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


def test_a_run_that_never_finished_is_reported_next_time(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
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


def test_build_open_flag_opens_the_new_cut(footage, tmp_path, monkeypatch, capsys, shared_cache):
    from roughcut import cli

    opened = []
    monkeypatch.setattr(cli, "open_in_final_cut", lambda path: opened.append(path) or "Opened in Final Cut Pro.")
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none", "--cache-dir", str(shared_cache), "--open"]) == 0
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


def test_cancel_from_the_app_stops_claude_and_logs_it(footage, tmp_path, shared_cache):
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
         "--transcriber", "none", "-o", str(out), "--cache-dir", str(shared_cache), "--progress-json"],
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


def test_revise_replans_from_notes_with_the_builds_cached_prompt(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, context="Family trip", target_seconds=60))
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


def test_revise_keeps_earlier_notes_in_play(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    pipeline.revise(out, "More B-roll in the wrap.", Options(inputs=[]))
    request = fake.calls[-1]["parts"][1].text
    assert "<earlier_notes>\n- Tighter hook.\n</earlier_notes>" in request and "More B-roll in the wrap." in request
    assert "Earlier notes, still applied:" in (out / "edit_report.md").read_text()
    assert _top_fcpxml(out) == ["footage v3.fcpxml"]


def test_revise_enforces_avoid_and_flags_missing_keeps(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
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


def test_revise_works_on_a_folder_from_before_saved_settings(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, context="Family trip to the lake", target_seconds=90))
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


def test_a_failed_revision_changes_nothing(footage, tmp_path, monkeypatch, fake, shared_cache):
    from roughcut.providers import ProviderError

    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}

    def overloaded(*a, **k):
        raise ProviderError("Anthropic API error 529: overloaded")

    monkeypatch.setattr(fake, "complete_json", overloaded)
    with pytest.raises(ProviderError, match="Nothing changed: v1 is still the latest cut"):
        pipeline.revise(out, "Tighter.", Options(inputs=[]))
    after = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != "roughcut.log"}
    assert after == before


def test_revise_progress_stream_for_the_app(footage, tmp_path, fake, capsys, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    capsys.readouterr()
    assert main(["revise", str(out), "Tighter hook.", "--progress-json"]) == 0
    events = _events(capsys.readouterr().out)
    assert [e["stage"] for e in events if e["event"] == "stage"] == ["plan", "cut", "write"]
    done = events[-1]["result"]
    assert done["version"] == 2 and done["stringout_seconds"] is None and done["project"].startswith("footage - Rough Cut v2")


def test_revise_needs_notes(tmp_path, capsys):
    assert main(["revise", str(tmp_path)]) == 1
    assert "Say what to change" in capsys.readouterr().err


def test_revise_reuses_unchanged_graphics_and_designs_only_new_ones(footage, tmp_path, monkeypatch, shared_cache):
    plan = _graphics_plan()
    # On the cold open's first words, so it's gone before the title card arrives (one graphic at a time).
    plan["graphics"].append({"kind": "stat", "segment": "V01.S005", "words": "The kids have been asking", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""})
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    # The designs are what's under test: skip the browser, keep everything else.
    monkeypatch.setattr(graphics, "_render_one", lambda job: {"id": job["id"], "error": "not rendered in this test", "console": []})
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, graphics=True))
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


# ---------------------------------------------------------------- T4: end-of-run summary and cost


def test_cost_is_estimated_per_step_and_nothing_is_silently_unpriced():
    from roughcut.providers import Usage, UsageLog, cost_breakdown, describe_cost, price_for

    assert price_for("claude-opus-5-5") == "claude-opus-5-5" and price_for("claude-opus-5") == "claude-opus-5"
    assert price_for("claude-opus-5-5-20260401") == "claude-opus-5-5"  # longest prefix, not the older Opus 5
    assert price_for("claude-sonnet-5-5 (subscription)") is None and price_for("gpt-5") is None
    api, plan = UsageLog(), UsageLog()
    api.add(Usage("claude-opus-5-5", "edit plan", input_tokens=100_000, output_tokens=10_000))
    api.add(Usage("claude-sonnet-5-5", "visual log", input_tokens=50_000, output_tokens=1_000, cache_read_tokens=100_000))
    api.add(Usage("claude-haiku-5-5", "visual log", input_tokens=200_000, output_tokens=1_000))  # over 100K: the higher price
    api.add(Usage("some-local-model", "graphics design", input_tokens=5, output_tokens=5))
    plan.add(Usage("claude-sonnet-5-5 (subscription)", "edit plan", input_tokens=9, output_tokens=9))
    b = cost_breakdown([api, plan])
    assert b["by_purpose"]["edit plan"] == pytest.approx(0.4 + 0.2)
    assert b["by_purpose"]["visual log"] == pytest.approx(0.1 + 0.01 + 0.02 + 0.1 + 0.0025)
    assert (b["calls"], b["subscription_calls"], b["unpriced_calls"]) == (5, 1, 1)
    text = describe_cost(b)
    assert text.startswith("about $0.83 (edit plan $0.60, visual log $0.23)")
    assert "1 request on your Claude plan" in text and "1 call to a model with no price on file" in text


def test_haiku_5_5_gets_effort_haiku_4_5_does_not():
    import httpx2
    from test_providers import SCHEMA, _anthropic_provider, _sse

    from roughcut.providers import TextPart

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": true}'))

    provider, _ = _anthropic_provider(handler)
    for model in ("claude-haiku-5-5", "claude-haiku-4-5"):
        provider.model = model
        provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="p")
    assert seen[0]["output_config"]["effort"] == "high" and "effort" not in seen[1]["output_config"]


class _Billed(FakeProvider):
    """A FakeProvider whose calls show up in the usage log like real API calls."""

    model = "claude-opus-5-5"

    def complete_json(self, system, parts, schema, *, schema_name, purpose, effort=None, max_tokens=64000):
        from roughcut.providers import Usage

        self.usage.add(Usage(self.model, purpose, input_tokens=10_000, output_tokens=1_000))
        return super().complete_json(system, parts, schema, schema_name=schema_name, purpose=purpose, effort=effort, max_tokens=max_tokens)


def test_the_end_of_a_run_says_what_made_it_in_and_what_it_cost(footage, tmp_path, monkeypatch, capsys):
    src = tmp_path / "footage"
    shutil.copytree(footage, src)
    (src / "half-copied.mov").write_bytes(b"\x00not a movie" * 100)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: _Billed(_graphics_plan()))
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", lambda job: {"id": job["id"], "error": "the renderer crashed", "console": []})
    out = tmp_path / "out"
    assert main(["build", str(src), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics"]) == 0
    printed = capsys.readouterr().out
    assert "In the cut: visual log 4 of 4 clips" in printed
    assert "graphics 0 of 1 on the timeline (1 didn't design or render; see Warnings)" in printed
    assert "1 file couldn't be read (half-copied.mov)" in printed
    assert "AI usage:   about $0.18 (visual log $0.06, edit plan $0.06, graphics design $0.06)" in printed
    assert "about $0.18 (visual log $0.06, edit plan $0.06, graphics design $0.06)" in (out / "edit_report.md").read_text()

    capsys.readouterr()
    assert main(["render", str(out), "--progress-json"]) == 0
    done = _events(capsys.readouterr().out)[-1]["result"]
    assert done["graphics"] == {"planned": 1, "placed": 1, "rendered": 0, "note": None}
    assert done["skipped"] == {"files": [], "plan_refs": 3} and done["usage"] is None  # a render calls no model
    assert done["visual_log"] == {"described": 4, "total": 4}


def test_graphics_on_but_planning_failed_is_said_plainly(footage, tmp_path, monkeypatch, capsys, shared_cache):
    from roughcut.providers import ProviderError

    class Broken(FakeProvider):
        def complete_json(self, *a, **k):
            if k.get("schema_name") == "edit_plan":
                raise ProviderError("overloaded")
            return super().complete_json(*a, **k)

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: Broken())
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(shared_cache), "--graphics", "--progress-json"]) == 3
    done = _events(capsys.readouterr().out)[-1]["result"]
    assert done["fell_back"] and done["graphics"]["note"] == "none: AI planning failed"
    assert "Motion graphics were on, but AI planning failed" in (out / "edit_report.md").read_text()


# ---------------------------------------------------------------- T20: a used-up plan or credit stops cleanly


FAKE_CLAUDE_REPLIES = r'''#!{python}
import json, os, sys
mode = os.environ["FAKE_CLAUDE_MODE"]
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai"}}))
    sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
step = "vision" if "clips" in schema.get("properties", {{}}) else "plan"
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(step + "\n")
sys.stdin.read()
if mode == step + "_limit":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1760000000"}}))
    sys.exit(1)
if mode == step + "_context":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Prompt is too long: context limit exceeded"}}))
    sys.exit(1)
if step == "vision":
    out = {{"clips": [{{"id": i, "description": "bars", "tags": ["test"], "shot": "wide", "quality": "good"}} for i in ("V01", "V02", "V03", "I01")]}}
else:
    out = json.loads(os.environ["FAKE_CLAUDE_PLAN"])
print(json.dumps({{"type": "result", "is_error": False, "result": "", "structured_output": out, "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
'''


@pytest.fixture
def claude_replies(tmp_path, monkeypatch):
    import os
    import stat
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE_REPLIES.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude-calls.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", json.dumps(CANNED_PLAN))

    def run(mode: str, out: Path, *extra: str) -> tuple[int, list[str], list[dict]]:
        import contextlib
        import io

        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        log.unlink(missing_ok=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["build", *extra, "-o", str(out), "--provider", "claude-code", "--model", "sonnet",
                         "--transcriber", "none", "--cache-dir", str(tmp_path / "cache"), "--workers", "1", "--progress-json"])
        progress.enable(False)
        calls = log.read_text().split() if log.exists() else []
        return code, calls, _events(buf.getvalue())

    return run


def test_a_used_up_plan_during_the_visual_log_stops_with_nothing_lost(footage, tmp_path, claude_replies):
    out = tmp_path / "out"
    code, calls, events = claude_replies("vision_limit", out, "--name", "x", str(footage))
    assert code == 1 and calls == ["vision"]  # stopped at the first batch: no planning, no stringout
    error = events[-1]
    assert error["event"] == "error" and error["kind"] == "usage_limit"
    assert "usage limit was reached during the visual log request (it resets" in error["message"]
    assert "Nothing is lost" in error["message"] and "Run the same command again after the reset" in error["message"]
    assert not list(out.glob("*.fcpxml")) and not (out / "plan.json").exists()
    assert "STOPPED: Your Claude plan's usage limit" in (out / "roughcut.log").read_text()


def test_a_used_up_plan_while_planning_never_delivers_a_stringout(footage, tmp_path, claude_replies):
    out = tmp_path / "out"
    assert claude_replies("none", out, "--name", "x", str(footage))[0] == 0  # a finished v1
    before = (out / "plan.json").read_text()
    code, calls, events = claude_replies("plan_limit", out, "--name", "x", str(footage))
    assert code == 1 and calls == ["plan"]  # the visual log came from the cache
    assert "visual descriptions of 4 of 4 clips are cached" in events[-1]["message"]
    assert _top_fcpxml(out) == ["x v1.fcpxml"] and (out / "plan.json").read_text() == before  # v1 untouched


def test_a_context_limit_is_not_a_plan_limit(footage, tmp_path, claude_replies):
    out = tmp_path / "out"
    code, calls, events = claude_replies("plan_context", out, "--name", "x", str(footage))
    assert code == 3 and events[-1]["result"]["fell_back"]  # announced stringout, as before


def test_an_empty_api_balance_is_a_limit_not_a_failure():
    import httpx2
    from test_providers import SCHEMA, _anthropic_provider

    from roughcut.providers import ProviderLimitError, TextPart

    def handler(request):
        body = {"type": "error", "error": {"type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}
        return httpx2.Response(400, json=body)

    provider, _ = _anthropic_provider(handler)
    with pytest.raises(ProviderLimitError, match="credit balance is too low.*Plans & Billing"):
        provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="edit_plan", purpose="edit plan")


def test_a_used_up_plan_during_revise_changes_nothing(footage, tmp_path, monkeypatch, fake, shared_cache):
    from roughcut.providers import ProviderLimitError

    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))

    def used_up(*a, **k):
        raise ProviderLimitError("Your Claude plan's usage limit was reached during the revision request (it resets 17:00).")

    monkeypatch.setattr(fake, "complete_json", used_up)
    with pytest.raises(ProviderLimitError, match="Nothing changed: v1 is still the latest cut. Run the same revise again after the reset."):
        pipeline.revise(out, "Tighter.", Options(inputs=[]))
    assert _top_fcpxml(out) == ["footage v1.fcpxml"]


# ---------------------------------------------------------------- T5: heartbeat and stall dump


def test_a_long_step_says_it_is_still_working_and_a_stall_leaves_a_stack(footage, tmp_path, monkeypatch, capsys, shared_cache):
    import time

    class SlowPlanner(FakeProvider):
        def complete_json(self, *a, **k):
            if k.get("schema_name") == "edit_plan":
                time.sleep(1.6)
            return super().complete_json(*a, **k)

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: SlowPlanner())
    monkeypatch.setattr(progress, "HEARTBEAT_SECONDS", 0.3)
    monkeypatch.setattr(progress, "STALL_SECONDS", 0.9)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(shared_cache), "--progress-json"]) == 0
    log = (out / "roughcut.log").read_text()
    assert "still working (" in log and "): planning the edit with fake-model" in log
    assert "most recent call first" in log and "in complete_json" in log  # exactly where it was waiting
    assert "nothing has moved for" in log
    events = _events(capsys.readouterr().out)
    assert any(e["event"] == "log" and e["message"].startswith("still working") for e in events)  # the app sees it too
    assert not progress._watch  # stopped with the run: no heartbeat or dump afterwards


def test_the_heartbeat_names_the_innermost_step():
    with progress.doing("planning the edit with opus"):
        with progress.doing("waiting for claude-opus-5-5 (edit plan)"):
            assert progress.current_activity()[0] == "waiting for claude-opus-5-5 (edit plan)"
        assert progress.current_activity()[0] == "planning the edit with opus"
    assert progress._elapsed(370) == "6m10s" and progress._elapsed(42) == "42s"


# ---------------------------------------------------------------- T15: readable paper edit and result.json


def test_the_paper_edit_converter_handles_what_the_report_writes():
    from roughcut.report_html import markdown_to_html

    md = "\n".join([
        "# Trip <script>alert(1)</script>",
        "",
        "> **The AI didn't plan this cut.** It's the stringout.",
        ">",
        "> AI planning failed: boom",
        "",
        "_A day out._",
        "",
        "## Chapters",
        "",
        "```",
        "0:00 Cold open",
        "0:12 The <summit>",
        "```",
        "",
        "### Cold open  `0:00`",
        "",
        "- `0:00` **V01.S001** Hello there, broll_2398.mov is_fine  ",
        "  <sub>talk.mov @ 0:01.0</sub>",
        "  - B-roll **V02** (broll.mp4) over V01.S001: cover",
        "- **I01@0-3** visual: photo.png",
        "",
        "## Check these",
        "",
        "- [ ] V01.S002: cut the restart",
        "",
        "| ID | File | Notes |",
        "|---|---|---|",
        "| V01 | talk.mov | a / b |",
    ])
    page = markdown_to_html(md)
    assert "<script>" not in page and "&lt;script&gt;" in page  # model and file text is escaped
    assert "<blockquote><p><strong>The AI didn't plan this cut.</strong> It's the stringout.</p><p>AI planning failed: boom</p></blockquote>" in page
    assert "<p><em>A day out.</em></p>" in page
    assert '<pre id="chapter-list">0:00 Cold open\n0:12 The &lt;summit&gt;</pre>' in page and 'id="copy-chapters"' in page
    assert '<h3 id="cold-open">Cold open  <code>0:00</code></h3>' in page
    assert "broll_2398.mov is_fine" in page  # underscores in names aren't italics
    assert "<br><sub>talk.mov @ 0:01.0</sub><ul><li>B-roll <strong>V02</strong>" in page.replace("\n", "")  # nested under its line
    assert '<li class="todo">' in page and "<td>a / b</td>" in page and "|---" not in page


def test_build_writes_a_readable_report_and_result_json(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    result = pipeline.build(_opts(footage, out, shared_cache))
    page = (out / "edit_report.html").read_text()
    assert page.startswith("<!doctype html>") and "@media print" in page and "Copy chapters" in page
    assert "footage - Rough Cut v1 (medium, fake-model)" in page
    saved = json.loads((out / "result.json").read_text())
    assert saved == json.loads(json.dumps(pipeline.result_summary(result)))
    assert saved["report_html"] == str(out / "edit_report.html")
    pipeline.rerender(out, Options(inputs=[]))
    assert json.loads((out / "result.json").read_text())["version"] == 2  # render writes it too
    assert (next((out / "history").glob("v1 *")) / "result.json").is_file()  # and v1's stays with v1


def test_the_report_page_in_a_browser(footage, tmp_path, fake, shared_cache):
    from roughcut.graphics import launch_browser, renderer_problem

    if renderer_problem() is not None:
        pytest.skip("no browser")
    from playwright.sync_api import sync_playwright

    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    chapters = (out / "edit_report.md").read_text().split("## Chapters\n\n```\n")[1].split("\n```")[0]
    with sync_playwright() as p:
        browser = launch_browser(p)
        try:
            for width in (1000, 390):
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto((out / "edit_report.html").as_uri())
                shot = tmp_path / f"report-{width}.png"
                page.screenshot(path=str(shot), full_page=True)
                assert shot.stat().st_size > 20_000
                assert page.evaluate("document.documentElement.scrollWidth") <= width  # no sideways scrolling on a phone
                page.click("#copy-chapters")
                assert page.evaluate("window.getSelection().toString()") == chapters
                assert page.inner_text("#copy-chapters") in ("Copied", "Selected: press ⌘C")
                page.close()
        finally:
            browser.close()


# ---------------------------------------------------------------- T19: keep the Mac awake, say when it's done


class _FakeCaffeinate:
    started: list = []

    def __init__(self, cmd, **kwargs):
        self.cmd, self.terminated = cmd, False
        _FakeCaffeinate.started.append(self)

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_a_run_on_a_mac_keeps_it_awake_and_says_when_it_is_done(footage, tmp_path, monkeypatch, capsys, shared_cache):
    import os
    import sys

    from roughcut import cli

    _FakeCaffeinate.started = []
    notified = []
    monkeypatch.setattr(sys, "platform", "darwin")
    real_keep_awake, real_signal_done = cli.keep_awake, cli.signal_done
    monkeypatch.setattr(cli, "keep_awake", lambda: real_keep_awake(popen=_FakeCaffeinate))
    monkeypatch.setattr(cli, "signal_done", lambda text: real_signal_done(text, run=lambda cmd, **k: notified.append(cmd)))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "NOTIFY_AFTER", 0.0)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "f", "--provider", "none", "--transcriber", "none", "--cache-dir", str(shared_cache)]) == 0
    (awake,) = _FakeCaffeinate.started
    assert awake.cmd == ["caffeinate", "-i", "-s", "-w", str(os.getpid())] and awake.terminated
    assert capsys.readouterr().out.endswith("\a")  # the bell: Terminal bounces its Dock icon
    (cmd,) = notified
    assert cmd[:2] == ["osascript", "-e"] and 'display notification "f - Rough Cut v1 (medium, no AI) is ready (0:' in cmd[2]

    notified.clear()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", str(empty), "-o", str(tmp_path / "o2"), "--provider", "none", "--transcriber", "none"]) == 1
    assert 'display notification "Stopped: No media files found.' in notified[0][2]


def test_no_bell_for_the_app_or_a_quick_run(footage, tmp_path, monkeypatch, capsys, shared_cache):
    from roughcut import cli

    notified = []
    monkeypatch.setattr(cli, "signal_done", lambda text: notified.append(text))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(shared_cache)]) == 0
    monkeypatch.setattr(cli, "NOTIFY_AFTER", 0.0)
    assert main(["render", str(out), "--progress-json"]) == 0  # the app has its own notification
    assert notified == []
    assert cli.keep_awake() is None  # not a Mac: nothing to start
    assert cli._applescript('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'


def test_plan_limit_wording_is_told_apart_from_a_context_limit():
    from roughcut.providers.claude_code import plan_limit_reached

    for said in ("Claude AI usage limit reached|1760000000", "5-hour limit reached ∙ resets 3pm", "You've hit your limit · resets 5pm"):
        assert plan_limit_reached(said.lower()), said
    for said in ("Prompt is too long", "Context limit reached: the conversation is too long", "Rate limited, retrying"):
        assert not plan_limit_reached(said.lower()), said


# ---------------------------------------------------------------- review fixes


def _write_version(out: Path, n: int, movs: list[str]) -> None:
    """What write_outputs does to the folder, without cutting anything: a new cut using these graphics clips."""
    from roughcut import versions

    uri = (out / "graphics").as_uri()
    versions.snapshot(out, before=n)
    (out / f"f v{n}.fcpxml").write_text("".join(f'<media-rep src="{uri}/{m}"/>' for m in movs))
    (out / "plan.json").write_text("{}")
    versions.record(out, {"n": n, "fcpxml": f"f v{n}.fcpxml"})
    versions.finish(out, n)


def test_graphics_of_a_cut_already_in_final_cut_outlive_its_history(tmp_path):
    """FCP reads imported media in place: a clip any version used must survive that version's history folder."""
    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    uri = (out / "graphics").as_uri()
    # A folder from before versions: one cut, a graphic named without its digest.
    (out / "f.fcpxml").write_text(f'<media-rep src="{uri}/G01-title-card.mov"/>')
    (out / "plan.json").write_text("{}")
    (out / "graphics" / "G01-title-card.mov").write_bytes(b"x")
    for n in range(2, 10):  # every re-cut changes the graphic's length, so it renders anew
        for name in (f"G01-title-card-{n:06d}.mov", f"G02-stat-{n:06d}.partial.mov"):
            (out / "graphics" / name).write_bytes(b"x")
        _write_version(out, n, [f"G01-title-card-{n:06d}.mov"])

    kept = sorted(int(p.name.split()[0][1:]) for p in (out / "history").iterdir())
    assert kept == [4, 5, 6, 7, 8]  # history of v1 (the old layout), v2 and v3 is gone...
    left = {p.name for p in (out / "graphics").iterdir()}
    assert "G01-title-card.mov" in left  # ...their clips are not
    assert {f"G01-title-card-{n:06d}.mov" for n in range(2, 10)} <= left
    assert not any(n.endswith(".partial.mov") for n in left)  # what no version used still goes
    listed = json.loads((out / "versions.json").read_text())["versions"]
    assert listed[0]["graphics"] == ["G01-title-card.mov"] and listed[0].get("pruned")


def test_the_apps_recut_gets_what_its_form_says(footage, tmp_path, capsys, shared_cache):
    """Re-cut sends --style always, --format/--keep-fillers/--no-stringout only when set: the rest means the default."""
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none",
                 "--cache-dir", str(shared_cache), "--format", "vertical", "--keep-fillers", "--no-stringout", "--progress-json"]) == 0
    capsys.readouterr()
    # Pacing tight, Format "From footage", Keep fillers off, Stringout on:
    assert main(["render", str(out), "--progress-json", "--style", "tight"]) == 0
    done = _events(capsys.readouterr().out)[-1]["result"]
    assert done["project"] == "footage - Rough Cut v2 (tight, no AI)" and done["stringout_seconds"] is not None
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["format"], settings["keep_fillers"], settings["stringout"]) == (None, False, True)

    # In Terminal, no flag still means "as before", and --format auto goes back to the footage's own shape.
    assert main(["render", str(out), "--format", "vertical"]) == 0
    assert main(["render", str(out)]) == 0
    assert "Rough Cut v4 (tight, vertical, no AI)" in next(out.glob("*.fcpxml")).read_text()
    assert main(["render", str(out), "--format", "auto"]) == 0
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert "footage - Rough Cut v5 (tight, no AI)" in projects


def test_a_run_started_with_nohup_survives_the_terminal_closing():
    import signal

    from roughcut import cli

    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        restore = cli._install_stop_handlers(json_mode=False, own_group=False)
        try:
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN  # nohup's choice stands
            assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN)  # a cancel is still logged
        finally:
            cli._restore_handlers(restore)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN

        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        restore = cli._install_stop_handlers(json_mode=False, own_group=False)
        try:
            assert signal.getsignal(signal.SIGHUP) not in (signal.SIG_DFL, signal.SIG_IGN)  # a plain Terminal run logs it
        finally:
            cli._restore_handlers(restore)
    finally:
        signal.signal(signal.SIGHUP, before)


def _fake_renders(monkeypatch, mov: Path) -> list[str]:
    """graphics._render_one without a browser: a real (transparent) ProRes clip, so the cut still validates."""
    rendered: list[str] = []

    def render_one(job):
        rendered.append(job["id"])
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "console": []}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)
    return rendered


@pytest.fixture(scope="module")
def clear_mov(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graphic") / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le",
           "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


def test_a_recut_keeps_the_previous_versions_graphics_manifest_in_history(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, graphics=True))
    v1_manifest = (out / "graphics.json").read_text()

    again = pipeline.rerender(out, Options(inputs=[], style="loose"))
    validate_dtd(again.fcpxml)
    assert (out / "graphics.json").read_text() != v1_manifest  # v2's graphic sits elsewhere
    assert (next((out / "history").glob("v1 *")) / "graphics.json").read_text() == v1_manifest  # v1's own, not v2's


def test_a_cut_written_before_its_graphics_has_a_matching_result_json(footage, tmp_path, monkeypatch, fake, shared_cache):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache))
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def crash(*a, **k):
        raise RuntimeError("the renderer crashed")

    monkeypatch.setattr(graphics, "render", crash)
    with pytest.raises(RuntimeError):
        pipeline.build(_opts(footage, out, shared_cache, graphics=True))
    result = json.loads((out / "result.json").read_text())
    assert Path(result["fcpxml"]).is_file() and Path(result["fcpxml"]).name == "footage v2.fcpxml" == _top_fcpxml(out)[0]
    assert result["version"] == 2 and result["partial"] is True
    assert result["graphics"]["note"] == "still being made when this cut was written"


def test_waiting_on_a_long_model_request_is_not_a_stall(tmp_path):
    """A long Opus plan or `claude -p` call is work, not a freeze: heartbeat yes, stack dump and stall warning no."""
    import subprocess
    import time

    from test_providers import SCHEMA, _anthropic_provider, _sse

    from roughcut.analyze import log_to
    from roughcut.providers import TextPart
    from roughcut.providers.claude_code import ClaudeCodeProvider

    def slow_claude(cmd, **k):
        time.sleep(1.6)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": {"ok": True}, "usage": {}}), stderr="")

    def slow_api(request):
        time.sleep(1.6)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": true}'))

    api, httpx2 = _anthropic_provider(slow_api)
    log = tmp_path / "roughcut.log"
    log_to(log)
    progress.start_watch(log, interval=0.3, stall=0.9)
    try:
        claude = ClaudeCodeProvider(binary="claude", run=slow_claude)
        assert claude.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="edit plan") == {"ok": True}
        assert api.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="edit plan") == {"ok": True}
    finally:
        progress.stop_watch()
        log_to(None)
    text = log.read_text()
    assert "still working (" in text and "waiting for Claude Code" in text and "waiting for claude-opus-5-5 (edit plan)" in text
    assert "most recent call first" not in text and "nothing has moved" not in text


@pytest.mark.parametrize("command", ["render", "revise"])
def test_graphics_rendered_before_their_names_had_a_digest_are_reused(command, footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    rendered = _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, graphics=True))
    # The layout an older roughcut left: footage.fcpxml using G01-title-card.mov, its digest only in graphics.json.
    manifest = json.loads((out / "graphics.json").read_text())
    (entry,) = manifest["graphics"]
    digest_name = Path(entry["file"]).name
    (out / entry["file"]).rename(out / "graphics" / "G01-title-card.mov")
    entry["file"] = "graphics/G01-title-card.mov"
    (out / "graphics.json").write_text(json.dumps(manifest))
    (out / "footage.fcpxml").write_text((out / "footage v1.fcpxml").read_text().replace(digest_name, "G01-title-card.mov"))
    (out / "footage v1.fcpxml").unlink()
    (out / "versions.json").unlink()
    rendered.clear()

    if command == "render":
        again = pipeline.rerender(out, Options(inputs=[]))
    else:  # the model keeps the plan and its graphic as they were
        again = pipeline.revise(out, "Keep it as it is.", Options(inputs=[]))
    assert rendered == []  # same design, same length: nothing to render
    assert (out / "graphics" / "G01-title-card.mov").is_file()  # the old cut's clip keeps its name
    new = json.loads((out / "graphics.json").read_text())["graphics"][0]["file"]
    assert new.startswith("graphics/G01-title-card-") and (out / new).is_file()
    assert sum(1 for c in again.timeline.connected if c.kind == "graphic") == 1
    validate_dtd(again.fcpxml)
    check_invariants(again.fcpxml)


def test_new_graphics_in_a_revision_get_the_videos_design_system(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    plan = _graphics_plan()
    plan["graphics"].append({"kind": "stat", "segment": "V01.S005", "words": "for weeks", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""})
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, graphics=True))
    assert json.loads((out / "graphics.json").read_text())["house_style"] == "test plate"  # what the designer said its system is

    pipeline.rerender(out, Options(inputs=[], style="tight"))
    assert json.loads((out / "graphics.json").read_text())["house_style"] == "test plate"  # a re-cut keeps it

    revised = json.loads(json.dumps(plan))
    revised["graphics"][1]["text"] = "Three Weeks"
    provider.plan = revised
    calls = len(provider.calls)
    pipeline.revise(out, "Say three weeks.", Options(inputs=[]))
    (design,) = [c for c in provider.calls[calls:] if c["schema_name"] == "graphics"]
    assert "<design_system>\nAlready established for this video; match it exactly:\ntest plate\n</design_system>" in design["parts"][-1].text
    assert json.loads((out / "graphics.json").read_text())["house_style"] == "test plate"


def test_new_graphics_in_a_revision_get_the_builds_logo_and_approved_look(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    from roughcut import presets

    plan = _graphics_plan()
    plan["graphics"].append({"kind": "stat", "segment": "V01.S005", "words": "The kids have been asking", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""})
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    _fake_renders(monkeypatch, clear_mov)
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    approved = tmp_path / "approved.html"
    approved.write_text("<html><body><div class=plate style='color:#123ABC'>Approved</div></body></html>")
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, shared_cache, graphics=True, graphics_logo=str(logo), graphics_look=(str(approved),)))

    def revise_designs(notes: str) -> tuple[str, pipeline.Result]:
        revised = json.loads(json.dumps(plan))
        revised["graphics"][1]["text"] = notes
        provider.plan = revised
        calls = len(provider.calls)
        result = pipeline.revise(out, f"Say {notes.lower()}.", Options(inputs=[]))
        (design,) = [c for c in provider.calls[calls:] if c["schema_name"] == "graphics"]
        return design["parts"][-1].text, result

    request, _ = revise_designs("Three Weeks")
    assert presets.LOGO_DIRECTION in request, "the new graphic may show the build's logo"
    assert presets.LOOK_DIRECTION in request and "#123ABC" in request, "and matches the approved look"

    logo.unlink()  # moved since the build: the revision still runs, designed without it
    request, result = revise_designs("Four Weeks")
    assert presets.LOGO_DIRECTION not in request and "#123ABC" in request
    assert any("logo" in w and "isn't there any more" in w for w in result.warnings)

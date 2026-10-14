"""Runs that stop part-way (Ctrl-C, the app's cancel, a used-up plan, a bug) and how they're finished.

Also what an upgrade, a moved song or a path typed without quotes must not cost the owner.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd

from roughcut import graphics, pipeline, progress
from roughcut.cli import main
from roughcut.pipeline import Options
from roughcut.providers import ProviderLimitError

TITLE = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}
# On the cold open's first words, so it's gone before the title card arrives (one graphic at a time).
STAT = {"kind": "stat", "segment": "V01.S005", "words": "The kids have been asking", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""}
LIMIT = "Your Claude plan's usage limit was reached during the graphics design request (it resets 17:00)."


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return tmp_path_factory.mktemp("analysis-cache")


@pytest.fixture(scope="module")
def clear_mov(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graphic") / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le",
           "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


def _opts(footage, out, cache, **kw) -> Options:
    return Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw)


def _plan(*graphics_specs: dict) -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [dict(g) for g in graphics_specs]
    return plan


def _events(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip().startswith("{")]


def _use(monkeypatch, provider) -> None:
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)


def _no_model(monkeypatch) -> None:
    def never(*a, **k):
        raise AssertionError("this step must not call the model")

    monkeypatch.setattr(pipeline, "make_provider", never)


def _fake_renders(monkeypatch, mov: Path, notes: dict[str, list[str]] | None = None) -> list[str]:
    """graphics._render_one without a browser: a real (transparent) ProRes clip, plus the QC notes given per kind."""
    rendered: list[str] = []

    def render_one(job):
        rendered.append(job["id"])
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "notes": list((notes or {}).get(job["kind"], [])), "seconds": 0.0}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)
    return rendered


class StopsWhileDesigning(FakeProvider):
    """Ctrl-C (or the app's cancel) while the graphics design request is out."""

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        if schema_name == "graphics":
            raise KeyboardInterrupt
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


class UsedUpWhileDesigning(FakeProvider):
    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        if schema_name == "graphics":
            self.calls.append({"schema_name": schema_name})
            raise ProviderLimitError(LIMIT)
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


def _graphic_clips(result) -> list[str]:
    return [c.note for c in result.timeline.connected if c.kind == "graphic"]


def _manifest(out: Path) -> dict[str, dict]:
    return {g["text"]: g for g in json.loads((out / "graphics.json").read_text())["graphics"]}


def _design_calls(provider, since: int = 0) -> list[dict]:
    return [c for c in provider.calls[since:] if c["schema_name"] == "graphics"]


# ---------------------------------------------------------------- a revise that stops while graphics are designed


def test_a_revise_stopped_while_designing_keeps_the_graphics_it_carried(footage, tmp_path, monkeypatch, cache, clear_mov):
    plan = _plan(TITLE, STAT)
    _use(monkeypatch, FakeProvider(plan))
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, cache, graphics=True))
    title = _manifest(out)["Heading Out"]
    (out / title["html"]).write_text((out / title["html"]).read_text() + "<!-- hand edit -->")

    revised = _plan(TITLE, {**STAT, "text": "Three Weeks"})  # the notes change one graphic
    _use(monkeypatch, StopsWhileDesigning(revised))
    with pytest.raises(KeyboardInterrupt):
        pipeline.revise(out, "Say three weeks.", Options(inputs=[]))

    # On disk: the new plan's graphics, the unchanged one with its (hand-edited) design, the changed one without.
    saved = _manifest(out)
    assert set(saved) == {"Heading Out", "Three Weeks"}
    assert "<!-- hand edit -->" in (out / saved["Heading Out"]["html"]).read_text()
    assert not saved["Three Weeks"].get("html")

    # `roughcut render` finishes the cut without the model: the carried graphic, and how to get the other.
    _no_model(monkeypatch)
    again = pipeline.rerender(out, Options(inputs=[]))
    assert _graphic_clips(again) == ["Heading Out"]
    assert again.summary["graphics"] == {
        "planned": 2, "placed": 2, "rendered": 1, "note": "1 not designed yet; `roughcut render --design-missing` adds it",
    }
    assert any(f"roughcut render {out} --design-missing" in w for w in again.warnings)
    assert "<!-- hand edit -->" in (out / _manifest(out)["Heading Out"]["html"]).read_text()  # still in the working folder
    validate_dtd(again.fcpxml)

    # --design-missing designs just that one, in the video's look, and the cut stays as planned.
    designer = FakeProvider(revised)
    _use(monkeypatch, designer)
    third = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = _design_calls(designer)
    request = call["parts"][-1].text
    assert "Three Weeks" in request and "Heading Out" not in request.split("<graphics>")[-1]
    assert "Match the graphics already made for this video" in request
    assert sorted(_graphic_clips(third)) == ["Heading Out", "Three Weeks"]
    assert third.summary["graphics"]["note"] is None and not any("--design-missing" in w for w in third.warnings)
    assert "<!-- hand edit -->" in (out / _manifest(out)["Heading Out"]["html"]).read_text()
    assert [c["schema_name"] for c in designer.calls] == ["graphics"]  # no re-planning
    validate_dtd(third.fcpxml)
    check_invariants(third.fcpxml)


def test_a_revise_stopped_before_its_graphics_were_placed_never_brings_back_old_texts(footage, tmp_path, monkeypatch, cache, clear_mov):
    plan = _plan(TITLE, STAT)
    _use(monkeypatch, FakeProvider(plan))
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, cache, graphics=True))

    _use(monkeypatch, FakeProvider(_plan(TITLE, {**STAT, "text": "Three Weeks"})))
    cut = pipeline.make_timelines

    def stopped(*a, **k):
        raise KeyboardInterrupt  # right after the new plan was saved, while cutting

    monkeypatch.setattr(pipeline, "make_timelines", stopped)
    with pytest.raises(KeyboardInterrupt):
        pipeline.revise(out, "Say three weeks.", Options(inputs=[]))
    monkeypatch.setattr(pipeline, "make_timelines", cut)

    _no_model(monkeypatch)
    again = pipeline.rerender(out, Options(inputs=[]))
    assert _graphic_clips(again) == ["Heading Out"]  # the unchanged graphic, never the old "Weeks"
    assert set(_manifest(out)) == {"Heading Out", "Three Weeks"} and not _manifest(out)["Three Weeks"].get("html")
    assert again.summary["graphics"]["rendered"] == 1 and "not designed yet" in again.summary["graphics"]["note"]


def test_a_revise_keeps_the_qc_notes_of_the_graphics_it_reuses(footage, tmp_path, monkeypatch, cache, clear_mov):
    plan = _plan(TITLE, STAT)
    _use(monkeypatch, FakeProvider(plan))
    rendered = _fake_renders(monkeypatch, clear_mov, notes={"title_card": ["reaches outside the title-safe area"]})
    out = tmp_path / "out"
    first = pipeline.build(_opts(footage, out, cache, graphics=True))
    assert any("title-safe" in w for w in first.warnings)
    rendered.clear()

    revised = pipeline.revise(out, "Keep it as it is.", Options(inputs=[]))
    assert rendered == []  # nothing changed, so nothing re-rendered...
    assert _manifest(out)["Heading Out"].get("notes") == ["reaches outside the title-safe area"]  # ...and the clip's problem is still reported
    assert any("G" in w and "title-safe" in w for w in revised.warnings)
    assert "title-safe" in (out / "edit_report.md").read_text()
    assert any("title-safe" in w for w in pipeline.rerender(out, Options(inputs=[])).warnings)  # and in every re-cut after


# ---------------------------------------------------------------- a build that stops while graphics are designed


def test_render_after_a_build_stopped_while_designing_says_the_graphics_are_missing_and_designs_them(footage, tmp_path, monkeypatch, capsys, cache, clear_mov):
    plan = _plan(TITLE)
    _use(monkeypatch, StopsWhileDesigning(plan))
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        pipeline.build(_opts(footage, out, cache, graphics=True))
    assert not (out / "graphics.json").exists()

    _no_model(monkeypatch)
    capsys.readouterr()
    assert main(["render", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Left out:   graphics 0 of 1 on the timeline (1 not designed yet; `roughcut render --design-missing` adds it)" in printed
    assert f"roughcut render {out} --design-missing" in (out / "edit_report.md").read_text()

    designer = FakeProvider(plan)
    _use(monkeypatch, designer)
    assert main(["render", str(out), "--design-missing", "--progress-json"]) == 0
    done = _events(capsys.readouterr().out)[-1]["result"]
    assert done["graphics"] == {"planned": 1, "placed": 1, "rendered": 1, "note": None}
    assert [c["schema_name"] for c in designer.calls] == ["graphics"]
    validate_dtd(Path(done["fcpxml"]))


def test_design_missing_needs_graphics_to_design(footage, tmp_path, monkeypatch, capsys, cache):
    _use(monkeypatch, FakeProvider())
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, cache))
    capsys.readouterr()
    assert main(["render", str(out), "--design-missing"]) == 1
    assert "no motion graphics to design" in capsys.readouterr().err


# ---------------------------------------------------------------- a used-up plan while designing graphics


def test_a_used_up_plan_while_designing_stops_the_run_and_render_designs_the_rest_later(footage, tmp_path, monkeypatch, capsys, cache, clear_mov):
    plan = _plan(TITLE)
    used_up = UsedUpWhileDesigning(plan)
    _use(monkeypatch, used_up)
    _fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(cache), "--graphics", "--progress-json"])
    assert code == 1  # a stop, not "Rough cut ready"
    error = _events(capsys.readouterr().out)[-1]
    assert error["event"] == "error" and error["kind"] == "usage_limit"
    assert LIMIT in error["message"] and "footage v1.fcpxml is written with 0 motion graphics" in error["message"]
    assert f"`roughcut render {out} --design-missing` designs the other 1 without re-planning" in error["message"]
    assert "resume" not in error  # the cut is written: plain `render` would only make it again
    result = json.loads((out / "result.json").read_text())
    assert not result.get("partial") and result["graphics"]["note"] == "1 not designed: the usage limit was reached"
    validate_dtd(out / "footage v1.fcpxml")

    designer = FakeProvider(plan)
    _use(monkeypatch, designer)
    again = pipeline.rerender(out, Options(inputs=[]), design_missing=True)  # after the reset
    assert _graphic_clips(again) == ["Heading Out"] and len(_design_calls(designer)) == 1
    validate_dtd(again.fcpxml)


def test_after_a_usage_limit_no_more_design_requests_are_sent():
    from test_graphics_render import small_cut

    a, plan, tl = small_cut()
    placed = graphics.place(plan.graphics, tl, a, [])
    assert len(placed) == 2

    class UsedUp(FakeProvider):
        supports_vision = False

        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            self.calls.append({"schema_name": schema_name})
            raise ProviderLimitError(LIMIT)

    provider = UsedUp()
    designer = pipeline.StopAtLimit(provider)
    warnings: list[str] = []
    html = graphics.design(designer, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings, batch=1)
    assert html == {} and len(provider.calls) == 1  # one request per batch otherwise, each failing the same way
    assert designer.limit is not None and str(designer.limit) == LIMIT


# ---------------------------------------------------------------- stopping from the app or by a bug


STOP_SCRIPT = r'''
import json, pathlib, sys, time
sys.path[:0] = [{root!r}, {tests!r}]
from conftest import CANNED_PLAN, FakeProvider
from roughcut import cli, graphics, pipeline

plan = json.loads(json.dumps(CANNED_PLAN))
plan["graphics"] = [{title!r}]


class Slow(FakeProvider):
    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        if schema_name == "graphics":
            pathlib.Path({marker!r}).write_text("designing")
            time.sleep(120)
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


pipeline.make_provider = lambda *a, **k: Slow(plan)
graphics.renderer_problem = lambda: None
sys.argv = ["roughcut", "build", {footage!r}, "-o", {out!r}, "--name", "footage", "--transcriber", "none", "--cache-dir", {cache!r},
            "--graphics", "--progress-json"]
sys.exit(cli.main())
'''


def test_cancelling_from_the_app_after_the_plan_was_saved_says_how_to_finish(footage, tmp_path, cache):
    root = Path(__file__).parents[1]
    marker, out = tmp_path / "designing", tmp_path / "out"
    script = tmp_path / "run.py"
    script.write_text(STOP_SCRIPT.format(
        root=str(root), tests=str(root / "tests"), title=TITLE, marker=str(marker), footage=str(footage), out=str(out), cache=str(cache),
    ))
    proc = subprocess.Popen([sys.executable, str(script)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert proc.poll() is None, proc.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "the build never got to designing graphics"
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)  # what the app's Cancel sends
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == -signal.SIGTERM
    last = _events(stdout)[-1]
    assert last["event"] == "error" and last["kind"] == "cancelled" and last["resume"] == f"roughcut render {out}"
    # The cut written before the graphics were made is the newest version, and the event names it (the app can offer it).
    assert last["written"] == {"version": 1, "fcpxml": str(out / "footage v1.fcpxml"), "project": json.loads((out / "result.json").read_text())["project"], "partial": True}
    assert "Version 1 (footage v1.fcpxml) was written without its motion graphics." in stderr
    assert f"roughcut render {out}" in stderr
    log = (out / "roughcut.log").read_text().rstrip()
    assert "STOPPED: cancelled" in log and log.endswith(f"roughcut render {out}")


def test_an_unexpected_error_after_the_plan_was_saved_still_offers_to_finish(footage, tmp_path, monkeypatch, capsys, cache):
    _use(monkeypatch, FakeProvider())

    def bug(*a, **k):
        raise TypeError("simulated bug")

    monkeypatch.setattr(pipeline, "make_timelines", bug)
    out = tmp_path / "out"
    with pytest.raises(TypeError):
        main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(cache), "--progress-json"])
    error = _events(capsys.readouterr().out)[-1]
    assert error["event"] == "error" and error["message"] == "Unexpected error (TypeError): simulated bug"
    assert error["resume"] == f"roughcut render {out}"  # the app's "Finish without AI"


def test_ctrl_c_ends_with_the_hint_and_no_traceback(footage, tmp_path, monkeypatch, capsys, cache):
    _use(monkeypatch, FakeProvider())

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "make_timelines", interrupted)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(cache)]) == 130
    err = capsys.readouterr().err
    assert err.rstrip().endswith(f"roughcut render {out}") and "Traceback" not in err
    assert (out / "roughcut.log").read_text().rstrip().endswith(f"roughcut render {out}")


# ---------------------------------------------------------------- upgrading, moved files, paths with spaces


def test_upgrading_a_folder_never_takes_the_owners_own_fcpxml(footage, tmp_path, cache):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "OCFoodFest", "--provider", "none", "--transcriber", "none", "--cache-dir", str(cache)]) == 0
    # The layout the release before versions left: OCFoodFest.fcpxml and plan.json naming it.
    (out / "versions.json").unlink()
    (out / "OCFoodFest v1.fcpxml").rename(out / "OCFoodFest.fcpxml")
    mine = out / "OCFoodFest - my FCP export after grading.fcpxml"
    mine.write_text("<fcpxml>graded in Final Cut</fcpxml>")
    later = time.time() + 60
    os.utime(mine, (later, later))  # newer than roughcut's own cut

    for _ in range(6):  # past the five versions history keeps
        assert main(["render", str(out)]) == 0
    assert mine.read_text() == "<fcpxml>graded in Final Cut</fcpxml>"
    assert not (out / "OCFoodFest.fcpxml").exists()  # roughcut's own old cut did move into history
    v1 = json.loads((out / "versions.json").read_text())["versions"][0]
    assert v1["fcpxml"] == "OCFoodFest.fcpxml" and v1["files"] == ["OCFoodFest.fcpxml"]


def test_a_recut_says_when_the_music_bed_has_moved(footage, tmp_path, cache):
    downloads = tmp_path / "Downloads x"
    downloads.mkdir()
    song = downloads / "song (1).m4a"
    shutil.copy(footage / "music" / "music.m4a", song)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none",
                 "--cache-dir", str(cache), f"--music={song}"]) == 0
    assert song.as_uri() in next(out.glob("*.fcpxml")).read_text()
    song.unlink()  # tidied out of Downloads

    again = pipeline.rerender(out, Options(inputs=[], style="tight"))
    (warning,) = [w for w in again.warnings if "song (1).m4a" in w]
    assert "the music bed" in warning and "missing media" in warning and str(song) in warning
    assert "song (1).m4a" in (out / "edit_report.md").read_text()


def test_a_missing_music_file_is_named_before_anything_is_made(footage, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--music", str(tmp_path / "nope.mp3")]) == 1
    err = capsys.readouterr().err
    assert "nope.mp3: can't find that file" in err and "not an audio file" not in err
    assert not out.exists()


def test_revise_takes_a_folder_path_with_spaces_typed_without_quotes(footage, tmp_path, monkeypatch, capsys, cache):
    fake = FakeProvider()
    _use(monkeypatch, fake)
    out = tmp_path / "My Movies" / "OC Food Fest_roughcut"
    pipeline.build(_opts(footage, out, cache))
    capsys.readouterr()
    assert main(["revise", *str(out).split(" "), "Tighter hook."]) == 0
    assert "Read that as" in capsys.readouterr().err
    latest = json.loads((out / "versions.json").read_text())["versions"][-1]
    assert latest["n"] == 2 and latest["notes"] == "Tighter hook."


def test_preset_look_from_takes_a_folder_path_with_spaces_typed_without_quotes(tmp_path, monkeypatch, capsys):
    from roughcut import presets

    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    live = tmp_path / "live out"
    (live / "graphics").mkdir(parents=True)
    (live / "graphics" / "G02-stat.html").write_text("<html><body><div class=plate>12 vendors</div></body></html>")
    (live / "graphics.json").write_text(json.dumps({"graphics": [{"id": "G02", "kind": "stat", "html": "graphics/G02-stat.html"}]}))
    assert main(["preset", "save", "mine", "--look-from", *str(live).split(" "), "G2"]) == 0
    (page,) = presets.load("mine").look
    assert Path(page).name.endswith("G02-stat.html") and "12 vendors" in Path(page).read_text()

"""A run that stops leaves the folder with the cut before it and, for each graphic that says the same words, its earlier design and clip.

An interim cut (written before the graphics are made) takes nothing off the top level; a redesign whose new designs or
renders don't come back keeps the earlier version's design and clip, marked `redesign_pending`, until `roughcut render
--design-missing` (or `roughcut render`) makes the new ones; the note, warnings and result keys say so.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd
from test_graphics_refs import GRAPHIC, STAT, make_pictures
from test_redesign import Fails, designed_texts, plan_of

from roughcut import graphics, pipeline, progress, versions
from roughcut.cli import main
from roughcut.pipeline import Options
from roughcut.providers import ProviderError, ProviderLimitError

LIMIT = "You've hit your usage limit."


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return tmp_path_factory.mktemp("unfinished-analysis-cache")


@pytest.fixture(scope="module")
def clear_mov(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graphic") / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


@pytest.fixture(scope="module")
def full_size_mov(tmp_path_factory) -> Path:
    """A transparent ProRes clip as big as the test cut (1920x1080 at its frame rate)."""
    path = tmp_path_factory.mktemp("graphic-full") / "clear-full.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=1920x1080:r=30000/1001:d=1", "-vf", "format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


class Renderer:
    """graphics._render_one without a browser: a real (transparent) ProRes clip, unless the graphic is told to fail."""

    def __init__(self, monkeypatch, mov: Path):
        self.mov, self.fail, self.fail_all, self.rendered = mov, set(), False, []
        monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
        monkeypatch.setattr(graphics, "_render_one", self.render_one)

    def render_one(self, job):
        self.rendered.append(job["slug"])
        if self.fail_all or job["id"] in self.fail:
            return {"id": job["id"], "error": "the renderer crashed", "notes": [], "seconds": 0.0}
        shutil.copy(self.mov, job["out"])
        return {"id": job["id"], "error": None, "notes": [], "seconds": 0.0}


def build_v1(footage, tmp_path, monkeypatch, cache, mov, *, provider_class=Fails, plan=None):
    provider = provider_class(plan or plan_of())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    renderer = Renderer(monkeypatch, mov)
    out = tmp_path / "out"
    result = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    return out, provider, renderer, result


def no_model(monkeypatch):
    def never(*a, **k):
        raise AssertionError("this step must not call the model")

    monkeypatch.setattr(pipeline, "make_provider", never)


def top(out: Path) -> list[str]:
    return sorted(p.name for p in out.glob("*.fcpxml"))


def uses(path: Path) -> list[str]:
    """The graphics clips an FCPXML plays."""
    return versions.graphics_in([path])


def entries(out: Path) -> dict[str, dict]:
    return {g["text"]: g for g in json.loads((out / "graphics.json").read_text())["graphics"]}


def result_json(out: Path) -> dict:
    return json.loads((out / "result.json").read_text())


def design_calls(provider, since: int = 0) -> list[dict]:
    return [c for c in provider.calls[since:] if c["schema_name"] == "graphics"]


# ------------------------------------------------------------------ (a) an interim cut takes nothing off the top level


@pytest.mark.parametrize("how", ["cancel", "crash", "crash_after_make", "cancel_while_designing"])
def test_a_revise_that_stops_after_its_interim_cut_leaves_the_cut_before_it_in_place(how, footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    assert len(v1_clips) == 2 and top(out) == ["footage v1.fcpxml"]

    real_make, real_accents = graphics.make, pipeline.place_accents
    if how == "crash_after_make":  # the graphics were designed and rendered; the final write never came
        seen = []

        def second_time_crashes(*a, **k):
            seen.append(1)
            if len(seen) == 2:
                raise RuntimeError("crashed after the graphics were made")
            return real_accents(*a, **k)

        monkeypatch.setattr(pipeline, "place_accents", second_time_crashes)
        error = RuntimeError
    elif how == "cancel_while_designing":  # Ctrl-C (or the app's Cancel) while the design request is out
        provider.fail = KeyboardInterrupt()
        error = KeyboardInterrupt
    else:
        error = KeyboardInterrupt if how == "cancel" else RuntimeError

        def stops(*a, **k):
            raise error("stopped while the graphics were made")

        monkeypatch.setattr(graphics, "make", stops)
    with pytest.raises(error):
        pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    monkeypatch.setattr(graphics, "make", real_make)
    monkeypatch.setattr(pipeline, "place_accents", real_accents)
    provider.fail = None

    # The cut with its graphics is where it was; the partial v2 is the newest, and says so everywhere.
    assert top(out) == ["footage v1.fcpxml", "footage v2.fcpxml"]
    assert uses(out / "footage v1.fcpxml") == v1_clips and uses(out / "footage v2.fcpxml") == []
    current = versions.current(out)
    assert current["n"] == 2 and current["partial"] is True and current["fcpxml"] == "footage v2.fcpxml"
    saved = result_json(out)
    assert saved["version"] == 2 and saved["partial"] is True and Path(saved["fcpxml"]).name == "footage v2.fcpxml"
    assert saved["graphics"]["note"] == "not made yet: this cut was written before its graphics"
    report = (out / "edit_report.md").read_text()
    assert report.split("\n")[2].startswith("> This cut is partial: it was written before its motion graphics were made")
    assert "The cut before it, v1, is still in this folder." in report
    validate_dtd(out / "footage v1.fcpxml")
    validate_dtd(out / "footage v2.fcpxml")
    assert next((out / "history").glob("v1 *")).is_dir()  # v1 is also in history, as the next write would have put it
    assert (out / "graphics" / v1_clips[0]).is_file()  # and its clips are where they were

    # Finishing it: the newest version is the only cut on top again, and it has the graphics.
    renderer.rendered.clear()
    no_model(monkeypatch)
    done = pipeline.rerender(out, Options(inputs=[]))
    assert (done.version, top(out)) == (3, ["footage v3.fcpxml"])
    assert "partial" not in result_json(out) and versions.current(out).get("partial") is None
    assert len(uses(done.fcpxml)) == 2
    validate_dtd(done.fcpxml)
    check_invariants(done.fcpxml)
    assert all((out / "graphics" / name).is_file() for name in v1_clips + uses(done.fcpxml))
    if how == "crash_after_make":
        assert renderer.rendered == []  # the clips the stopped run made are used, not rendered again
        assert not set(uses(done.fcpxml)) & set(v1_clips)  # and they are the new look
        assert done.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
    else:  # the new designs were never made: the version stands on the earlier ones, and says so
        assert set(uses(done.fcpxml)) == set(v1_clips)
        assert done.summary["graphics"]["pending"] == 2 and "keep their earlier look" in done.summary["graphics"]["note"]


def test_a_build_over_an_earlier_version_that_stops_after_its_interim_cut_leaves_that_version(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov, plan=plan_of())
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the renderer crashed")))
    with pytest.raises(RuntimeError):
        pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert top(out) == ["footage v1.fcpxml", "footage v2.fcpxml"]
    assert len(uses(out / "footage v1.fcpxml")) == 2 and uses(out / "footage v2.fcpxml") == []
    assert versions.current(out)["partial"] is True and result_json(out)["version"] == 2 and result_json(out)["partial"] is True


def test_the_error_event_of_a_stopped_revise_names_the_partial_cut(footage, tmp_path, monkeypatch, cache, clear_mov, capsys):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the renderer crashed")))
    notes = tmp_path / "notes.txt"
    notes.write_text("Make the graphics bolder.")
    capsys.readouterr()
    assert main(["revise", str(out), "--progress-json", f"--notes-file={notes}", "--redesign-graphics"]) == 1
    captured = capsys.readouterr()
    error = [json.loads(ln) for ln in captured.out.splitlines() if ln.startswith("{")][-1]
    assert error["event"] == "error" and error["resume"] == f"roughcut render {out}"
    assert error["written"] == {"version": 2, "fcpxml": str(out / "footage v2.fcpxml"), "project": result_json(out)["project"], "partial": True}
    assert "Version 2 (footage v2.fcpxml) was written without its motion graphics; the cut before it is still in the folder." in captured.err
    # A stop before anything is written says nothing of the kind.
    assert main(["render", str(tmp_path / "nowhere"), "--progress-json"]) == 1
    assert "written" not in [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")][-1]


def test_a_stop_by_ctrl_c_prints_the_partial_cut_with_the_finishing_commands(footage, tmp_path, monkeypatch, cache, clear_mov, capsys):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    notes = tmp_path / "notes.txt"
    notes.write_text("Make the graphics bolder.")
    assert main(["revise", str(out), f"--notes-file={notes}", "--redesign-graphics"]) == 130
    err = capsys.readouterr().err
    assert "Stopped (Ctrl-C)." in err and f"roughcut render {out}" in err
    assert f"`roughcut render {out} --design-missing` designs the rest" in err


# ------------------------------------------------------------------ (b) a redesign whose designs don't come back


@pytest.mark.parametrize("how", ["flag", "pictures", "planner"])
def test_a_usage_limit_during_a_redesign_keeps_the_earlier_designs_and_clips_until_the_reset(how, footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips, v1_html = uses(v1.fcpxml), {g["text"]: (out / g["html"]).read_text() for g in entries(out).values()}
    notes = "Make the graphics orange and bold."
    opts, kw = Options(inputs=[]), {}
    if how == "flag":
        kw["redesign_graphics"] = True
    elif how == "pictures":
        opts = Options(inputs=[], graphics_refs=tuple(str(p) for p in make_pictures(tmp_path / "board", 1)))
    else:
        provider.plan = plan_of(redesign_graphics="yes")
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError) as stop:
        pipeline.revise(out, notes, opts, **kw)

    # v2 is written with the graphics v1 had, in their earlier look; the error says what was written and what to run.
    message = str(stop.value)
    assert f"footage v2.fcpxml is written with 2 motion graphics, 2 of them in their earlier look because the new look wasn't designed" in message
    assert f"After the reset, `roughcut render {out} --design-missing` designs the new look without re-planning the cut." in message
    assert stop.value.written["version"] == 2 and stop.value.written["partial"] is False and stop.value.written["design_missing"] is True
    assert top(out) == ["footage v2.fcpxml"] and uses(out / "footage v2.fcpxml") == v1_clips  # not fewer than v1 had
    assert "partial" not in result_json(out)
    graphics_row = result_json(out)["graphics"]
    assert graphics_row == {
        "planned": 2, "placed": 2, "rendered": 2, "pending": 2,
        "note": f"2 graphics keep their earlier look: the new look wasn't designed because the usage limit was reached; "
                f"after the reset run `roughcut render {out} --design-missing`",
    }
    marks = entries(out)
    assert {g["redesign_pending"] for g in marks.values()} == {"design"} and {g["pending_reason"] for g in marks.values()} == {"the usage limit was reached"}
    assert {t: (out / g["html"]).read_text() for t, g in marks.items()} == v1_html  # the earlier designs, as they were
    assert "keep their earlier one" in " ".join(result_json(out)["changes"]["items"]) and "(see Warnings)" in " ".join(result_json(out)["changes"]["items"])
    warning = next(w for w in result_json(out)["warnings"] if w.startswith("The new look for the graphics wasn't designed because the usage limit was reached"))
    assert "After the reset" in warning and f"`roughcut render {out} --design-missing`" in warning
    assert "> The new look for the graphics wasn't designed" in (out / "edit_report.md").read_text().split("## Revision notes")[0]
    assert settings_of(out)["graphics_direction"] == [notes] and settings_of(out)["graphics_redesign"] == ("fresh" if how != "planner" else "restyle")
    validate_dtd(out / "footage v2.fcpxml")
    check_invariants(out / "footage v2.fcpxml")

    # A plain re-cut calls no model and keeps them as they are; it says they still wait.
    no_model(monkeypatch)
    again = pipeline.rerender(out, Options(inputs=[]), overrides={"style"})
    assert uses(again.fcpxml) == v1_clips and again.summary["graphics"]["pending"] == 2 and top(out) == ["footage v3.fcpxml"]
    assert {g["redesign_pending"] for g in entries(out).values()} == {"design"}

    # After the reset: --design-missing designs each once, in the look the notes asked for, and renders the new ones.
    provider.fail = None
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    before = len(provider.calls)
    renderer.rendered.clear()
    finished = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(provider, before)
    request = call["parts"][-1].text
    assert f"graphics look or move, and ignore the rest:\n{notes}\n</editor_direction>" in request
    assert ("<current_look>" in request) == (how == "planner") and "<design_system>" not in request
    assert sorted(designed_texts(call)) == ["Heading Out", "Weeks"]  # both graphics in the one request
    assert len(renderer.rendered) == 2 and not set(uses(finished.fcpxml)) & set(v1_clips)
    assert finished.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
    assert not any("redesign_pending" in g for g in entries(out).values()) and "pending" not in result_json(out)["graphics"]
    assert not any(w.startswith("The new look for the graphics") for w in finished.warnings)
    assert all((out / "graphics" / name).is_file() for name in v1_clips)  # Final Cut still reads v1's clips in place
    validate_dtd(finished.fcpxml)
    check_invariants(finished.fcpxml)

    # Never twice: nothing is waiting, so asking again designs nothing.
    again = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert len(design_calls(provider, before)) == 1 and again.summary["graphics"]["note"] is None


def settings_of(out: Path) -> dict:
    return json.loads((out / "plan.json").read_text())["settings"]


def test_a_failed_design_request_keeps_the_earlier_designs_pending_too(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    provider.fail = ProviderError("overloaded")
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert uses(result.fcpxml) == v1_clips and top(out) == ["footage v2.fcpxml"]
    assert {g["pending_reason"] for g in entries(out).values()} == {"the design request failed"}
    assert result.summary["graphics"]["pending"] == 2
    assert result.summary["graphics"]["note"].startswith("2 graphics keep their earlier look: the new look wasn't designed (see Warnings); run `roughcut render ")
    assert sum("no new design came back, so it keeps its earlier design" in w for w in result.warnings) == 2  # as before, one each
    assert any(w.startswith("The new look for the graphics wasn't designed: 2 graphics in this version keep their earlier look") for w in result.warnings)

    provider.fail = None
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert not set(uses(done.fcpxml)) & set(v1_clips) and done.summary["graphics"]["note"] is None and "redesign_pending" not in json.dumps(entries(out))


def test_a_revision_after_a_stopped_redesign_still_owes_the_new_look(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange.", Options(inputs=[]), redesign_graphics=True)
    provider.fail = None
    before = len(provider.calls)
    second = pipeline.revise(out, "Tighter hook.", Options(inputs=[]))  # says nothing about the graphics
    assert uses(second.fcpxml) == v1_clips and len(design_calls(provider, before)) == 0
    assert {g["redesign_pending"] for g in entries(out).values()} == {"design"} and second.summary["graphics"]["pending"] == 2
    third = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(provider, before)
    assert "Make the graphics orange." in call["parts"][-1].text  # the notes that asked for the look went with it
    assert third.summary["graphics"]["note"] is None and not set(uses(third.fcpxml)) & set(v1_clips)


def test_a_redesign_stopped_with_a_reworded_graphic_leaves_that_one_to_design_missing(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    provider.plan = plan_of(GRAPHIC, {**STAT, "text": "Three Weeks"})
    with pytest.raises(ProviderLimitError) as stop:
        pipeline.revise(out, "Make them bolder, and say three weeks.", Options(inputs=[]), redesign_graphics=True)
    assert "footage v2.fcpxml is written with 1 motion graphic, 1 of them in its earlier look because the new look wasn't designed" in str(stop.value)
    assert "designs the new look for 1 and the 1 without a design without re-planning the cut." in str(stop.value)
    row = result_json(out)["graphics"]
    assert (row["planned"], row["placed"], row["rendered"], row["pending"]) == (2, 2, 1, 1)
    assert row["note"].startswith("1 not designed: the usage limit was reached; 1 graphic keeps its earlier look: the new look wasn't designed because")
    assert set(entries(out)) == {"Heading Out", "Three Weeks"} and entries(out)["Heading Out"]["redesign_pending"] == "design"
    assert not entries(out)["Three Weeks"].get("html")

    provider.fail = None
    before = len(provider.calls)
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(provider, before)  # both in one request: each graphic is designed once
    assert sorted(designed_texts(call)) == ["Heading Out", "Three Weeks"]
    assert done.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}


def test_a_first_build_stopped_by_the_usage_limit_is_as_it_was(footage, tmp_path, monkeypatch, cache, clear_mov):
    provider = Fails(plan_of())
    provider.fail = ProviderLimitError(LIMIT)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    Renderer(monkeypatch, clear_mov)
    out = tmp_path / "out"
    with pytest.raises(ProviderLimitError) as stop:
        pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert "footage v1.fcpxml is written with 0 motion graphics; the plan and every design that came back are saved." in str(stop.value)
    assert f"designs the other 2 without re-planning the cut." in str(stop.value)
    assert result_json(out)["graphics"] == {"planned": 2, "placed": 2, "rendered": 0, "note": "2 not designed: the usage limit was reached"}
    assert not any("redesign_pending" in g for g in json.loads((out / "graphics.json").read_text())["graphics"])


# ------------------------------------------------------------------ (c) renders that fail after their designs came back


def test_a_redesign_whose_renders_all_fail_keeps_the_earlier_clips_and_says_why(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips, v1_html = uses(v1.fcpxml), {t: (out / g["html"]).read_text() for t, g in entries(out).items()}
    renderer.fail_all = True
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)

    assert uses(result.fcpxml) == v1_clips and top(out) == ["footage v2.fcpxml"]  # not fewer than v1 had
    marks = entries(out)
    assert {g["redesign_pending"] for g in marks.values()} == {"render"} and {g["pending_reason"] for g in marks.values()} == {"its new design didn't render"}
    assert all(g["rendered"] != g["digest"] for g in marks.values())  # the clip is the earlier design's
    assert {t: (out / g["html"]).read_text() != v1_html[t] for t, g in marks.items()} == {"Heading Out": True, "Weeks": True}  # the design is the new one
    row = result.summary["graphics"]
    assert (row["planned"], row["placed"], row["rendered"], row["pending"]) == (2, 2, 2, 2)
    assert row["note"] == f"2 graphics keep their earlier clip: the new design didn't render (see Warnings); `roughcut render {out}` renders them again"
    assert any(w.startswith("graphics G01, G02: the new designs didn't render, so they keep the clip the version before had") for w in result.warnings)
    items = result.changes["items"]
    assert "Graphics: 2 could not be rendered and keep their earlier clip (see Warnings)" in items
    assert 'Graphic "Heading Out": the new design didn\'t render, so it keeps its earlier clip' in items
    assert not any(i.startswith("Graphics: 2 redesigned") for i in items)
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)

    # The renderer works again: a plain re-cut renders the new designs (no model), and the flags go.
    renderer.fail_all = False
    renderer.rendered.clear()
    no_model(monkeypatch)
    done = pipeline.rerender(out, Options(inputs=[]))
    assert len(renderer.rendered) == 2 and not set(uses(done.fcpxml)) & set(v1_clips)
    assert done.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
    assert "redesign_pending" not in json.dumps(entries(out))
    assert all((out / "graphics" / name).is_file() for name in v1_clips)


def test_a_redesign_where_one_render_fails_keeps_just_that_clip(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    renderer.fail = {"G01"}  # the first on the timeline: the cold open's stat
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    marks = entries(out)
    pending = {t for t, g in marks.items() if g.get("redesign_pending")}
    assert len(pending) == 1 and result.summary["graphics"]["pending"] == 1 and result.summary["graphics"]["rendered"] == 2
    kept = next(g for g in marks.values() if g.get("redesign_pending"))
    assert kept["file"].rsplit("/", 1)[1] in v1_clips  # the clip it had
    assert next(g for g in marks.values() if not g.get("redesign_pending"))["file"].rsplit("/", 1)[1] not in v1_clips  # the other is new
    assert result.summary["graphics"]["note"].startswith("1 graphic keeps its earlier clip: the new design didn't render (see Warnings);")
    assert "Graphics: 1 redesigned, 1 could not be rendered and keeps its earlier clip (see Warnings)" in result.changes["items"]


def test_a_graphic_with_no_earlier_clip_that_fails_to_render_is_left_out_and_the_note_says_so(footage, tmp_path, monkeypatch, cache, clear_mov):
    provider = Fails(plan_of())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    renderer = Renderer(monkeypatch, clear_mov)
    renderer.fail_all = True
    out = tmp_path / "out"
    result = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert uses(result.fcpxml) == [] and result.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 0, "note": "2 didn't design or render; see Warnings"}
    # A re-cut with the renderer still broken says the same, and leaves them out; with it fixed they come back.
    again = pipeline.rerender(out, Options(inputs=[]), overrides={"style"})
    assert again.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 0, "note": "2 didn't design or render; see Warnings"}
    renderer.fail_all = False
    fixed = pipeline.rerender(out, Options(inputs=[]), overrides={"style"})
    assert len(uses(fixed.fcpxml)) == 2 and fixed.summary["graphics"]["note"] is None


def test_an_earlier_clip_of_another_length_is_not_put_in_the_place_of_a_new_one(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    renderer.fail_all = True
    provider.plan = plan_of({**GRAPHIC, "seconds": 4.0}, STAT)  # the title card now holds longer: the old clip is the wrong length
    result = pipeline.revise(out, "Hold the title longer, and make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert len(uses(result.fcpxml)) == 1 and set(uses(result.fcpxml)) <= set(v1_clips)
    row = result.summary["graphics"]
    assert (row["rendered"], row["pending"]) == (1, 1)
    assert row["note"].startswith("1 graphic keeps its earlier clip: the new design didn't render") and row["note"].endswith("1 didn't design or render; see Warnings")


def test_a_graphics_json_from_before_the_canvas_was_saved_is_checked_against_the_clip(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    manifest = json.loads((out / "graphics.json").read_text())
    del manifest["canvas"]  # as an older release wrote it
    (out / "graphics.json").write_text(json.dumps(manifest))
    renderer.fail_all = True
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    # The test clip is 320x180 in a 1920x1080 cut: the clip itself says it doesn't fit, so nothing stands in for the new renders.
    assert uses(result.fcpxml) == [] and result.summary["graphics"]["note"] == "2 didn't design or render; see Warnings"


def test_an_older_graphics_json_whose_clips_are_the_cuts_size_still_gives_them_to_a_failed_render(footage, tmp_path, monkeypatch, cache, full_size_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, full_size_mov)
    v1_clips = uses(v1.fcpxml)
    manifest = json.loads((out / "graphics.json").read_text())
    del manifest["canvas"]
    (out / "graphics.json").write_text(json.dumps(manifest))
    renderer.fail_all = True
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert uses(result.fcpxml) == v1_clips and result.summary["graphics"]["pending"] == 2


# ------------------------------------------------------------------ (d) a version with no graphics says why


def test_a_revision_that_returns_no_graphics_says_so_and_what_to_do(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.plan = {**CANNED_PLAN, "graphics": []}
    result = pipeline.revise(out, "Graphics: make them bolder, orange.", Options(inputs=[]), redesign_graphics=True)
    said = "No graphics in this version: the revision returned none. To bring graphics back, revise again and ask for the ones you want in your notes; they are designed again."
    assert said in result.warnings and not any("didn't ask for any" in w for w in result.warnings)
    assert said in result.changes["items"] and result.changes["items"].index(said) < result.changes["items"].index("Removed graphic: \"Heading Out\"")
    assert result.summary["graphics"]["note"] == "the revision returned none" and uses(result.fcpxml) == []
    report = (out / "edit_report.md").read_text()
    assert f"- {said}" in report.split("## Paper edit")[0]  # in the list of what changed, at the top of the paper edit


def test_a_revision_whose_graphics_are_on_lines_that_are_not_in_the_cut_says_that(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    # V01.S002 ("Um, wait, let me start over.") is not in the cut: the graphics asked for are dropped when the plan is checked.
    provider.plan = plan_of({**GRAPHIC, "segment": "V01.S002"}, {**STAT, "segment": "V01.S002"})
    result = pipeline.revise(out, "Graphics: make them bolder, orange.", Options(inputs=[]), redesign_graphics=True)
    said = "No graphics in this version: the revision asked for 2 graphics, but their lines aren't in this cut (see Warnings). Revise again and say which graphics to keep."
    assert said in result.warnings and any("V01.S002 isn't in the cut; skipped" in w for w in result.warnings)
    assert said in result.changes["items"] and result.summary["graphics"]["note"] == "their lines aren't in this cut"


def test_a_build_with_no_graphics_in_the_plan_says_what_it_always_said(footage, tmp_path, monkeypatch, cache):
    provider = FakeProvider({**CANNED_PLAN, "graphics": []})
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    result = pipeline.build(Options(inputs=[footage], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert "Motion graphics were on, but the plan didn't ask for any." in result.warnings
    assert result.summary["graphics"]["note"] == "the plan asked for none"
    provider.plan = plan_of({**GRAPHIC, "segment": "V01.S002"})
    other = pipeline.build(Options(inputs=[footage], out=tmp_path / "out2", name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert "No graphics in this cut: the plan asked for 1 graphic, but their lines aren't in the cut (see Warnings)." in other.warnings


def test_a_cut_none_of_whose_planned_graphics_fits_says_so(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    real_place = graphics.place
    monkeypatch.setattr(graphics, "place", lambda *a, **k: [])  # as if every graphic's words were cut
    result = pipeline.revise(out, "Cut the interview down.", Options(inputs=[]))
    said = "No graphics in this version: none of the 2 planned graphics fits this cut (their words aren't in it, or they would cover each other; see Warnings). Revise again and say which graphics to keep."
    assert said in result.warnings and result.summary["graphics"]["note"] == "none fit this cut (see Warnings)" and uses(result.fcpxml) == []
    other = pipeline.build(Options(inputs=[footage], out=tmp_path / "out2", name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert "No graphics in this cut: none of the 2 planned graphics fits it (their words aren't in it, or they would cover each other; see Warnings)." in other.warnings
    monkeypatch.setattr(graphics, "place", real_place)


# ------------------------------------------------------------------ (e) a re-cut after a revision brings the Stringout project back


def test_a_recut_of_a_revision_says_the_stringout_project_is_back(footage, tmp_path, monkeypatch, cache):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    first = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache))
    assert len(check_invariants(first.fcpxml)) == 2  # Rough Cut and Stringout
    revised = pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    assert len(check_invariants(revised.fcpxml)) == 1 and json.loads((out / "plan.json").read_text())["settings"]["stringout"] is True
    back = (
        "A Stringout project is back: v2 is a revision, which writes only the Rough Cut, and a re-cut writes both. "
        "To leave it out, turn Stringout off (Terminal: --no-stringout)."
    )
    same = pipeline.rerender(out, Options(inputs=[]), overrides=set())  # the same cut again
    assert same.changes["unchanged"] is True and same.changes["items"][-1] == back and len(check_invariants(same.fcpxml)) == 2
    assert back in same.report.read_text()
    tighter = pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    assert tighter.changes["items"][0] == "Pacing: medium → tight" and back not in tighter.changes["items"]  # v3 had one; this one carries on
    # A revision then a re-cut that leaves it out changes nothing about the project list (the revision wrote none): it says
    # nothing about it, as the app's Finish graphics needs; with the cut changed it lists only the change.
    pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    quiet = pipeline.rerender(out, Options(inputs=[], stringout=False), overrides={"stringout"})
    assert quiet.changes["unchanged"] is True and quiet.changes["items"][0].startswith("This is the same cut as v5")
    assert not any("Stringout" in i for i in quiet.changes["items"]) and len(check_invariants(quiet.fcpxml)) == 1
    assert json.loads((out / "plan.json").read_text())["settings"]["stringout"] is False  # the next re-cut carries on without it
    looser = pipeline.rerender(out, Options(inputs=[], style="loose"), overrides={"style"})
    assert looser.changes["items"][0].startswith("Pacing:") and not any("Stringout" in i for i in looser.changes["items"])


# ------------------------------------------------------------------ (f) importing a version after the first


def test_a_version_after_the_first_says_what_to_choose_when_final_cut_asks_about_same_names(footage, tmp_path, monkeypatch, cache, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    out = tmp_path / "out"
    keep_both = "If Final Cut asks about items with the same names, choose Keep Both: each version is its own project."
    args = ["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(cache)]
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert keep_both not in printed and keep_both not in (out / "edit_report.md").read_text()
    assert main(["render", str(out), "--style", "tight"]) == 0
    printed = capsys.readouterr().out
    assert printed.rstrip().endswith(keep_both) and "Then open the project \"footage - Rough Cut v2 (tight, fake-model)\"" in printed
    assert f"- **Importing:** {keep_both}" in (out / "edit_report.md").read_text()
    assert keep_both in (out / "edit_report.html").read_text()


# ------------------------------------------------------------------ a build, revise or re-cut that goes right is as it was


def test_runs_that_go_right_write_no_partial_pending_or_note_keys(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    assert result_json(out)["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
    for step in (
        lambda: pipeline.revise(out, "Tighter hook.", Options(inputs=[])),
        lambda: pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True),
        lambda: pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"}),
    ):
        result = step()
        assert top(out) == [f"footage v{result.version}.fcpxml"] and len(uses(result.fcpxml)) == 2
        assert "partial" not in result_json(out) and versions.current(out).get("partial") is None
        assert result.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
        text = json.dumps(json.loads((out / "graphics.json").read_text()))
        assert "redesign_pending" not in text and "pending_reason" not in text
        assert not any(w.startswith(("The new look for the graphics", "This cut is partial", "No graphics in this")) for w in result.warnings)
        validate_dtd(result.fcpxml)
        check_invariants(result.fcpxml)


# ------------------------------------------------------------------ the Mac app's Finish graphics button


def app_offers_finish_graphics(row: dict) -> bool:
    """RunResult.canFinishGraphics in mac/Sources/RoughcutKit/StoppedRun.swift, on a done payload or a result.json."""
    g = row.get("graphics")
    if not g or g["planned"] <= 0:
        return False
    return bool(row.get("partial")) or g.get("pending", 0) + max(g["placed"] - g["rendered"], 0) > 0


def test_the_apps_finish_graphics_button_is_offered_exactly_where_graphics_are_still_to_be_made(footage, tmp_path, monkeypatch, cache, clear_mov):
    """The app decides from `graphics` (planned / placed / rendered / pending) and `partial` alone: this is what the engine
    writes in each case, so a change to those keys that the Swift rule doesn't follow fails here."""
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    assert not app_offers_finish_graphics(result_json(out))  # everything made

    # A revision that stops after its cut without graphics: the partial version, with nothing on the timeline yet.
    real_make = graphics.make
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the renderer crashed")))
    with pytest.raises(RuntimeError):
        pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    monkeypatch.setattr(graphics, "make", real_make)
    assert result_json(out)["partial"] is True and app_offers_finish_graphics(result_json(out))

    # Finish graphics (render --design-missing) makes them; nothing is left to finish.
    pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert "partial" not in result_json(out) and not app_offers_finish_graphics(result_json(out))

    # The usage limit during a redesign: the version stands on the earlier look (pending), until the reset.
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make them orange.", Options(inputs=[]), redesign_graphics=True)
    assert result_json(out)["graphics"]["pending"] == 2 and app_offers_finish_graphics(result_json(out))
    provider.fail = None
    pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert not app_offers_finish_graphics(result_json(out))

    # Every render fails after the designs came back and no earlier clip fits: placed, none rendered.
    out2 = tmp_path / "out2"
    renderer.fail_all = True
    failed = pipeline.build(Options(inputs=[footage], out=out2, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert (failed.summary["graphics"]["placed"], failed.summary["graphics"]["rendered"]) == (2, 0)
    assert app_offers_finish_graphics(result_json(out2))
    renderer.fail_all = False

    # A first build the usage limit stopped while designing: placed, not designed.
    provider.fail = ProviderLimitError(LIMIT)
    out3 = tmp_path / "out3"
    with pytest.raises(ProviderLimitError):
        pipeline.build(Options(inputs=[footage], out=out3, name="footage", transcriber="none", cache_dir=cache, graphics=True))
    assert app_offers_finish_graphics(result_json(out3))
    provider.fail = None

    # Nothing to make: the plan's graphics don't fit the cut (the revision needs to say which to keep), or it asked for none.
    monkeypatch.setattr(graphics, "place", lambda *a, **k: [])
    none_fit = pipeline.revise(out, "Cut the interview down.", Options(inputs=[]))
    assert none_fit.summary["graphics"]["planned"] == 2 and not app_offers_finish_graphics(result_json(out))

"""A revision that is about how the graphics look designs them again; one that isn't keeps every design."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd
from test_graphics_refs import GRAPHIC, STAT, make_pictures

from roughcut import graphics, pipeline
from roughcut.cli import main
from roughcut.pipeline import Options

UNCHANGED = (
    "This version plays the same lines, B-roll and graphics as v1. "
    "If you meant how the graphics look, attach reference images or tick Redesign graphics."
)


@pytest.fixture(scope="module")
def shared_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("redesign-analysis-cache")


@pytest.fixture(scope="module")
def clear_mov(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graphic") / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


class Restyles(FakeProvider):
    """A model that designs differently every time it is asked, so a design made again is a new file."""

    designs = 0

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        reply = super().complete_json(system, parts, schema, schema_name=schema_name, **kw)
        if schema_name == "graphics":
            Restyles.designs += 1
            reply = {**reply, "graphics": [{**g, "html": g["html"] + f"<!-- design {Restyles.designs} -->"} for g in reply["graphics"]]}
        return reply


def plan_of(*specs, **more) -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [dict(s) for s in (specs or (GRAPHIC, STAT))]
    plan.update(more)
    return plan


def built(footage, tmp_path, monkeypatch, cache, mov, plan=None, **opts) -> tuple[Path, FakeProvider]:
    provider = Restyles(plan or plan_of())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)

    def render_one(job):
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "console": []}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True, **opts))
    return out, provider


def asked(provider) -> list[dict]:
    return [c for c in provider.calls if c["schema_name"] == "graphics"]


def designed_texts(call) -> list[str]:
    return [g["text"] for g in json.loads(call["parts"][-1].text.split("<graphics>")[1].split("</graphics>")[0])]


def settings_of(out: Path) -> dict:
    return json.loads((out / "plan.json").read_text())["settings"]


def manifest_of(out: Path) -> dict:
    return json.loads((out / "graphics.json").read_text())


def clips_in(out: Path) -> set[str]:
    return {p.name for p in (out / "graphics").glob("*.mov")}


def test_a_revision_that_changes_nothing_about_the_graphics_keeps_every_design(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    before = len(asked(provider))
    clips = clips_in(out)
    result = pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    assert len(asked(provider)) == before  # no design call at all
    assert clips_in(out) == clips and sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2
    # The plan didn't change either: the app is told so, with what to try.
    assert result.changes == {"from_version": 1, "items": [UNCHANGED], "unchanged": True}
    assert json.loads((out / "result.json").read_text())["changes"] == result.changes
    assert UNCHANGED in result.report.read_text()


@pytest.mark.parametrize("how", ["flag", "pictures", "planner"])
def test_every_graphic_is_designed_again_when_asked_for_one_way_or_another(how, footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    assert manifest_of(out)["house_style"] == "test plate"
    old_clips = clips_in(out)
    old_html = {g["html"]: (out / g["html"]).read_text() for g in manifest_of(out)["graphics"]}
    before = len(asked(provider))
    notes = "Make the graphics bolder and orange."
    opts = Options(inputs=[])
    kw: dict = {}
    if how == "flag":
        kw["redesign_graphics"] = True
        note = "Redesigning the graphics: every graphic is designed again, as asked."
    elif how == "pictures":
        opts = Options(inputs=[], graphics_refs=tuple(str(p) for p in make_pictures(tmp_path / "board", 2)))
        note = "Redesigning the graphics: the new reference images set the look."
    else:
        provider.plan = plan_of(redesign_graphics="yes")
        note = "Redesigning the graphics: your notes ask for a different look."
    result = pipeline.revise(out, notes, opts, **kw)

    (call,) = asked(provider)[before:]  # one request, both graphics
    assert sorted(designed_texts(call)) == ["Heading Out", "Weeks"]
    request = call["parts"][-1].text
    assert f"<editor_direction>\nThe editor's notes for this revision, written about the whole cut. Follow what concerns how the graphics look or move, and ignore the rest:\n{notes}\n</editor_direction>" in request
    assert "<design_system>" not in request and "Match the graphics already made" not in request  # the earlier look doesn't hold them to it
    assert note in result.report.read_text() and note in [i for i in result.changes["items"]]
    assert any(i.startswith("Graphics: 2 redesigned") for i in result.changes["items"]) and result.changes["unchanged"] is False
    assert note in (out / "roughcut.log").read_text()

    # The earlier designs and clips stay (history keeps v1's designs; Final Cut reads the old clips in place)...
    history = next((out / "history").glob("v1 *"))
    assert {rel: (history / rel).read_text() for rel in old_html} == old_html
    assert old_clips <= clips_in(out) and len(clips_in(out)) > len(old_clips)
    # ...and the new designs are new files, in the cut.
    assert {(out / g["html"]).read_text() for g in manifest_of(out)["graphics"]}.isdisjoint(old_html.values())
    assert manifest_of(out)["house_style"] == "test plate" and sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)

    if how == "pictures":  # and they show the designer the new pictures, saved in place of the old ones
        assert call["parts"][0].text.startswith("The editor attached 2 reference images")
        assert len(settings_of(out)["graphics_refs"]) == 2
    else:
        assert not any("reference image" in getattr(p, "text", "") for p in call["parts"])


def test_the_planner_is_asked_and_its_no_or_silence_keeps_the_designs(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    before = len(asked(provider))
    for answer in ("no", "unspecified", None):
        plan = plan_of(**({} if answer is None else {"redesign_graphics": answer}))
        provider.plan = plan
        pipeline.revise(out, "Change the words of the title card.", Options(inputs=[]))
    assert len(asked(provider)) == before
    request = next(c for c in reversed(provider.calls) if c["purpose"] == "revision")["parts"][1].text
    assert 'redesign_graphics: yes only when these notes ask to change how the graphics look or move' in request
    assert "Return each graphic's direction exactly as <previous_plan> gives it unless these notes change it" in request
    assert "redesign_graphics" in pipeline.plan_schema(True)["required"] and pipeline.plan_schema(False)["properties"]["redesign_graphics"]["enum"] == ["yes", "no", "unspecified"]
    # A build is never asked to read it, and a revision's answer is kept in its plan.
    first_plan = next(c for c in provider.calls if c["purpose"] == "edit plan")["parts"][-1].text
    assert "redesign_graphics" not in first_plan
    provider.plan = plan_of(redesign_graphics="yes")
    pipeline.revise(out, "Make them bolder.", Options(inputs=[]))
    assert json.loads((out / "plan.json").read_text())["plan"]["redesign_graphics"] == "yes"


def test_a_planner_that_leaves_the_field_out_or_garbles_it_means_unspecified():
    from roughcut.analyze import Analysis
    from roughcut.plan import normalize

    from conftest import SPEECH, make_clip

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    base = {"sections": [{"name": "x", "items": ["V01.S003"], "broll": []}]}
    assert normalize(base, a).redesign_graphics == "unspecified"
    assert normalize({**base, "redesign_graphics": " YES "}, a).redesign_graphics == "yes"
    assert normalize({**base, "redesign_graphics": True}, a).redesign_graphics == "yes"
    assert normalize({**base, "redesign_graphics": "maybe"}, a).redesign_graphics == "unspecified"
    assert normalize({**base, "redesign_graphics": ["yes"]}, a).redesign_graphics == "unspecified"
    assert normalize({**base, "redesign_graphics": "no"}, a).to_json()["redesign_graphics"] == "no"


def test_a_changed_direction_designs_just_that_graphic(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, plan=plan_of({**GRAPHIC, "direction": "Bold  Left"}, STAT))
    before = len(asked(provider))

    same = plan_of({**GRAPHIC, "direction": " bold left "}, STAT)  # case and spacing aren't a change
    provider.plan = same
    result = pipeline.revise(out, "Keep the graphics.", Options(inputs=[]))
    assert len(asked(provider)) == before and result.changes["unchanged"] is True

    clips = clips_in(out)
    changed = plan_of({**GRAPHIC, "direction": "bold, bottom right"}, STAT)
    provider.plan = changed
    result = pipeline.revise(out, "Put the title card bottom right.", Options(inputs=[]))
    (call,) = asked(provider)[before:]
    assert designed_texts(call) == ["Heading Out"]  # the other graphic keeps its design
    assert "bold, bottom right" in call["parts"][-1].text and "Match the graphics already made" in call["parts"][-1].text
    items = result.changes["items"]
    assert 'Graphic "Heading Out": direction now "bold, bottom right"; designed again' in items
    assert any(i.startswith("Graphics: 1 kept as they were, 1 changed and designed again") for i in items)
    assert len(clips_in(out)) == len(clips) + 1  # one new clip
    # What the reviser is shown is the direction it must return unless the notes change it.
    request = next(c for c in reversed(provider.calls) if c["purpose"] == "revision")["parts"][1].text
    assert '(direction: "bold left")' in request  # the previous plan's, as the model returned it
    pipeline.revise(out, "Keep the graphics.", Options(inputs=[]))
    request = next(c for c in reversed(provider.calls) if c["purpose"] == "revision")["parts"][1].text
    assert '(direction: "bold, bottom right")' in request


def test_a_folder_whose_graphics_json_has_no_direction_still_matches_its_designs(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    for planned in ("", "bold left"):
        out, provider = built(footage, tmp_path / (planned or "plain").replace(" ", "-"), monkeypatch, shared_cache, clear_mov,
                              plan=plan_of({**GRAPHIC, "direction": planned}, {**STAT, "direction": planned}))
        manifest = manifest_of(out)
        for entry in manifest["graphics"]:
            del entry["direction"]  # as an older release wrote it
        (out / "graphics.json").write_text(json.dumps(manifest))
        before = len(asked(provider))
        result = pipeline.revise(out, "Keep it as it is.", Options(inputs=[]))
        assert len(asked(provider)) == before, planned  # same graphics, same designs
        assert result.changes["unchanged"] is True
        assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2


def test_redesigning_with_graphics_off_says_so_and_designs_nothing(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    before = len(asked(provider))
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), graphics=False, redesign_graphics=True)
    assert len(asked(provider)) == before
    assert "Redesign graphics was asked for, but motion graphics are off for this cut, so there is nothing to redesign." in result.warnings
    assert not any(c.kind == "graphic" for c in result.timeline.connected)
    assert any(i.startswith("Motion graphics are off: the 2 graphics of v1 are not in this version") for i in result.changes["items"])

    # Pictures with graphics off: left out with a warning; the pictures the run has already are kept for a later --graphics.
    out2, provider2 = built(footage, tmp_path / "second", monkeypatch, shared_cache, clear_mov, graphics_refs=tuple(str(p) for p in make_pictures(tmp_path / "board", 1)))
    saved = settings_of(out2)["graphics_refs"]
    assert len(saved) == 1
    other = make_pictures(tmp_path / "other", 1)[0]
    result = pipeline.revise(out2, "Plain.", Options(inputs=[], graphics_refs=(str(other),)), graphics=False)
    assert "Reference images are only used by motion graphics, which are off for this cut, so they were left out." in result.warnings
    assert settings_of(out2)["graphics_refs"] == saved

    # The planner reading a look change when they are off is answered with today's warning.
    provider2.plan = plan_of(redesign_graphics="yes")
    result = pipeline.revise(out2, "Make the graphics orange.", Options(inputs=[]), graphics=False)
    assert any(w.startswith("Your notes ask for graphics, but motion graphics are off for this cut") for w in result.warnings)


def test_pictures_replace_the_runs_earlier_ones_and_the_settings_keep_stored_names_only(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    first = make_pictures(tmp_path / "board", 2)
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, graphics_refs=tuple(str(p) for p in first))
    old = settings_of(out)["graphics_refs"]
    assert len(old) == 2
    new = make_pictures(tmp_path / "newboard", 3)[2]
    pipeline.revise(out, "", Options(inputs=[], graphics_refs=(str(new),)))  # no notes: the pictures are the request
    now = settings_of(out)["graphics_refs"]
    assert len(now) == 1 and now != old and all((out / n).is_file() for n in old + now)  # the earlier files stay
    texts = json.dumps(json.loads((out / "plan.json").read_text())) + (out / "graphics.json").read_text()
    assert str(new) not in texts and str(tmp_path / "newboard") not in texts
    (call,) = asked(provider)[-1:]
    assert "<editor_direction>" not in call["parts"][-1].text  # no notes, nothing to direct with

    # A later revision with no pictures given keeps them for new designs.
    changed = plan_of(GRAPHIC, {**STAT, "text": "Three Weeks"})
    provider.plan = changed
    before = len(asked(provider))
    pipeline.revise(out, "Say three weeks.", Options(inputs=[]))
    (call,) = asked(provider)[before:]
    assert call["parts"][0].text.startswith("The editor attached 1 reference image ") and settings_of(out)["graphics_refs"] == now


def test_the_command_line_redesigns_and_prints_what_changed(footage, tmp_path, monkeypatch, shared_cache, clear_mov, capsys):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    capsys.readouterr()
    assert main(["revise", str(out), "Tighter hook."]) == 0
    printed = capsys.readouterr().out
    assert "Nothing visible changed from v1:" in printed and UNCHANGED in printed
    assert "--graphics-ref IMAGE" in printed and "--redesign-graphics" in printed  # the Terminal words for the same advice

    assert main(["revise", str(out), "--redesign-graphics"]) == 0  # no notes needed
    printed = capsys.readouterr().out
    assert "Changed from v2:" in printed and "Graphics: 2 redesigned" in printed
    assert "Redesigning the graphics: every graphic is designed again, as asked." in printed

    capsys.readouterr()
    assert main(["revise", str(out)]) == 1  # still needs something to do
    assert "Say what to change" in capsys.readouterr().err

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
from roughcut.cli import main, summary_lines
from roughcut.pipeline import Options
from roughcut.providers import ImagePart, ProviderError, ProviderLimitError

UNCHANGED = (
    "This version plays the same lines, B-roll and graphics as v1. If you meant how the graphics look, attach reference images "
    "or turn on Redesign the graphics (Terminal: --graphics-ref IMAGE or --redesign-graphics)."
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


class Fails(Restyles):
    """A designer that can be made to fail, or to run out of plan, after the build: set `fail` to the error it raises."""

    fail: Exception | None = None

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        if schema_name == "graphics" and self.fail is not None:
            self.calls.append({"system": system, "parts": parts, "schema_name": schema_name, "purpose": kw.get("purpose")})
            raise self.fail
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


class RejectsPictures(Restyles):
    """A model that turns down any design request carrying the editor's reference images (as a provider does for a picture it won't take)."""

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        if schema_name == "graphics" and any(getattr(p, "text", "").startswith("The editor attached") for p in parts):
            self.calls.append({"system": system, "parts": parts, "schema_name": schema_name, "purpose": kw.get("purpose")})
            raise ProviderError("the image could not be processed")
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


def plan_of(*specs, **more) -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [dict(s) for s in (specs or (GRAPHIC, STAT))]
    plan.update(more)
    return plan


def built(footage, tmp_path, monkeypatch, cache, mov, plan=None, provider_class=Restyles, **opts) -> tuple[Path, FakeProvider]:
    provider = provider_class(plan or plan_of())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)

    def render_one(job):
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "console": []}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)
    out = tmp_path / "out"
    provider.result = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, graphics=True, **opts))
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


def clips_in_cut(out: Path) -> list[str]:
    """The graphics clips the newest version's FCPXML uses."""
    from roughcut import versions

    return versions.graphics_in([out / versions.current(out)["fcpxml"]])


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
    assert "Terminal: --graphics-ref IMAGE or --redesign-graphics" in printed  # the Terminal words are in the sentence

    assert main(["revise", str(out), "--redesign-graphics"]) == 0  # no notes needed
    printed = capsys.readouterr().out
    assert "Changed from v2:" in printed and "Graphics: 2 redesigned" in printed
    assert "Redesigning the graphics: every graphic is designed again, as asked." in printed

    capsys.readouterr()
    assert main(["revise", str(out)]) == 1  # still needs something to do
    assert "Say what to change" in capsys.readouterr().err


def test_redesigning_a_cut_that_has_no_graphics_says_so(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, plan=plan_of())
    provider.plan = {**CANNED_PLAN, "graphics": []}  # the reviser drops them all
    before = len(asked(provider))
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert len(asked(provider)) == before
    note = "This cut has no graphics to redesign: ask for some in your notes, for example a lower third when someone is named."
    assert note in result.changes["items"]
    assert not any(i.startswith("Redesigning the graphics") for i in result.changes["items"])
    assert any(i.startswith("Graphics: ") and "2 removed" in i for i in result.changes["items"])  # they went with the plan

    again = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)  # nothing else changed now
    assert again.changes["unchanged"] is True
    assert again.changes["items"] == ["This version plays the same lines and B-roll as v2.", note]  # what changed nothing comes first


# ------------------------------------------------------------------ what a redesign came to, and what it builds on


def test_a_redesign_whose_designs_never_come_back_keeps_the_earlier_ones_and_says_so(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=Fails)
    clips, old = clips_in(out), {g["html"]: (out / g["html"]).read_text() for g in manifest_of(out)["graphics"]}
    provider.fail = ProviderError("overloaded")
    before = len(asked(provider))
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert len(asked(provider)) == before + 1  # asked once (no pictures to leave out, so no second try)

    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2 and clips_in(out) == clips  # the same two clips
    kept = [w for w in result.warnings if "no new design came back, so it keeps its earlier design" in w]
    assert len(kept) == 2 and any("graphics design failed" in w and "overloaded" in w for w in result.warnings)
    assert {(out / g["html"]).read_text() for g in manifest_of(out)["graphics"]} == set(old.values())
    assert manifest_of(out)["house_style"] == "test plate"
    items = result.changes["items"]
    assert "Graphics: 2 got no new design and keep their earlier one (see Warnings)" in items
    assert not any(i.startswith("Graphics: 2 redesigned") for i in items)  # it doesn't claim what didn't happen
    assert result.changes["unchanged"] is False and "Graphics: 2 got no new design" in result.report.read_text()
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)


def test_a_redesigned_graphic_with_new_words_and_no_design_is_left_out_not_called_new(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=Fails)
    provider.fail = ProviderError("overloaded")
    provider.plan = plan_of(GRAPHIC, {**STAT, "text": "Three Weeks"})  # the reviser rewords one of them
    result = pipeline.revise(out, "Make the graphics bolder, and say three weeks.", Options(inputs=[]), redesign_graphics=True)
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 1  # the reworded one has no design to fall back on
    items = result.changes["items"]
    assert "Graphics: 1 got no new design and keeps its earlier one, 1 could not be made and is not in this version (see Warnings)" in items
    assert 'Graphic "Three Weeks": could not be made, so it is not in this version' in items
    assert not any(i.startswith("New graphic") for i in items)


def test_a_redesign_does_not_call_a_graphic_that_never_had_room_a_failure(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    crowded = {**GRAPHIC, "kind": "quote", "text": "Crowded Out"}  # on the same words as the title card: one graphic at a time
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, plan=plan_of(GRAPHIC, STAT, crowded))
    assert len(manifest_of(out)["graphics"]) == 2  # it never had room, in the build or after
    result = pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2
    items = result.changes["items"]
    assert "Graphics: 2 redesigned, 1 had no room in this cut (see Warnings)" in items
    assert 'Graphic "Crowded Out": had no room in this cut (see Warnings)' in items
    assert not any("could not be made" in i for i in items)  # nothing failed
    assert any("would cover" in w and "Crowded Out" in w for w in result.warnings)  # the warning the line points to
    assert any("had no room in this cut" in i for i in json.loads((out / "result.json").read_text())["changes"]["items"])


@pytest.mark.parametrize("how", ["flag", "planner"])
def test_a_redesign_stopped_by_the_usage_limit_is_finished_in_the_look_the_notes_asked_for(how, footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=Fails)
    notes = "Make the graphics orange and bold."
    kw: dict = {}
    if how == "flag":
        kw["redesign_graphics"] = True
    else:
        provider.plan = plan_of(redesign_graphics="yes")
    provider.fail = ProviderLimitError("You've hit your usage limit.")
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, notes, Options(inputs=[]), **kw)
    # Their earlier designs don't quietly stand in for them: they are still to be made, and the version says so.
    saved = json.loads((out / "result.json").read_text())
    assert saved["changes"]["items"].count("Graphics: 2 could not be made and are not in this version (see Warnings)") == 1
    assert settings_of(out)["graphics_direction"] == [notes] and pipeline.undesigned(out) == 2

    provider.fail = None
    before = len(asked(provider))
    result = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = asked(provider)[before:]
    request = call["parts"][-1].text
    assert f"graphics look or move, and ignore the rest:\n{notes}\n</editor_direction>" in request  # the notes went with the resume
    assert ("<current_look>" in request) == (how == "planner")  # notes alone change the look they have now; the flag starts over
    assert "<design_system>" not in request
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2


def test_the_graphics_a_stopped_redesign_gets_when_it_resumes_are_added_not_rendered_again(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=Fails)
    provider.fail = ProviderLimitError("You've hit your usage limit.")
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange.", Options(inputs=[]), redesign_graphics=True)
    assert clips_in_cut(out) == []  # the stopped version has no graphics on its timeline

    provider.fail = None
    result = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2
    items = result.changes["items"]
    # They are on the timeline for the first time: nothing was rendered again, no design of theirs was edited.
    assert "Graphics: 2 added to the cut" in items
    assert not any("rendered again" in i for i in items)
    assert "Graphics: 2 added to the cut" in result.report.read_text()

    # A design edited by hand afterwards replaces the clip the version before had: that one is rendered again.
    html = next((out / "graphics").glob("G*-stat.html"))
    html.write_text(html.read_text().replace("#F26B21", "#2155F2"))
    edited = pipeline.rerender(out, Options(inputs=[]))
    assert edited.changes["items"] == ["Graphics: 1 was rendered again (a design you edited, or timing that moved)"]


def test_look_notes_build_on_each_other_until_something_starts_the_look_over(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    before = len(asked(provider))
    first, second, third = "Make the graphics orange and bold.", "Now make them a bit larger.", "Like this."

    pipeline.revise(out, first, Options(inputs=[]), redesign_graphics=True)
    request = asked(provider)[before]["parts"][-1].text
    assert f"{first}\n</editor_direction>" in request and "<current_look>" not in request  # the flag starts the look over
    assert settings_of(out)["graphics_direction"] == [first]

    provider.plan = plan_of(redesign_graphics="yes")
    result = pipeline.revise(out, second, Options(inputs=[]))
    request = asked(provider)[before + 1]["parts"][-1].text
    assert f"oldest first; a later note wins where it disagrees with an earlier one." in request
    assert f"\n1. {first}\n2. {second}\n</editor_direction>" in request  # the orange is not forgotten
    assert "<current_look>\nHow these graphics look now. The editor's notes ask for a different look: change what they ask for and keep the rest of it:\ntest plate" in request
    assert "<design_system>" not in request and "Match the graphics already made" not in request
    assert settings_of(out)["graphics_direction"] == [first, second]
    assert "Redesigning the graphics: your notes ask for a different look." in result.changes["items"]

    # A revision that leaves the look alone keeps the notes and sends none.
    provider.plan = plan_of()
    pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    assert len(asked(provider)) == before + 2 and settings_of(out)["graphics_direction"] == [first, second]
    # A re-cut keeps them too.
    pipeline.rerender(out, Options(inputs=[]), overrides=set())
    assert settings_of(out)["graphics_direction"] == [first, second]

    # Asking again from the flag, or from new pictures, starts the list over.
    pipeline.revise(out, third, Options(inputs=[]), redesign_graphics=True)
    assert settings_of(out)["graphics_direction"] == [third]
    pipeline.revise(out, "", Options(inputs=[], graphics_refs=tuple(str(p) for p in make_pictures(tmp_path / "board", 1))))
    assert settings_of(out)["graphics_direction"] == []


def test_a_request_the_model_turns_down_with_the_pictures_is_sent_again_without_them(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    pictures = tuple(str(p) for p in make_pictures(tmp_path / "board", 2))
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=RejectsPictures, graphics_refs=pictures)
    calls = asked(provider)
    assert len(calls) == 2  # with the pictures, turned down; then without
    assert any(isinstance(p, ImagePart) for p in calls[0]["parts"][:3]) and calls[0]["parts"][0].text.startswith("The editor attached 2 reference images")
    assert not any(getattr(p, "text", "").startswith("The editor attached") for p in calls[1]["parts"])
    manifest = manifest_of(out)
    assert len(manifest["graphics"]) == 2 and all(g.get("html") for g in manifest["graphics"])  # the graphics were made
    log = (out / "roughcut.log").read_text()
    assert "a request with the reference images failed and was sent again without them" in log
    warning = next(w for w in provider.result.warnings if "turned down the design request" in w)
    assert warning.startswith("The model turned down the design request for G01-G02 (the image could not be processed) with the reference images")
    assert "designed without seeing the look you attached" in warning and warning in (out / "edit_report.md").read_text()
    assert provider.result.summary["references"] == {"stored": 2, "used": 0, "unseen": False, "dropped": True}
    assert "(a request with them failed and was sent again without them; see Warnings)" in "\n".join(summary_lines(provider.result.summary))


def test_a_failed_request_without_pictures_is_not_sent_twice(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, provider_class=Fails)
    provider.fail = ProviderError("overloaded")
    before = len(asked(provider))
    pipeline.revise(out, "Make them bolder.", Options(inputs=[]), redesign_graphics=True)
    assert len(asked(provider)) == before + 1


def test_only_pictures_or_the_switch_designs_the_graphics_again_and_the_planner_is_not_asked(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    revisions = lambda: [c for c in provider.calls if c["purpose"] == "revision"]  # noqa: E731
    lines = lambda r: [i.ref for s in r.plan.sections for i in s.items]  # noqa: E731
    first = pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    assert len(revisions()) == 1
    before = len(asked(provider))

    picture = make_pictures(tmp_path / "board", 1)[0]
    with_picture = pipeline.revise(out, "", Options(inputs=[], graphics_refs=(str(picture),)))
    flagged = pipeline.revise(out, "", Options(inputs=[]), redesign_graphics=True)
    assert len(revisions()) == 1 and len(asked(provider)) == before + 2  # no model call for the cut; one design call each
    assert lines(with_picture) == lines(first) == lines(flagged)
    assert with_picture.changes["items"] == [
        "Redesigning the graphics: the new reference images set the look.",
        "Graphics: 2 redesigned (every graphic is designed again)",
    ]  # nothing about the lines or the length: they are the same
    assert "No model call" in (out / "prompt.md").read_text()
    assert "no notes: the cut stays as it is" in (out / "roughcut.log").read_text()
    # What the earlier plan read from its brief (pacing, punch-ins) isn't applied again as if these notes said it.
    assert not any(i.startswith(("Pacing:", "Punch-ins:")) for i in flagged.changes["items"])

    # With notes, or with a change to the graphics setting, the planner is asked as before.
    pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert len(revisions()) == 2


def test_a_graphic_held_longer_keeps_its_design_and_is_not_called_unchanged(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov)
    before = len(asked(provider))
    provider.plan = plan_of({**GRAPHIC, "seconds": 5.0}, STAT)
    result = pipeline.revise(out, "Hold the title longer.", Options(inputs=[]))
    assert len(asked(provider)) == before  # the design is kept
    assert result.changes["unchanged"] is False
    assert 'Graphic "Heading Out": now holds 5 s (was 2.5 s); design kept' in result.changes["items"]
    assert "Graphics: 1 kept as they were, 1 kept with another length" in result.changes["items"]


def test_a_recut_does_not_lose_the_direction_an_older_graphics_json_never_saved(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    out, provider = built(footage, tmp_path, monkeypatch, shared_cache, clear_mov, plan=plan_of({**GRAPHIC, "direction": "left"}, {**STAT, "direction": "left"}))
    manifest = manifest_of(out)
    for entry in manifest["graphics"]:
        del entry["direction"]  # as an older release wrote it
    (out / "graphics.json").write_text(json.dumps(manifest))
    pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    assert [g["direction"] for g in manifest_of(out)["graphics"]] == ["left", "left"]
    before = len(asked(provider))
    result = pipeline.revise(out, "Keep it as it is.", Options(inputs=[]))
    assert len(asked(provider)) == before and result.changes["unchanged"] is True

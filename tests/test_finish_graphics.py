"""Finishing a stopped revision's graphics (`render --design-missing`): the look it is finished in, what the stopped cut says it
changed, what a still-active usage limit writes, and the flags of graphics a cut has no room for.

The stops themselves, and what they leave on the top level, are in test_unfinished_graphics.py.
"""

from __future__ import annotations

import json

import pytest
from test_end_to_end import check_invariants, validate_dtd
from test_graphics_refs import make_pictures
from test_redesign import plan_of
from test_unfinished_graphics import (  # noqa: F401 - fixtures
    LIMIT,
    _reset_progress,
    build_v1,
    cache,
    clear_mov,
    design_calls,
    entries,
    no_model,
    result_json,
    settings_of,
    top,
    uses,
)

from roughcut import graphics, pipeline, versions
from roughcut.cli import main
from roughcut.pipeline import Options, RoughcutError
from roughcut.providers import ProviderLimitError
from roughcut.revise import GRAPHICS_NOT_YET, NO_NEW_LOOK, recut_items, redesign_notes

LOWER = {"kind": "lower_third", "segment": "V01.S004", "words": "going to be a long day", "text": "Long Day", "subtext": "", "seconds": 2.0, "direction": ""}


# ------------------------------------------------------------------ the look a stopped redesign is finished in


@pytest.mark.parametrize("how", ["flag", "pictures"])
def test_a_redesign_without_notes_is_finished_from_the_pictures_or_the_flag_not_the_earlier_look(how, footage, tmp_path, monkeypatch, cache, clear_mov):
    """Pictures or --redesign-graphics alone start the look over: the earlier look the graphics keep until then isn't what the
    new designs are matched to (`<design_system>` says "match it exactly")."""
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    v1_clips = uses(v1.fcpxml)
    provider.fail = ProviderLimitError(LIMIT)
    opts, kw = Options(inputs=[]), {}
    if how == "flag":
        kw["redesign_graphics"] = True
    else:
        opts = Options(inputs=[], graphics_refs=tuple(str(p) for p in make_pictures(tmp_path / "board", 1)))
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "", opts, **kw)
    assert settings_of(out)["graphics_redesign"] == "fresh" and not settings_of(out).get("graphics_direction")  # no notes to send
    assert {g["redesign_pending"] for g in entries(out).values()} == {"design"} and uses(out / "footage v2.fcpxml") == v1_clips

    provider.fail = None
    before = len(provider.calls)
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(provider, before)
    request = call["parts"][-1].text
    assert "<design_system>" not in request and "<current_look>" not in request and "Match the graphics already made" not in request
    assert not set(uses(done.fcpxml)) & set(v1_clips) and done.summary["graphics"] == {"planned": 2, "placed": 2, "rendered": 2, "note": None}
    assert "redesign_pending" not in json.dumps(entries(out))


def test_a_plain_revision_between_a_stopped_redesign_and_finishing_it_leaves_the_new_look_owed_to_every_graphic(footage, tmp_path, monkeypatch, cache, clear_mov):
    """A graphic the plain revision adds is designed in the look the others still have: it waits with them, so finishing designs
    all three from the notes that asked for the new look, and none is called finished in the old one."""
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    notes = "Make the graphics orange and bold."
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, notes, Options(inputs=[]), redesign_graphics=True)
    provider.fail = None
    provider.plan = plan_of(provider.plan["graphics"][0], provider.plan["graphics"][1], LOWER)
    plain = pipeline.revise(out, "Add a lower third.", Options(inputs=[]))
    marks = entries(out)
    assert sorted(marks) == ["Heading Out", "Long Day", "Weeks"] and {g["redesign_pending"] for g in marks.values()} == {"design"}
    assert marks["Long Day"]["pending_reason"] == "designed in the earlier look"
    assert plain.summary["graphics"]["pending"] == 3

    before = len(provider.calls)
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(provider, before)
    request = call["parts"][-1].text
    assert notes in request and "<design_system>" not in request  # the notes went with all of them
    assert sorted(g["text"] for g in json.loads(request.split("<graphics>")[1].split("</graphics>")[0])) == ["Heading Out", "Long Day", "Weeks"]
    assert done.summary["graphics"] == {"planned": 3, "placed": 3, "rendered": 3, "note": None}
    assert "redesign_pending" not in json.dumps(entries(out))


def test_a_pending_graphic_a_re_cut_has_no_room_for_is_still_owed_its_new_look(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange and bold.", Options(inputs=[]), redesign_graphics=True)
    assert {g["redesign_pending"] for g in entries(out).values()} == {"design"}
    provider.fail = None
    no_model(monkeypatch)
    real_place = graphics.place
    monkeypatch.setattr(graphics, "place", lambda specs, tl, analysis, warnings: [g for g in real_place(specs, tl, analysis, warnings) if g.spec.text != "Weeks"])
    pipeline.rerender(out, Options(inputs=[]), overrides={"style"})
    left = {g["text"]: g for g in json.loads((out / "graphics.json").read_text())["graphics"] if g.get("text")}
    assert left["Weeks"]["placed"] is False and left["Weeks"]["redesign_pending"] == "design" and left["Heading Out"]["redesign_pending"] == "design"
    monkeypatch.setattr(graphics, "place", real_place)
    again = pipeline.rerender(out, Options(inputs=[]), overrides={"style"})
    assert {g["redesign_pending"] for g in entries(out).values()} == {"design"}
    assert again.summary["graphics"]["pending"] == 2 and again.summary["graphics"]["note"].startswith("2 graphics keep their earlier look")


# ------------------------------------------------------------------ a still-active usage limit writes nothing new


def test_finishing_the_graphics_while_the_usage_limit_is_still_on_writes_no_copy_of_the_last_version(footage, tmp_path, monkeypatch, cache, clear_mov, capsys):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange and bold.", Options(inputs=[]), redesign_graphics=True)
    v2_bytes = (out / "footage v2.fcpxml").read_bytes()
    saved_result, saved_versions = (out / "result.json").read_text(), (out / "versions.json").read_text()
    saved_marks = (out / "graphics.json").read_text()

    # Still limited: no design comes back, so no version is written; the error says so and what to run.
    with pytest.raises(ProviderLimitError) as stop:
        pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    message = str(stop.value)
    assert LIMIT in message and "Nothing was written: v2 is still the newest version" in message
    assert f"`roughcut render {out} --design-missing`" in message and not hasattr(stop.value, "written")
    assert top(out) == ["footage v2.fcpxml"] and (out / "footage v2.fcpxml").read_bytes() == v2_bytes
    assert (out / "result.json").read_text() == saved_result and (out / "versions.json").read_text() == saved_versions
    assert (out / "graphics.json").read_text() == saved_marks

    # The same through the CLI (the app's Finish graphics): an error event of kind usage_limit with nothing written.
    capsys.readouterr()
    assert main(["render", str(out), "--design-missing", "--progress-json"]) == 1
    error = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")][-1]
    assert error["event"] == "error" and error["kind"] == "usage_limit" and "written" not in error and "Nothing was written" in error["message"]
    assert top(out) == ["footage v2.fcpxml"]

    # After the reset it finishes, as before.
    provider.fail = None
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert (done.version, top(out)) == (3, ["footage v3.fcpxml"]) and done.summary["graphics"]["note"] is None


def test_finishing_the_graphics_without_a_renderer_writes_no_version_without_them(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange.", Options(inputs=[]), redesign_graphics=True)
    provider.fail = None
    saved_versions = (out / "versions.json").read_text()
    no_model(monkeypatch)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: "Chromium is not installed")
    with pytest.raises(RoughcutError, match="renderer isn't available: Chromium is not installed. Nothing was written"):
        pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    assert top(out) == ["footage v2.fcpxml"] and (out / "versions.json").read_text() == saved_versions
    assert len(uses(out / "footage v2.fcpxml")) == 2


# ------------------------------------------------------------------ what a stopped cut says about its graphics


def test_the_cut_written_before_its_graphics_does_not_say_what_became_of_them(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the renderer crashed")))
    with pytest.raises(RuntimeError):
        pipeline.revise(out, "Make the graphics bolder and orange.", Options(inputs=[]), redesign_graphics=True)
    saved = result_json(out)
    assert saved["partial"] is True and saved["graphics"] == {"planned": 2, "placed": 0, "rendered": 0, "note": pipeline.GRAPHICS_NOT_MADE_NOTE}
    said = " ".join(saved["changes"]["items"])
    assert GRAPHICS_NOT_YET in saved["changes"]["items"] and saved["changes"]["unchanged"] is False
    assert not any(word in said for word in ("redesigned", "new look", "Redesigning the graphics", "designed again", "Removed graphic", "New graphic"))
    head = (out / "edit_report.md").read_text().split("## Paper edit")[0]
    assert f"- {GRAPHICS_NOT_YET}" in head and "Redesigning the graphics" not in head and "redesigned" not in head


def test_a_redesign_that_got_no_new_design_does_not_say_it_is_designing_again(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    provider.fail = ProviderLimitError(LIMIT)
    with pytest.raises(ProviderLimitError):
        pipeline.revise(out, "Make the graphics orange and bold.", Options(inputs=[]), redesign_graphics=True)
    items = result_json(out)["changes"]["items"]
    assert items[0] == NO_NEW_LOOK and not any("every graphic is designed again" in i for i in items)
    report = (out / "edit_report.md").read_text().split("## Paper edit")[0]
    assert f"**{NO_NEW_LOOK}**" in report and "every graphic is designed again" not in report


def test_a_cut_finished_after_a_partial_one_counts_each_graphic_once(footage, tmp_path, monkeypatch, cache, clear_mov):
    """v2 was written before its graphics (a crash): finishing it adds them; v2 had none, so none "kept the earlier look in v2"."""
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    real_make = graphics.make
    monkeypatch.setattr(graphics, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the renderer crashed")))
    with pytest.raises(RuntimeError):
        pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    monkeypatch.setattr(graphics, "make", real_make)
    done = pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    items = done.changes["items"]
    assert "Graphics: 2 added to the cut (v2 was written before its graphics were made)" in items
    assert not any("kept the earlier look" in i or "new look" in i for i in items)
    assert len(uses(done.fcpxml)) == 2 and top(out) == ["footage v3.fcpxml"]
    validate_dtd(done.fcpxml)
    check_invariants(done.fcpxml)


def test_a_stop_between_the_partial_cut_and_its_result_is_still_named_as_the_newest_cut(footage, tmp_path, monkeypatch, cache, clear_mov):
    out, provider, renderer, v1 = build_v1(footage, tmp_path, monkeypatch, cache, clear_mov)
    real_save = pipeline.save_result

    def stops_on_the_partial(result, *, partial=False):
        if partial:
            raise RuntimeError("crashed before result.json")
        return real_save(result, partial=partial)

    monkeypatch.setattr(pipeline, "save_result", stops_on_the_partial)
    with pytest.raises(RuntimeError):
        pipeline.revise(out, "Make the graphics bolder.", Options(inputs=[]), redesign_graphics=True)
    assert versions.current(out)["n"] == 2 and result_json(out)["version"] == 1  # result.json is the older
    found = pipeline.partial_cut(out)
    assert found == {"version": 2, "fcpxml": str(out / "footage v2.fcpxml"), "project": versions.current(out)["project"], "partial": True}


# ------------------------------------------------------------------ the change lists themselves (no run)


def test_redesign_notes_say_what_came_of_the_redesign():
    lead = "Redesigning the graphics: every graphic is designed again, as asked."
    other = "Pacing: loose"
    assert redesign_notes([lead, other], lead, None) == [lead, other]
    assert redesign_notes([lead, other], lead, {"interim": True}) == [other]  # nothing is designed in the cut written before them
    assert redesign_notes([lead, other], lead, {"placed": {0, 1}, "fell_back": {0, 1}}) == [NO_NEW_LOOK, other]
    assert redesign_notes([lead, other], lead, {"placed": {0, 1}, "fell_back": {1}}) == [lead, other]  # one came back
    assert redesign_notes([lead, other], lead, {"placed": set(), "fell_back": set()}) == [lead, other]  # none had room: no failure to report
    assert redesign_notes([other], "", {"interim": True}) == [other]


def test_a_re_cut_after_a_partial_version_adds_its_graphics_once():
    items, unchanged = recut_items({}, {}, 2, before={"seconds": 30.0}, after={"seconds": 30.0}, added=2, redesigned=2, previous_partial=True)
    assert items == ["Graphics: 2 added to the cut (v2 was written before its graphics were made)"] and unchanged is False
    items, _ = recut_items({}, {}, 2, before={"seconds": 30.0}, after={"seconds": 30.0}, added=0, redesigned=2, previous_partial=False)
    assert items == ["Graphics: 2 got their new look (designed now; they kept the earlier look in v2)"]

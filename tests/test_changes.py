"""What a revision or a re-cut changed, said in words: the paper edit's block, the app's list, the CLI's summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, SPEECH, FakeProvider, make_clip

from roughcut import pipeline, progress, revise
from roughcut.analyze import Analysis
from roughcut.cli import change_lines, main
from roughcut.pipeline import Options
from roughcut.plan import Plan, normalize
from roughcut.revise import change_items, changes_lines, recut_items, setting_deltas

GFX = "This version plays the same lines, B-roll and graphics as v1. If you meant how the graphics look, attach reference images or tick Redesign graphics."


def analysis() -> Analysis:
    return Analysis(clips=[
        make_clip("V01", seconds=20, sentences=SPEECH),
        make_clip("V02", seconds=10),
        make_clip("V03", seconds=10),
        make_clip("A01", kind="audio", seconds=60),
        make_clip("A02", kind="audio", seconds=60),
    ])


def plan(a: Analysis, *, items=("V01.S003", "V01.S004", "V01.S005"), broll=(), graphics=(), music=(), spellings=(), sections=None):
    raw = {
        "sections": sections or [{"name": "Heading out", "purpose": "", "items": list(items), "broll": [dict(b) for b in broll]}],
        "graphics": [dict(g) for g in graphics],
        "music": [dict(m) for m in music],
        "spellings": [dict(s) for s in spellings],
    }
    return normalize(raw, a)


def box(text="Heading Out", *, kind="title_card", segment="V01.S003", words="heading up the mountain", sub="", direction="") -> dict:
    return {"kind": kind, "segment": segment, "words": words, "text": text, "subtext": sub, "seconds": 2.5, "direction": direction}


def items_of(before: Plan, after: Plan, a: Analysis, **kw):
    kw.setdefault("before_seconds", 30.0)
    kw.setdefault("after_seconds", 30.0)
    kw.setdefault("previous_version", 1)
    return change_items(before, after, a, **kw)


# ------------------------------------------------------------------ lines, sections, B-roll, music, spellings


def test_lines_sections_and_broll_are_named_with_the_length():
    a = analysis()
    old = plan(a, broll=[{"clip": "V02", "over": ["V01.S003"], "source_in": 0, "words": "", "reason": ""}])
    new = plan(a, sections=[
        {"name": "Heading out", "purpose": "", "items": ["V01.S003", "V01.S006"], "broll": []},
        {"name": "Wrap", "purpose": "", "items": ["V01.S005"], "broll": []},
    ])
    items, unchanged = items_of(old, new, a, before_seconds=30.0, after_seconds=20.0)
    assert unchanged is False
    assert items == [
        "Length: 0:30 → 0:20 (−0:10)",
        'Removed V01.S004 "It\'s going to be a long day."',
        'Added V01.S006 "And honestly, uh, I needed it too."',
        'Sections added: "Wrap"',
        "B-roll shots: 1 → 0",
    ]
    # The paper edit shows the same items, the IDs in bold, under the heading the tests have always found there.
    assert changes_lines(items, 1) == [
        "Changes from v1:", "",
        "- Length: 0:30 → 0:20 (−0:10)",
        '- Removed **V01.S004** "It\'s going to be a long day."',
        '- Added **V01.S006** "And honestly, uh, I needed it too."',
        '- Sections added: "Wrap"',
        "- B-roll shots: 1 → 0",
    ]
    assert revise.describe_changes(old, new, a, before_seconds=30.0, after_seconds=20.0, previous_version=1) == changes_lines(items, 1)
    assert changes_lines(["Length: 0:01 → 0:02 (+0:01)"], None)[0] == "Changes from the previous cut:"


def test_a_picture_range_a_reorder_and_a_long_list_are_said_briefly():
    a = analysis()
    old = plan(a, items=("V01.S001", "V01.S003", "V01.S004", "V01.S005", "V01.S006", "V02@1-4"))
    new = plan(a, items=("V01.S006", "V01.S005", "V01.S004", "V01.S003", "V01.S001", "V03@2-5"))
    items, _ = items_of(old, new, a)
    assert "Removed V02@1-4 (picture)" in items and "Added V03@2-5 (picture)" in items
    assert "Reordered: lines that stayed are in a different order" in items
    lots = plan(a, items=tuple(f"V01.S00{n}" for n in range(1, 7)))
    few = plan(a, items=("V01.S001",))
    items, _ = items_of(lots, few, a)
    assert sum(i.startswith("Removed ") for i in items) == 5  # all five, under the limit
    assert changes_lines(["Removed V02@1-4 (picture)"], 2)[2] == "- Removed **V02@1-4** (picture)"


def test_other_footage_in_the_same_number_of_shots_is_still_a_change():
    a = analysis()
    shot = lambda clip: [{"clip": clip, "over": ["V01.S003"], "source_in": 0, "words": "", "reason": ""}]  # noqa: E731
    items, unchanged = items_of(plan(a, broll=shot("V02")), plan(a, broll=shot("V03")), a)
    assert unchanged is False and "B-roll: 1 of 1 shots are other footage or on other lines" in items


def test_a_song_swap_names_both_songs():
    a = analysis()
    cue = lambda clip: [{"clip": clip, "first_section": 0, "last_section": 0, "source_in": 0}]  # noqa: E731
    items, _ = items_of(plan(a, music=cue("A01")), plan(a, music=cue("A02")), a)
    assert "Music: A01.wav → A02.wav" in items
    items, _ = items_of(plan(a), plan(a, music=cue("A01")), a)
    assert "Music: none → A01.wav" in items
    items, _ = items_of(plan(a, music=cue("A01")), plan(a), a)
    assert "Music: A01.wav → none" in items


def test_name_spellings_added_changed_and_undone():
    a = analysis()
    fix = lambda correct: [{"heard": "the kids", "correct": correct}]  # noqa: E731
    items, _ = items_of(plan(a), plan(a, spellings=fix("the Kidds")), a)
    assert 'Spelling: "the kids" is written "the Kidds"' in items
    items, _ = items_of(plan(a, spellings=fix("the Kidds")), plan(a, spellings=fix("the Kidd")), a)
    assert 'Spelling changed: "the kids" was "the Kidds", now "the Kidd"' in items
    items, _ = items_of(plan(a, spellings=fix("the Kidds")), plan(a), a)
    assert 'Spelling undone: "the kids" is no longer written "the Kidds"' in items


# ------------------------------------------------------------------ graphics


def test_graphics_are_counted_as_kept_changed_new_removed_and_redesigned():
    a = analysis()
    stat = box("Weeks", kind="stat", segment="V01.S005", words="The kids have been asking")
    both = plan(a, graphics=[box(), stat])
    # Something else changed, and the graphics didn't: say they were kept.
    items, unchanged = items_of(both, plan(a, items=("V01.S003", "V01.S005"), graphics=[box(), stat]), a)
    assert unchanged is False and items[-1] == "Graphics: 2 kept as they were"

    # Text, subtext and direction in words; a new one; a removed one.
    items, _ = items_of(both, plan(a, graphics=[box("Three Weeks", sub="and counting", direction="orange"), box("Wrap", kind="quote", segment="V01.S004", words="a long day")]), a)
    assert items[1:] == [
        "Graphics: 1 changed and designed again, 1 new, 1 removed",
        'Graphic "Heading Out": text now "Three Weeks"; subtext now "and counting"; direction now "orange"; designed again',
        'New graphic: "Wrap" (quote)',
        'Removed graphic: "Weeks"',
    ]
    items, _ = items_of(plan(a, graphics=[box(sub="x", direction="orange")]), plan(a, graphics=[box()]), a)
    assert 'Graphic "Heading Out": subtext removed; direction removed; designed again' in items

    # Case and spacing in the direction or words aren't a change.
    items, unchanged = items_of(plan(a, graphics=[box(direction="Bold  Left")]), plan(a, graphics=[box(direction=" bold left ")]), a)
    assert unchanged is True

    # Redesigned: every one, whatever else changed.
    items, unchanged = items_of(both, both, a, redesigned=True)
    assert unchanged is False and items[1:] == ["Graphics: 2 redesigned (every graphic is designed again)"]
    items, _ = items_of(both, plan(a, graphics=[box("Three Weeks"), stat, box("Wrap", segment="V01.S004", words="a long day")]), a, redesigned=True)
    assert "Graphics: 2 redesigned, 1 new (every graphic is designed again)" in items

    # Many changes: the first ten are named, the rest counted.
    many_old = plan(a, graphics=[])
    many_new = plan(a, graphics=[box(f"Card {n}", kind="quote", segment="V01.S004", words=f"word {n}") for n in range(12)])
    items, _ = items_of(many_old, many_new, a)
    assert items[1] == "Graphics: 12 new" and sum(i.startswith("New graphic:") for i in items) == 10 and items[-1] == "…and 2 more graphic changes"


def test_graphics_that_went_off_are_said():
    a = analysis()
    items, unchanged = items_of(plan(a, graphics=[box(), box("Two", segment="V01.S005", words="The kids")]), plan(a), a, graphics_on=False)
    assert unchanged is False and "Motion graphics are off: the 2 graphics of v1 are not in this version" in items


# ------------------------------------------------------------------ nothing changed


def test_when_nothing_a_viewer_would_see_changed_it_says_so_with_what_to_try():
    a = analysis()
    same = plan(a, graphics=[box()])
    assert items_of(same, same, a) == ([GFX], True)
    assert items_of(same, same, a, previous_version=None)[0] == [GFX.replace("as v1.", "as the previous cut.")]
    off = items_of(plan(a), plan(a), a, graphics_on=False)
    assert off == (["This version plays the same lines and B-roll as v1. Motion graphics are off for this cut: turn them on to add or change them."], True)
    none = items_of(plan(a), plan(a), a)
    assert none[1] is True and none[0][0].startswith("This version plays the same lines and B-roll as v1. This cut has no graphics: ask for them in your notes")
    # What the notes made of the settings is still told, ahead of it; advice isn't a change.
    advice = "Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none."
    assert items_of(plan(a), plan(a), a, graphics_on=False, settings_notes=[advice])[0][0] == advice
    assert items_of(plan(a), plan(a), a, graphics_on=False, settings_notes=[advice])[1] is True
    # The length is no part of it: a plan that plays the same is the same, however the frames fell.
    assert items_of(same, same, a, before_seconds=30.0, after_seconds=29.0)[1] is True


def test_settings_a_flag_or_the_notes_changed_count_as_changes():
    a = analysis()
    same = plan(a)
    was = {"style": "medium", "punch_in": False, "format": None, "keep_fillers": False}
    now = {"style": "tight", "punch_in": True, "format": "vertical", "keep_fillers": True}
    items, unchanged = items_of(same, same, a, was=was, now=now)
    assert unchanged is False
    assert items[1:] == ["Pacing: medium → tight", "Punch-ins: off → on", "Format: from the footage → vertical", "Fillers (um, uh): cut → kept"]
    assert setting_deltas(now, was) == ["Pacing: tight → medium", "Punch-ins: on → off", "Format: vertical → from the footage", "Fillers (um, uh): kept → cut"]
    assert setting_deltas(was, was) == [] and setting_deltas({}, now) == []  # a setting the earlier version didn't save is unknown, not different
    # A note that already says it (settings_from_notes) isn't repeated, and counts as the change.
    note = "Pacing: tight, as your notes ask (v1 was medium)."
    items, unchanged = items_of(same, same, a, was=was, now={**was, "style": "tight"}, settings_notes=[note])
    assert unchanged is False and items == ["Length: 0:30 → 0:30 (+0:00)", note]
    # Redesign notes are changes too; the explanation of why a flag won isn't.
    why = "Your notes ask for tight pacing, but --style loose was given, so this version is loose."
    assert items_of(same, same, a, was=was, now=was, settings_notes=[why])[1] is True


# ------------------------------------------------------------------ a re-cut


def test_a_recut_is_told_in_numbers_or_as_the_same_cut():
    was = {"style": "medium", "punch_in": False, "format": None, "keep_fillers": False}
    before = {"seconds": 30.0, "edits": 12, "broll": 3}
    same = recut_items(was, was, 2, before=before, after=dict(before))
    assert same == (["This is the same cut as v2: its settings are the same, so it plays exactly as v2 does. To change it, pick another pacing, format or punch-in setting, or revise it with notes."], True)

    tight = recut_items(was, {**was, "style": "tight"}, 2, before=before, after={"seconds": 26.5, "edits": 14, "broll": 3})
    assert tight == (["Pacing: medium → tight", "Length: 0:30 → 0:26 (−0:03)", "Edits: 12 → 14"], False)

    framing = recut_items(was, {**was, "punch_in": True}, 2, before=before, after=dict(before))
    assert framing == (["Punch-ins: off → on", "Length, edits and B-roll came out the same (punch-ins only change the framing)"], False)

    # A hand-edited graphic is a change at the same settings.
    edited = recut_items(was, was, 2, before=before, after=dict(before), rerendered=2)
    assert edited[1] is False and edited[0] == ["Graphics: 2 were rendered again (a design you edited, or timing that moved)"]

    # A sub-second move in the length is told in tenths.
    close = recut_items(was, {**was, "style": "loose"}, 2, before={"seconds": 30.0}, after={"seconds": 30.4, "edits": 12, "broll": 3})
    assert close[0][1] == "Length: 0:30.0 → 0:30.4 (+0:00.4)"

    # An older folder that saved nothing to compare with says so rather than claiming it is the same cut.
    older = recut_items({}, {"style": "tight", "punch_in": False, "format": None, "keep_fillers": False}, 1, before={"seconds": None}, after={"seconds": 20.0, "edits": 5, "broll": 0})
    assert older[1] is False and "nothing to compare" in older[0][0]


def test_the_command_line_says_what_changed_in_plain_lines():
    assert change_lines(None) == []
    changed = {"from_version": 2, "items": ["Length: 0:30 → 0:20 (−0:10)", "Pacing: medium → tight"], "unchanged": False}
    assert change_lines(changed) == ["Changed from v2:", "  - Length: 0:30 → 0:20 (−0:10)", "  - Pacing: medium → tight"]
    nothing = change_lines({"from_version": 1, "items": [GFX], "unchanged": True})
    assert nothing[0] == "Nothing visible changed from v1:" and nothing[1] == f"  {GFX}"
    assert "--graphics-ref IMAGE" in nothing[2] and "--redesign-graphics" in nothing[2]
    assert change_lines({"from_version": None, "items": ["x"], "unchanged": False})[0] == "Changed from the previous cut:"


# ------------------------------------------------------------------ in runs


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture(scope="module")
def shared_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("changes-analysis-cache")


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


def build(footage, out, cache, **kw):
    return pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw))


def revised_plan() -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["sections"][1]["items"] = [i for i in plan["sections"][1]["items"] if i != "V01.S004"]
    return plan


def test_a_first_build_has_no_changes_and_a_revision_lists_them(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    first = build(footage, out, shared_cache)
    assert first.changes is None and pipeline.result_summary(first)["changes"] is None
    assert json.loads((out / "result.json").read_text())["changes"] is None

    fake.plan = revised_plan()
    result = pipeline.revise(out, "Drop the long-day line.", Options(inputs=[]))
    changes = pipeline.result_summary(result)["changes"]
    assert set(changes) == {"from_version", "items", "unchanged"} and changes["from_version"] == 1 and changes["unchanged"] is False
    assert changes["items"][0].startswith("Length: 0:1") and 'Removed V01.S004 "It\'s going to be a long day."' in changes["items"]
    assert json.loads((out / "result.json").read_text())["changes"] == changes
    report = result.report.read_text()
    assert report.count('Removed **V01.S004** "It\'s going to be a long day."') == 1 and "Changes from v1:" in report
    assert "No lines added or removed" not in report


def test_the_apps_stream_carries_the_changes_of_a_revision_and_of_a_recut(footage, tmp_path, fake, shared_cache, capsys):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    capsys.readouterr()
    fake.plan = revised_plan()
    assert main(["revise", str(out), "Drop the long-day line.", "--progress-json"]) == 0
    done = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    assert done["event"] == "done" and done["result"]["changes"]["from_version"] == 1
    assert main(["render", str(out), "--style", "loose", "--progress-json"]) == 0
    done = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    assert done["result"]["changes"]["from_version"] == 2 and done["result"]["changes"]["items"][0] == "Pacing: medium → loose"


def test_notes_that_change_the_pacing_are_listed_once_and_a_flag_that_does_is_listed_too(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    fake.plan = dict(revised_plan(), pacing="loose")
    result = pipeline.revise(out, "Make it looser.", Options(inputs=[]))
    note = "Pacing: loose, as your notes ask (v1 was medium)."
    assert result.changes["items"].count(note) == 1 and not any(i.startswith("Pacing: medium") for i in result.changes["items"])
    report = result.report.read_text()
    assert report.count(note) == 1  # in the revision block, not repeated in the list below it
    assert report.index(note) < report.index("Changes from v1:")

    fake.plan = revised_plan()
    result = pipeline.revise(out, "Shorter.", Options(inputs=[], style="tight", format="vertical", keep_fillers=True), overrides={"style", "format", "keep_fillers"})
    items = result.changes["items"]
    assert "Pacing: loose → tight" in items and "Format: from the footage → vertical" in items and "Fillers (um, uh): cut → kept" in items


def test_a_recut_at_the_same_settings_says_it_is_the_same_cut(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    again = pipeline.rerender(out, Options(inputs=[]))
    assert again.changes == {
        "from_version": 1, "unchanged": True,
        "items": ["This is the same cut as v1: its settings are the same, so it plays exactly as v1 does. To change it, pick another pacing, format or punch-in setting, or revise it with notes."],
    }
    report = again.report.read_text()
    assert report.index("## Re-cut") < report.index("## Paper edit") and "Changes from v1:" in report


def test_a_recut_at_another_pace_reports_the_numbers_it_changed(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    first = build(footage, out, shared_cache)
    tight = pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    items = tight.changes["items"]
    assert items[0] == "Pacing: medium → tight" and tight.changes["unchanged"] is False and tight.changes["from_version"] == 1
    if len(first.timeline.spine) != len(tight.timeline.spine):
        assert f"Edits: {len(first.timeline.spine)} → {len(tight.timeline.spine)}" in items
    else:
        assert not any(i.startswith("Edits:") for i in items)
    if abs(tight.timeline.seconds - first.timeline.seconds) >= 0.05:
        assert any(i.startswith("Length: ") for i in items)
    assert tight.timeline.seconds < first.timeline.seconds and any(i.startswith("Length: ") for i in items)  # this footage's pauses shrink

    # The next re-cut is measured from this one, not from the build.
    loose = pipeline.rerender(out, Options(inputs=[], style="loose"), overrides={"style"})
    assert loose.changes["from_version"] == 2 and loose.changes["items"][0] == "Pacing: tight → loose"
    assert "Re-cut" in (out / "edit_report.md").read_text()


def test_a_folder_from_before_versions_and_results_still_re_cuts_and_says_what_it_can(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    (out / "result.json").unlink()
    (out / "versions.json").unlink()
    same = pipeline.rerender(out, Options(inputs=[]))
    assert same.changes["from_version"] == 1 and same.changes["unchanged"] is True  # same settings, nothing else to compare
    (out / "result.json").unlink()
    (out / "versions.json").unlink()
    plan = json.loads((out / "plan.json").read_text())
    del plan["settings"]  # an older release saved none
    (out / "plan.json").write_text(json.dumps(plan))
    older = pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    assert older.changes["unchanged"] is False and "nothing to compare" in older.changes["items"][0]
    for text in ('{"edits": "many", "version": 1}', "not json", "[]"):  # and a result.json that is not what it should be
        (out / "result.json").write_text(text)
        assert pipeline.rerender(out, Options(inputs=[], style="medium"), overrides={"style"}).changes is not None


def test_the_graphics_a_recut_renders_again_are_counted(footage, tmp_path, monkeypatch, shared_cache):
    import shutil

    from conftest import ffmpeg
    from test_graphics_refs import GRAPHIC, STAT

    from roughcut import graphics

    mov = tmp_path / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", str(mov))

    def render_one(job):
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "console": []}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [GRAPHIC, STAT]
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(plan))
    out = tmp_path / "out"
    build(footage, out, shared_cache, graphics=True)
    nothing = pipeline.rerender(out, Options(inputs=[]))
    assert nothing.changes["unchanged"] is True
    html = out / "graphics" / "G02-title-card.html"
    html.write_text(html.read_text().replace("#F26B21", "#2155F2"))
    edited = pipeline.rerender(out, Options(inputs=[]))
    assert edited.changes["unchanged"] is False
    assert edited.changes["items"] == ["Graphics: 1 was rendered again (a design you edited, or timing that moved)"]

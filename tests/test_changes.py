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

GFX = (
    "This version plays the same lines, B-roll and graphics as v1. If you meant how the graphics look, attach reference images "
    "or turn on Redesign the graphics (Terminal: --graphics-ref IMAGE or --redesign-graphics)."
)


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
    assert unchanged is False and "B-roll: 1 of 1 shots changed (other footage, other lines or words, or another part of the footage)" in items


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
    assert items == [
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
    assert unchanged is False and items == ["Graphics: 2 redesigned (every graphic is designed again)"]
    items, _ = items_of(both, plan(a, graphics=[box("Three Weeks"), stat, box("Wrap", segment="V01.S004", words="a long day")]), a, redesigned=True)
    assert "Graphics: 2 redesigned, 1 new (every graphic is designed again)" in items

    # Many changes: the first ten are named, the rest counted.
    many_old = plan(a, graphics=[])
    many_new = plan(a, graphics=[box(f"Card {n}", kind="quote", segment="V01.S004", words=f"word {n}") for n in range(12)])
    items, _ = items_of(many_old, many_new, a)
    assert items[0] == "Graphics: 12 new" and sum(i.startswith("New graphic:") for i in items) == 10 and items[-1] == "…and 2 more graphic changes"


def test_graphics_that_went_off_are_said():
    a = analysis()
    items, unchanged = items_of(plan(a, graphics=[box(), box("Two", segment="V01.S005", words="The kids")]), plan(a), a, graphics_on=False)
    assert unchanged is False and "Motion graphics are off: the 2 graphics of v1 are not in this version" in items


def test_b_roll_from_another_part_of_the_footage_is_a_change():
    a = analysis()
    shot = lambda src, clip="V02": plan(a, broll=[{"clip": clip, "over": ["V01.S003"], "source_in": src, "words": "", "reason": ""}])  # noqa: E731
    moved = "B-roll: 1 of 1 shots changed (other footage, other lines or words, or another part of the footage)"
    assert items_of(shot(-1), shot(-1), a)[1] is True  # the software picks both times
    assert items_of(shot(3.0), shot(3.04), a)[1] is True  # a hundredth of a second isn't another part
    for was, now in ((0, 3.0), (-1, 3.0), (3.0, -1), (3.0, 9.5)):
        assert items_of(shot(was), shot(now), a) == ([moved], False), (was, now)
    # A picture has no start to move.
    pictures = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("S01", kind="image")])
    still = lambda src: plan(pictures, broll=[{"clip": "S01", "over": ["V01.S003"], "source_in": src, "words": "", "reason": ""}])  # noqa: E731
    assert items_of(still(0), still(5), pictures)[1] is True


def test_music_that_starts_elsewhere_in_the_song_is_a_change_unless_it_is_back_timed():
    a = analysis()
    two = lambda src, last: plan(  # noqa: E731
        a,
        sections=[{"name": "A", "purpose": "", "items": ["V01.S003"], "broll": []}, {"name": "B", "purpose": "", "items": ["V01.S005"], "broll": []}],
        music=[{"clip": "A01", "first_section": 0, "last_section": last, "source_in": src}],
    )
    assert items_of(two(0, 0), two(9, 0), a) == (["Music starts at another point in the song"], False)  # it ends before the cut does
    assert items_of(two(0, 0), two(0.04, 0), a)[1] is True
    assert items_of(two(0, 1), two(9, 1), a)[1] is True  # it runs to the end: back-timed from the song's ending, whatever it says


def test_a_graphic_held_for_another_time_is_a_change_and_keeps_its_design():
    a = analysis()
    held = lambda seconds: plan(a, graphics=[{**box(), "seconds": seconds}])  # noqa: E731
    items, unchanged = items_of(held(2.5), held(5.0), a)
    assert unchanged is False
    assert items == ["Graphics: 1 kept with another length", 'Graphic "Heading Out": now holds 5 s (was 2.5 s); design kept']
    items, _ = items_of(held(5.0), held(2.5), a)
    assert items[1] == 'Graphic "Heading Out": now holds 2.5 s (was 5 s); design kept'
    assert items_of(held(2.5), held(2.7), a)[1] is True  # under a third of a second isn't one
    assert items_of(held(0), held(3.0), a)[1] is True  # no time asked for is the kind's usual (3 s for a title card)
    # Designed again, the length is no part of it.
    assert items_of(held(2.5), held(5.0), a, redesigned=True)[0] == ["Graphics: 1 redesigned (every graphic is designed again)"]
    # With another change too, it is told beside the kept ones.
    stat = box("Weeks", kind="stat", segment="V01.S005", words="The kids have been asking")
    items, _ = items_of(plan(a, graphics=[box(), stat]), plan(a, graphics=[{**box(), "seconds": 6}, stat]), a)
    assert items[0] == "Graphics: 1 kept as they were, 1 kept with another length"


def test_graphics_that_did_not_make_it_are_not_counted_as_made():
    a = analysis()
    stat = box("Weeks", kind="stat", segment="V01.S005", words="The kids have been asking")
    both = plan(a, graphics=[box(), stat])
    # Redesigned: one new design came back; the other never did and kept its earlier design.
    items, unchanged = items_of(both, both, a, redesigned=True, outcome={"in_cut": {0, 1}, "fell_back": {1}})
    assert unchanged is False
    assert items == [
        "Graphics: 1 redesigned, 1 got no new design and keeps its earlier one (see Warnings)",
        'Graphic "Weeks": no new design came back, so it keeps its earlier one',
    ]
    # Neither came back, and they have nothing to keep: they are not in the cut.
    items, _ = items_of(both, both, a, redesigned=True, outcome={"in_cut": set(), "fell_back": set()})
    assert items[0] == "Graphics: 2 could not be made and are not in this version (see Warnings)"
    assert 'Graphic "Weeks": could not be made, so it is not in this version' in items
    # A new graphic that has no room, or didn't render, isn't "new".
    items, _ = items_of(plan(a), plan(a, graphics=[box()]), a, outcome={"in_cut": set(), "fell_back": set()})
    assert items == ["Graphics: 1 could not be made and is not in this version (see Warnings)", 'Graphic "Heading Out": could not be made, so it is not in this version']
    # One that was only kept isn't blamed for what it never did; the graphics that did come out are told as before.
    assert items_of(both, both, a, outcome={"in_cut": {0}, "fell_back": set()})[1] is True
    items, _ = items_of(both, both, a, redesigned=True, outcome={"in_cut": {0, 1}, "fell_back": set()})
    assert items == ["Graphics: 2 redesigned (every graphic is designed again)"]


def test_a_graphic_that_never_had_room_is_not_a_failure_but_one_placed_and_not_made_is():
    a = analysis()
    stat = box("Weeks", kind="stat", segment="V01.S005", words="The kids have been asking")
    both = plan(a, graphics=[box(), stat])
    # Placed, but its design or render never came back: it could not be made.
    items, _ = items_of(both, both, a, redesigned=True, outcome={"in_cut": {0}, "placed": {0, 1}, "fell_back": set()})
    assert items == [
        "Graphics: 1 redesigned, 1 could not be made and is not in this version (see Warnings)",
        'Graphic "Weeks": could not be made, so it is not in this version',
    ]
    # Not placed: there was no room for it in this cut, and nothing failed.
    items, unchanged = items_of(both, both, a, redesigned=True, outcome={"in_cut": {0}, "placed": {0}, "fell_back": set()})
    assert unchanged is False
    assert items == [
        "Graphics: 1 redesigned, 1 had no room in this cut (see Warnings)",
        'Graphic "Weeks": had no room in this cut (see Warnings)',
    ]
    # A new graphic that has no room isn't "new" either, and the two kinds of "not in the cut" are told apart.
    box3 = box("Third", kind="quote", segment="V01.S004", words="a long day")
    items, _ = items_of(plan(a), plan(a, graphics=[box(), stat, box3]), a, outcome={"in_cut": {0}, "placed": {0, 1}, "fell_back": set()})
    assert items[0] == "Graphics: 1 new, 1 could not be made and is not in this version, 1 had no room in this cut (see Warnings)"
    assert 'Graphic "Weeks": could not be made, so it is not in this version' in items
    assert 'Graphic "Third": had no room in this cut (see Warnings)' in items
    assert [i for i in items if i.startswith("New graphic")] == ['New graphic: "Heading Out" (title card)']  # only the one that is in the cut
    # An outcome that doesn't say what was placed (an older caller) is read as every graphic having been.
    items, _ = items_of(both, both, a, redesigned=True, outcome={"in_cut": {0}, "fell_back": set()})
    assert items[1] == 'Graphic "Weeks": could not be made, so it is not in this version'


def test_the_sentence_that_says_nothing_changed_comes_first_whatever_else_was_said():
    a = analysis()
    advice = ["Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none.", "Redesign graphics was asked for, but motion graphics are off for this cut, so there is nothing to redesign."]
    items, unchanged = items_of(plan(a), plan(a), a, graphics_on=False, settings_notes=advice)
    assert unchanged is True and items[0].startswith("This version plays the same lines and B-roll as v1.") and items[1:] == advice


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
    # What the notes made of the settings is still told, after it; advice isn't a change.
    advice = "Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none."
    told = items_of(plan(a), plan(a), a, graphics_on=False, settings_notes=[advice])
    assert told == (["This version plays the same lines and B-roll as v1. Motion graphics are off for this cut: turn them on to add or change them.", advice], True)
    # The length is no part of it: a plan that plays the same is the same, however the frames fell.
    assert items_of(same, same, a, before_seconds=30.0, after_seconds=29.0)[1] is True


def test_settings_a_flag_or_the_notes_changed_count_as_changes():
    a = analysis()
    same = plan(a)
    was = {"style": "medium", "punch_in": False, "format": None, "keep_fillers": False}
    now = {"style": "tight", "punch_in": True, "format": "vertical", "keep_fillers": True}
    items, unchanged = items_of(same, same, a, was=was, now=now)
    assert unchanged is False
    assert items == ["Pacing: medium → tight", "Punch-ins: off → on", "Format: from the footage → vertical", "Fillers (um, uh): cut → kept"]  # the length didn't move: no line for it
    assert setting_deltas(now, was) == ["Pacing: tight → medium", "Punch-ins: on → off", "Format: vertical → from the footage", "Fillers (um, uh): kept → cut"]
    assert setting_deltas(was, was) == [] and setting_deltas({}, now) == []  # a setting the earlier version didn't save is unknown, not different
    # A note that already says it (settings_from_notes) isn't repeated, and counts as the change.
    note = "Pacing: tight, as your notes ask (v1 was medium)."
    items, unchanged = items_of(same, same, a, was=was, now={**was, "style": "tight"}, settings_notes=[note])
    assert unchanged is False and items == [note]
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

    # Clips the earlier version didn't have, with nothing of theirs to replace, are new to the cut.
    first = recut_items(was, was, 2, before=before, after=dict(before), added=2)
    assert first == (["Graphics: 2 added to the cut"], False)
    assert recut_items(was, was, 2, before=before, after=dict(before), added=1)[0] == ["Graphics: 1 added to the cut"]
    both = recut_items(was, was, 2, before=before, after=dict(before), added=1, rerendered=1)
    assert both[0] == ["Graphics: 1 added to the cut", "Graphics: 1 was rendered again (a design you edited, or timing that moved)"]

    # A sub-second move in the length is told in tenths.
    close = recut_items(was, {**was, "style": "loose"}, 2, before={"seconds": 30.0}, after={"seconds": 30.4, "edits": 12, "broll": 3})
    assert close[0][1] == "Length: 0:30.0 → 0:30.4 (+0:00.4)"

    # An older folder that saved nothing to compare with says so rather than claiming it is the same cut.
    older = recut_items({}, {"style": "tight", "punch_in": False, "format": None, "keep_fillers": False}, 1, before={"seconds": None}, after={"seconds": 20.0, "edits": 5, "broll": 0})
    assert older[1] is False and "nothing to compare" in older[0][0]


def test_graphics_clips_new_to_a_recut_are_added_or_replace_one_the_earlier_version_had():
    new_graphics = revise.new_graphics
    # The earlier version had no graphics (a redesign that stopped before any came back): all are on the timeline for the first time.
    assert new_graphics({"G01-title-card-aaaaaa.mov", "G02-stat-bbbbbb.mov"}, []) == (2, 0)
    # An edited design (or new timing) is a new file in the place of the clip it replaces: same kind, no longer in the cut.
    assert new_graphics({"G01-title-card-cccccc.mov", "G02-stat-bbbbbb.mov"}, ["G01-title-card-aaaaaa.mov", "G02-stat-bbbbbb.mov"]) == (0, 1)
    # A graphic of a kind the earlier cut didn't have, or one more than it had, is added.
    assert new_graphics({"G01-title-card-aaaaaa.mov", "G02-stat-bbbbbb.mov"}, ["G01-title-card-aaaaaa.mov"]) == (1, 0)
    assert new_graphics(
        {"G01-stat-aaaaaa.mov", "G02-stat-bbbbbb.mov"}, ["G01-stat-cccccc.mov"],
    ) == (1, 1)
    # Clips from before digests were in the names are matched the same way.
    assert new_graphics({"G01-quote-aaaaaa.mov"}, ["G01-quote.mov"]) == (0, 1)
    assert new_graphics({"G01-kinetic-caption-aaaaaa.mov"}, ["G01-lower-third.mov"]) == (1, 0)
    # Nothing new, or nothing known about the earlier version: nothing to say.
    assert new_graphics({"G01-stat-aaaaaa.mov"}, ["G01-stat-aaaaaa.mov"]) == (0, 0)
    assert new_graphics(set(), ["G01-stat-aaaaaa.mov"]) == (0, 0)


def test_every_render_setting_a_recut_can_change_is_compared():
    was = {
        "style": "medium", "punch_in": False, "format": None, "keep_fillers": False,
        "broll_db": -20.0, "music_db": -14.0, "fill": True, "stringout": True, "dialogue_level": -16.0, "fades": True,
    }
    now = {**was, "broll_db": -12.0, "music_db": -20.0, "fill": False, "stringout": False, "dialogue_level": None, "fades": False}
    assert setting_deltas(was, now) == [
        "Dialogue level: -16 LUFS → as recorded", "B-roll sound: -20 dB → -12 dB", "Music level: -14 dB → -20 dB",
        "Fades: on → off", "Picture over bare narration: on → off", "Stringout project: on → off",
    ]
    assert setting_deltas(now, was)[0] == "Dialogue level: as recorded → -16 LUFS"
    assert setting_deltas(was, was) == [] and setting_deltas({}, now) == []  # a setting the earlier version didn't save is unknown, not different
    before = {"seconds": 30.0, "edits": 12, "broll": 3}
    for key, value in (("dialogue_level", -12.0), ("fades", False), ("broll_db", -30.0), ("music_db", -30.0), ("fill", False), ("stringout", False)):
        items, unchanged = recut_items(was, {**was, key: value}, 2, before=before, after=dict(before))
        assert unchanged is False and "same cut" not in items[0], key
    items, _ = recut_items(was, {**was, "dialogue_level": -12.0, "punch_in": True}, 2, before=before, after=dict(before))
    assert items[-1] == "Length, edits and B-roll came out the same (punch-ins only change the framing; levels and fades only change how it sounds)"
    assert recut_items(was, {**was, "fill": False}, 2, before=before, after=dict(before))[0][-1] == "Length, edits and B-roll came out the same"
    # A revision that is given one of them says it too.
    items, unchanged = items_of(plan(analysis()), plan(analysis()), analysis(), was=was, now={**was, "music_db": -20.0})
    assert unchanged is False and items == ["Music level: -14 dB → -20 dB"]


def test_the_command_line_says_what_changed_in_plain_lines():
    assert change_lines(None) == []
    changed = {"from_version": 2, "items": ["Length: 0:30 → 0:20 (−0:10)", "Pacing: medium → tight"], "unchanged": False}
    assert change_lines(changed) == ["Changed from v2:", "  - Length: 0:30 → 0:20 (−0:10)", "  - Pacing: medium → tight"]
    nothing = change_lines({"from_version": 1, "items": [GFX], "unchanged": True})
    assert nothing == ["Nothing visible changed from v1:", f"  {GFX}"]  # the Terminal words are in the sentence itself
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


def test_a_recut_that_only_changes_levels_or_fades_is_not_called_the_same_cut(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    louder = pipeline.rerender(out, Options(inputs=[], dialogue_level=-12.0), overrides={"dialogue_level"})
    assert louder.changes["unchanged"] is False and louder.changes["from_version"] == 1
    assert louder.changes["items"][0] == "Dialogue level: -16 LUFS → -12 LUFS"
    assert not any("same cut" in i for i in louder.changes["items"])
    plain = pipeline.rerender(out, Options(inputs=[], fades=False, music_db=-30.0), overrides={"fades", "music_db"})
    assert plain.changes["items"][:2] == ["Music level: -14 dB → -30 dB", "Fades: on → off"] and plain.changes["unchanged"] is False
    same = pipeline.rerender(out, Options(inputs=[]))  # nothing given: the saved settings, so the same cut again
    assert same.changes["unchanged"] is True and same.changes["from_version"] == 3
    # A revision given one of them lists it as well.
    fake.plan = revised_plan()
    result = pipeline.revise(out, "Shorter.", Options(inputs=[], broll_db=-30.0), overrides={"broll_db"})
    assert "B-roll sound: -20 dB → -30 dB" in result.changes["items"]


def test_a_revision_that_asks_for_graphics_while_they_are_off_leads_with_the_nothing_changed_sentence(footage, tmp_path, fake, shared_cache):
    out = tmp_path / "out"
    build(footage, out, shared_cache)
    fake.plan = dict(CANNED_PLAN, graphics_asked=True)
    result = pipeline.revise(out, "Add a lower third.", Options(inputs=[]))
    items = result.changes["items"]
    assert result.changes["unchanged"] is True
    assert items[0] == "This version plays the same lines and B-roll as v1. Motion graphics are off for this cut: turn them on to add or change them."
    assert items[1] == "Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none." and len(items) == 2
    # The command that turns them on is for Terminal: it is in the warnings, not in what the app lists.
    assert not any("roughcut revise" in i for i in items)
    assert any("roughcut revise" in w and "--graphics" in w for w in result.warnings)
    assert main(["revise", str(out), "Add a lower third."]) == 0


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

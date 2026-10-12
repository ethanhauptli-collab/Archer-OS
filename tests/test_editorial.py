"""Editorial: B-roll on its words, one use per line, filler and pause rules, length checks."""

from __future__ import annotations

import re
from fractions import Fraction

import pytest
from conftest import make_clip
from test_narration import narration_project

from roughcut.analyze import Analysis
from roughcut.phrases import frame_at
from roughcut.plan import PLAN_SCHEMA, normalize
from roughcut.timeline import STYLES, build_timeline

# ------------------------------------------------------------ N3: B-roll on its words


def _word_frame(tl, analysis, seg_id, text):
    seg = analysis.segment(seg_id)
    w = next(w for w in seg.words if w.text.strip(".,").lower() == text.lower())
    return frame_at(tl, seg.clip_id, w.start)


def _honda_plan(words=("", "Honda Center", "two hotels", "parks")):
    clips = ["I01", "I02", "V01", "I04"]
    return {
        "sections": [
            {
                "name": "Plan",
                "items": ["A01.S003"],
                "broll": [{"clip": c, "over": ["A01.S003"], "words": w, "source_in": -1, "reason": ""} for c, w in zip(clips, words)],
            }
        ]
    }


def test_broll_comes_in_on_the_words_it_illustrates():
    a = narration_project()
    plan = normalize(_honda_plan(), a)
    assert [b.words for b in plan.sections[0].broll] == ["", "Honda Center", "two hotels", "parks"]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", fill=False)
    shots = {c.clip_id: c for c in tl.connected}
    line_start, line_end = tl.seg_ranges["A01.S003"]
    assert shots["I01"].start == 0 < line_start  # the first shot starts with the line: on frame 0, not after its padding
    assert abs(shots["I02"].start - _word_frame(tl, a, "A01.S003", "Honda")) <= 2
    assert abs(shots["V01"].start - _word_frame(tl, a, "A01.S003", "two")) <= 2
    assert abs(shots["I04"].start - _word_frame(tl, a, "A01.S003", "parks")) <= 2
    assert shots["I01"].end == shots["I02"].start  # an anchored shot ends the one before it
    assert shots["V01"].end == shots["I04"].start
    assert max(c.end for c in tl.connected) == tl.total_frames > line_end  # and runs to the end, not to the padding
    assert not [w for w in tl.warnings if "B-roll" in w]


def test_a_lone_shot_named_on_later_words_starts_on_them():
    from roughcut.timeline import _gaps_without_picture

    a = narration_project()

    def one(clip, words):
        raw = {"sections": [{"name": "x", "items": ["A01.S002"], "broll": [{"clip": clip, "over": ["A01.S002"], "words": words, "source_in": -1, "reason": ""}]}]}
        return build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")

    # "In Anaheim, Katella Commons is trying to build one.": the planner names the place on its only shot.
    tl = one("I01", "Katella Commons")
    shot = next(c for c in tl.connected if c.clip_id == "I01")
    assert abs(shot.start - _word_frame(tl, a, "A01.S002", "Katella")) <= 2
    assert _gaps_without_picture(tl, a) == []  # auto-fill has the line until then
    # Words at the very top of the line: the shot just starts with it.
    tl = one("V01", "In Anaheim")
    assert next(c for c in tl.connected if c.clip_id == "V01").start <= tl.seg_ranges["A01.S002"][0]
    assert [c.clip_id for c in tl.connected if c.note == "auto-fill"] == []


def test_unfound_words_fall_back_to_the_even_split():
    a = narration_project()
    even = build_timeline(normalize(_honda_plan(("", "", "", "")), a), a, style=STYLES["medium"], name="t", fill=False)
    tl = build_timeline(normalize(_honda_plan(("", "Disneyland", "", "")), a), a, style=STYLES["medium"], name="t", fill=False)
    assert [(c.clip_id, c.start, c.frames) for c in tl.connected] == [(c.clip_id, c.start, c.frames) for c in even.connected]
    assert any('"Disneyland" isn\'t heard' in w for w in tl.warnings)


def test_unanchored_shots_share_the_time_up_to_the_next_anchor():
    a = narration_project()
    tl = build_timeline(normalize(_honda_plan(("", "", "", "parks")), a), a, style=STYLES["medium"], name="t", fill=False)
    shots = {c.clip_id: c for c in tl.connected}
    parks = _word_frame(tl, a, "A01.S003", "parks")
    assert abs(shots["I04"].start - parks) <= 2
    # I01, I02 and V01 split the time before "parks"; the stills are capped, the video fills its share.
    assert shots["I01"].start == 0  # the line opens the timeline: on frame 0, not after the padding
    assert shots["V01"].end == shots["I04"].start


def test_a_plan_saved_before_words_existed_still_renders(tmp_path):
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut.fcpxml import build_fcpxml

    assert "words" in PLAN_SCHEMA["properties"]["sections"]["items"]["properties"]["broll"]["items"]["required"]
    a = narration_project()
    old = _honda_plan()
    for b in old["sections"][0]["broll"]:
        del b["words"]
    plan = normalize(old, a)
    assert all(b.words == "" for b in plan.sections[0].broll)
    assert plan.to_json()["sections"][0]["broll"][0]["words"] == ""
    for raw in (old, _honda_plan()):
        tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
        path = tmp_path / "x.fcpxml"
        path.write_text(build_fcpxml([tl], a, event_name="e"))
        validate_dtd(path)
        check_invariants(path)


def test_words_said_twice_anchor_on_the_occurrence_after_the_previous_shot():
    sentences = [(1.0, 13.0, "Parks matter here because the city plans more parks near the river.")]
    a = Analysis(clips=[make_clip("A01", kind="audio", seconds=15, sentences=sentences, role="voiceover"), make_clip("V01", seconds=60), make_clip("V02", seconds=60)])
    raw = {"sections": [{"name": "x", "items": ["A01.S001"], "broll": [
        {"clip": "V01", "over": ["A01.S001"], "words": "", "source_in": -1, "reason": ""},
        {"clip": "V02", "over": ["A01.S001"], "words": "parks", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    seg = a.segment("A01.S001")
    second = [w for w in seg.words if w.text.strip(".,").lower() == "parks"][1]
    assert abs(v02.start - frame_at(tl, "A01", second.start)) <= 2


def test_lead_is_about_two_frames():
    a = narration_project()
    tl = build_timeline(normalize(_honda_plan(), a), a, style=STYLES["medium"], name="t", fill=False)
    i04 = next(c for c in tl.connected if c.clip_id == "I04")
    lead = _word_frame(tl, a, "A01.S003", "parks") - i04.start
    assert 1 <= lead <= 3
    assert Fraction(lead) * tl.frame_duration < Fraction(1, 8)


def _no_flashes_or_repeated_stills(tl, a):
    fd = tl.frame_duration
    tiny = round(Fraction(1, 2) / fd)
    assert all(c.frames >= tiny for c in tl.connected), [(c.clip_id, c.start, c.frames) for c in tl.connected]
    stills = [c.clip_id for c in tl.connected if a.clip(c.clip_id).media.kind == "image"]
    assert len(stills) == len(set(stills)), stills
    shots = sorted(tl.connected, key=lambda c: c.start)
    assert not [(p.clip_id, q.start) for p, q in zip(shots, shots[1:]) if p.clip_id == q.clip_id and p.end == q.start and q.note == "auto-fill"]


def test_fill_never_flashes_a_few_frames_or_repeats_a_still():
    a = narration_project()
    for words in (("", "Honda Center", "two hotels", "parks"), ("", "", "", "")):
        tl = build_timeline(normalize(_honda_plan(words), a), a, style=STYLES["medium"], name="t")
        _no_flashes_or_repeated_stills(tl, a)
        assert min(c.start for c in tl.connected) == 0  # the first shot starts with the narration's padding
    # A capped still at the start moves up rather than leaving a sliver for a flash shot.
    raw = {"sections": [{"name": "Plan", "items": ["A01.S003"], "broll": [{"clip": "I02", "over": ["A01.S003"], "words": "", "source_in": -1, "reason": ""}]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    _no_flashes_or_repeated_stills(tl, a)
    first = min(tl.connected, key=lambda c: c.start)
    assert first.clip_id == "I02" and first.start == 0


def test_when_one_video_is_all_that_is_left_fill_cuts_between_distant_parts():
    a = narration_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002", "A01.S003", "A01.S004"], "broll": [
        {"clip": f"I0{i + 1}", "over": [f"A01.S00{i + 1}"], "words": "", "source_in": -1, "reason": ""} for i in range(4)
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    stills = [c.clip_id for c in tl.connected if a.clip(c.clip_id).media.kind == "image"]
    assert len(stills) == len(set(stills))
    fill = sorted((c for c in tl.connected if c.clip_id == "V01"), key=lambda c: c.start)
    assert len(fill) >= 3  # the 17 s after the capped still keeps the fill pacing
    for p, q in zip(fill, fill[1:]):
        if p.end == q.start:
            assert abs(float(q.src_in - (p.src_in + p.frames * tl.frame_duration))) >= 5  # another part, not a jump


# ------------------------------------------------------------ T9: one use per line


def _reads():
    from test_narration import VO

    a01 = make_clip("A01", kind="audio", seconds=44, sentences=VO, role="voiceover")
    # The second read says the long line a little differently, with an "um".
    second = VO[:2] + [(12.5, 36.0, "Around Honda Center the plan adds homes, um, offices, two hotels, restaurants and twenty acres of parks.")] + VO[3:]
    a02 = make_clip("A02", kind="audio", seconds=44, sentences=second, role="voiceover")
    a02.take_of, a02.same_words = "A01", 0.97
    return a01, a02


def test_the_same_line_from_another_read_plays_once():
    a01, a02 = _reads()
    a = Analysis(clips=[a01, a02, make_clip("V01", seconds=60)])
    raw = {
        "sections": [
            {"name": "Open", "items": ["A01.S003", "A01.S004"], "broll": []},
            {"name": "Again", "items": ["A02.S003", "A02.S001"], "broll": [
                {"clip": "V01", "over": ["A02.S003", "A02.S001"], "words": "", "source_in": -1, "reason": ""},
            ]},
        ],
        "alternates": [{"chosen": "A01.S004", "others": ["A02.S004"], "note": "cleaner"}],
        "flags": [{"ref": "A02.S003", "note": "check the read"}],
    }
    plan = normalize(raw, a)
    refs = [i.ref for s in plan.sections for i in s.items]
    assert refs == ["A01.S003", "A01.S004", "A02.S001"]
    assert any("A02.S003 says the same line as A01.S003" in w for w in plan.warnings)
    assert {"chosen": "A01.S003", "others": ["A02.S003"], "note": "the same line from another read or export"} in plan.alternates
    assert plan.flags[0]["ref"] == "A01.S003"  # follows the line to the use that was kept
    assert plan.sections[1].broll[0].over == ["A02.S001"]
    # A short line in two reads of one script is the same line too.
    assert [i.ref for i in normalize({"sections": [{"name": "x", "items": ["A01.S004", "A02.S004"]}]}, a).sections[0].items] == ["A01.S004"]


def _split_reads():
    """Two reads of one script; Whisper ended the first sentence with a period in one and a comma in the other."""
    a01 = make_clip("A01", kind="audio", seconds=12, role="voiceover", sentences=[
        (0.5, 4.0, "Over forty chefs are cooking along the pier."), (4.5, 9.0, "Tickets start at twenty dollars and kids get in free.")])
    a02 = make_clip("A02", kind="audio", seconds=12, role="voiceover", sentences=[
        (0.5, 9.0, "Over forty chefs are cooking along the pier, tickets start at twenty dollars and kids get in free.")])
    a02.take_of, a02.same_words = "A01", 0.95
    return Analysis(clips=[a01, a02])


def _kept(a, *items):
    plan = normalize({"sections": [{"name": "x", "items": list(items)}]}, a)
    return [i.ref for i in plan.sections[0].items], plan


def test_a_line_said_again_inside_another_reads_sentence_plays_once():
    a = _split_reads()
    # Half of a sentence already in the cut: dropped, like any repeat.
    refs, plan = _kept(a, "A02.S001", "A01.S001", "A01.S002")
    assert refs == ["A02.S001"]
    assert {"chosen": "A02.S001", "others": ["A01.S001", "A01.S002"], "note": "the same line from another read or export"} in plan.alternates
    # A sentence holding a line in the cut, whose rest is in the cut too (before it, or later on): dropped.
    for items in (["A01.S001", "A01.S002", "A02.S001"], ["A01.S001", "A02.S001", "A01.S002"]):
        refs, plan = _kept(a, *items)
        assert refs == ["A01.S001", "A01.S002"], items
        assert any(w.startswith("A02.S001 says the same line as A01.S001") for w in plan.warnings)
    # Its rest isn't anywhere else: dropping it would lose "tickets start at twenty dollars", so it stays, flagged.
    refs, plan = _kept(a, "A01.S001", "A02.S001")
    assert refs == ["A01.S001", "A02.S001"]
    assert any(w.startswith("A02.S001 says the line A01.S001 says") for w in plan.warnings)
    assert [f["ref"] for f in plan.flags] == ["A02.S001"] and "Repeats A01.S001" in plan.flags[0]["note"]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", fill=False)
    assert any(m.kind == "todo" and m.frame == tl.seg_ranges["A02.S001"][0] and "Repeats A01.S001" in m.value for m in tl.markers)


def test_a_reworded_read_holding_the_line_counts_and_scattered_words_do_not():
    from roughcut.duplicates import contains_line, line_words

    short = line_words("The OC Food Festival takes over Huntington Beach on Saturday.")
    long = line_words("So, the OC Food Festival takes over Huntington Beach this Saturday, June 14th.")
    assert contains_line(long, short, same_takes=True) and contains_line(long, short, same_takes=False)
    # Its words, in order, but spread through a long line about something else.
    spread = line_words("We went to the market and then down to the park with the kids today because the weather was nice.")
    assert not contains_line(spread, line_words("We went to the park today."), same_takes=False)
    assert not contains_line(spread, line_words("We went to the park today."), same_takes=True)
    # Short lines across unrelated clips stay the planner's call, as with whole lines.
    assert not contains_line(long, line_words("The OC Food Festival."), same_takes=False)
    v01 = make_clip("V01", seconds=10, sentences=[(1.0, 8.0, "So, the OC Food Festival takes over Huntington Beach this Saturday, June 14th.")])
    v02 = make_clip("V02", seconds=10, sentences=[(1.0, 6.0, "The OC Food Festival takes over Huntington Beach on Saturday.")])
    a = Analysis(clips=[v01, v02])
    assert _kept(a, "V01.S001", "V02.S001")[0] == ["V01.S001"]


def test_a_shared_line_across_unrelated_clips_is_dropped_only_when_long():
    thanks = [(1.0, 2.0, "Thank you."), (3.0, 8.0, "It opens this summer in Anaheim near the arena.")]
    v01 = make_clip("V01", seconds=10, sentences=thanks)
    v02 = make_clip("V02", seconds=10, sentences=thanks)  # a different cutdown of the same promo
    a = Analysis(clips=[v01, v02])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V02.S001", "V01.S002", "V02.S002"]}]}, a)
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001", "V02.S001", "V01.S002"]
    assert plan.alternates == [{"chosen": "V01.S002", "others": ["V02.S002"], "note": "the same line from another read or export"}]
    # Retakes inside one recording stay the planner's call.
    one = Analysis(clips=[make_clip("V01", seconds=20, sentences=thanks + [(10.0, 15.0, "It opens this summer in Anaheim near the arena.")])])
    assert len(normalize({"sections": [{"name": "x", "items": ["V01.S002", "V01.S003"]}]}, one).sections[0].items) == 2


def _rundown(*more: str):
    """A restaurant rundown read to camera, with a retake: S008 and S010 are the same line, "let me redo that" between.
    `more`: lines read after it, from S012."""
    lines = [
        "First up, Kogi is Roy Choi's truck.", "It started the whole movement.", "Then Golden Hour for the sunset.",
        "The bar sits right on the sand.", "Next is Sushi Kaze.", "Chef Kenji Tanaka does omakase.", "Now for pizza.",
        "Dough Boy Pizza comes from Marco Belini.", "Wait, let me redo that.", "Dough Boy Pizza comes from Marco Belini.",
        "That's the list for this year.", *more,
    ]
    speech = [(1.0 + 3 * n, 3.4 + 3 * n, text) for n, text in enumerate(lines)]
    return Analysis(clips=[make_clip("V01", seconds=40, sentences=speech), make_clip("V02", seconds=20)])


def test_the_planners_alternates_decide_which_take_of_a_line_plays():
    """The planner says it chose S010 over S008 (alternates) but its sections play both, or only S008: the line
    plays once, as the take it says it chose."""
    a = _rundown()
    assert a.segment("V01.S008").text == a.segment("V01.S010").text
    alternates = [{"chosen": "V01.S010", "others": ["V01.S008"], "note": "Used the retake."}]
    # Both in the sections: the other take is dropped.
    raw = {
        "sections": [{"name": "Rundown", "items": ["V01.S001", "V01.S007", "V01.S008", "V01.S010", "V01.S011"], "broll": [
            {"clip": "V02", "over": ["V01.S008"], "words": "Dough Boy Pizza", "source_in": -1, "reason": ""}]}],
        "alternates": alternates,
        "flags": [{"ref": "V01.S010", "note": "Used the retake. S008 and S009 are cut."}],
    }
    plan = normalize(raw, a, source="m:x")
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001", "V01.S007", "V01.S010", "V01.S011"]
    assert any(w.startswith("V01.S008 is another take of V01.S010") for w in plan.warnings)
    assert plan.sections[0].broll[0].over == ["V01.S010"]  # the shot on the line follows it to the take that plays
    assert plan.alternates == [{"chosen": "V01.S010", "others": ["V01.S008"], "note": "Used the retake."}]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", fill=False)
    assert "V01.S008" not in tl.seg_ranges and "V01.S010" in tl.seg_ranges
    # Only the other in the sections: the chosen take plays in its place.
    raw = {
        "sections": [{"name": "Rundown", "items": ["V01.S001", "V01.S007", "V01.S008", "V01.S011"], "broll": []}],
        "alternates": alternates,
        "graphics": [{"kind": "lower_third", "segment": "V01.S008", "words": "Marco Belini", "text": "Marco Bellini", "subtext": "Dough Boy Pizza",
                      "seconds": 3, "direction": ""}],
    }
    plan = normalize(raw, a, source="m:x")
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001", "V01.S007", "V01.S010", "V01.S011"]
    assert any(w.startswith("V01.S008 is another take of V01.S010") and "in its place" in w for w in plan.warnings)
    assert [g.segment for g in plan.graphics] == ["V01.S010"]
    # More than one other take in the sections: the chosen one in the first one's place, the rest dropped.
    third = _rundown("Dough Boy Pizza, that's from Marco Belini.")
    raw = {"sections": [{"name": "x", "items": ["V01.S008", "V01.S007", "V01.S012"]}],
           "alternates": [{"chosen": "V01.S010", "others": ["V01.S008", "V01.S012", "V01.S010", "V01.S404"], "note": ""}]}
    plan = normalize(raw, third, source="m:x")
    assert [i.ref for i in plan.sections[0].items] == ["V01.S010", "V01.S007"]
    assert plan.alternates == [{"chosen": "V01.S010", "others": ["V01.S008", "V01.S012"], "note": ""}]
    # Alternates that contradict each other don't swap a take back and forth.
    raw = {"sections": [{"name": "x", "items": ["V01.S008", "V01.S007"]}],
           "alternates": [{"chosen": "V01.S010", "others": ["V01.S008"], "note": ""}, {"chosen": "V01.S008", "others": ["V01.S010"], "note": ""}]}
    assert [i.ref for i in normalize(raw, a, source="m:x").sections[0].items] == ["V01.S010", "V01.S007"]
    # The stringout plays everything, so `render` of a cut made without AI makes the same cut.
    raw = {"sections": [{"name": "x", "items": ["V01.S008", "V01.S010"]}], "alternates": alternates}
    assert [i.ref for i in normalize(raw, a, source="heuristic").sections[0].items] == ["V01.S008", "V01.S010"]
    # Odd shapes from a model are still never fatal.
    raw = {"sections": [{"name": "x", "items": "V01.S008"}, {"name": "y", "items": ["V01.S008"]}], "alternates": [{"chosen": "V01.S010", "others": 8}, "x"]}
    assert [i.ref for s in normalize(raw, a, source="m:x").sections for i in s.items] == ["V01.S008"]
    # The planner and the reviser are told which take "chosen" is, so a note that swaps takes isn't swapped back.
    from roughcut.prompts import PLANNER_SYSTEM
    from roughcut.revise import REVISE_INSTRUCTION

    assert '"chosen" is the take in your sections' in " ".join(PLANNER_SYSTEM.split())
    assert "the take you use becomes that alternate's chosen one" in " ".join(REVISE_INSTRUCTION.split())


def test_a_line_the_planner_lists_as_another_take_is_one_only_when_it_reads_that_line():
    """The live plan listed the "let me redo that" flub among the Dough Boy line's other takes. It isn't one: no Alt take
    keyword, marker or report line names it, and a plan that keeps it in the cut keeps it."""
    from roughcut.fcpxml import ALT_TAKE, build_fcpxml
    from roughcut.report import build_report

    a = _rundown()
    alternates = [{"chosen": "V01.S010", "others": ["V01.S008", "V01.S009"], "note": "S009 is the 'let me redo that' flub; S010 is the clean redo."}]
    raw = {"sections": [{"name": "Rundown", "items": ["V01.S001", "V01.S007", "V01.S010", "V01.S011"], "broll": []}], "alternates": alternates}
    plan = normalize(raw, a, source="m:x")
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001", "V01.S007", "V01.S010", "V01.S011"]
    assert [(alt["chosen"], alt["others"]) for alt in plan.alternates] == [("V01.S010", ["V01.S008"])]
    assert any(w.startswith("V01.S009 doesn't say the line V01.S010 says") for w in plan.warnings)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", fill=False)
    xml = build_fcpxml([tl], a, event_name="e", alternates=plan.alternates)
    alt_notes = re.findall(rf'value="{ALT_TAKE}" note="([^"]*)"', xml)
    assert len(alt_notes) == 1 and alt_notes[0].startswith("V01.S008: another read of V01.S010")
    assert [m.value for m in tl.markers if m.value.startswith("ALT takes")] == ["ALT takes: V01.S008"]
    report = build_report(plan, tl, a)
    assert "**V01.S010** chosen over V01.S008." in report and "V01.S009" not in report.split("## Alternate takes", 1)[1].split("##", 1)[0]

    # Kept in the cut (a false start the plan wants), it stays: only real retakes are replaced or dropped.
    raw = {"sections": [{"name": "Rundown", "items": ["V01.S007", "V01.S009", "V01.S010"], "broll": []}], "alternates": alternates}
    plan = normalize(raw, a, source="m:x")
    assert [i.ref for i in plan.sections[0].items] == ["V01.S007", "V01.S009", "V01.S010"]
    assert not any(w.startswith("V01.S009 is another take") for w in plan.warnings)
    # The flub alone listed: no alternate at all.
    raw = {"sections": [{"name": "Rundown", "items": ["V01.S007", "V01.S010"]}], "alternates": [{"chosen": "V01.S010", "others": ["V01.S009"], "note": ""}]}
    assert normalize(raw, a, source="m:x").alternates == []
    # A take is most of the same words, the line with um/uh, or one inside the other (a false start, a longer read).
    from roughcut.duplicates import another_read

    line = a.segment("V01.S010").text
    assert another_read("Um, Dough Boy Pizza comes from, uh, Marco Belini.", line) and another_read("Dough Boy Pizza comes", line)
    assert another_read("Dough Boy Pizza comes from Marco Belini, who trained in Naples for ten years.", line)
    assert not another_read("Wait, hold on, let me redo that.", line) and not another_read("Now for pizza.", line)


# ------------------------------------------------------------ T10: fillers and pacing


@pytest.mark.parametrize("name", ["tight", "medium", "loose"])
def test_keep_fillers_keeps_the_rest_of_the_style(name):
    from dataclasses import asdict

    from roughcut.pipeline import Options, style_for

    base = STYLES[name]
    kept = style_for(Options(inputs=[], style=name, keep_fillers=True))
    assert kept.remove_fillers is False
    assert {k: v for k, v in asdict(kept).items() if k != "remove_fillers"} == {k: v for k, v in asdict(base).items() if k != "remove_fillers"}
    assert style_for(Options(inputs=[], style=name)) == base


def _short_uh(kind):
    from roughcut.segments import build_segments
    from roughcut.transcribe import Word

    clip = make_clip("A01" if kind == "audio" else "V01", kind=kind, seconds=6, role="voiceover" if kind == "audio" else "aroll")
    words = [Word(1.0, 1.4, "We"), Word(1.4, 1.8, "went"), Word(1.8, 2.0, "uh,"), Word(2.0, 2.4, "to"), Word(2.4, 2.9, "the"), Word(2.9, 3.4, "park.")]
    clip.segments = build_segments(clip.id, words)
    return clip


def test_a_short_uh_is_cut_from_narration_and_left_on_camera():
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import speech_pieces

    vo = _short_uh("audio")
    pieces = speech_pieces(vo.segments[0], vo, STYLES["medium"], 0)
    assert len(pieces) == 2 and pieces[0].b <= 1.8 + 1e-9 and pieces[1].a >= 2.0 - 1e-9
    a = Analysis(clips=[vo, make_clip("V09", seconds=30)])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    kept = [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]
    assert not [k for k in kept if k[0] < 1.95 and k[1] > 1.85], kept  # no part of the uh plays

    cam = _short_uh("video")
    assert len(speech_pieces(cam.segments[0], cam, STYLES["medium"], 0)) == 1  # a 0.2 s cut would jump the picture
    a = Analysis(clips=[cam])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert any(n.startswith("Left 1 short um/uh on camera") for n in tl.notes)
    keep = build_timeline(heuristic_plan(a), a, style=STYLES["medium"].__class__(0.1, 0.2, 0.6, remove_fillers=False), name="t")
    assert not any("um/uh" in n for n in keep.notes)


# ------------------------------------------------------------ T14: length and review


@pytest.mark.parametrize("style", ["tight", "medium", "loose"])
def test_the_kept_length_shown_to_the_planner_is_what_gets_built(style):
    from conftest import SPEECH

    from roughcut.prompts import render_media
    from roughcut.timeline import kept_length

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    clip = a.clip("V01")
    for seg in clip.segments:
        kept, cuts = kept_length(seg, clip, a, STYLES[style])
        tl = build_timeline(normalize({"sections": [{"name": "x", "items": [seg.id]}]}, a), a, style=STYLES[style], name="t")
        assert abs(tl.seconds - kept) <= 2 * len(tl.spine) * float(tl.frame_duration), seg.id
        assert cuts == len(tl.spine) - 1, seg.id
    media = render_media(a, STYLES[style])
    uh = clip.segments[5]  # "And honestly, uh, I needed it too.": the uh is cut
    kept, _ = kept_length(uh, clip, a, STYLES[style])
    assert f"{uh.id} [0:15.4-0:18.0 | 2.6s → {kept:.1f}s, 1 edit] And honestly" in media
    assert render_media(a, STYLES[style]) == media  # deterministic, so the prompt cache still hits


def test_review_finds_an_uncovered_jump_cut_and_marks_it():
    from conftest import SPEECH

    from roughcut.plan import heuristic_plan

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=10)])
    raw = {"sections": [{"name": "Open", "items": ["V01.S003", "V01.S005"]}, {"name": "End", "items": ["V01.S006"]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t", target_seconds=10)
    rv = tl.review
    assert rv.target == 10 and "target 0:10" in rv.length_line() and "under" in rv.length_line()
    assert [s for s, _ in rv.sections] == ["Open", "End"] and sum(sec for _, sec in rv.sections) == pytest.approx(tl.seconds)
    lines = [(j.before, j.after) for j in rv.jump_cuts]
    assert (["V01.S003"], ["V01.S005"]) in lines and (["V01.S005"], ["V01.S006"]) in lines
    jump = next(j for j in rv.jump_cuts if j.before == ["V01.S003"])
    todo = [m for m in tl.markers if m.kind == "todo" and m.value.startswith("Jump cut")]
    assert jump.frame in [m.frame for m in todo] and "V01.S003" in todo[0].note
    assert any("Jump cuts on camera with no B-roll over them: " in x for x in rv.lines(tl.frame_duration))

    # A cutaway over the next line hides the jump: it starts on the edit, not after the padding.
    raw["sections"][0]["broll"] = [{"clip": "V02", "over": ["V01.S005"], "words": "", "source_in": -1, "reason": ""}]
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    assert (["V01.S003"], ["V01.S005"]) not in [(j.before, j.after) for j in tl.review.jump_cuts]
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    edit = next(sc.offset for sc in tl.spine if "V01.S005" in sc.seg_ids)
    assert v02.start == edit

    # The stringout is all jump cuts by design: reviewed, not marked.
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert tl.review.jump_cuts and not [m for m in tl.markers if m.value.startswith("Jump cut")]


def test_review_counts_fill_and_repeated_stills():
    a = narration_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S002", "I02@0-3", "A01.S003"], "broll": [
        {"clip": "I02", "over": ["A01.S002"], "words": "", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    assert tl.review.repeated_stills == {"I02": 2}
    assert tl.review.filled_shots > 0 and tl.review.filled_seconds > 10


def test_report_and_render_say_how_long_against_the_target(footage, tmp_path, monkeypatch, capsys):
    from conftest import FakeProvider

    from roughcut import pipeline
    from roughcut.cli import main

    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "-t", "1m", "--no-menu", "--name", "f"]) == 0
    printed = capsys.readouterr().out
    assert "Rough cut: 0:" in printed and ", target 1:00 (" in printed
    report = (out / "edit_report.md").read_text()
    assert "target 1:00" in report and "## Cut check" in report
    assert main(["render", str(out), "--style", "tight"]) == 0
    assert ", target 1:00 (" in capsys.readouterr().out
    assert "target 1:00" in (out / "edit_report.md").read_text()


# ------------------------------------------------------------ T6: finished promos over a music bed

# One sentence with a 0.8 s breath in the middle (longer than medium's 0.6 s), then another.
PROMO = [(1.0, 3.0, "So this is the new arena district"), (3.8, 5.6, "coming to Anaheim next year."), (6.4, 9.0, "It opens with restaurants and music.")]
BEDS = {
    "music": "aevalsrc=exprs='0.02*(sin(2*PI*262*t)+sin(2*PI*330*t)+sin(2*PI*392*t)+0.5*sin(2*PI*(523+262*floor(mod(t*2,2)))*t))':s=48000:d=11",
    "noise": "anoisesrc=c=pink:a=0.06:d=11:r=48000",
}


@pytest.fixture(scope="module")
def promos(tmp_path_factory):
    import json
    import shutil
    import subprocess

    from conftest import spread_words

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("promos")
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in PROMO)
    speech = f"0.5*sin(2*PI*180*t)*(0.6+0.4*sin(2*PI*5*t))*({gates})"
    words = [w for s, e, text in PROMO for w in spread_words(s, e, text)]
    for name, bed in BEDS.items():
        folder = root / name
        folder.mkdir()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=4", "-f", "lavfi", "-i", "smptebars=size=320x180:rate=25:duration=4",
             "-f", "lavfi", "-i", "color=c=navy:size=320x180:rate=25:duration=3",
             "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d=11", "-f", "lavfi", "-i", bed,
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a][4:a]amix=inputs=2:normalize=0[a]",
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(folder / "promo.mov")],
            check=True,
        )
        (folder / "promo.json").write_text(json.dumps({"words": words}))
    return root


def _analyse(folder, cache_dir):
    from roughcut.analyze import Cache, analyze

    return analyze([folder / "promo.mov"], transcriber=None, cache=Cache(cache_dir))


def _spans(tl):
    return [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]


def test_a_promo_over_music_keeps_its_breaths_and_noise_does_not(promos, tmp_path):
    from roughcut.plan import heuristic_plan
    from roughcut.prompts import render_media

    music = _analyse(promos / "music", tmp_path / "cache")
    clip = music.clips[0]
    assert clip.silence_db > -35 and clip.bed == "music" and clip.cuts_per_min >= 4  # two picture cuts in 11 s
    assert any("speech over a music bed in a finished edit" in n for n in music.notes)
    assert "a finished edit, speech over a music bed: take consecutive lines together" in render_media(music, STYLES["medium"])
    tl = build_timeline(heuristic_plan(music), music, style=STYLES["medium"], name="t")
    assert any(a < 3.0 and b > 3.8 for a, b in _spans(tl)), _spans(tl)  # the 0.8 s breath plays

    noise = _analyse(promos / "noise", tmp_path / "cache")
    assert noise.clips[0].silence_db > -35 and noise.clips[0].bed == "noise"
    assert "music bed" not in render_media(noise, STYLES["medium"])
    tl = build_timeline(heuristic_plan(noise), noise, style=STYLES["medium"], name="t")
    assert not any(a < 3.2 and b > 3.6 for a, b in _spans(tl)), _spans(tl)  # dead air over room noise goes

    # Cached: a second analysis doesn't measure again, and the result survives analysis.json.
    again = Analysis.from_json(_analyse(promos / "music", tmp_path / "cache").to_json())
    assert (again.clips[0].bed, again.clips[0].cuts_per_min) == ("music", clip.cuts_per_min)


def test_a_flagged_line_that_starts_on_a_jump_cut_gets_one_todo():
    v01 = make_clip("V01", seconds=12, sentences=[
        (0.5, 2.0, "Hello there my good friends."), (2.6, 5.0, "Um, so the ferry leaves every hour."), (5.4, 8.0, "Come early and bring a jacket."),
    ])
    a = Analysis(clips=[v01])
    raw = {"sections": [{"name": "x", "items": ["V01.S001", "V01.S002", "V01.S003"]}], "flags": [{"ref": "V01.S002", "note": "check this line"}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    edit = tl.seg_ranges["V01.S002"][0]
    assert edit in [j.frame for j in tl.review.jump_cuts]  # the um is cut, so the line starts on a jump
    assert [m.value for m in tl.markers if m.kind == "todo" and m.frame == edit] == ["check this line"]
    assert [m.frame for m in tl.markers] == sorted(m.frame for m in tl.markers)


def test_a_flag_about_a_cutaway_becomes_a_marker_on_it(tmp_path):
    from conftest import SPEECH
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut.fcpxml import build_fcpxml

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=10)])
    raw = {"sections": [{"name": "x", "items": ["V01.S003", "V01.S004", "V01.S005"], "broll": [
        {"clip": "V02", "over": ["V01.S004"], "words": "", "source_in": -1, "reason": ""},
    ]}], "flags": [{"ref": "V02", "note": "B-roll is a test pattern: re-link the real footage"}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    assert [m.frame for m in tl.markers if m.value.startswith("B-roll is a test pattern")] == [v02.start]
    path = tmp_path / "t.fcpxml"
    path.write_text(build_fcpxml([tl], a, event_name="e"))
    validate_dtd(path)
    check_invariants(path)
    assert path.read_text().count("B-roll is a test pattern") >= 1


def test_jumps_in_a_music_bed_get_markers_and_a_clashing_cue_a_warning():
    from conftest import SPEECH

    v01 = make_clip("V01", seconds=20, sentences=SPEECH)
    v01.bed, v01.cuts_per_min = "music", 12.0
    a = Analysis(clips=[v01, make_clip("A01", kind="audio", seconds=60)])
    raw = {"sections": [{"name": "x", "items": ["V01.S005", "V01.S003", "V01.S004"]}], "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 0}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    todo = [m for m in tl.markers if m.kind == "todo"]
    assert [m.value for m in todo] == ["Music jumps: smooth this edit"]  # S005 -> S003; S003+S004 play straight through
    assert len({m.frame for m in todo}) == len(todo)  # not also marked as a jump cut
    assert any("1 edit jumps the music" in n for n in tl.notes)
    assert any("music A01 plays over V01" in w for w in tl.warnings)


# ------------------------------------------------------------ T21: moments in long B-roll

MOMENTS = [
    {"at": 6.0, "note": "wide shot of the boardwalk, people walking", "usable": True},
    {"at": 30.0, "note": "close-up of a coffee cup on a cafe table", "usable": True},
    {"at": 45.0, "note": "shaky pan, lens flare", "usable": False},
    {"at": 54.0, "note": "kids riding bikes past the pier", "usable": True},
]


def _walk_project():
    sentences = [(0.5, 5.0, "We started the morning down by the pier."), (5.5, 10.0, "Then we stopped for coffee at a little cafe.")]
    walk = make_clip("V02", seconds=60)
    walk.visual = {"description": "Walking tour of the beach town", "tags": ["walk", "beach"], "shot": "pov", "quality": "good", "moments": [dict(m) for m in MOMENTS]}
    return Analysis(clips=[make_clip("A01", kind="audio", seconds=11, sentences=sentences, role="voiceover"), walk])


def test_a_cutaway_left_to_the_software_starts_on_the_moment_that_fits():
    a = _walk_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002"], "broll": [
        {"clip": "V02", "over": ["A01.S002"], "words": "", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    shot = next(c for c in tl.connected if c.clip_id == "V02")
    assert float(shot.src_in) == pytest.approx(30.0, abs=0.05)  # "coffee ... cafe", not 15% in
    raw["sections"][0]["broll"][0]["source_in"] = 12
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    assert float(next(c for c in tl.connected if c.clip_id == "V02").src_in) == pytest.approx(12, abs=0.05)  # the planner's pick stands


def test_fill_starts_long_clips_on_their_moments_and_never_on_an_unusable_one():
    from roughcut.plan import heuristic_plan

    a = _walk_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    starts = sorted(round(float(c.src_in)) for c in tl.connected if c.clip_id == "V02")
    assert starts and set(starts) <= {6, 30, 54}, starts
    assert 30 in starts  # the coffee line gets the cafe


def test_moments_are_logged_once_with_labelled_frames_and_stills_stay_cached(tmp_path):
    import shutil
    import subprocess

    from conftest import FakeProvider

    from roughcut.analyze import Cache, Clip
    from roughcut.media import probe
    from roughcut.prompts import render_media
    from roughcut.vision import describe_clips, moment_times

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")

    class Logger(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, purpose, effort=None, max_tokens=64000):
            if schema_name != "clip_moments":
                return super().complete_json(system, parts, schema, schema_name=schema_name, purpose=purpose)
            self.calls.append({"schema_name": schema_name, "parts": parts})
            labels = [p.text for p in parts if hasattr(p, "text") and " @" in p.text]
            clip_id = labels[0].split()[0]
            times = [float(lbl.rsplit("(", 1)[1].rstrip("s)")) for lbl in labels]
            return {"clips": [{"id": clip_id, "moments": [{"at": t + 0.4, "note": f"frame at {t:g}", "usable": i != 1} for i, t in enumerate(times)]}]}

    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp_path / "walk.mp4"))
    ffmpeg("-f", "lavfi", "-i", "smptebars=size=160x90:rate=25:duration=6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp_path / "short.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x90:duration=1", "-frames:v", "1", str(tmp_path / "photo.png"))

    def project():
        clips = [Clip(id="V01", media=probe(tmp_path / "walk.mp4"), role="broll"), Clip(id="V02", media=probe(tmp_path / "short.mp4"), role="broll"),
                 Clip(id="I01", media=probe(tmp_path / "photo.png"), role="still")]
        return Analysis(clips=clips)

    cache = Cache(tmp_path / "cache")
    a, provider = project(), Logger()
    assert describe_clips(a, provider, cache, workers=1) == []
    assert [c["schema_name"] for c in provider.calls] == ["clip_log", "clip_moments"]
    sampled = moment_times(a.clip("V01"))
    assert len(sampled) == 3 and not moment_times(a.clip("V02")) and not moment_times(a.clip("I01"))
    parts = provider.calls[1]["parts"]
    kinds = ["label" if getattr(p, "text", "").startswith("V01 @") else "image" if hasattr(p, "data") else "text" for p in parts]
    assert kinds == ["text"] + ["label", "image"] * 3 + ["text"]  # each frame says when it is
    moments = a.clip("V01").visual["moments"]
    assert [m["at"] for m in moments] == sampled  # snapped to the frames it was shown
    assert [m["usable"] for m in moments] == [True, False, True]
    assert a.clip("V01").visual["description"]  # the description is kept alongside
    assert f"Moments: {sampled[0]:g}s frame at" in render_media(a, STYLES["medium"]) and "(not usable)" in render_media(a, STYLES["medium"])

    again, provider = project(), Logger()
    describe_clips(again, provider, cache, workers=1)
    assert provider.calls == []  # stills, descriptions and moments all come from the cache
    assert again.clip("V01").visual["moments"] == moments and again.clip("I01").visual["description"]


HOME = [(1.0, 4.0, "We drove all the way back home from the coast."), (5.2, 7.5, "And then we unpacked the car and the bikes."), (8.65, 10.5, "It was late by then for everyone.")]
HOME_SILENCES = [(0.0, 1.0), (4.0, 4.45), (4.75, 5.2), (7.5, 7.95), (8.2, 8.65), (10.5, 12.0)]
HOME_BREATH = (4.45, 4.75)  # quiet; the sound between 7.95 and 8.2 is loud, so it's not a breath


def _home_clip(kind, breaths):
    from conftest import spread_words

    from roughcut.segments import build_segments
    from roughcut.transcribe import Word

    words = [Word(**w) for s, e, text in HOME for w in spread_words(s, e, text)]
    for x, y in zip(words, words[1:]):  # Whisper: each word runs up to the next
        x.end = y.start
    clip = make_clip("V01" if kind == "video" else "A01", kind=kind, seconds=12, silences=list(HOME_SILENCES), role="aroll" if kind == "video" else "voiceover")
    clip.segments = build_segments(clip.id, words, gap_split=5.0)  # one long segment, like run-on speech
    clip.breaths = list(breaths)
    return clip


def _home_spans(kind, breaths, style):
    from roughcut.plan import heuristic_plan

    a = Analysis(clips=[_home_clip(kind, breaths)])
    return _spans(build_timeline(heuristic_plan(a), a, style=STYLES[style], name="t"))


def _plays(spans, t):
    return any(a <= t < b for a, b in spans)


def test_a_pause_split_by_a_breath_is_one_pause_on_camera():
    # Each half (0.45 s) is under medium's 0.6 s, so without the breath marked the whole 1.2 s pause stays.
    assert _plays(_home_spans("video", [], "medium"), 4.6)
    spans = _home_spans("video", [HOME_BREATH], "medium")
    assert not any(_plays(spans, t) for t in (4.3, 4.6, 4.9))  # one edit, breath and all
    assert _plays(spans, 3.9) and _plays(spans, 5.25)
    assert _plays(spans, 8.0)  # the loud sound between two pauses is never cut
    # Tight cut each half and left the breath as a half-second flash; now it's one edit.
    flashes = [(a, b) for a, b in _home_spans("video", [], "tight") if b - a < 0.6 and a < 4.6 < b]
    assert flashes and not _plays(_home_spans("video", [HOME_BREATH], "tight"), 4.6)


def test_narration_keeps_the_breath_and_loses_the_silence_around_it():
    spans = _home_spans("audio", [HOME_BREATH], "medium")
    assert _plays(spans, 4.6)  # sound is never removed from narration
    assert not _plays(spans, 4.3) and not _plays(spans, 4.95)
    assert _plays(spans, 3.9) and _plays(spans, 5.25)


def test_a_silence_holding_a_word_is_never_joined_across():
    from roughcut.timeline import _pause_clusters

    clip = _home_clip("video", [HOME_BREATH])
    assert [(s, e) for s, e, _ in _pause_clusters(clip)][1] == (4.0, 5.2)
    clip.silences[2] = (4.75, 5.6)  # now holds a whole word (quiet speech under a raised level)
    assert (4.0, 5.6) not in [(s, e) for s, e, _ in _pause_clusters(clip)]


@pytest.fixture(scope="module")
def breath_footage(tmp_path_factory):
    import json
    import shutil
    import subprocess

    from conftest import spread_words

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("breath")
    speech = "+".join(f"0.5*sin(2*PI*200*t)*between(t,{s},{e})" for s, e, _ in HOME)
    expr = f"{speech}+0.06*sin(2*PI*400*t)*between(t,4.45,4.75)+0.3*sin(2*PI*300*t)*between(t,7.95,8.2)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30000/1001:duration=12",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(root / "cam.mov")], check=True)
    words = [w for s, e, text in HOME for w in spread_words(s, e, text)]
    for x, y in zip(words, words[1:]):
        x["end"] = y["start"]
    (root / "cam.json").write_text(json.dumps({"words": words}))
    return root


def test_breaths_are_found_by_level_and_cut_on_camera(breath_footage, tmp_path):
    from roughcut.analyze import Cache, analyze
    from roughcut.plan import heuristic_plan

    a = analyze([breath_footage / "cam.mov"], transcriber=None, cache=Cache(tmp_path / "cache"))
    clip = a.clips[0]
    assert len(clip.breaths) == 1 and clip.breaths[0] == pytest.approx(HOME_BREATH, abs=0.03)  # the loud one isn't
    spans = _spans(build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t"))
    assert not _plays(spans, 4.6) and _plays(spans, 8.0)
    assert Analysis.from_json(a.to_json()).clips[0].breaths == clip.breaths


@pytest.mark.parametrize("bed,pieces", [("music", 1), ("noise", 2), (None, 2)])
def test_pauses_over_a_music_bed_stay_up_to_a_second(bed, pieces):
    from roughcut.timeline import kept_length

    clip = make_clip("V01", seconds=12, sentences=PROMO, silences=[(3.0, 3.8), (5.6, 6.4)])
    clip.bed = bed
    a = Analysis(clips=[clip])
    seg = clip.segments[0]  # "... district [0.8 s] coming to Anaheim next year."
    assert kept_length(seg, clip, a, STYLES["medium"])[1] == pieces - 1
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V01.S002"]}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert len(tl.spine) == pieces + (0 if bed == "music" else 1)  # S001 and S002 join over music too
    assert kept_length(seg, clip, a, STYLES["loose"])[1] == 0  # loose keeps 1.2 s anyway


# ------------------------------------------------------------ review fixes


def _adjacent_replays(tl):
    """Back-to-back shots of one clip whose source ranges overlap: the same footage again."""
    fd = tl.frame_duration
    shots = sorted(tl.connected, key=lambda c: c.start)
    return [
        (p.clip_id, q.start)
        for p, q in zip(shots, shots[1:])
        if p.clip_id == q.clip_id and p.end == q.start and q.src_in < p.src_in + p.frames * fd and p.src_in < q.src_in + q.frames * fd
    ]


def test_fill_from_one_logged_clip_never_replays_the_shot_on_screen():
    from roughcut.plan import heuristic_plan

    lines = [(0.5 + i * 10, 10.0 + i * 10, f"Line {i} walks along the river past the old city wall.") for i in range(4)]
    for moments in (
        [{"at": 6.0, "note": "wide", "usable": True}, {"at": 30.0, "note": "close", "usable": True}],
        [{"at": 6.0, "note": "wide", "usable": True}, {"at": 18.0, "note": "shaky", "usable": False}, {"at": 30.0, "note": "close", "usable": True}],
    ):
        walk = make_clip("V01", seconds=40)
        walk.visual = {"description": "river walk", "tags": [], "moments": moments}
        a = Analysis(clips=[make_clip("A01", kind="audio", seconds=42, sentences=lines, role="voiceover"), walk])
        tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
        assert len(tl.connected) >= 8 and not _adjacent_replays(tl), [(c.start, float(c.src_in)) for c in tl.connected]
        starts = [round(float(c.src_in)) for c in sorted(tl.connected, key=lambda c: c.start)]
        assert starts[:2] == [6, 30]  # the good moments first
        fd = tl.frame_duration
        assert not [c.start for c in tl.connected if float(c.src_in) <= 18.0 < float(c.src_in + c.frames * fd)]  # never the shaky part


def test_a_cutaway_named_on_later_words_still_covers_the_jump_cut_before_them():
    from conftest import SPEECH

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=10)])
    raw = {"sections": [{"name": "x", "items": ["V01.S003", "V01.S005"], "broll": [
        {"clip": "V02", "over": ["V01.S003", "V01.S005"], "words": "kids have been asking", "source_in": -1, "reason": "cover the jump cut"},
    ]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    edit = next(sc.offset for sc in tl.spine if "V01.S005" in sc.seg_ids)
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    assert v02.start == edit  # on the jump, not after it at "kids"
    assert tl.review.jump_cuts == [] and not [m for m in tl.markers if m.value.startswith("Jump cut")]


def test_cutaways_at_the_very_start_and_end_never_flash():
    from conftest import SPEECH

    from roughcut.timeline import ConnectedClip, _extend_shot_after

    a = narration_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002"], "broll": [
        {"clip": "V01", "over": ["A01.S001"], "words": "", "source_in": 0, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    _no_flashes_or_repeated_stills(tl, a)
    first = min(tl.connected, key=lambda c: c.start)
    assert (first.clip_id, first.start, first.src_in) == ("V01", 0, 0)  # no stray still before it, and the still isn't used up

    # On camera: a cutaway over the first and last lines covers the whole cut, not 3..total-6.
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=30)])
    lines = ["V01.S003", "V01.S004", "V01.S005"]
    raw = {"sections": [{"name": "x", "items": lines, "broll": [{"clip": "V02", "over": lines, "words": "", "source_in": -1, "reason": ""}]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    assert [(c.start, c.end) for c in tl.connected] == [(0, tl.total_frames)]

    # A video already at the head of its source closes a sliver before it by playing on longer.
    a = narration_project()
    tl = build_timeline(normalize({"sections": [{"name": "x", "items": ["A01.S003"]}]}, a), a, style=STYLES["medium"], name="t", fill=False)
    shot = ConnectedClip("V01", Fraction(0), 100, 10, 1, "broll")
    tl.connected = [shot]
    assert _extend_shot_after(tl, a, 5, 10, max_still=150)
    assert (shot.start, shot.src_in, shot.frames) == (5, 0, 105)


def test_render_of_a_stringout_plays_every_line_again(tmp_path):
    import json

    from roughcut.pipeline import Options, rerender
    from roughcut.plan import heuristic_plan

    shared = "Katella Commons opens next summer with twenty acres of parks."
    p1 = make_clip("V01", seconds=12, sentences=[(1.0, 4.0, "Welcome to the new arena district."), (5.0, 10.0, shared)])
    p2 = make_clip("V02", seconds=12, sentences=[(1.0, 4.0, "Here is what is coming next year."), (5.0, 10.0, shared)])
    a = Analysis(clips=[p1, p2])
    plan = heuristic_plan(a, title="promos (dead air removed)")
    built = build_timeline(plan, a, style=STYLES["medium"], name="t")
    refs = [i.ref for s in plan.sections for i in s.items]
    assert [i.ref for s in normalize(plan.to_json(), a, source="heuristic").sections for i in s.items] == refs
    a.save(tmp_path / "analysis.json")
    (tmp_path / "plan.json").write_text(json.dumps({"name": "promos", "plan": plan.to_json(), "raw": None}))
    again = rerender(tmp_path, Options(inputs=[])).timeline
    assert [(s.clip_id, s.src_in, s.frames) for s in again.spine] == [(s.clip_id, s.src_in, s.frames) for s in built.spine]
    assert not [m for m in again.markers if m.value.startswith("ALT")]
    # A planned cut still plays the shared line once.
    assert len([i for s in normalize(plan.to_json(), a, source="fake").sections for i in s.items]) == len(refs) - 1


@pytest.mark.parametrize("style", ["tight", "medium", "loose"])
def test_fill_never_leaves_a_few_frames_after_a_short_clip(style):
    from test_narration import VO

    from roughcut.timeline import _gaps_without_picture

    vo = make_clip("A01", kind="audio", seconds=44, sentences=VO, role="voiceover")
    a = Analysis(clips=[vo, make_clip("V01", seconds=120), make_clip("V06", seconds=3)])
    tl = build_timeline(normalize({"sections": [{"name": "x", "items": ["A01.S003"]}]}, a), a, style=STYLES[style], name="t")
    tiny = round(Fraction(1, 2) / tl.frame_duration)
    assert all(c.frames >= tiny for c in tl.connected), [(c.clip_id, c.start, c.frames) for c in tl.connected]
    assert _gaps_without_picture(tl, a) == [] and not _adjacent_replays(tl)


@pytest.mark.parametrize("seconds", [2.9, 3.0, 3.1, 3.2])
@pytest.mark.parametrize("last", [20.0, 8.4])
def test_a_capped_still_moved_up_for_a_short_clip_never_leaves_black_after_it(seconds, last):
    """A clip a few frames short of the gap before a still at its longest moves the still up.
    Its end moves up too, and those frames must be filled (with more narration after it, or none)."""
    from roughcut.timeline import _gaps_without_picture

    lines = [(0.3, 3.2, "The pier wakes up early."), (3.5, 15.0 if last > 10 else last, "Over forty chefs are cooking along the boardwalk with fresh fish and tacos and music all afternoon long.")]
    if last > 10:
        lines.append((15.3, last, "Come hungry and stay late."))
    vo = make_clip("A01", kind="audio", seconds=last + 1, sentences=lines, role="voiceover")
    a = Analysis(clips=[vo, make_clip("I01", kind="image"), make_clip("V01", seconds=seconds)])
    raw = {"sections": [{"name": "x", "items": [s.id for s in vo.segments], "broll": [
        {"clip": "I01", "over": ["A01.S002"], "words": "", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["tight"], name="t")
    assert _gaps_without_picture(tl, a) == [], [(c.clip_id, c.start, c.end) for c in sorted(tl.connected, key=lambda c: c.start)]
    first = min(tl.connected, key=lambda c: c.start)
    assert first.clip_id == "V01" and first.start == 0
    still = next(c for c in tl.connected if c.clip_id == "I01")
    assert still.start == first.end  # moved up to meet the short clip, not a flash between them


def _replays_within(tl, a, seconds=10):
    """Pairs of shots of one video, at most `seconds` apart, that show some of the same footage."""
    fd = tl.frame_duration
    gap = round(Fraction(seconds) / fd)
    shots = sorted((c for c in tl.connected if a.clip(c.clip_id).media.kind == "video"), key=lambda c: c.start)
    return [
        (p.clip_id, p.start, q.start)
        for i, p in enumerate(shots)
        for q in shots[i + 1 :]
        if p.clip_id == q.clip_id and q.start - p.end <= gap and q.src_in < p.src_in + p.frames * fd and p.src_in < q.src_in + q.frames * fd
    ]


def _shown_twice(tl, clip_id) -> float:
    """Seconds of a clip's footage that more than one of its shots shows."""
    fd = tl.frame_duration
    shots = [c for c in tl.connected if c.clip_id == clip_id]
    return sum(
        max(0.0, float(min(p.src_in + p.frames * fd, q.src_in + q.frames * fd) - max(p.src_in, q.src_in)))
        for i, p in enumerate(shots)
        for q in shots[i + 1 :]
    )


HARBOR = [(0.5, 4.0, "The harbor wakes up before dawn."), (4.3, 8.3, "Boats head out past the jetty."), (8.6, 12.0, "By noon the fish market is full.")]


def _harbor(*clips):
    vo = make_clip("A01", kind="audio", seconds=13, sentences=HARBOR, role="voiceover")
    return Analysis(clips=[vo, *clips])


def _over_lines(*shots):
    return {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002", "A01.S003"], "broll": [
        {"clip": clip, "over": [line], "words": "", "source_in": source_in, "reason": ""} for clip, line, source_in in shots
    ]}]}


def test_fill_shows_the_part_of_a_short_clip_it_has_not_shown_yet():
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import _gaps_without_picture

    lines = [(0.5, 7.0, "The harbor wakes up before dawn."), (7.5, 14.0, "Boats head out past the jetty."), (14.5, 21.0, "By noon the fish market is full.")]
    vo = make_clip("A01", kind="audio", seconds=22, sentences=lines, role="voiceover")
    a = Analysis(clips=[vo] + [make_clip(f"V0{i}", seconds=8) for i in (1, 2, 3)])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert _gaps_without_picture(tl, a) == []
    reused = [cid for cid in ("V01", "V02", "V03") if sum(c.clip_id == cid for c in tl.connected) == 2]
    assert reused, [(c.clip_id, c.start) for c in tl.connected]
    for cid in reused:  # a 4 s shot again from an 8 s clip: the 4 s it hasn't shown, less the 0.8 s it skipped at the head
        assert _shown_twice(tl, cid) <= 0.8 + float(tl.frame_duration), (cid, [(c.start, float(c.src_in)) for c in tl.connected if c.clip_id == cid])


def test_a_clip_planned_twice_never_replays_what_it_just_showed():
    from roughcut.timeline import _gaps_without_picture

    # The software chooses both in-points: the second picks up where the first got to.
    a = _harbor(make_clip("V01", seconds=30), make_clip("V03", seconds=8))
    tl = build_timeline(normalize(_over_lines(("V03", "A01.S001", -1), ("V03", "A01.S003", -1)), a), a, style=STYLES["medium"], name="t")
    first, second = sorted((c for c in tl.connected if c.clip_id == "V03"), key=lambda c: c.start)
    assert second.src_in == first.src_in + first.frames * tl.frame_duration
    assert not _adjacent_replays(tl) and _replays_within(tl, a) == [] and _gaps_without_picture(tl, a) == []

    # The planner's in-points, 4 s and then 6 s into an 8 s clip on the next line: 6 s backs off into the first shot.
    a = _harbor(make_clip("V01", seconds=30), make_clip("V04", seconds=8))
    tl = build_timeline(normalize(_over_lines(("V04", "A01.S001", 4), ("V04", "A01.S002", 6)), a), a, style=STYLES["medium"], name="t")
    assert not _adjacent_replays(tl) and _replays_within(tl, a) == [] and _gaps_without_picture(tl, a) == []
    assert any(w.startswith("B-roll V04 from 6s would replay") for w in tl.warnings), tl.warnings

    # With room left in the clip, the second carries on from where the first got to, and the few frames
    # between the lines are closed without showing any of it twice.
    a = _harbor(make_clip("V01", seconds=30), make_clip("V04", seconds=12))
    tl = build_timeline(normalize(_over_lines(("V04", "A01.S001", 2), ("V04", "A01.S002", 4)), a), a, style=STYLES["medium"], name="t")
    first, second = sorted((c for c in tl.connected if c.clip_id == "V04"), key=lambda c: c.start)
    assert first.src_in == pytest.approx(2, abs=0.05)  # the planner's first in-point stays
    assert first.end == second.start and second.src_in == first.src_in + first.frames * tl.frame_duration
    assert any("carries on from" in w for w in tl.warnings), tl.warnings
    assert _replays_within(tl, a) == [] and _gaps_without_picture(tl, a) == []


def test_fill_never_replays_the_planned_shot_it_leads_into():
    a = _harbor(make_clip("V02", seconds=10))  # the only clip there is
    raw = {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002"], "broll": [
        {"clip": "V02", "over": ["A01.S001"], "words": "wakes up before dawn", "source_in": 1.5, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    shots = sorted(tl.connected, key=lambda c: c.start)
    assert shots[0].note == "auto-fill" and shots[1].note != "auto-fill"  # fill covers the line until its words
    assert _replays_within(tl, a) == [], [(c.start, float(c.src_in), c.frames) for c in shots]


def test_render_media_needs_the_run_style():
    from roughcut.prompts import render_media

    with pytest.raises(TypeError):
        render_media(narration_project())  # a silent medium default would misstate tight/loose lengths


def _linear_clamp_end(t, word_start, silences, eps=0.05):
    return next((s for s, e in silences if word_start < s < t and e >= t - eps), t)


def _linear_clamp_start(t, word_end, silences, eps=0.05):
    return next((e for s, e in silences if s <= t + eps and t < e < word_end), t)


def _linear_pause(prev_start, next_end, silences, eps=0.05):
    return max((e - s for s, e in silences if s >= prev_start + eps and e <= next_end - eps), default=0.0)


def test_pause_lookups_by_bisection_match_a_full_scan():
    import random

    from roughcut.timeline import _Spans

    rng = random.Random(7)
    for _ in range(200):
        t, silences = 0.0, []
        for _ in range(rng.randint(0, 30)):
            t += rng.uniform(0.0, 2.0)
            length = rng.uniform(0.01, 1.5)
            silences.append((round(t, 3), round(t + length, 3)))
            t += length
        spans = _Spans(silences)
        for _ in range(30):
            a = rng.uniform(-0.5, t + 0.5)
            b = a + rng.uniform(0.0, 3.0)
            assert spans.clamp_end(b, a) == _linear_clamp_end(b, a, silences)
            assert spans.clamp_start(a, b) == _linear_clamp_start(a, b, silences)
            assert spans.pause_between(a, b) == _linear_pause(a, b, silences)
            assert spans.holds(a, b) == any(a <= s and e <= b for s, e in silences)


def test_a_long_recording_renders_and_cuts_in_seconds():
    import random
    import time

    from roughcut.pauses import breath_candidates
    from roughcut.plan import heuristic_plan
    from roughcut.prompts import render_media

    rng = random.Random(1)
    vocab = "the city plans more parks near river and homes offices hotels restaurants we went down to coast".split()
    sentences, silences, t = [], [], 0.5
    for i in range(1500):
        n = rng.randint(6, 14)
        sentences.append((t, t + n * 0.3, " ".join(rng.choice(vocab) for _ in range(n)) + "."))
        silences.append((t + n * 0.3, t + n * 0.3 + 0.7))
        if i % 3 == 0:
            silences.append((t + n * 0.15, t + n * 0.15 + 0.4))
        t += n * 0.3 + 0.7
    clip = make_clip("V01", seconds=t + 2, sentences=sentences, silences=sorted(silences))
    a = Analysis(clips=[clip])
    words = sorted((w.start, w.end) for s in clip.segments for w in s.words)
    split = [half for s, e in clip.silences for half in ((s, (s + e) / 2 - 0.1), ((s + e) / 2 + 0.1, e))]
    began = time.monotonic()
    render_media(a, STYLES["medium"])
    build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert breath_candidates(split, words)
    assert time.monotonic() - began < 10  # was minutes: every segment and piece scanned the whole clip

"""Editorial: B-roll on its words, one use per line, filler and pause rules, length checks."""

from __future__ import annotations

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
    assert shots["I01"].start == line_start  # the first shot starts with the line
    assert abs(shots["I02"].start - _word_frame(tl, a, "A01.S003", "Honda")) <= 2
    assert abs(shots["V01"].start - _word_frame(tl, a, "A01.S003", "two")) <= 2
    assert abs(shots["I04"].start - _word_frame(tl, a, "A01.S003", "parks")) <= 2
    assert shots["I01"].end == shots["I02"].start  # an anchored shot ends the one before it
    assert shots["V01"].end == shots["I04"].start
    assert max(c.end for c in tl.connected) <= line_end
    assert not [w for w in tl.warnings if "B-roll" in w]


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
    assert shots["I01"].start == tl.seg_ranges["A01.S003"][0]
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

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

"""Narration-led pieces, modeled on the first real project (OCVIBE).

That run had three reads of one voiceover script, a promo exported in three
aspect ratios, and 276 stills with descriptive names. Narration must never
play over black, the same footage must not repeat, and long narrated lines
need several shots.
"""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import NTSC, make_clip

from roughcut.analyze import Analysis, Cache
from roughcut.duplicates import mark_duplicates
from roughcut.plan import heuristic_plan, normalize
from roughcut.prompts import render_media
from roughcut.timeline import STYLES, _gaps_without_picture, build_timeline

VO = [
    (0.5, 6.0, "Orange County has no single downtown."),
    (6.5, 12.0, "In Anaheim, Katella Commons is trying to build one."),
    (12.5, 36.0, "Around Honda Center the plan adds homes, offices, two hotels, restaurants and twenty acres of parks."),
    (36.5, 42.0, "Most of it isn't built yet."),
]


def still(clip_id: str, name: str):
    c = make_clip(clip_id, kind="image")
    c.media.name = name
    c.media.path = f"/footage/{name}"
    return c


def narration_project():
    vo = make_clip("A01", kind="audio", seconds=44, sentences=VO, role="voiceover")
    return Analysis(
        clips=[
            vo,
            still("I01", "Katella_Commons_Entrance.jpg"),
            still("I02", "Honda_Center_Aerial.jpg"),
            still("I03", "Barrel_Bar_Interior.jpg"),
            still("I04", "Parks_Plaza_Rendering.jpg"),
            make_clip("V01", seconds=120),
        ]
    )


def covered_frames(tl, analysis) -> set[int]:
    frames = set()
    for c in tl.connected:
        if analysis.clip(c.clip_id).media.has_video:
            frames.update(range(c.start, c.end))
    return frames


def test_stringout_of_narration_has_no_black_frames():
    a = narration_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert tl.spine and all(a.clip(sc.clip_id).media.kind == "audio" for sc in tl.spine)
    assert _gaps_without_picture(tl, a) == []
    assert covered_frames(tl, a) >= set(range(tl.total_frames))
    assert any("Filled" in n for n in tl.notes)


def test_fill_matches_what_is_being_said():
    a = narration_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")

    def shots_during(seg_id):
        f0, f1 = tl.seg_ranges[seg_id]
        return [c.clip_id for c in tl.connected if c.start < f1 and c.end > f0]

    assert "I01" in shots_during("A01.S002")  # "Katella Commons" -> Katella_Commons_Entrance.jpg
    assert "I02" in shots_during("A01.S003")  # "Honda Center" -> Honda_Center_Aerial.jpg
    assert "I04" in shots_during("A01.S003")  # "parks" -> Parks_Plaza_Rendering.jpg
    # Long lines get several shots, none longer than the fill length (+ tail).
    fill = tc_frames(STYLES["medium"].fill_shot, tl) + tc_frames(0.5, tl)
    assert all(c.frames <= fill for c in tl.connected)


def tc_frames(seconds, tl):
    return round(Fraction(seconds).limit_denominator(1000) / tl.frame_duration)


def test_no_fill_leaves_gaps():
    a = narration_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t", fill=False)
    assert _gaps_without_picture(tl, a) == [(0, tl.total_frames)]


def test_several_shots_share_a_long_line():
    a = narration_project()
    raw = {
        "sections": [
            {
                "name": "Plan",
                "items": ["A01.S003"],
                "broll": [
                    {"clip": "I02", "over": ["A01.S003"], "source_in": -1, "reason": ""},
                    {"clip": "V01", "over": ["A01.S003"], "source_in": 40, "reason": ""},
                    {"clip": "I04", "over": ["A01.S003"], "source_in": -1, "reason": ""},
                ],
            }
        ]
    }
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    planned = [c for c in tl.connected if c.note != "auto-fill"]
    assert [c.clip_id for c in planned] == ["I02", "V01", "I04"]
    # Back to back on one lane, splitting the line.
    assert planned[0].end == planned[1].start and planned[1].end == planned[2].start
    assert len({c.lane for c in planned}) == 1
    assert planned[1].src_in == pytest.approx(40, abs=0.05)


def test_a_single_still_is_capped_and_the_rest_filled():
    a = narration_project()
    raw = {"sections": [{"name": "Plan", "items": ["A01.S003"], "broll": [{"clip": "I02", "over": ["A01.S003"], "source_in": -1, "reason": ""}]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    first = tl.connected[0]
    assert first.clip_id == "I02" and first.frames <= tc_frames(STYLES["medium"].max_still, tl)
    assert _gaps_without_picture(tl, a) == []


def test_talking_head_is_not_filled():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=VO[:2]), still("I01", "Katella_Commons_Entrance.jpg")])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert tl.connected == []  # the speaker is the picture


def test_fill_writes_valid_fcpxml(tmp_path):
    lxml = pytest.importorskip("lxml.etree")
    from test_end_to_end import DTD

    from roughcut.fcpxml import build_fcpxml

    a = narration_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    path = tmp_path / "x.fcpxml"
    path.write_text(build_fcpxml([tl], a, event_name="e"))
    dtd = lxml.DTD(open(DTD, "rb"))
    assert dtd.validate(lxml.parse(str(path))), dtd.error_log


# ------------------------------------------------------------ duplicates


def promo(clip_id, w, h):
    c = make_clip(clip_id, seconds=64.5, sentences=[(1, 5, "Roy Choi is opening a restaurant."), (6, 10, "It opens this summer in Anaheim.")])
    c.media.width, c.media.height = w, h
    return c


def test_alternate_exports_collapse_to_the_landscape_one(tmp_path):
    clips = [promo("V18", 2160, 3840), promo("V19", 3840, 2160), promo("V20", 1440, 1080)]
    notes = mark_duplicates(clips, Cache(tmp_path))
    assert [c.duplicate_of for c in clips] == ["V19", None, "V19"]
    assert notes and "V19" in notes[0]
    vertical = [promo("V18", 2160, 3840), promo("V19", 3840, 2160)]
    mark_duplicates(vertical, Cache(tmp_path), prefer_vertical=True)
    assert [c.duplicate_of for c in vertical] == [None, "V18"]


def test_narration_takes_are_linked_not_hidden(tmp_path):
    a01 = make_clip("A01", kind="audio", seconds=44, sentences=VO, role="voiceover")
    a03 = make_clip("A03", kind="audio", seconds=44, sentences=VO, role="voiceover")
    # An earlier draft: same script with the long sentence reworded.
    reworded = (12.5, 36.0, "The Honda Center plan adds housing, offices, hotels, and lots of restaurants plus some parks.")
    draft = make_clip("A02", kind="audio", seconds=44, sentences=VO[:2] + [reworded] + VO[3:], role="voiceover")
    other = make_clip("A04", kind="audio", seconds=30, sentences=[(1, 9, "A completely different topic about kayaking and weather.")], role="voiceover")
    a = Analysis(clips=[a01, draft, a03, other])
    mark_duplicates(a.clips, Cache(tmp_path))
    takes = {c.id: c.take_of for c in a.clips}
    keep = next(k for k, v in takes.items() if v is None and k != "A04")
    assert takes["A04"] is None
    assert sorted(k for k, v in takes.items() if v == keep) == sorted({"A01", "A02", "A03"} - {keep})
    assert not any(c.hidden for c in a.clips)
    # The stringout reads the narration once.
    sections = [s.name for s in heuristic_plan(a).sections]
    assert sections.count("A01") + sections.count("A02") + sections.count("A03") == 1
    # The planner sees them as alternatives.
    media = render_media(a)
    assert "of the narration script in" in media and "never use the same line twice" in media


def test_hidden_versions_are_left_out_of_the_prompt(tmp_path):
    clips = [promo("V18", 2160, 3840), promo("V19", 3840, 2160)]
    mark_duplicates(clips, Cache(tmp_path))
    media = render_media(Analysis(clips=clips))
    assert "## V19" in media and "## V18" not in media
    assert "also exported as V18" in media


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_silent_footage_saved_twice_is_detected(tmp_path):
    from roughcut.media import probe

    a = tmp_path / "Concert_Hall_July.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(a)], check=True)
    b = tmp_path / "Concert_Hall_Construction_July.mp4"
    shutil.copy(a, b)
    c = tmp_path / "Different.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "smptebars=size=640x360:rate=24:duration=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(c)], check=True)
    from roughcut.analyze import Clip

    clips = [Clip(id=f"V0{i}", media=probe(p), role="broll") for i, p in enumerate((a, b, c), 1)]
    mark_duplicates(clips, Cache(tmp_path / "cache"))
    assert [x.duplicate_of for x in clips].count(None) == 2
    assert {clips[0].duplicate_of, clips[1].duplicate_of} == {None, clips[0].id if clips[1].duplicate_of else clips[1].id}
    assert clips[2].duplicate_of is None

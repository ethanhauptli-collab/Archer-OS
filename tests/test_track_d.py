"""Audio and Final Cut details: frame-rate warnings, roles and notes, browser
keyword ranges for alternate reads, speech level matching, de-click fades and
the music bed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest
from conftest import NTSC, FakeProvider, make_clip
from test_end_to_end import check_invariants, validate_dtd

from roughcut import pipeline
from roughcut.analyze import Analysis
from roughcut.fcpxml import build_fcpxml
from roughcut.media import MediaInfo, frame_rate_warnings
from roughcut.pipeline import Options
from roughcut.plan import normalize
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import STYLES, build_timeline


@pytest.fixture(scope="module")
def built(footage, tmp_path_factory):
    """One planned build of the shared synthetic footage (fake model)."""
    tmp = tmp_path_factory.mktemp("track_d")
    provider = FakeProvider()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "make_provider", lambda *a, **k: provider)
        result = pipeline.build(Options(inputs=[footage], out=tmp / "out", name="footage", transcriber="none", cache_dir=tmp / "cache"))
    return result


def _write(tmp_path, timelines, analysis, **kw):
    path = tmp_path / "t.fcpxml"
    path.write_text(build_fcpxml(timelines, analysis, event_name="Test", **kw))
    return path


# ------------------------------------------------------------ T16 frame rates


def _video(name: str, fd: Fraction, vfr: bool = False) -> MediaInfo:
    return MediaInfo(path=f"/x/{name}", name=name, kind="video", duration=Fraction(10), start=Fraction(0), has_video=True, has_audio=True, frame_duration=fd, vfr=vfr)


def test_frame_rate_warnings_group_rates_and_list_vfr():
    project = NTSC
    used = [
        _video("a.mov", NTSC),
        _video("gopro.mp4", Fraction(1001, 60000)),
        _video("film.mov", Fraction(1001, 24000)),
        _video("phone.mov", NTSC, vfr=True),
        MediaInfo(path="/x/s.jpg", name="s.jpg", kind="image", duration=Fraction(0), start=Fraction(0), has_video=True, has_audio=False),
    ]
    lines = frame_rate_warnings(project, used)
    assert len(lines) == 2
    assert "2 clips" in lines[0] and "29.97 fps project" in lines[0]
    assert "59.94 fps: gopro.mp4" in lines[0] and "23.98 fps: film.mov" in lines[0]
    assert "judder" in lines[0]  # 23.98 in 29.97 isn't a whole multiple
    assert "Automatic Speed" in lines[0]  # the 59.94 clip could be slow motion
    assert "phone.mov" in lines[1] and "a.mov" not in lines[1]
    # A clean project says nothing; 59.94 in 29.97 alone doesn't judder.
    assert frame_rate_warnings(project, [_video("a.mov", NTSC)]) == []
    assert "judder" not in frame_rate_warnings(project, [_video("g.mp4", Fraction(1001, 60000))])[0]
    slower = frame_rate_warnings(project, [_video("film.mov", Fraction(1001, 24000))])[0]
    assert slower.startswith("1 clip in this cut runs") and "Automatic Speed" not in slower


def test_report_warns_about_the_2398_clip_in_a_2997_cut(built):
    report = built.report.read_text()
    warnings = report.split("## Warnings", 1)[1]
    assert "23.98 fps: broll_2398.mov" in warnings and "29.97 fps project" in warnings


# ------------------------------------------------------------- T7 roles/notes


def _rough(root):
    return next(p for p in root.iter("project") if p.get("name").endswith("Rough Cut"))


def test_roles_and_planner_notes_in_the_planned_cut(built):
    validate_dtd(built.fcpxml)
    check_invariants(built.fcpxml)
    root = ET.parse(built.fcpxml).getroot()
    rough = _rough(root)
    spine = list(rough.find("sequence/spine"))
    talk = [el for el in spine if el.get("name") == "talk"]
    assert talk and all(el.get("audioRole") == "dialogue" and el.get("videoRole") is None for el in talk)
    # The 23.98 B-roll range on the spine is B-roll picture with no audio.
    film = next(el for el in spine if el.get("name") == "broll_2398")
    assert film.get("videoRole") == "B-Roll" and film.get("audioRole") is None

    music = next(el for el in rough.iter("asset-clip") if el.get("lane") == "-1")
    assert music.get("audioRole") == "music.Music Bed"

    cutaway = next(el for el in rough.iter("asset-clip") if el.get("name") == "broll" and el.get("lane"))
    assert cutaway.get("videoRole") == "B-Roll.Planned" and cutaway.get("audioRole") == "effects.Nat Sound"
    assert cutaway[0].tag == "note" and cutaway[0].text == "cover the jump cut"
    photo = next(el for el in rough.iter("video") if el.get("lane"))
    assert photo.get("role") == "B-Roll.Planned" and photo.find("note").text == "photo"

    # Browser clips carry roles too, so clips dragged in by hand land in the same lanes.
    event = root.find("library/event")
    browser = {el.get("name"): el for el in event if el.tag in ("asset-clip", "clip")}
    assert browser["talk"].get("audioRole") == "dialogue"
    assert browser["music"].get("audioRole") == "music"
    assert browser["broll"].get("videoRole") == "B-Roll"
    assert browser["photo"].find("video").get("role") == "B-Roll"


def test_auto_fill_shots_are_their_own_subrole_without_a_note(tmp_path):
    from test_narration import narration_project

    from roughcut.plan import heuristic_plan

    a = narration_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    root = ET.parse(path).getroot()
    rough = root.find(".//project")
    narration = list(rough.find("sequence/spine"))
    assert narration and all(el.get("audioRole") == "dialogue.Narration" for el in narration)
    fills = [el for el in rough.iter() if el.tag in ("asset-clip", "video") and el.get("lane")]
    assert fills
    for el in fills:
        assert (el.get("role") or el.get("videoRole")) == "B-Roll.Auto-fill"
        assert el.find("note") is None


def test_graphics_are_titles_and_their_text_is_the_note(tmp_path):
    from roughcut.timeline import ConnectedClip

    talk = make_clip("V01", seconds=20, sentences=[(1.0, 4.0, "Welcome to the show everyone.")])
    gfx = make_clip("G01", seconds=4, role="graphic")
    gfx.media.has_audio = False
    a = Analysis(clips=[talk, gfx])
    tl = build_timeline(normalize({"title": "t", "sections": [{"name": "A", "items": ["V01.S001"]}]}, a), a, style=STYLES["medium"], name="t")
    tl.connected.append(ConnectedClip("G01", Fraction(0), 30, 10, lane=1, kind="graphic", note="Jane Doe\x01, host"))
    path = _write(tmp_path, [tl], a, browser_clips=False)
    validate_dtd(path)
    el = next(e for e in ET.parse(path).getroot().iter("asset-clip") if e.get("name") == "G01")
    assert el.get("videoRole") == "titles.Graphics" and el.get("audioRole") is None
    assert el.find("note").text == "Jane Doe , host"  # illegal XML characters scrubbed

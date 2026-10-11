"""roughcut's FCPXML conventions, checked against files Final Cut Pro wrote.

tests/fixtures/fcp_exports holds genuine Final Cut Pro exports (10.7 and 11,
FCPXML 1.11 and 1.13; provenance and MIT licenses in LICENSES.txt there).
Each test states a convention roughcut relies on and checks Final Cut's own
output follows it, and where it can, that roughcut's output does too. A change
to fcpxml.py that drifts from how Final Cut writes FCPXML should fail here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import make_clip
from test_end_to_end import DTD, RATE_UNDEFINED, STORY, check_invariants, effective_formats, format_problems, validate_dtd

from roughcut import timecode as tc
from roughcut.analyze import Analysis
from roughcut.fcpxml import FADE_SHAPE, STILL_ORIGIN, _roles, build_fcpxml
from roughcut.plan import heuristic_plan, normalize
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import STYLES, build_timeline

EXPORTS = Path(__file__).parent / "fixtures" / "fcp_exports"
FILES = sorted(EXPORTS.glob("*.fcpxml"))
MARKS = {"marker", "chapter-marker", "keyword", "rating", "analysis-marker"}


def load(name: str) -> ET.Element:
    return ET.parse(EXPORTS / name).getroot()


def test_the_fixtures_are_final_cut_exports():
    assert len(FILES) == 8
    for f in FILES:
        root = ET.parse(f).getroot()
        assert root.get("version") in ("1.11", "1.13")
        assert root.find("library").get("location", "").endswith(".fcpbundle/")  # written from a library
        assert all(a.get("uid") for a in root.iter("asset"))  # Final Cut's own media ids
        assert all(p.get("modDate") for p in root.iter("project"))


def test_apples_dtd_accepts_what_final_cut_wrote():
    """The vendored 1.10 DTD agrees with real exports once the one newer element is set aside.

    adjust-colorConform arrived in FCPXML 1.11 and Final Cut 10.7+ writes it on
    every clip; nothing else in these files is outside the 1.10 DTD.
    """
    lxml = pytest.importorskip("lxml.etree")
    dtd = lxml.DTD(open(DTD, "rb"))
    for f in FILES:
        doc = lxml.parse(str(f))
        doc.getroot().set("version", "1.10")
        for el in doc.getroot().xpath("//adjust-colorConform"):
            el.getparent().remove(el)
        assert dtd.validate(doc), f"{f.name}: " + "; ".join(str(e) for e in dtd.error_log.filter_from_errors())


def test_every_time_value_reads_and_writes_back_exactly():
    names = ("offset", "start", "duration", "tcStart", "frameDuration", "time", "posterOffset", "audioStart", "audioDuration")
    seen = 0
    for f in FILES:
        for el in ET.parse(f).getroot().iter():
            for key in names:
                if el.get(key) is not None:
                    value = T(el.get(key))
                    assert isinstance(value, Fraction) and T(tc.fcpx_time(value)) == value
                    seen += 1
    assert seen > 500


def test_format_names_agree_with_final_cut():
    """Where roughcut names a format it uses Final Cut's name; sizes Final Cut leaves unnamed
    (vertical 1080x1920, 1920x960) roughcut leaves unnamed too."""
    checked = unnamed = 0
    for f in FILES:
        for fmt in ET.parse(f).getroot().iter("format"):
            if fmt.get("name") == RATE_UNDEFINED or not fmt.get("frameDuration"):
                continue
            ours = tc.fcp_format_name(int(fmt.get("width")), int(fmt.get("height")), T(fmt.get("frameDuration")))
            if fmt.get("name") is None:
                assert ours is None, f"{f.name}: Final Cut leaves {fmt.get('width')}x{fmt.get('height')} unnamed"
                unnamed += 1
            elif ours is not None:
                assert ours == fmt.get("name")
                checked += 1
    assert checked >= 5 and unnamed >= 2


def test_final_cut_names_a_clips_format_when_it_differs_from_what_it_inherits():
    counted = 0
    for f in FILES:
        root = ET.parse(f).getroot()
        assert format_problems(root, primary_audio_may_name_format=False) == [], f.name
        counted += sum(1 for _ in effective_formats(root))
    assert counted >= 40
    # The cases that matter most for roughcut, each seen in a real export:
    vertical = load("VerticalShort.fcpxml")  # a 16:9 clip anchored in a 9:16 project
    clip = next(vertical.iter("asset-clip"))
    assert clip.get("format") == "r5" and clip.find("conform-rate").get("scaleEnabled") == "0"
    audio = load("AudioOnly.fcpxml")  # audio-only in the primary storyline, and connected to it
    spine = audio.find(".//spine")
    assert all(c.get("format") is None for c in spine.findall("asset-clip"))
    assert all(c.get("format") == "r4" for c in spine.findall("asset-clip/asset-clip"))


def test_fades_and_levels_are_written_like_final_cut():
    shapes, with_amount = set(), 0
    for f in FILES:
        for adjust in ET.parse(f).getroot().iter("adjust-volume"):
            for fade in adjust.iter():
                if fade.tag in ("fadeIn", "fadeOut"):
                    shapes.add(fade.get("type"))
            params = adjust.findall("param")
            assert all(p.get("name") == "amount" for p in params)
            for p in params:  # fades and keyframes live inside <param name="amount">, in that order
                tags = [c.tag for c in p]
                assert tags == sorted(tags, key=["fadeIn", "fadeOut", "keyframeAnimation"].index)
            if params and adjust.get("amount"):
                with_amount += 1  # a level and fade handles together
            if adjust.find("param/keyframeAnimation") is not None:
                assert adjust.get("amount") is None  # the keyframes carry the level
    assert shapes == {FADE_SHAPE}  # Final Cut's default fade handles, in and out
    assert with_amount >= 1


def test_volume_keyframes_are_in_the_clips_own_source_time():
    """Keyframe times count from the clip's source origin (like its `start`), not from the edit."""
    root = load("TimelineWithSecondaryStorylineWithAudioKeyframes.fcpxml")
    clips = [c for c in root.iter("asset-clip") if c.find("adjust-volume/param/keyframeAnimation") is not None]
    assert len(clips) >= 2
    offset_from_zero = False
    for clip in clips:
        start = T(clip.get("audioStart") or clip.get("start", "0s"))
        end = start + T(clip.get("audioDuration") or clip.get("duration"))
        times = [T(k.get("time")) for k in clip.iter("keyframe")]
        assert times == sorted(times) and all(start <= t <= end for t in times)
        assert all(k.get("value").endswith("dB") for k in clip.iter("keyframe"))
        offset_from_zero |= start > 0 and min(times) > T(clip.get("duration"))
    assert offset_from_zero  # a clip trimmed in: its keyframes sit past its own length, so they're source time


def test_anchored_clips_and_markers_live_in_the_parents_source_time():
    seen = 0
    for f in FILES:
        for spine in ET.parse(f).getroot().iter("spine"):
            for parent in spine:
                if parent.tag != "asset-clip":
                    continue
                start = T(parent.get("start", "0s"))
                end = start + T(parent.get("duration"))
                for child in parent:
                    if child.get("lane") is not None:
                        assert start <= T(child.get("offset")) <= end, f"{f.name}: {child.get('name')}"
                        seen += 1
    assert seen >= 5


def test_markers_and_notes_are_written_like_final_cut():
    todo = standard = chapters = notes = 0
    for f in FILES:
        root = ET.parse(f).getroot()
        for m in root.iter("marker"):
            assert m.get("completed") in (None, "0", "1")
            todo += m.get("completed") == "0"
            standard += m.get("completed") is None
            notes += m.get("note") is not None
        for c in root.iter("chapter-marker"):
            assert c.get("posterOffset") is not None
            chapters += 1
        for parent in root.iter():
            kids = [k.tag for k in parent]
            if "note" in kids:  # <note> comes first in a clip (after a <video>'s params)
                assert kids.index("note") == len([k for k in kids[: kids.index("note")] if k == "param"])
    assert todo and standard and chapters and notes


def test_child_order_in_a_clip_matches_final_cut():
    """Inside a clip: note, conform-rate, adjustments (volume last), anchored items, then markers and keywords."""

    def rank(tag: str) -> int:
        if tag == "note":
            return 0
        if tag in ("conform-rate", "timeMap"):
            return 1
        if tag.startswith("adjust-"):
            return 3 if tag in ("adjust-volume", "adjust-panner") else 2
        if tag in STORY:
            return 4
        if tag in MARKS:
            return 5
        return 6

    checked = 0
    for f in FILES:
        for clip in ET.parse(f).getroot().iter("asset-clip"):
            ranks = [rank(k.tag) for k in clip]
            assert ranks == sorted(ranks), f"{f.name}: {[k.tag for k in clip]}"
            checked += 1
    assert checked >= 40


def test_stills_and_audio_only_media_look_like_final_cuts():
    formats_seen = set()
    for f in FILES:
        root = ET.parse(f).getroot()
        formats = {x.get("id"): x for x in root.iter("format")}
        assets = {a.get("id"): a for a in root.iter("asset")}
        for a in assets.values():
            if a.get("hasVideo") != "1":
                assert a.get("format") is None and a.get("hasAudio") == "1"  # audio-only assets name no format
                formats_seen.add("audio")
            elif T(a.get("duration")) == 0:  # a still: zero length, rate-undefined format with a size
                fmt = formats[a.get("format")]
                assert fmt.get("name") == RATE_UNDEFINED and fmt.get("width") and fmt.get("frameDuration") is None
                formats_seen.add("still")
        for v in root.iter("video"):
            asset = assets.get(v.get("ref"))
            if asset is not None and T(asset.get("duration")) == 0:
                assert not any(c.get("ref") == v.get("ref") for c in root.iter("asset-clip"))  # stills are <video>
    assert formats_seen == {"audio", "still"}

    # In a 23.98 project Final Cut puts a still's origin on the frame at or just before 3600s;
    # roughcut writes 3600s exactly, inside that same frame.
    root = load("ImageSample.fcpxml")
    seq_fd = T(root.find(".//format[@id='r1']").get("frameDuration"))
    start = T(root.find(".//spine/video").get("start"))
    assert STILL_ORIGIN - seq_fd < start <= STILL_ORIGIN and (start / seq_fd).denominator == 1


ROLE = re.compile(r"^[^.?]+(\.[^.?]+)?$")  # main role, optionally .subrole (captions add ?captionFormat=)


def test_role_strings_have_final_cuts_shape():
    builtin = set()
    for f in FILES:
        root = ET.parse(f).getroot()
        for el in root.iter():
            for key in ("role", "audioRole", "videoRole"):
                value = el.get(key)
                if value and "?captionFormat=" not in value:
                    assert ROLE.match(value), value
                    main = value.split(".")[0]
                    if main.lower() in ("dialogue", "music", "effects", "video", "titles"):
                        builtin.add(main)
    assert builtin <= {"dialogue", "music", "effects", "video", "titles"}  # built-in roles are lowercase
    assert {"dialogue", "effects", "music"} <= builtin

    clip = make_clip("V01", seconds=10)
    still = make_clip("I01", kind="image")
    for c in (clip, still):
        for role in ("aroll", "broll", "voiceover", "music", "still", "graphic"):
            c.role = role
            for use in ("speech", "spine", "broll", "music", "graphic", "browser"):
                for value in _roles(c, use, "auto-fill") + _roles(c, use):
                    assert value is None or ROLE.match(value), value


# ------------------------------------------------------------ roughcut follows the same rules


def _narrated(tmp_path) -> Path:
    from test_narration import narration_project

    a = narration_project()  # narration (audio only) in the primary storyline, video and stills over it
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="narrated")
    assert any(a.clip(c.clip_id).media.kind == "video" for c in tl.connected)
    path = tmp_path / "narrated.fcpxml"
    path.write_text(build_fcpxml([tl], a, event_name="e"))
    return path


def _mixed(tmp_path, fmt: str | None) -> Path:
    talk = make_clip("V01", seconds=30, sentences=[(1.0, 5.0, "We built the first boat in this garage."), (9.0, 13.0, "It took us three whole winters to finish.")])
    pal = make_clip("V02", seconds=20)  # 25p B-roll in a 29.97 cut
    pal.media.frame_duration, pal.media.rate, pal.media.duration = Fraction(1, 25), "25", Fraction(20)
    a = Analysis(clips=[talk, pal])
    raw = {"title": "t", "sections": [{"name": "A", "items": ["V01.S001", "V02@2-6", "V01.S002"]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", format_override=fmt)
    path = tmp_path / f"mixed-{fmt}.fcpxml"
    path.write_text(build_fcpxml([tl], a, event_name="e"))
    return path


def test_roughcut_names_clip_formats_the_way_final_cut_does(tmp_path):
    """A clip without `format` takes its parent's: B-roll over narration read as rate-undefined,
    a 16:9 clip in a vertical cut as 9:16, a 25p clip in a 29.97 cut as 29.97."""
    for path in (_narrated(tmp_path), _mixed(tmp_path, None), _mixed(tmp_path, "vertical")):
        validate_dtd(path)
        check_invariants(path)
        root = ET.parse(path).getroot()
        assert format_problems(root, primary_audio_may_name_format=True) == [], path.name

    root = ET.parse(tmp_path / "narrated.fcpxml").getroot()
    formats = {f.get("id"): f for f in root.iter("format")}
    narration = root.find(".//spine/asset-clip")
    assert formats[narration.get("format")].get("name") == RATE_UNDEFINED
    broll = [c for c in narration.findall("asset-clip") if c.get("videoRole")]
    assert broll and all(formats[c.get("format")].get("name") == "FFVideoFormat1080p2997" for c in broll)

    root = ET.parse(tmp_path / "mixed-None.fcpxml").getroot()
    pal = next(c for c in root.iter("asset-clip") if c.get("name") == "V02" and c.get("offset"))
    assert root.find(f".//format[@id='{pal.get('format')}']").get("name") == "FFVideoFormat1080p25"
    conform = pal.find("conform-rate")  # real time: the cut's timing assumes no speed change
    assert conform.attrib == {"scaleEnabled": "0", "srcFrameRate": "25"} and list(pal)[0] is conform
    assert all(c.find("conform-rate") is None for c in root.iter("asset-clip") if c.get("name") == "V01")

    root = ET.parse(tmp_path / "mixed-vertical.fcpxml").getroot()
    seq_fmt = root.find(".//sequence").get("format")
    assert all(c.get("format") not in (None, seq_fmt) for c in root.find(".//spine"))
    browser = root.find(".//event").findall("asset-clip")
    assert browser and all(c.get("format") for c in browser)  # browser clips always name theirs

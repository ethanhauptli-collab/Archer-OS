"""Audio and Final Cut details: frame-rate warnings, roles and notes, browser
keyword ranges for alternate reads, speech level matching, de-click fades and
the music bed."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest
from conftest import NTSC, SPEECH, TALK_SECONDS, FakeProvider, ffmpeg, make_clip, spread_words
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


# ------------------------------------------------- T8 alternate-read keywords

SCRIPT = [
    (0.5, 4.0, "Orange County has no single downtown."),
    (4.5, 9.0, "In Anaheim, Katella Commons is trying to build one."),
    (9.5, 13.0, "Most of it isn't built yet."),
]
SCRIPT_B = [  # the second read: same lines, slightly different words and timing
    (1.2, 4.9, "Orange County has no single downtown, really."),
    (5.6, 10.4, "In Anaheim, Katella Commons is trying to build one."),
    (11.0, 14.6, "Most of it is not built yet."),
]


def _keywords_of(root, name):
    clip = next(e for e in root.find("library/event") if e.get("name") == name)
    return clip, clip.findall("keyword")


def test_other_reads_of_used_lines_get_alt_take_ranges(tmp_path):
    a01 = make_clip("A01", kind="audio", seconds=15, sentences=SCRIPT, role="voiceover")
    a02 = make_clip("A02", kind="audio", seconds=16, sentences=SCRIPT_B, role="voiceover")
    a02.take_of, a02.same_words = "A01", 0.85
    v01 = make_clip("V01", seconds=10)
    v02 = make_clip("V02", seconds=10)
    v02.duplicate_of = "V01"
    a = Analysis(clips=[a01, a02, v01, v02])
    # The planner took line 1 from the first read and line 2 from the second.
    plan = normalize({"title": "t", "sections": [{"name": "A", "items": ["A01.S001", "A02.S002"]}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t - Rough Cut", fill=False)
    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    root = ET.parse(path).getroot()

    clip, kws = _keywords_of(root, "A02")
    c0, cd = T(clip.get("start")), T(clip.get("duration"))
    whole, ranges = kws[0], kws[1:]
    assert "Alt narration read" in whole.get("value") and "A01.wav" in whole.get("note")
    assert [k.get("value") for k in ranges] == ["Alt take"]
    k = ranges[0]
    s, d = T(k.get("start")), T(k.get("duration"))
    assert c0 <= s and s + d <= c0 + cd
    assert abs(float(s - c0) - 1.2) < 0.01 and abs(float(s + d - c0) - SCRIPT_B[0][1]) < 0.05
    assert "A02.S001: another read of A01.S001 at 0:00 in the cut" in k.get("note")
    assert "Orange County has no single downtown." in k.get("note")

    # The first read's version of line 2 is the alternate for the second read's line.
    _, kws1 = _keywords_of(root, "A01")
    alts = [k for k in kws1 if k.get("value") == "Alt take"]
    assert len(alts) == 1 and "another read of A02.S002" in alts[0].get("note")
    # Line 3 isn't in the cut, so neither read of it is marked.
    assert all("S003" not in k.get("note", "") for k in kws + kws1)

    _, dup = _keywords_of(root, "V02")
    assert "Duplicate export" in dup[0].get("value") and "V01.mov" in dup[0].get("note")
    _, orig = _keywords_of(root, "V01")
    assert "Duplicate export" not in orig[0].get("value")


def test_planner_alternates_mark_the_other_take_in_the_browser(built):
    validate_dtd(built.fcpxml)
    root = ET.parse(built.fcpxml).getroot()
    clip, kws = _keywords_of(root, "talk")
    alts = [k for k in kws if k.get("value") == "Alt take"]
    # CANNED_PLAN: V01.S003 chosen over V01.S001 ("So today we're heading up the mountain.").
    assert len(alts) == 1 and alts[0].get("note").startswith("V01.S001: another read of V01.S003 at ")
    s, d = T(alts[0].get("start")), T(alts[0].get("duration"))
    c0, cd = T(clip.get("start")), T(clip.get("duration"))
    assert c0 <= s and s + d <= c0 + cd
    assert ((s - c0) / NTSC).denominator == 1 and (d / NTSC).denominator == 1  # on the clip's frame grid
    assert abs(float(s - c0) - 1.0) < 0.05


# ------------------------------------------------------- T2 level matching


LINES = {
    "loud": [
        "The ferry leaves the harbor at dawn.", "Gulls follow it out past the breakwater.", "Nobody on deck says much.",
        "The captain has done this run for thirty years.", "He still checks the weather twice an hour.", "Today it looks calm.",
    ],
    "quiet": [
        "Our bakery opened its doors last spring.", "We start the ovens before four.", "Sourdough sells out first, every single day.",
        "My sister handles the coffee side of things.", "Regulars know each other by name now.", "That was the whole idea.",
    ],
    "noisy": [
        "This trail climbs nearly two thousand feet.", "Bring more water than you think you need.", "The switchbacks get steep near the top.",
        "Wildflowers cover the meadow in early June.", "Watch the sky for afternoon storms.", "The view makes it worth it.",
    ],
}


def talker(root, name: str, amp: float, noise: float = 0.0, created: str = "09:00"):
    """A 20 s talking clip: voice-like tone bursts where the sidecar's words are, over optional noise."""
    script = [(s, e, line) for (s, e, _), line in zip(SPEECH, LINES[name])]
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in script)
    voice = f"{amp}*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t))*({gates})"
    expr = voice + (f"+{noise}*(2*random(0)-1)" if noise else "")
    path = root / f"{name}.mov"
    ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30000/1001:duration={TALK_SECONDS}",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d={TALK_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-metadata", f"creation_time=2026-08-12T{created}:00Z", str(path),
    )
    words = [w for s, e, text in script for w in spread_words(s, e, text)]
    (root / f"{name}.json").write_text(json.dumps({"words": words}))
    return path


@pytest.fixture(scope="module")
def talkers(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("talkers")
    talker(root, "loud", 0.6, created="09:00")  # about -9 LUFS
    talker(root, "quiet", 0.075, created="09:01")  # 18 dB under loud
    talker(root, "noisy", 0.075, noise=0.0025, created="09:02")  # as quiet, over hiss at about -52 LUFS
    return root


def test_gated_loudness_and_speech_spans():
    from roughcut.ffmpeg_ops import gated_loudness, level_summary

    assert gated_loudness([-120.0] * 10) is None
    assert gated_loudness([-20.0] * 10) == -20.0
    # Quiet blocks (pauses) fall under the relative gate and don't drag speech down.
    assert gated_loudness([-20.0] * 10 + [-45.0] * 30) == -20.0
    # 4 s of speech at -20 between pauses of room tone at -50; peaks -6 in speech.
    m = [-50.0] * 10 + [-20.0] * 40 + [-50.0] * 50
    peak = [-40.0] * 10 + [-6.0] * 40 + [-30.0] * 50
    s = level_summary({"step": 0.1, "m": m, "peak": peak}, spans=[(1.2, 5.0)], duration=10.0)
    assert s["lufs"] == -20.0 and s["floor"] == -50.0 and s["peak"] == -6.0
    assert 5.0 <= s["end"] <= 5.4  # last block loud enough to count
    whole = level_summary({"step": 0.1, "m": m, "peak": peak}, duration=10.0)
    assert whole["lufs"] == -20.0  # gating keeps the integrated value on the speech


def test_level_match_limits():
    from roughcut.timeline import level_match

    clip = make_clip("V01", seconds=10)
    assert level_match(clip, -16.0) is None  # not measured
    clip.loudness, clip.loudness_floor, clip.peak_db = -24.0, -70.0, -20.0
    assert level_match(clip, -16.0) == (8.0, "")
    clip.loudness = -40.0
    assert level_match(clip, -16.0) == (12.0, "held to +12 dB")
    clip.loudness = 0.0
    assert level_match(clip, -16.0) == (-12.0, "held to -12 dB")
    # A boost stops where the background would get loud, and never becomes a cut.
    clip.loudness, clip.loudness_floor = -26.0, -50.0
    assert level_match(clip, -16.0) == (5.0, "boost limited by background noise")
    clip.loudness_floor = -40.0
    assert level_match(clip, -16.0) == (0.0, "boost limited by background noise")
    # ...or where peaks would clip.
    clip.loudness_floor, clip.peak_db = -80.0, -4.0
    assert level_match(clip, -16.0) == (3.0, "boost limited by peaks")
    # Nat sound sits under the dialogue: base gain plus the correction, and its quiet parts aren't noise.
    clip.loudness, clip.loudness_floor, clip.peak_db = -26.0, -30.0, -20.0
    assert level_match(clip, -16.0, -20.0, guard_noise=False) == (-10.0, "")


def _spine_volumes(path) -> dict[str, set]:
    rough = _rough(ET.parse(path).getroot())
    out: dict[str, set] = {}
    for el in rough.find("sequence/spine"):
        vol = el.find("adjust-volume")
        out.setdefault(el.get("name"), set()).add(float(vol.get("amount", "0dB").removesuffix("dB")) if vol is not None else 0.0)
    return out


def test_speech_from_different_sources_is_matched(talkers, tmp_path):
    out = tmp_path / "out"
    result = pipeline.build(Options(inputs=[talkers], out=out, name="talkers", provider="none", transcriber="none", cache_dir=tmp_path / "cache"))
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    analysis = Analysis.load(out / "analysis.json")
    loud, quiet, noisy = (next(c for c in analysis.clips if c.media.stem == n) for n in ("loud", "quiet", "noisy"))
    assert abs((loud.loudness - quiet.loudness) - 18.0) < 1.0  # measured over the words
    assert noisy.loudness_floor > quiet.loudness_floor + 20  # the background is what differs

    vols = _spine_volumes(result.fcpxml)
    assert len(vols["loud"]) == len(vols["quiet"]) == 1  # one gain per source
    (v_loud,), (v_quiet,), (v_noisy,) = vols["loud"], vols["quiet"], vols["noisy"]
    assert abs((v_quiet - v_loud) - 18.0) <= 1.0
    assert abs(loud.loudness + v_loud - (-16.0)) <= 0.5 and abs(quiet.loudness + v_quiet - (-16.0)) <= 0.5
    # The noisy clip isn't lifted into its own hiss.
    assert v_noisy < v_quiet and noisy.loudness_floor + v_noisy <= -45.0 + 0.5

    report = result.report.read_text()
    levels = report.split("## Levels", 1)[1].split("## ", 1)[0]
    assert "about -16 LUFS" in levels and "| quiet.mov | dialogue |" in levels
    assert "boost limited by background noise" in levels


def test_dialogue_level_off_and_older_folders(talkers, tmp_path):
    out = tmp_path / "out"
    opts = Options(inputs=[talkers], out=out, name="talkers", provider="none", transcriber="none", cache_dir=tmp_path / "cache", dialogue_level=None)
    result = pipeline.build(opts)
    assert all(v == {0.0} for v in _spine_volumes(result.fcpxml).values())
    assert "## Levels" not in result.report.read_text()

    # A folder from before level matching (no loudness saved) is measured at render time.
    path = out / "analysis.json"
    data = json.loads(path.read_text())
    for c in data["clips"]:
        for key in ("loudness", "loudness_floor", "peak_db", "sound_end"):
            c.pop(key, None)
    path.write_text(json.dumps(data))
    again = pipeline.rerender(out, Options(inputs=[], cache_dir=tmp_path / "cache2"))
    vols = _spine_volumes(again.fcpxml)
    assert abs((vols["quiet"].pop() - vols["loud"].pop()) - 18.0) <= 1.0


# ------------------------------------------------------------------ T3 fades


def _fades(el) -> tuple[Fraction, Fraction]:
    param = el.find("adjust-volume/param[@name='amount']")
    if param is None:
        return Fraction(0), Fraction(0)
    fi, fo = param.find("fadeIn"), param.find("fadeOut")
    return (T(fi.get("duration")) if fi is not None else Fraction(0)), (T(fo.get("duration")) if fo is not None else Fraction(0))


def test_declick_fades_stay_off_the_words(built):
    from conftest import SPEECH

    validate_dtd(built.fcpxml)
    root = ET.parse(built.fcpxml).getroot()
    origin = T(next(a for a in root.iter("asset") if a.get("name") == "talk").get("start"))
    words = [(w["start"], w["end"]) for s, e, text in SPEECH for w in spread_words(s, e, text)]
    faded = 0
    for project in root.iter("project"):
        for el in project.find("sequence/spine"):
            if el.get("name") != "talk":
                continue
            fade_in, fade_out = _fades(el)
            a = T(el.get("start")) - origin
            b = a + T(el.get("duration"))
            assert fade_in <= Fraction(15, 1000) and fade_out <= Fraction(15, 1000)
            assert (fade_in * 48000).denominator == 1 and (fade_out * 48000).denominator == 1  # whole samples
            kept = [(ws, we) for ws, we in words if min(we, b) - max(ws, a) > 0.03]  # not a sliver at a frame edge
            for ws, we in kept:  # no kept word sounds inside a fade (a cut that lands on a word gets none)
                assert not fade_in or ws >= a + fade_in, f"fade-in at {float(a):.3f} touches a word"
                assert not fade_out or we <= b - fade_out, f"fade-out at {float(b):.3f} touches a word"
            faded += bool(fade_in) + bool(fade_out)
    assert faded >= 6  # most edits got one
    # Cutaway nat sound eases in and out; stills never carry audio settings.
    rough = _rough(root)
    cutaway = next(el for el in rough.iter("asset-clip") if el.get("name") == "broll" and el.get("lane"))
    assert _fades(cutaway) == (Fraction(3, 10), Fraction(3, 10))
    assert all(v.find("adjust-volume") is None for v in rough.iter("video"))


def test_fades_follow_the_edits_and_can_be_turned_off(tmp_path):
    from roughcut.timeline import _continuous

    # Two sentences 0.3 s apart in different sections: their padding meets, so the audio is continuous.
    talk = make_clip("V01", seconds=30, sentences=[(1.0, 4.0, "The first line ends here."), (4.3, 8.0, "And the next line follows on."), (15.0, 18.0, "Much later, one more.")])
    nat = make_clip("V02", seconds=20)  # B-roll with sound, used as a range in the main storyline
    a = Analysis(clips=[talk, nat])
    raw = {"title": "t", "sections": [{"name": "A", "items": ["V01.S001"]}, {"name": "B", "items": ["V01.S002", "V01.S003", "V02@2-10"]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    fd = tl.frame_duration
    s1, s2, s3, b = tl.spine
    assert _continuous(s1, s2, fd) and s1.section != s2.section
    assert s1.fade_out == 0 and s2.fade_in == 0  # no fade inside continuous sound
    assert s1.fade_in == s2.fade_out == s3.fade_in == Fraction(15, 1000)
    assert b.fade_in == b.fade_out and 0 <= 6 * fd - b.fade_in < Fraction(1, 48000)  # nat sound: 6 frames, in whole samples
    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    check_invariants(path)

    flat = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fades=False)
    path = _write(tmp_path, [flat], a)
    assert ET.parse(path).getroot().find(".//fadeIn") is None


def test_cli_fades_off(footage, tmp_path):
    from roughcut.cli import main

    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--fades", "off"]) == 0
    fcpxml = next(out.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    root = ET.parse(fcpxml).getroot()
    assert root.find(".//fadeIn") is None and root.find(".//adjust-volume") is not None  # levels still matched

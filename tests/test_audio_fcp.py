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
    # "footage - Rough Cut v1 (medium, fake-model)" from a run; "t - Rough Cut" from build_timeline here.
    return next(p for p in root.iter("project") if " - Rough Cut" in p.get("name"))


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


def talker(root, name: str, amp: float, noise: float = 0.0, created: str = "09:00", *, lines=None, script=None, bed: str = ""):
    """A 20 s talking clip: voice-like tone bursts where the sidecar's words are, over
    optional noise and an optional continuous `bed` (an aevalsrc expression)."""
    script = script or [(s, e, line) for (s, e, _), line in zip(SPEECH, lines or LINES[name])]
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in script)
    voice = f"{amp}*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t))*({gates})"
    expr = voice + (f"+{noise}*(2*random(0)-1)" if noise else "") + (f"+{bed}" if bed else "")
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
    assert s["speech"] == -20.0 and s["floor"] == -50.0 and s["speech_peak"] == -6.0
    assert 5.0 <= s["end"] <= 5.4  # last block loud enough to count
    whole = level_summary({"step": 0.1, "m": m, "peak": peak}, duration=10.0)
    assert whole["lufs"] == -20.0  # gating keeps the integrated value on the speech
    assert whole["speech"] is None and whole["speech_peak"] is None

    # One 2 s line in 30 s of loud ambience (an action-cam clip): the whole clip
    # (what its nat sound plays at) and the words (what its dialogue plays at) differ.
    m = [-35.0] * 100 + [-20.0] * 20 + [-35.0] * 180
    peak = [-25.0] * 100 + [-8.0] * 20 + [-25.0] * 180
    s = level_summary({"step": 0.1, "m": m, "peak": peak}, spans=[(10.2, 12.0)], duration=30.0)
    assert s["speech"] == -20.0 and s["speech_peak"] == -8.0
    assert -31.0 < s["lufs"] < -30.0 and s["peak"] == -8.0
    # Under half a second of words is too little to measure.
    assert level_summary({"step": 0.1, "m": m, "peak": peak}, spans=[(10.2, 10.5)])["speech"] is None


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

    # Speech is matched by its words, not the whole clip (falling back to it when the words weren't measured).
    clip.loudness, clip.loudness_floor, clip.peak_db = -34.0, -60.0, -14.0
    assert level_match(clip, -16.0, speech=True) == (12.0, "held to +12 dB")
    clip.speech_loudness, clip.speech_peak = -26.0, -20.0
    assert level_match(clip, -16.0, speech=True) == (10.0, "")
    # A loud background (a promo's music bed, a busy cafe) doesn't hold the words under the
    # dialogue level: the clip keeps its own balance and the note says the background comes up.
    for floor in (-40.0, -33.0):
        clip.loudness_floor = floor
        volume, why = level_match(clip, -16.0, speech=True)
        assert volume == 10.0 and f"its background, {-26 - floor:.0f} dB under the words, comes up too" == why
    # A boost stops where the peaks would clip (in half-dB steps, never past -1 dBFS) and
    # says how far under the dialogue level that leaves it; it never becomes a cut.
    clip.loudness_floor, clip.speech_peak = -70.0, -3.8
    volume, why = level_match(clip, -16.0, speech=True)
    assert volume == 2.5 and why.startswith("peaks would clip, so it plays 8 dB under the dialogue level") and "Limiter" in why
    clip.speech_peak = 2.0
    assert level_match(clip, -16.0, speech=True)[0] == 0.0
    # Nat sound sits under the dialogue: base gain plus the correction of the whole clip.
    clip.loudness, clip.loudness_floor, clip.peak_db = -26.0, -30.0, -20.0
    assert level_match(clip, -16.0, -20.0) == (-10.0, "")
    clip.peak_db = -4.0
    assert level_match(clip, -16.0, -6.0) == (3.0, "boost limited by peaks")


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
    assert abs((loud.speech_loudness - quiet.speech_loudness) - 18.0) < 1.0  # measured over the words
    assert noisy.loudness_floor > quiet.loudness_floor + 20  # the background is what differs

    vols = _spine_volumes(result.fcpxml)
    assert len(vols["loud"]) == len(vols["quiet"]) == 1  # one gain per source
    (v_loud,), (v_quiet,), (v_noisy,) = vols["loud"], vols["quiet"], vols["noisy"]
    assert abs((v_quiet - v_loud) - 18.0) <= 1.0
    for clip, v in ((loud, v_loud), (quiet, v_quiet), (noisy, v_noisy)):
        assert abs(clip.speech_loudness + v - (-16.0)) <= 0.5
    # Its hiss sits far enough under the words to need no note.
    report = result.report.read_text()
    levels = report.split("## Levels", 1)[1].split("## ", 1)[0]
    assert "about -16 LUFS" in levels and "| quiet.mov | dialogue |" in levels
    assert "background" not in levels.split("| noisy.mov |", 1)[1].split("\n", 1)[0]


def test_dialogue_level_off_and_older_folders(talkers, tmp_path):
    out = tmp_path / "out"
    opts = Options(inputs=[talkers], out=out, name="talkers", provider="none", transcriber="none", cache_dir=tmp_path / "cache", dialogue_level=None)
    result = pipeline.build(opts)
    assert all(v == {0.0} for v in _spine_volumes(result.fcpxml).values())
    assert "## Levels" not in result.report.read_text()
    # A re-cut keeps the run's own setting: still off.
    again = pipeline.rerender(out, Options(inputs=[], cache_dir=tmp_path / "cache2"))
    assert all(v == {0.0} for v in _spine_volumes(again.fcpxml).values())

    # A folder from before level matching (no loudness saved, no level setting) is measured at render time.
    path = out / "analysis.json"
    data = json.loads(path.read_text())
    for c in data["clips"]:
        for key in ("loudness", "loudness_floor", "peak_db", "sound_end", "speech_loudness", "speech_peak"):
            c.pop(key, None)
    path.write_text(json.dumps(data))
    saved = json.loads((out / "plan.json").read_text())
    for key in ("dialogue_level", "fades"):
        saved["settings"].pop(key)
    (out / "plan.json").write_text(json.dumps(saved))
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

    from roughcut.timeline import EDGE_DECLICK

    validate_dtd(built.fcpxml)
    root = ET.parse(built.fcpxml).getroot()
    origin = T(next(a for a in root.iter("asset") if a.get("name") == "talk").get("start"))
    words = [(w["start"], w["end"]) for s, e, text in SPEECH for w in spread_words(s, e, text)]
    faded = 0
    for project in root.iter("project"):
        spine = list(project.find("sequence/spine"))
        for i, el in enumerate(spine):
            if el.get("name") != "talk":
                continue
            fade_in, fade_out = _fades(el)
            a = T(el.get("start")) - origin
            b = a + T(el.get("duration"))
            assert fade_in <= Fraction(15, 1000) and fade_out <= Fraction(15, 1000)
            assert (fade_in * 48000).denominator == 1 and (fade_out * 48000).denominator == 1  # whole samples
            kept = [(ws, we) for ws, we in words if min(we, b) - max(ws, a) > 0.03]  # not a sliver at a frame edge
            for ws, we in kept:
                # The 15 ms de-click never sounds over a word. An edit that's already on (or inside) a word,
                # next to a cut um or retake, gets only the few-millisecond one that stops the tick.
                assert fade_in <= EDGE_DECLICK or ws >= a + fade_in, f"fade-in at {float(a):.3f} touches a word"
                assert fade_out <= EDGE_DECLICK or we <= b - fade_out, f"fade-out at {float(b):.3f} touches a word"
                if ws < a + fade_in:
                    assert ws >= a - Fraction(1, 30) - EDGE_DECLICK, "only an edit at a word's start fades into it"
                if we > b - fade_out:
                    assert we <= b + Fraction(1, 30) + EDGE_DECLICK, "only an edit at a word's end fades out of it"
            # Every edit where the sound jumps gets a fade; continuous speech across an edit gets none.
            prev, nxt = (spine[i - 1] if i else None), (spine[i + 1] if i + 1 < len(spine) else None)
            joined_before = prev is not None and prev.get("name") == "talk" and T(prev.get("start")) + T(prev.get("duration")) == T(el.get("start"))
            joined_after = nxt is not None and nxt.get("name") == "talk" and T(el.get("start")) + T(el.get("duration")) == T(nxt.get("start"))
            assert bool(fade_in) != joined_before and bool(fade_out) != joined_after, (float(a), float(b))
            faded += bool(fade_in) + bool(fade_out)
    assert faded >= 6
    # Cutaway nat sound eases in and out; stills never carry audio settings.
    rough = _rough(root)
    cutaway = next(el for el in rough.iter("asset-clip") if el.get("name") == "broll" and el.get("lane"))
    assert _fades(cutaway) == (Fraction(3, 10), Fraction(3, 10))
    assert all(v.find("adjust-volume") is None for v in rough.iter("video"))


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_a_cut_um_right_against_the_words_still_gets_a_declick(kind):
    """Whisper's word times touch, so cutting an um puts each edit on (or a frame inside) the next word."""
    from roughcut.plan import heuristic_plan
    from roughcut.segments import build_segments
    from roughcut.timeline import DECLICK, EDGE_DECLICK
    from roughcut.transcribe import Word

    times = [("Tickets", 0.5, 1.0), ("start", 1.0, 1.5), ("at", 1.5, 1.9), ("um", 1.9, 2.23), ("twenty", 2.23, 2.6), ("dollars", 2.6, 3.1),
             ("and", 3.1, 3.4), ("uh", 3.4, 3.47), ("kids", 3.47, 3.9), ("get", 3.9, 4.2), ("in", 4.2, 4.4), ("free.", 4.4, 4.9)]
    clip = make_clip("A01" if kind == "audio" else "V01", kind=kind, seconds=6, role="voiceover" if kind == "audio" else "aroll")
    clip.segments = build_segments(clip.id, [Word(s, e, t) for t, s, e in times])
    a = Analysis(clips=[clip])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    fd = tl.frame_duration
    assert len(tl.spine) == (3 if kind == "audio" else 2)  # every um/uh cut from narration; on camera the 70 ms uh stays
    first, last = tl.spine[0], tl.spine[-1]
    assert first.fade_in == DECLICK and last.fade_out == DECLICK  # room before the first word and after the last
    for p, q in zip(tl.spine, tl.spine[1:]):  # the edits at the cut fillers
        assert p.fade_out == EDGE_DECLICK and q.fade_in == EDGE_DECLICK, (p, q)
        assert float(p.src_in + p.frames * fd) <= p.wb + 1e-9 and float(q.src_in) >= q.wa - 1e-9  # on or inside the kept words
        assert (p.fade_out * 48000).denominator == 1


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

    # `render` keeps the run's settings unless they're typed again.
    assert main(["render", str(out)]) == 0
    root = ET.parse(next(out.glob("* v2.fcpxml"))).getroot()
    assert root.find(".//fadeIn") is None and root.find(".//adjust-volume") is not None
    assert main(["render", str(out), "--fades", "on", "--dialogue-level", "off"]) == 0
    v3 = next(out.glob("* v3.fcpxml"))
    validate_dtd(v3)
    assert ET.parse(v3).getroot().find(".//fadeIn") is not None
    assert all(v == {0.0} for v in _spine_volumes(v3).values())  # clip levels as recorded
    assert main(["render", str(out), "--style", "tight"]) == 0  # and that's what the next re-cut starts from
    assert all(v == {0.0} for v in _spine_volumes(next(out.glob("* v4.fcpxml"))).values())


# --------------------------------------------------------------- T24 music bed


def _level_at(cc, frame: int) -> float:
    """The music clip's volume at a timeline frame, interpolating its keyframes like Final Cut."""
    pts = [(cc.start + k, v) for k, v in cc.keyframes]
    if not pts:
        return cc.volume_db or 0.0
    if frame <= pts[0][0]:
        return pts[0][1]
    for (f0, v0), (f1, v1) in zip(pts, pts[1:]):
        if f0 <= frame <= f1:
            return v0 + (v1 - v0) * (frame - f0) / (f1 - f0)
    return pts[-1][1]


def _music_project(song_seconds: float = 60.0, sound_end: float | None = 55.0):
    talk = make_clip("V01", seconds=30, sentences=[(1.0, 5.0, "We built the first boat in this garage."), (9.0, 13.0, "It took us three whole winters to finish.")])
    photo = make_clip("I01", kind="image")
    song = make_clip("A01", kind="audio", seconds=song_seconds, role="music")
    song.sound_end = sound_end
    return Analysis(clips=[talk, photo, song])


RAW_WITH_MUSIC = {
    "title": "t",
    "sections": [{"name": "A", "items": ["V01.S001", "I01@0-4", "V01.S002"]}],
    "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 3}],
}


def test_music_ducks_under_speech_and_comes_up_between(tmp_path):
    a = _music_project()
    tl = build_timeline(normalize(RAW_WITH_MUSIC, a), a, style=STYLES["medium"], name="t", music_db=-14.0)
    fd = tl.frame_duration
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    talk1, still, talk2 = tl.spine
    assert _level_at(cc, (talk1.offset + talk1.end) // 2) == -14.0
    assert _level_at(cc, (talk2.offset + talk2.end) // 2) == -14.0
    assert _level_at(cc, (still.offset + still.end) // 2) == -6.0  # 4 s with nobody speaking: up 8 dB
    assert _level_at(cc, talk2.offset) == -14.0  # back down by the time the voice comes in
    assert all(0 <= k < cc.frames for k, _ in cc.keyframes)

    # Back-timed: the song's ending (55 s) lands on the cut's last frame, so no fade-out.
    end = cc.src_in + cc.frames * fd
    assert cc.start == 0 and cc.frames == tl.total_frames
    assert Fraction(55) - fd < end <= Fraction(55)
    assert cc.fade_in == Fraction(1) and cc.fade_out == 0
    assert any("back-timed" in n for n in tl.notes)

    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    check_invariants(path)
    music = next(el for el in ET.parse(path).getroot().iter("asset-clip") if el.get("lane") == "-1")
    start, dur = T(music.get("start")), T(music.get("duration"))
    adjust = music.find("adjust-volume")
    assert adjust.get("amount") is None  # the keyframes carry the level
    times = [T(k.get("time")) for k in adjust.iter("keyframe")]
    assert times and times == sorted(times) and all(start <= t < start + dur for t in times)
    assert [k.get("value") for k in adjust.iter("keyframe")][:2] == ["-14dB", "-6dB"]
    assert adjust.find("param/fadeIn").get("duration") == "1s" and adjust.find("param/fadeOut") is None


def test_music_cut_off_mid_song_fades_out_and_short_songs_warn():
    # A cue that ends mid-song (not the last one) keeps its source_in and fades out over 2 s.
    a = _music_project()
    raw = {
        "title": "t",
        "sections": [{"name": "A", "items": ["V01.S001", "I01@0-4"]}, {"name": "B", "items": ["V01.S002"]}],
        "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 3}],
    }
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.src_in == tc_floor(Fraction(3), tl.frame_duration)
    assert cc.end < tl.total_frames and cc.fade_out == min(Fraction(2), _samples_of(cc.frames * tl.frame_duration / 4))

    # A song shorter than the cut starts at its top and says it ends early.
    short = _music_project(song_seconds=6.0, sound_end=5.5)
    tl = build_timeline(normalize(RAW_WITH_MUSIC, short), short, style=STYLES["medium"], name="t")
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.src_in == 0 and cc.end < tl.total_frames
    assert any("shorter than" in w for w in tl.warnings)


def tc_floor(t: Fraction, fd: Fraction) -> Fraction:
    from roughcut import timecode as tc

    return tc.floor_frames(t, fd) * fd


def _samples_of(t: Fraction) -> Fraction:
    from roughcut.timeline import _samples

    return _samples(t)


def test_music_flag_bed_replaces_the_plans_choice():
    from roughcut.timeline import _music_cues

    a = _music_project()
    bed = make_clip("A02", kind="audio", seconds=90, role="music")
    bed.music_bed = True
    a.clips.append(bed)
    plan = normalize(RAW_WITH_MUSIC, a)
    notes: list[str] = []
    (cue,) = _music_cues(plan, a, True, notes)
    assert (cue.clip_id, cue.first_section, cue.last_section) == ("A02", 0, 0)
    assert "A02.wav (--music) runs under the whole cut instead of the plan's choice (A01.wav)" in notes[0]
    # When the plan uses the --music song, its cues stand; the stringout gets no bed at all.
    raw = dict(RAW_WITH_MUSIC, music=[{"clip": "A02", "first_section": 0, "last_section": 0, "source_in": 10}])
    assert [c.clip_id for c in _music_cues(normalize(raw, a), a, True, [])] == ["A02"]
    assert _music_cues(normalize({"title": "t", "sections": RAW_WITH_MUSIC["sections"]}, a), a, False, []) == []


def test_music_flag_end_to_end(footage, tmp_path):
    from roughcut.cli import main

    song = tmp_path / "Downloads" / "Sunrise Theme.m4a"
    song.parent.mkdir()
    # 40 s: a tone that dies away at 36 s, then silence (a song with a real ending).
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=exprs='0.3*sin(2*PI*330*t)*between(t,0,36)*min(1,(36-t)/3)':s=48000:d=40", "-ac", "2", "-c:a", "aac", str(song))
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--music", str(song), "--name", "footage"])
    assert code == 0
    analysis = Analysis.load(out / "analysis.json")
    bed = next(c for c in analysis.clips if c.media.name == "Sunrise Theme.m4a")
    assert bed.music_bed and bed.role == "music" and bed.transcript_source == "music" and not bed.segments
    assert 35.5 < bed.sound_end < 37.5

    fcpxml = out / "footage v1.fcpxml"
    validate_dtd(fcpxml)
    check_invariants(fcpxml)
    root = ET.parse(fcpxml).getroot()
    rough = _rough(root)
    (music,) = [el for el in rough.iter("asset-clip") if el.get("lane") == "-1"]
    assert music.get("name") == "Sunrise Theme" and music.get("audioRole") == "music.Music Bed"
    end = T(music.get("start")) + T(music.get("duration"))
    assert abs(float(end) - bed.sound_end) < 0.05  # back-timed onto the song's ending
    stringout = next(p for p in root.iter("project") if p.get("name") == "footage - Stringout v1")
    assert not [el for el in stringout.iter("asset-clip") if el.get("lane") == "-1"]
    report = (out / "edit_report.md").read_text()
    assert "Sunrise Theme.m4a (--music) runs under the whole cut" in report

    assert main(["build", str(footage), "-o", str(tmp_path / "o2"), "--provider", "none", "--music", str(footage / "photo.png")]) == 1


# --------------------------------------------- level matching on real-world shapes


@pytest.fixture(scope="module")
def busy(tmp_path_factory):
    """Speech over the backgrounds real shoots have: a finished promo's music bed,
    a cafe's room noise, and an action-cam clip with one line in its ambience
    (too little speech to count as talking footage)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("busy")
    talker(root, "clean", 0.6, created="09:00", lines=LINES["loud"])
    chord = "0.03*(sin(2*PI*330*t)+sin(2*PI*415*t))*(0.8+0.2*sin(2*PI*2*t))"
    talker(root, "promo", 0.12, created="09:01", lines=LINES["quiet"], bed=chord)
    talker(root, "cafe", 0.08, noise=0.007, created="09:02", lines=LINES["noisy"])
    talker(root, "cam", 0.1, noise=0.008, created="09:03", script=[(8.0, 10.4, "Look at that, it actually floats.")])
    return root


BUSY_PLAN = {
    "title": "Busy",
    "sections": [{"name": "Open", "items": ["V01.S001", "V04.S001", "V02.S001", "V03.S001", "V01.S004"]}],
}


def test_speech_over_a_bed_or_a_busy_room_reaches_the_dialogue_level(busy, tmp_path):
    provider = FakeProvider(BUSY_PLAN)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "make_provider", lambda *a, **k: provider)
        result = pipeline.build(Options(inputs=[busy], out=tmp_path / "out", name="busy", transcriber="none", cache_dir=tmp_path / "cache"))
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    analysis = Analysis.load(tmp_path / "out" / "analysis.json")
    clips = {c.media.stem: c for c in analysis.clips}
    assert [clips[n].id for n in ("clean", "promo", "cafe", "cam")] == ["V01", "V02", "V03", "V04"]
    promo, cafe, cam = clips["promo"], clips["cafe"], clips["cam"]
    assert promo.speech_loudness - promo.loudness_floor < 12  # the bed is part of the mix
    # A noisy room: the old fixed -45 LUFS noise ceiling allowed it no boost at all.
    assert cafe.loudness_floor > -45 and cafe.speech_loudness - cafe.loudness_floor < 20
    # The cam line is measured on its words, well above its ambience.
    assert cam.role == "broll" and cam.speech_loudness > cam.loudness + 3

    vols = _spine_volumes(result.fcpxml)
    for name in ("clean", "promo", "cafe", "cam"):
        (v,) = vols[name]
        assert abs(clips[name].speech_loudness + v - (-16.0)) <= 0.75, name

    rough = _rough(ET.parse(result.fcpxml).getroot())
    (cam_el,) = [el for el in rough.find("sequence/spine") if el.get("name") == "cam"]
    assert cam_el.get("audioRole") == "dialogue" and cam_el.get("videoRole") is None

    levels = result.report.read_text().split("## Levels", 1)[1].split("## ", 1)[0]
    rows = {line.split("|")[1].strip(): line for line in levels.splitlines() if line.startswith("| ") and ".mov" in line}
    assert "| dialogue |" in rows["cam.mov"]
    assert "under the words, comes up too" in rows["promo.mov"] and "under the words, comes up too" in rows["cafe.mov"]
    assert "noise" not in levels and "as recorded" not in levels


def test_a_line_from_a_broll_clip_plays_as_dialogue(tmp_path):
    talk = make_clip("V01", seconds=30, sentences=[(1.0, 5.0, "We built the first boat in this garage."), (9.0, 13.0, "It took us three whole winters to finish.")])
    talk.loudness = talk.speech_loudness = -20.0
    talk.loudness_floor, talk.peak_db, talk.speech_peak = -60.0, -8.0, -8.0
    cam = make_clip("V02", seconds=40, sentences=[(10.0, 12.4, "Look at that, it actually floats.")], role="broll")
    cam.loudness, cam.loudness_floor, cam.peak_db = -34.0, -40.0, -14.0
    cam.speech_loudness, cam.speech_peak = -24.0, -14.0
    a = Analysis(clips=[talk, cam])
    raw = {
        "title": "t",
        "sections": [{"name": "A", "items": ["V01.S001", "V02.S001", "V01.S002"], "broll": [{"clip": "V02", "over": ["V01.S002"], "source_in": 20, "reason": "the boat in the water"}]}],
    }
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t - Rough Cut", dialogue_level=-16.0, broll_db=-20.0)
    (line,) = [sc for sc in tl.spine if sc.clip_id == "V02"]
    assert line.volume_db == 8.0  # its words to -16, not nat sound 6 dB under
    (cutaway,) = [cc for cc in tl.connected if cc.clip_id == "V02"]
    assert cutaway.volume_db == -8.0  # as a cutaway its whole clip is the nat sound: -20 + 12 (held)
    rows = {(r["clip"], r["use"]): r for r in tl.levels}
    assert rows[("V02", "dialogue")]["lufs"] == -24.0 and rows[("V02", "dialogue")]["words"]
    assert rows[("V02", "cutaway nat sound")]["lufs"] == -34.0
    assert line.fade_in <= Fraction(15, 1000) and line.fade_out <= Fraction(15, 1000)  # de-clicked like speech

    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    check_invariants(path)
    spine = list(_rough(ET.parse(path).getroot()).find("sequence/spine"))
    v02 = next(el for el in spine if el.get("name") == "V02")
    assert v02.get("audioRole") == "dialogue" and v02.get("videoRole") is None
    child = next(el for el in spine[-1] if el.get("name") == "V02")
    assert child.get("audioRole") == "effects.Nat Sound" and child.get("videoRole") == "B-Roll.Planned"


def _montage(song_loudness: float = -9.5):
    photos = [make_clip(f"I0{i}", kind="image") for i in (1, 2, 3)]
    song = make_clip("A01", kind="audio", seconds=60, role="music")
    song.loudness, song.loudness_floor, song.peak_db, song.sound_end = song_loudness, -30.0, -0.5, 55.0
    raw = {
        "title": "t",
        "sections": [{"name": "A", "items": ["I01@0-4", "I02@0-4", "I03@0-4"]}],
        "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 0}],
    }
    a = Analysis(clips=photos + [song])
    return normalize(raw, a), a


def test_a_cut_nobody_speaks_in_plays_the_song_at_the_dialogue_level(tmp_path):
    plan, a = _montage()
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", dialogue_level=-16.0, music_db=-14.0)
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.volume_db == -6.5 and cc.keyframes == []  # -9.5 LUFS to -16: the song is the programme
    (row,) = tl.levels
    assert row["volume"] == -6.5 and "nobody speaks" in row["why"]
    assert cc.fade_in == 1  # still fades in
    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    check_invariants(path)
    music = next(el for el in ET.parse(path).getroot().iter("asset-clip") if el.get("lane") == "-1")
    assert music.find("adjust-volume").get("amount") == "-6.5dB" and music.find(".//keyframe") is None

    # Levels off: the song plays as recorded, not 14 dB under a dialogue that isn't there.
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", dialogue_level=None, music_db=-14.0)
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.volume_db is None and cc.keyframes == []


def test_music_never_comes_up_past_the_dialogue_level():
    a = _music_project()
    song = a.clip("A01")
    song.loudness, song.loudness_floor, song.peak_db = -9.5, -30.0, -0.5
    plan = normalize(RAW_WITH_MUSIC, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", dialogue_level=-16.0, music_db=-4.0)
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.volume_db == -10.5  # 4 dB under the dialogue while people talk
    assert max(v for _, v in cc.keyframes) == -6.5  # and up to the dialogue level between, not +8 dB past it
    row = next(r for r in tl.levels if r["use"] == "music bed")
    assert "comes up 4 dB" in row["why"]
    # With the default bed level it still comes up the full 8 dB.
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", dialogue_level=-16.0, music_db=-14.0)
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert max(v for _, v in cc.keyframes) == cc.volume_db + 8


def test_fades_off_keeps_the_music_fades():
    a = _music_project()
    raw = {
        "title": "t",
        "sections": [{"name": "A", "items": ["V01.S001", "I01@0-4"]}, {"name": "B", "items": ["V01.S002"]}],
        "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 3}],
    }
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fades=False)
    (cc,) = [c for c in tl.connected if c.kind == "music"]
    assert cc.fade_in == 1 and cc.fade_out > 0  # the bed still fades in, and out where it's cut off
    assert all(sc.fade_in == sc.fade_out == 0 for sc in tl.spine)  # the edits get none


def test_dialogue_level_takes_lufs_after_a_space(tmp_path):
    from roughcut.cli import LEVEL_OFF, _glue_levels, build_parser, main

    for value, want in (("-14LUFS", -14.0), ("-14", -14.0), ("off", LEVEL_OFF), ("-16 LUFS", -16.0)):
        args = build_parser().parse_args(_glue_levels(["render", "out", "--dialogue-level", value]))
        assert args.dialogue_level == want
    assert build_parser().parse_args(["render", "out"]).dialogue_level is None  # not given: the saved run's level
    assert build_parser().parse_args(_glue_levels(["render", "out", "--dialogue-level=-14LUFS"])).dialogue_level == -14.0
    # Through main the option parses (and the missing folder is the only complaint).
    assert main(["render", str(tmp_path / "missing"), "--dialogue-level", "-14LUFS"]) == 1
    with pytest.raises(SystemExit):
        main(["render", str(tmp_path / "missing"), "--dialogue-level", "-60LUFS"])


def test_a_vfr_clip_in_the_cut_is_warned_about_once():
    from roughcut.report import build_report

    phone = make_clip("V01", seconds=30, sentences=[(1.0, 5.0, "We built the first boat in this garage."), (9.0, 13.0, "It took us three whole winters.")])
    other = make_clip("V02", seconds=10)
    for clip in (phone, other):
        clip.media.vfr = True
        clip.media.warnings = ["variable frame rate (avg 29.62 fps); treated as 29.97"]
    a = Analysis(clips=[phone, other])
    a.warnings = [f"{c.media.name}: {c.media.warnings[0]}" for c in (phone, other)]
    plan = normalize({"title": "t", "sections": [{"name": "A", "items": ["V01.S001", "V01.S002"]}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    warnings = build_report(plan, tl, a).split("## Warnings", 1)[1]
    mentions = [line for line in warnings.splitlines() if "V01.mov" in line]
    assert len(mentions) == 1 and "V01.mov (avg 29.62 fps)" in mentions[0]
    # A VFR clip that isn't in the cut keeps its own line.
    assert "V02.mov: variable frame rate (avg 29.62 fps)" in warnings


# ------------------------------------------------------------------- fuzzing


def _script(rng, n: int, length: float, words: list[str]) -> list[tuple[float, float, str]]:
    out, t = [], rng.uniform(0.3, 1.5)
    for _ in range(n):
        dur = rng.uniform(0.8, 5.0)
        if t + dur > length - 0.5:
            break
        text = " ".join(rng.choice(words) for _ in range(max(2, int(dur * 2.5)))).capitalize() + "."
        out.append((round(t, 3), round(t + dur, 3), text))
        t += dur + rng.choice([0.1, 0.3, 0.6, 1.2, 2.5])
    return out


def _silences(script, length):
    gaps, cursor = [], 0.0
    for s, e, _ in script:
        if s - cursor > 0.35:
            gaps.append((cursor, s))
        cursor = e
    if length - cursor > 0.35:
        gaps.append((cursor, length))
    return gaps


def fuzz_one(seed: int, tmp) -> None:
    """One random project through build_timeline and build_fcpxml, checked end to end."""
    import random

    from roughcut.plan import heuristic_plan

    rng = random.Random(seed)
    vocab = "boat garage winter harbor ferry bakery trail summit coffee morning kids family river bridge light".split()
    clips = []
    talk_script = _script(rng, 8, 60, vocab)
    clips.append(make_clip("V01", seconds=60, sentences=talk_script, silences=_silences(talk_script, 60)))
    vo_script = _script(rng, 6, 40, vocab)
    clips.append(make_clip("A01", kind="audio", seconds=40, sentences=vo_script, role="voiceover", silences=_silences(vo_script, 40)))
    take = [(s + 0.4, e + 0.4, text) for s, e, text in vo_script]
    a02 = make_clip("A02", kind="audio", seconds=41, sentences=take, role="voiceover")
    a02.take_of, a02.same_words = "A01", 0.95
    clips.append(a02)
    clips.append(make_clip("V02", seconds=rng.choice([3, 20])))
    # A B-roll clip with one spoken line, which the plan may use as a story item.
    clips.append(make_clip("V04", seconds=20, sentences=[(6.0, 8.4, "look at that boat go")], role="broll"))
    silent = make_clip("V03", seconds=8)
    silent.media.has_audio = False
    clips += [silent, make_clip("I01", kind="image"), make_clip("I02", kind="image")]
    song_len = rng.choice([4.0, 30.0, 120.0])
    song = make_clip("M01", kind="audio", seconds=song_len, role="music")
    song.sound_end = rng.choice([None, song_len, song_len - 3.0])
    song.music_bed = rng.random() < 0.3
    clips.append(song)
    for c in clips:
        if c.media.has_audio and rng.random() < 0.8:
            c.loudness = rng.uniform(-40, -6)
            c.loudness_floor = c.loudness - rng.uniform(5, 50)
            c.peak_db = min(0.0, c.loudness + rng.uniform(5, 20))
            if c.segments and rng.random() < 0.8:
                c.speech_loudness = c.loudness + rng.uniform(0, 12)
                c.speech_peak = min(0.0, c.speech_loudness + rng.uniform(5, 25))
    a = Analysis(clips=clips)

    segs = [s.id for c in clips for s in c.segments]
    ranges = ["V02@0.5-2.5", "I01@0-3", "I02@0-2", "V03@1-5", "V01@10-13", "A01@2-6"]
    sections = []
    for k in range(rng.randint(1, 4)):
        items = rng.sample(segs, rng.randint(1, min(5, len(segs)))) + rng.sample(ranges, rng.randint(0, 2))
        rng.shuffle(items)
        seg_items = [i for i in items if "." in i]
        broll = []
        if seg_items and rng.random() < 0.7:
            over = seg_items[: rng.randint(1, len(seg_items))]
            for clip_id in rng.sample(["V02", "V03", "I01", "I02", "V01"], rng.randint(1, 3)):
                broll.append({"clip": clip_id, "over": over, "source_in": rng.choice([-1, 0, 2.5]), "reason": rng.choice(["auto-fill", "cover the jump", "shows \x07 the boat"])})
        sections.append({"name": f"S{k}", "items": items, "broll": broll})
    music = []
    if rng.random() < 0.7:
        first = rng.randrange(len(sections))
        music.append({"clip": "M01", "first_section": first, "last_section": rng.randrange(first, len(sections)), "source_in": rng.choice([0, 7.5, 500])})
    alternates = [{"chosen": rng.choice(segs), "others": rng.sample(segs, 2), "note": "x"}]
    plan = normalize({"title": "fuzz", "sections": sections, "music": music, "alternates": alternates}, a)
    if not plan.sections:
        return
    style = STYLES[rng.choice(list(STYLES))]
    kw = dict(
        style=style,
        dialogue_level=rng.choice([None, -16.0, -23.0]),
        fades=rng.random() < 0.8,
        broll_db=rng.choice([None, -20.0, -10.0]),
        music_db=rng.choice([-14.0, -20.0]),
        format_override=rng.choice([None, "1080p@23.98", "vertical@25"]),
    )
    tl = build_timeline(plan, a, name="fuzz - Rough Cut", **kw)
    timelines = [tl]
    s_plan = heuristic_plan(a)
    if s_plan.sections:
        timelines.append(build_timeline(s_plan, a, name="fuzz - Stringout", music_beds=False, **kw))
    path = tmp / f"fuzz-{seed}.fcpxml"
    path.write_text(build_fcpxml(timelines, a, event_name="fuzz", alternates=plan.alternates))
    validate_dtd(path)
    check_invariants(path)

    from roughcut.timeline import EDGE_DECLICK, _continuous, _is_speech, _measured

    fd = tl.frame_duration
    for t_ in timelines:
        for i, sc in enumerate(t_.spine):
            clip = a.clip(sc.clip_id)
            lufs, peak = _measured(clip, _is_speech(sc, clip))
            if sc.volume_db and sc.volume_db > 0:
                assert sc.volume_db <= 12.0 and (peak is None or peak + sc.volume_db <= -1.0), "a boost clips"
            length = sc.frames * t_.frame_duration
            assert sc.fade_in + sc.fade_out <= length
            # Only the few-millisecond edge de-click may reach a word, and only from an edit already on it.
            if sc.wa is not None and sc.src_in + sc.fade_in > Fraction(sc.wa).limit_denominator(1000000):
                assert sc.fade_in <= EDGE_DECLICK and sc.src_in + t_.frame_duration > Fraction(sc.wa).limit_denominator(1000000), "fade-in reaches a word"
            if sc.wb is not None and sc.src_in + length - sc.fade_out < Fraction(sc.wb).limit_denominator(1000000):
                assert sc.fade_out <= EDGE_DECLICK and sc.src_in + length - t_.frame_duration < Fraction(sc.wb).limit_denominator(1000000), "fade-out reaches a word"
            if clip.media.has_audio and clip.media.kind != "image":  # every audio jump gets a fade when they're on
                head = not _continuous(t_.spine[i - 1] if i else None, sc, t_.frame_duration)
                tail = not _continuous(sc, t_.spine[i + 1] if i + 1 < len(t_.spine) else None, t_.frame_duration)
                assert bool(sc.fade_in) == (kw["fades"] and head) and bool(sc.fade_out) == (kw["fades"] and tail)
        for cc in t_.connected:
            assert cc.fade_in + cc.fade_out <= cc.frames * fd
            frames = [k for k, _ in cc.keyframes]
            assert frames == sorted(set(frames)) and all(0 <= k <= cc.frames for k in frames)
    root = ET.parse(path).getroot()
    for el in root.iter("asset-clip"):
        adjust = el.find("adjust-volume")
        if adjust is None:
            continue
        start, dur = T(el.get("start", "0s")), T(el.get("duration"))
        fades = [T(f.get("duration")) for f in adjust.iter() if f.tag in ("fadeIn", "fadeOut")]
        assert sum(fades, Fraction(0)) <= dur and all((f * 48000).denominator == 1 for f in fades)
        assert all(start <= T(k.get("time")) <= start + dur for k in adjust.iter("keyframe"))


def test_fuzz_audio_and_fcp_details(tmp_path):
    for seed in range(30):
        fuzz_one(seed, tmp_path)


# ------------------------------------------------------------ punch-ins on jump cuts


def _jumpy(*, broll_over=None, punch_in=True):
    """One talking clip cut on three lines (two jump cuts), optionally with a cutaway over the second line."""
    clips = [make_clip("V01", seconds=TALK_SECONDS, sentences=SPEECH), make_clip("V02", seconds=10)]
    a = Analysis(clips=clips)
    broll = [{"clip": "V02", "over": [broll_over], "words": "", "source_in": 0, "reason": "x"}] if broll_over else []
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V01.S003", "V01.S005"], "broll": broll}]}, a, source="fake:fake")
    return a, build_timeline(plan, a, style=STYLES["tight"], name="t", punch_in=punch_in)


def test_jump_cuts_alternate_a_punch_in(tmp_path):
    from roughcut.timeline import PUNCH_IN

    a, tl = _jumpy()
    assert [sc.scale for sc in tl.spine] == [1.0, PUNCH_IN, 1.0] and tl.review.punched == 2
    assert not [m for m in tl.markers if m.kind == "todo" and m.value.startswith("Jump cut")], "punched in, not left as to-dos"
    assert any(line.startswith("Punched in: 2 of those cuts") for line in tl.review.lines(tl.frame_duration))
    path = _write(tmp_path, [tl], a)
    validate_dtd(path)
    check_invariants(path)
    spine = list(ET.parse(path).getroot().find(".//project/sequence/spine"))
    assert [el.find("adjust-transform").get("scale") if el.find("adjust-transform") is not None else None for el in spine] == [None, "1.12 1.12", None]
    second = [child.tag for child in spine[1]]
    assert second.index("adjust-transform") < second.index("adjust-volume"), "the DTD's order: video adjustments, then volume"
    # Off (the default), nothing changes and the jump cuts stay as to-dos.
    a, plain = _jumpy(punch_in=False)
    assert all(sc.scale == 1.0 for sc in plain.spine) and [m for m in plain.markers if m.value.startswith("Jump cut")]


def test_a_cut_b_roll_covers_is_not_punched(tmp_path):
    a, tl = _jumpy(broll_over="V01.S003")
    # The cutaway runs over all of S003, so it hides both cuts (into S003 and out of it): nothing to punch.
    assert [c.clip_id for c in tl.connected] == ["V02"] and not tl.review.jump_cuts
    assert [sc.scale for sc in tl.spine] == [1.0, 1.0, 1.0] and tl.review.punched == 0


def test_the_brief_turns_punch_ins_on_and_re_cuts_keep_them(footage, tmp_path, capsys):
    from roughcut.cli import main

    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", str(footage), "-o", str(out), "-c", "Punch in on the jump cuts.", *common]) == 0
    assert "punching in on jump cuts" in capsys.readouterr().err
    assert json.loads((out / "plan.json").read_text())["settings"]["punch_in"] is True
    assert main(["render", str(out), "--style", "loose"]) == 0
    assert json.loads((out / "plan.json").read_text())["settings"]["punch_in"] is True
    assert main(["render", str(out), "--no-punch-in"]) == 0
    assert json.loads((out / "plan.json").read_text())["settings"]["punch_in"] is False

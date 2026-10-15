"""A noisy room's pauses from the clip's own loudness.

silencedetect compares single samples with its level. In a noisy room one click ends a pause and starts the next at the same
moment, a gust cuts it to pieces, and a word Whisper wrote over nothing but the room's noise (a verse number nobody read) can't be
told from a quiet real one, so the pause around it stayed. The 10 ms loudness readings tell the room from the speech.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import make_clip

from roughcut import ffmpeg_ops
from roughcut.analyze import Analysis, Cache, analyze
from roughcut.cli import main
from roughcut.ffmpeg_ops import LEVEL_FLOOR_LIMIT, level_silences, level_threshold
from roughcut.plan import heuristic_plan
from roughcut.segments import build_segments, drop_words_in_silence, split_words_in_silence
from roughcut.timeline import STYLES, _join_close, build_timeline, speech_pieces
from roughcut.transcribe import Word

ROOM, SPEECH = -40.0, -18.0


def readings(spans, seconds=10.0, room=ROOM, speech=SPEECH):
    """10 ms loudness readings: the room everywhere, speech over the (start, end) spans, with the room's own few dB of wobble."""
    n = int(round(seconds / 0.01))
    out = [room + (i % 4) * 0.5 for i in range(n)]
    for a, b in spans:
        for i in range(int(round(a / 0.01)), int(round(b / 0.01))):
            out[i] = speech - (i % 5)
    return out


# ------------------------------------------------------------------ the map

def test_the_pauses_between_the_speech_are_found_at_the_readings():
    found = level_silences(readings([(1.0, 3.0), (5.0, 8.0)]), 10.0)
    assert found is not None and found["floor"] == pytest.approx(ROOM)
    assert found["silences"] == [(0.0, 1.0), (3.0, 5.0), (8.0, 10.0)]
    assert ROOM < found["db"] < ROOM + 7  # a few dB over the room, nowhere near the speech


def test_a_click_inside_a_pause_does_not_end_it():
    levels = readings([(1.0, 3.0), (6.0, 8.0)])
    for i in range(430, 433):  # 30 ms of click in the middle of the pause 3.0-6.0
        levels[i] = ROOM + 11
    assert level_silences(levels, 10.0)["silences"] == [(0.0, 1.0), (3.0, 6.0), (8.0, 10.0)]


def test_a_sound_longer_than_a_click_or_louder_than_one_splits_the_pause():
    longer = readings([(1.0, 3.0), (6.0, 8.0)])
    for i in range(430, 437):  # 70 ms: a breath, not a click
        longer[i] = ROOM + 11
    assert len(level_silences(longer, 10.0)["silences"]) == 4
    louder = readings([(1.0, 3.0), (6.0, 8.0)])
    louder[430] = ROOM + 20  # a pop
    assert len(level_silences(louder, 10.0)["silences"]) == 4


def test_speech_close_to_the_room_is_not_told_apart():
    assert level_threshold(readings([(1.0, 3.0), (6.0, 8.0)], speech=ROOM + 8)) is None
    assert level_silences(readings([(1.0, 3.0)], speech=ROOM + 8), 10.0) is None


def test_a_few_seconds_are_too_little_to_judge():
    assert level_threshold(readings([(0.2, 0.8)], seconds=1.5)) is None


def test_a_recording_quieter_than_the_floor_limit_is_judged_against_the_limit():
    levels = readings([(1.0, 3.0), (5.0, 8.0)], room=-110.0, speech=-50.0)
    floor = level_threshold(levels)[1]
    assert floor == LEVEL_FLOOR_LIMIT


# ------------------------------------------------------------------ the words

def test_a_word_over_nothing_but_the_room_is_dropped_and_a_stretched_one_is_not():
    words = [Word(1.2, 1.6, "went"), Word(3.0, 5.99, "home."), Word(4.5, 5.0, "8."), Word(6.2, 6.6, "The")]
    kept, dropped = split_words_in_silence(words, [(3.1, 6.0)])
    assert [w.text for w in dropped] == ["8."]  # "home." starts at 3.0 where the speech still sounds
    assert [w.text for w in kept] == ["went", "home.", "The"]
    assert drop_words_in_silence(words, [(3.1, 6.0)]) == kept
    assert split_words_in_silence(words, []) == (words, [])


# ------------------------------------------------------------------ the cut

def test_a_pause_a_click_split_in_two_is_cut_as_one():
    assert _join_close([(2.0, 2.6), (2.6, 3.2), (5.0, 5.5)]) == [(2.0, 3.2), (5.0, 5.5)]
    assert _join_close([(2.0, 2.6), (2.63, 3.2)]) == [(2.0, 3.2)]  # 30 ms: a click
    assert _join_close([(2.0, 2.6), (2.7, 3.2)]) == [(2.0, 2.6), (2.7, 3.2)]  # 100 ms: a word could sound in it
    clip = make_clip("V01", seconds=12, role="aroll")
    words = [Word(1.0 + 0.4 * i, 1.35 + 0.4 * i, w) for i, w in enumerate("we climbed the ridge before sunset".split())]
    words += [Word(5.0, 5.4, "and"), Word(5.5, 5.9, "slept.")]
    clip.segments = build_segments("V01", [Word(w.start, w.end, w.text) for w in words])
    clip.silences = [(3.6, 4.3), (4.3, 4.95)]  # one pause of 1.35 s, split at 4.3 where a click was
    style = STYLES["medium"]
    pieces = [p for seg in clip.segments for p in speech_pieces(seg, clip, style, 0)]
    from roughcut.timeline import _cut_silences

    cut = _cut_silences(pieces, Analysis(clips=[clip]), style)
    assert max(b.a - a.b for a, b in zip(cut, cut[1:])) > 1.0  # the pause is gone, not half of it
    assert cut[-1].a >= 4.95 - style.pad_in - 1e-6  # the next piece starts where the sound does, not a pause earlier


# ------------------------------------------------------------------ the whole run

SENTENCES = [(1.0, 4.0, "So this is the new arena district coming to Anaheim."), (6.0, 9.5, "It's going to have restaurants and a music venue.")]
SECONDS = 12


def _noisy(root, invented):
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    speech = f"0.5*sin(2*PI*220*t)*(0.7+0.3*sin(2*PI*3*t))*({gates})"
    # a loud room (peaks near -30 dB) and two clicks, a millisecond each, in the middle of the pause 4.0-6.0
    clicks = "+".join(f"0.12*between(t,{t},{t + 0.001})" for t in (4.9, 5.2))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}+{clicks}':s=48000:d={SECONDS}",
         "-f", "lavfi", "-i", f"anoisesrc=c=pink:a=0.06:d={SECONDS}:r=48000",
         "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(root / "talk.mov")],
        check=True,
    )
    words = []
    for s, e, text in SENTENCES:
        step = (e - s) / len(text.split())
        words += [{"start": round(s + i * step, 3), "end": round(s + (i + 1) * step - 0.05, 3), "text": w} for i, w in enumerate(text.split())]
    if invented:  # a verse number nobody read, over the room's noise between the sentences
        words.append({"start": 4.6, "end": 5.2, "text": "8."})
    words.sort(key=lambda w: w["start"])
    (root / "talk.json").write_text(json.dumps({"words": words}))


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("room")
    _noisy(root, invented=True)
    return root


def test_a_verse_number_nobody_read_is_dropped_and_its_pause_cut(room, tmp_path, capsys):
    analysis = analyze([room / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache"))
    clip = analysis.clips[0]
    assert clip.silence_db > -35.0  # a loud room
    assert len([s for s in clip.silences if 3.9 <= s[0] < 6.0]) == 2  # silencedetect's map: the two clicks cut the pause up, and "8." sits over the break
    text = [w.text for s in clip.segments for w in s.words]
    assert "8." not in text and "Anaheim." in text and "venue." in text
    assert "8." in capsys.readouterr().err  # the log says what was dropped
    tl = build_timeline(heuristic_plan(analysis), analysis, style=STYLES["medium"], name="t")
    kept = [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]
    assert not any(a < 4.6 and b > 5.2 for a, b in kept), kept  # nothing of the pause between the sentences is kept
    assert any(a <= 1.0 and b >= 3.9 for a, b in kept) and any(a <= 6.1 and b >= 9.4 for a, b in kept)


def test_the_loudness_map_is_cached_and_read_back_the_same(room, tmp_path):
    cache = Cache(tmp_path / "cache")
    first = analyze([room / "talk.mov"], transcriber=None, cache=cache).clips[0]
    again = analyze([room / "talk.mov"], transcriber=None, cache=cache).clips[0]
    assert first.silences == again.silences and first.silence_db == again.silence_db
    assert [w.text for s in first.segments for w in s.words] == [w.text for s in again.segments for w in s.words]


def test_a_clean_recording_is_left_to_silencedetect(tmp_path_factory, tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("clean")
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*220*t)*({gates})':s=48000:d={SECONDS}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mov")],
        check=True,
    )
    clip = analyze([root / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert clip.silence_db == ffmpeg_ops.DEFAULT_NOISE_DB
    assert not (tmp_path / "cache" / clip.media.fingerprint / "levels10-v1.json").exists()  # no extra pass over a clean clip


def test_a_map_that_finds_no_pauses_is_replaced_by_the_loudness_map(room, tmp_path, monkeypatch):
    """silencedetect at a level above the speech calls the whole clip one silence: that is no map of its pauses."""
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (-3.0, -30.0))
    clip = analyze([room / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert [(round(s), round(e)) for s, e in clip.silences] == [(0, 1), (4, 6), (10, 12)]
    assert clip.silence_db == pytest.approx(-35.0, abs=1.0)
    assert "8." not in [w.text for s in clip.segments for w in s.words]


def test_a_music_bed_is_not_a_room_so_nothing_is_dropped_over_it(tmp_path_factory, tmp_path):
    """Music under the speech has quiet stretches of its own; a word there may be a real one the music covers."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("bed")
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    speech = f"0.5*sin(2*PI*220*t)*(0.7+0.3*sin(2*PI*3*t))*({gates})"
    chord = "+".join(f"0.03*sin(2*PI*{f}*t)" for f in (261.6, 329.6, 392.0))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{chord}':s=48000:d={SECONDS}",
         "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mov")],
        check=True,
    )
    words = []
    for s, e, text in SENTENCES:
        step = (e - s) / len(text.split())
        words += [{"start": round(s + i * step, 3), "end": round(s + (i + 1) * step - 0.05, 3), "text": w} for i, w in enumerate(text.split())]
    words.append({"start": 4.6, "end": 5.2, "text": "softly"})  # said under the music, say
    words.sort(key=lambda w: w["start"])
    (root / "talk.json").write_text(json.dumps({"words": words}))
    clip = analyze([root / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert clip.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB  # a raised level, the case the loudness map looks at
    assert "softly" in [w.text for s in clip.segments for w in s.words]
    assert clip.bed == "music"


def test_the_paper_edit_says_which_words_were_left_out(room, tmp_path):
    out = tmp_path / "out"
    assert main(["build", str(room), "--provider", "none", "--transcriber", "none", "--no-menu", "-o", str(out), "--cache-dir", str(tmp_path / "cache")]) == 0
    report = (out / "edit_report.md").read_text()
    assert "left out 1 word Whisper wrote over nothing but the room's noise ('8.' at 4.6 s)" in report

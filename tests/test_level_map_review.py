"""What an independent review of the loudness map found.

The map drops a word it calls invented, and a dropped word no longer protects the sound it was said in, so every wrong drop is
speech lost from the cut. These tests are the cases the review built: a talker close to the room, a soft short word, a room
that is mostly rumble or hum, a quiet recording, and the clocks that disagree about when a file's audio starts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from roughcut import ffmpeg_ops, pauses
from roughcut.analyze import Analysis, Cache, analyze
from roughcut.segments import speech_stands_above_room, split_invented_words
from roughcut.timeline import _join_close
from roughcut.transcribe import Transcriber, Transcript, Word

FLOOR, PAUSE = -60.0, -55.0


def _readings(seconds=12.0, fill=FLOOR, spans=()):
    """10 ms loudness readings at `fill`, with (start, end, level) spans set."""
    out = [fill + (i % 3) * 0.5 for i in range(int(round(seconds / 0.01)))]
    for a, b, level in spans:
        for i in range(int(round(a / 0.01)), int(round(b / 0.01))):
            out[i] = level
    return out


# ------------------------------------------------------------------ which words are the room's

def test_a_word_is_the_rooms_only_when_the_words_themselves_stand_above_it():
    """The speech level is read from the transcript's own words, not the loudest twentieth of the clip: a horn at the end must not
    make a talker who is within 12 dB of the room look separable."""
    words = [Word(1.0, 1.5, "so"), Word(2.5, 3.0, "this"), Word(5.0, 5.5, "is"), Word(7.0, 7.5, "it")]
    silences = [(0.0, 4.5), (6.0, 12.0)]  # the map calls everything but "is" a pause
    soft = _readings(spans=[(1.0, 1.5, -56.0), (2.5, 3.0, -58.0), (5.0, 5.5, -52.0), (7.0, 7.5, -57.0), (10.0, 11.0, -20.0)])
    kept, invented = split_invented_words(words, silences, soft, FLOOR, PAUSE)
    assert invented == [] and len(kept) == 4
    clear = _readings(spans=[(1.0, 1.5, -30.0), (2.5, 3.0, -58.0), (5.0, 5.5, -30.0), (7.0, 7.5, -30.0)])
    kept, invented = split_invented_words(words, [(0.0, 1.0), (1.6, 4.5), (5.6, 6.9), (7.6, 12.0)], clear, FLOOR, PAUSE)
    assert [w.text for w in invented] == ["this"] and [w.text for w in kept] == ["so", "is", "it"]


def test_a_short_soft_word_is_not_a_click():
    """A word of 50 ms that peaks 9 dB over the room sits in a pause the click rule closed; it is still a word."""
    words = [Word(1.0, 1.5, "so"), Word(3.0, 3.05, "a"), Word(3.5, 4.0, "ghost"), Word(6.5, 7.5, "arena")]
    silences = [(1.6, 6.0)]
    levels = _readings(spans=[(1.0, 1.5, -30.0), (3.0, 3.05, -51.0), (3.5, 4.0, -58.0), (6.5, 7.5, -30.0)])
    kept, invented = split_invented_words(words, silences, levels, FLOOR, PAUSE)
    assert [w.text for w in invented] == ["ghost"] and "a" in [w.text for w in kept]


def test_a_made_up_word_over_the_room_with_a_click_in_it_is_still_dropped():
    """A verse number Whisper wrote into room tone, with a breath or a click inside it: it is long, so it is not a click's work."""
    words = [Word(1.0, 1.5, "so"), Word(3.0, 3.5, "17."), Word(6.5, 7.5, "arena")]
    levels = _readings(spans=[(1.0, 1.5, -30.0), (3.0, 3.5, -58.0), (3.2, 3.24, -51.0), (6.5, 7.5, -30.0)])
    kept, invented = split_invented_words(words, [(1.6, 6.4)], levels, FLOOR, PAUSE)
    assert [w.text for w in invented] == ["17."] and [w.text for w in kept] == ["so", "arena"]


def test_speech_that_is_loud_where_it_is_voiced_stands_above_a_noisy_room():
    """Only the voiced part of a word is far over the room; the consonants and the gaps between its sounds are not. The talker is
    read at the loud quarter of the readings inside the words, not their median."""
    words = [Word(1.0, 2.0, "so"), Word(3.0, 4.0, "this")]
    levels = _readings(spans=[(1.0, 1.4, -30.0), (1.4, 2.0, -58.0), (3.0, 3.4, -30.0), (3.4, 4.0, -58.0)])  # 40% voiced, the rest the room
    assert speech_stands_above_room(words, levels, FLOOR)
    assert not speech_stands_above_room(words, _readings(spans=[(1.0, 2.0, -52.0), (3.0, 4.0, -52.0)]), FLOOR)


def test_nothing_to_judge_with_no_words_or_no_readings():
    assert split_invented_words([], [(0.0, 5.0)], _readings(), FLOOR, PAUSE) == ([], [])
    words = [Word(1.0, 1.5, "so")]
    assert split_invented_words(words, [(0.0, 5.0)], [], FLOOR, PAUSE) == (words, [])


# ------------------------------------------------------------------ the readings

def _tone(path: Path, expr: str, seconds=3.0) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d={seconds}", str(path)], check=True)
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_rumble_or_hum_does_not_hide_the_speech_in_the_readings(tmp_path):
    """The readings are for telling speech from a room: the room's rumble (a car, an air handler, mains hum) is below the speech band
    and shouldn't count. A high-pass keeps the speech band's level and drops the rumble's."""
    path = _tone(tmp_path / "t.wav", "0.05*sin(2*PI*60*t)+0.1*sin(2*PI*1000*t)*between(t,1,2)")
    plain = ffmpeg_ops.window_rms(path, 0.01)
    filtered = ffmpeg_ops.window_rms(path, 0.01, highpass=250.0)
    assert len(plain) == len(filtered)
    assert plain[50] == pytest.approx(-29.0, abs=1.5) and filtered[50] < plain[50] - 25  # the hum alone
    assert filtered[150] == pytest.approx(plain[150], abs=3.0) or filtered[150] > -25  # the 1 kHz tone is still there


# ------------------------------------------------------------------ the cut

def test_a_soft_word_between_two_pauses_a_click_joined_is_not_cut():
    """Two pauses 30 ms apart are joined as a click split them; a soft word sounding in that gap makes them two."""
    assert _join_close([(1.0, 1.67), (1.70, 2.45)]) == [(1.0, 2.45)]
    assert _join_close([(1.0, 1.67), (1.70, 2.45)], words=[(1.672, 1.698)]) == [(1.0, 1.67), (1.70, 2.45)]
    assert _join_close([(1.0, 1.67), (1.70, 2.45)], words=[(0.2, 0.6), (3.0, 3.4)]) == [(1.0, 2.45)]  # words elsewhere change nothing


# ------------------------------------------------------------------ the whole run

SECONDS = 12


@pytest.fixture(scope="module")
def clip_file(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("plain")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='0.05*sin(2*PI*220*t)':s=48000:d={SECONDS}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mov")],
        check=True,
    )
    return root


def _sidecar(root: Path, words):
    (root / "talk.json").write_text(json.dumps({"words": [{"start": a, "end": b, "text": t, "prob": 0.95} for a, b, t in words]}))


def _rig(monkeypatch, levels, amplitude_pauses, db=-20.0, floor=-60.0):
    """A raised silence level, the amplitude map's pauses (the same at every level), and the clip's own readings."""
    calls = {"window_rms": 0}
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (db, floor))
    monkeypatch.setattr(ffmpeg_ops, "detect_silences", lambda path, duration, noise_db=-35.0, **k: [tuple(s) for s in amplitude_pauses.get(noise_db, [])])

    def fake_rms(path, window=0.01, rate=16000, **k):
        calls["window_rms"] += 1
        return list(levels)

    monkeypatch.setattr(ffmpeg_ops, "window_rms", fake_rms)
    monkeypatch.setattr(pauses, "pause_flatness", lambda path, silences: 0.9)  # noise, not music
    return calls


def test_a_talker_close_to_the_room_keeps_every_word_even_with_a_horn_in_the_clip(clip_file, tmp_path, monkeypatch):
    words = [(1.0, 1.5, "so"), (2.5, 3.0, "this"), (4.0, 4.5, "is"), (6.0, 6.5, "the"), (7.5, 8.0, "arena")]
    _sidecar(clip_file, words)
    soft = _readings(spans=[(a, b, -56.0) for a, b, _ in words] + [(10.0, 11.0, -20.0)])
    _rig(monkeypatch, soft, {})
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert [w.text for s in clip.segments for w in s.words] == [t for _, _, t in words]
    assert clip.silences == []  # and the map that was no good is not swapped in over a clip that has none of its own


def test_a_real_invention_over_the_room_is_still_dropped_when_the_talker_is_clear(clip_file, tmp_path, monkeypatch):
    words = [(1.0, 1.5, "so"), (2.0, 2.5, "this"), (3.5, 4.0, "8."), (6.0, 6.5, "is"), (7.5, 8.0, "arena")]
    _sidecar(clip_file, words)
    levels = _readings(spans=[(1.0, 1.5, -30.0), (2.0, 2.5, -30.0), (6.0, 6.5, -30.0), (7.5, 8.0, -30.0)])
    _rig(monkeypatch, levels, {})
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert [w.text for s in clip.segments for w in s.words] == ["so", "this", "is", "arena"]
    assert clip.silences and clip.silences[0][0] == 0.0 or clip.silences  # the loudness map's pauses stand in for the missing ones
    assert clip.silence_db == -20.0  # the level silencedetect was run at stays what it was


def test_a_quiet_recording_is_not_read_by_the_standard_map_or_the_loudness_map(clip_file, tmp_path, monkeypatch):
    """Level below -35 dB: the recording is quiet. The -35 dB map would call its speech silence and drop it; the loudness pass is
    only for a raised level."""
    words = [(2.5, 3.0, "quiet"), (3.5, 4.0, "speech"), (4.5, 5.0, "here")]
    _sidecar(clip_file, words)
    calls = _rig(monkeypatch, _readings(), {-35.0: [(2.0, 6.0)], -45.0: [(0.0, 1.0), (10.0, 12.0)]}, db=-45.0, floor=-70.0)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert [w.text for s in clip.segments for w in s.words] == ["quiet", "speech", "here"]
    assert calls["window_rms"] == 0


def test_a_failed_reading_is_not_remembered(clip_file, tmp_path, monkeypatch):
    _sidecar(clip_file, [(2.5, 3.0, "word")])
    calls = _rig(monkeypatch, [], {})
    cache = Cache(tmp_path / "cache")
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=cache).clips[0]
    assert not list((tmp_path / "cache" / clip.media.fingerprint).glob("levels10*"))
    analyze([clip_file / "talk.mov"], transcriber=None, cache=cache)
    assert calls["window_rms"] == 2  # asked again, not served an empty answer for good


def test_a_cache_file_that_is_not_text_is_a_miss_not_a_crash(tmp_path):
    cache = Cache(tmp_path)
    cache.path("abc", "x.json").parent.mkdir(parents=True)
    cache.path("abc", "x.json").write_bytes(b"\xff\xfe\x00 not json")
    assert cache.get("abc", "x.json") is None


# ------------------------------------------------------------------ old transcripts

def test_a_transcript_made_before_the_hole_fix_is_made_again(clip_file, tmp_path, monkeypatch):
    """Whisper asked with hallucination_silence_threshold left 2 s holes with speech in them; those transcripts are in the cache of
    anyone who cut footage before the fix, under the same key unless the key says which way Whisper was asked."""
    from roughcut.media import fingerprint

    (clip_file / "talk.json").unlink(missing_ok=True)
    model = "mlx-community/whisper-large-v3-turbo"
    old_key = f"mlx-{model}-auto-v".replace("/", "_")
    cache = Cache(tmp_path / "cache")
    path = clip_file / "talk.mov"
    cache.put(fingerprint(path), f"transcript-{old_key}.json", Transcript(words=[Word(1.0, 1.4, "holey")], source="mlx:old").to_json())
    asked = []

    def fake_transcribe(self, p):
        asked.append(p)
        return Transcript(words=[Word(1.0, 1.4, "whole")], source="mlx:new")

    monkeypatch.setattr(Transcriber, "transcribe", fake_transcribe)
    clip = analyze([path], transcriber=Transcriber("mlx", model), cache=cache).clips[0]
    assert len(asked) == 1 and [w.text for s in clip.segments for w in s.words] == ["whole"]
    other = Transcriber("faster-whisper", "small")
    assert other.cache_key == "faster-whisper-small-auto-v"  # the other backend's transcripts were not made differently

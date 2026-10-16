"""What an independent review of the loudness map found.

The map drops a word it calls invented, and a dropped word no longer protects the sound it was said in, so every wrong drop is
speech lost from the cut. These tests are the cases the review built: a talker close to the room, a soft short word, a room
that is mostly rumble or hum, a quiet recording, and the clocks that disagree about when a file's audio starts.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from roughcut import ffmpeg_ops, pauses
from roughcut.analyze import LEVEL_HIGHPASS, WHOLE_BAND_MAX_RISE, Analysis, Cache, LevelMap, analyze, read_level_map, room_for_words, whole_band_map
from roughcut.segments import speech_over_room, speech_stands_above_room, split_invented_words
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


def _rig(monkeypatch, levels, amplitude_pauses, db=-20.0, floor=-60.0, high=None):
    """A raised silence level, the amplitude map's pauses (the same at every level), and the clip's own readings: `levels` read over the
    whole band, `high` (default: the same) read with the rumble filtered out."""
    calls = {"window_rms": 0}
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (db, floor))
    monkeypatch.setattr(ffmpeg_ops, "detect_silences", lambda path, duration, noise_db=-35.0, **k: [tuple(s) for s in amplitude_pauses.get(noise_db, [])])

    def fake_rms(path, window=0.01, rate=16000, highpass=None, **k):
        calls["window_rms"] += 1
        return list(levels if highpass is None or high is None else high)

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
    first = calls["window_rms"]
    analyze([clip_file / "talk.mov"], transcriber=None, cache=cache)
    assert first >= 1 and calls["window_rms"] == 2 * first  # asked again, not served an empty answer for good


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


# ------------------------------------------------------------------ how a noisy clip's loudness is read

SPEECH = [(1.0, 1.5), (2.0, 2.5), (6.0, 6.5), (7.5, 8.0)]
GOOD_WORDS = [(1.0, 1.5, "so"), (2.0, 2.5, "this"), (6.0, 6.5, "is"), (7.5, 8.0, "arena")]
WORDS_WITH_A_MADE_UP_NUMBER = [(1.0, 1.5, "so"), (2.0, 2.5, "this"), (3.5, 4.0, "8."), (6.0, 6.5, "is"), (7.5, 8.0, "arena")]


def _kept(clip):
    return [w.text for s in clip.segments for w in s.words]


def test_a_noisy_clip_is_read_with_the_rumble_filtered_out_and_over_the_whole_band_where_that_finds_no_pauses(clip_file, tmp_path, monkeypatch):
    """A rumble hides the speech in the whole band and not in the speech band; broadband wind is the other way about (a high-pass
    takes a deep voice's low end with it and leaves the wind)."""
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    # Rumble: the whole band has the speech 6 dB over the room (rumble -54 dB, speech -48), the speech band 30 dB over.
    whole = _readings(fill=-54.0, spans=[(a, b, -48.0) for a, b in SPEECH])
    speech_band = _readings(fill=-60.0, spans=[(a, b, -30.0) for a, b in SPEECH])
    calls = _rig(monkeypatch, whole, {}, high=speech_band)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "rumble")).clips[0]
    assert _kept(clip) == ["so", "this", "is", "arena"]  # the speech band tells the room from the speech: the made-up number goes
    assert calls["window_rms"] == 1  # and the whole band was never read
    # Wind: the whole band separates the speech (30 dB), the high-passed one has lost the voice (3 dB over the wind).
    whole = _readings(fill=-60.0, spans=[(a, b, -30.0) for a, b in SPEECH])
    filtered = _readings(fill=-60.0, spans=[(a, b, -57.0) for a, b in SPEECH])
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "wind")).clips[0]
    assert _kept(clip) == ["so", "this", "is", "arena"]
    assert clip.silences and any(s[0] < 1.0 for s in clip.silences)  # and its pauses are the whole band's
    assert calls["window_rms"] == 2
    stored = sorted(p.name for p in (tmp_path / "wind" / clip.media.fingerprint).glob("*levels*"))
    assert stored == ["flatness-levels-whole-v1.json", "levels10-v3.json", "levels10-whole-v1.json"]  # judged on the whole band's pauses, under its own name


def test_speech_over_room_is_the_loud_quarter_of_the_readings_under_the_words_over_the_floor():
    words = [Word(1.0, 1.5, "so"), Word(3.0, 3.5, "this")]
    levels = _readings(spans=[(1.0, 1.5, -30.0), (3.0, 3.5, -30.0)])
    assert speech_over_room(words, levels, FLOOR) == pytest.approx(30.0, abs=0.01)
    assert speech_over_room([], levels, FLOOR) is None
    assert speech_over_room(words, [], FLOOR) is None


def _scene(horn_in_filtered=True, filtered_speech=-52.0, filtered_floor=-60.0, whole_speech=-46.0, whole_floor=-60.0, truck_under_loud_words=False,
           soft=None):
    """Both readings of a clip: its four words (the last two softer by `soft` dB when given) and a loud event at 10.0-11.5 s in the
    filtered readings (so that they find a map, whatever the words are), the whole band's room at `whole_floor`."""
    filtered = _readings(fill=filtered_floor, spans=[(a, b, filtered_speech - (soft or 0.0) * (i >= 2)) for i, (a, b) in enumerate(SPEECH)] + ([(10.0, 11.5, filtered_floor + 18.0)] if horn_in_filtered else []))
    whole = _readings(fill=whole_floor, spans=[(a, b, (-20.0 if truck_under_loud_words and i < 2 else whole_speech - (soft or 0.0) * (i >= 2))) for i, (a, b) in enumerate(SPEECH)])
    return filtered, whole


def test_the_whole_band_is_not_even_read_while_the_filtered_reading_can_tell_the_words_from_the_room(clip_file, tmp_path, monkeypatch):
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(filtered_speech=-45.0, whole_speech=-20.0)  # the words 15 dB over the room in the filtered readings
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "easy")).clips[0]
    assert _kept(clip) == ["so", "this", "is", "arena"] and calls["window_rms"] == 1


def test_a_loud_low_sound_over_the_whole_band_does_not_make_it_the_better_reading(clip_file, tmp_path, monkeypatch):
    """A steady rumble (the whole band's room at -48 dB, the filtered room at -60) and a truck going by under the loud words: over the
    whole band the loud quarter of the words' readings is the truck, so by that alone the whole band stands the talker 28 dB over
    the room, and its pause level (6 dB over the rumble) holds the talker's soft words, which stand 8 dB over the room that matters.
    They are real words, and the filtered reading, where the talker stands 14 dB over the room, keeps them."""
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(horn_in_filtered=False, filtered_speech=-46.0, whole_floor=-48.0, whole_speech=-46.0, truck_under_loud_words=True, soft=None)
    # the last two words are the soft ones: 6 dB under in the filtered readings; in the whole band the rumble covers them
    filtered = _readings(fill=-60.0, spans=[(1.0, 1.5, -46.0), (2.0, 2.5, -46.0), (6.0, 6.5, -52.0), (7.5, 8.0, -52.0)])
    whole = _readings(fill=-48.0, spans=[(1.0, 1.5, -20.0), (2.0, 2.5, -20.0), (6.0, 6.5, -46.0), (7.5, 8.0, -46.0)])
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "truck")).clips[0]
    assert _kept(clip) == ["so", "this", "is", "arena"]  # the soft words stay; the made-up number is the filtered room's, and goes
    assert calls["window_rms"] == 1


def test_the_whole_band_stands_in_where_the_filtered_reading_cannot_tell_the_words_from_the_room(clip_file, tmp_path, monkeypatch):
    """The words stand 8 dB over the room in the filtered readings (a loud event gives them a map) and 14 dB over it in the whole
    band, whose room is as loud as the filtered one: wind, not rumble. The made-up number is the room's."""
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(filtered_speech=-52.0, whole_speech=-46.0)
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "wind")).clips[0]
    assert _kept(clip) == ["so", "this", "is", "arena"] and calls["window_rms"] == 2


def test_the_whole_band_is_no_fallback_where_the_filter_took_a_rumble_out_of_the_room(clip_file, tmp_path, monkeypatch):
    """The same words, but the whole band's room is 12 dB louder than the filtered one: that is rumble, and over it the words only
    look clear of the room because something low and loud goes by under them. Every word is kept, the made-up number included."""
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(filtered_speech=-52.0, whole_floor=-48.0, whole_speech=-20.0)
    _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "rumble")).clips[0]
    assert _kept(clip) == ["so", "this", "8.", "is", "arena"]


def test_the_words_guard_is_twelve_dB_to_the_dot(clip_file, tmp_path, monkeypatch):
    def used(speech_db, name):
        """Is the clip's loudness map put to use? Its pauses stand in for silencedetect's (none here), and the made-up number goes."""
        _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
        filtered, whole = _scene(filtered_speech=speech_db, whole_speech=speech_db)
        _rig(monkeypatch, whole, {}, high=filtered)
        clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / name)).clips[0]
        assert ("8." not in _kept(clip)) == bool(clip.silences)
        return bool(clip.silences)

    assert used(-47.0, "thirteen")  # 13 dB over the room
    assert used(-48.0, "twelve")  # exactly 12: enough
    assert not used(-49.0, "eleven")  # 11: the loudness can't tell the talker from the room, and none of it is used


def test_a_clip_with_no_words_takes_the_filtered_map_when_there_is_one(clip_file, tmp_path, monkeypatch):
    (clip_file / "talk.json").unlink(missing_ok=True)
    filtered, whole = _scene(filtered_speech=-52.0, whole_speech=-46.0)
    filtered = _readings(spans=[(a, b, -46.0) for a, b in SPEECH] + [(10.0, 10.6, -46.0)])
    whole = _readings(spans=[(a, b, -46.0) for a, b in SPEECH])
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "nowords")).clips[0]
    assert clip.transcript_source == "silence-map" and not [s for s in clip.silences if s[0] < 10.3 < s[1]]  # the cough splits the pause: the filtered map
    assert calls["window_rms"] == 1


def test_read_level_map_takes_the_filtered_reading_and_the_whole_band_only_where_that_finds_no_pauses():
    read = lambda filtered, whole: (lambda hp: filtered if hp else whole)  # noqa: E731
    clear = _readings(spans=[(a, b, -40.0) for a, b in SPEECH])
    blurred = _readings(spans=[(a, b, -57.0) for a, b in SPEECH])
    first = read_level_map(read(clear, blurred), 12.0)
    assert first.highpass == LEVEL_HIGHPASS and first.found["silences"]
    first = read_level_map(read(blurred, clear), 12.0)  # the filter lost the voice, the room is as loud either way
    assert first.highpass is None and first.found["silences"]
    loud_rumble = _readings(fill=-60.0 + WHOLE_BAND_MAX_RISE + 3.0, spans=[(a, b, -20.0) for a, b in SPEECH])
    assert read_level_map(read(blurred, loud_rumble), 12.0) is None  # the whole band's room is rumble: no way of reading this one
    assert read_level_map(read([-50.0] * 100, [-50.0] * 100), 1.0) is None  # under two seconds
    assert read_level_map(read(blurred, blurred), 12.0) is None


def test_a_map_that_finds_no_pauses_is_no_map():
    """Speech that never leaves a quiet stretch of 0.35 s: the loudness can tell speech from room, and there is no pause to cut."""
    always = _readings(spans=[(0.0, 12.0, -40.0)])
    always = [v if (i % 100) else -60.0 for i, v in enumerate(always)]  # a 10 ms dip every second
    assert read_level_map(lambda hp: always, 12.0) is None
    assert whole_band_map(lambda hp: always, 12.0, None) is None


def test_the_whole_band_needs_the_filter_to_have_taken_little_of_the_room_out():
    clear = _readings(fill=-60.0, spans=[(a, b, -30.0) for a, b in SPEECH])
    louder = _readings(fill=-60.0 + WHOLE_BAND_MAX_RISE, spans=[(a, b, -30.0) for a, b in SPEECH])
    assert whole_band_map(lambda hp: clear, 12.0, -60.0) is not None
    assert whole_band_map(lambda hp: louder, 12.0, -60.0) is not None  # up to the line
    assert whole_band_map(lambda hp: _readings(fill=-60.0 + WHOLE_BAND_MAX_RISE + 1.0, spans=[(a, b, -30.0) for a, b in SPEECH]), 12.0, -60.0) is None
    assert whole_band_map(lambda hp: clear, 12.0, None) is not None  # no filtered room to compare with


def test_room_for_words_keeps_the_first_reading_unless_it_cannot_tell_the_words_from_the_room():
    words = [Word(a, b, "w") for a, b in SPEECH]
    filtered, whole = _scene(filtered_speech=-52.0, whole_speech=-46.0)  # 8 dB over the room filtered, 14 whole
    read = lambda hp: filtered if hp else whole  # noqa: E731
    first = read_level_map(read, 12.0)
    assert first.highpass == LEVEL_HIGHPASS
    lm, over, levels = room_for_words(first, words, read, 12.0)
    assert lm.highpass is None and over == pytest.approx(14.0, abs=0.6) and levels is whole
    strong, _ = _scene(filtered_speech=-45.0, whole_speech=-20.0)  # 15 dB over the room filtered: the whole band, however loud, is not asked
    lm, over, levels = room_for_words(read_level_map(lambda hp: strong, 12.0), words, lambda hp: strong if hp else whole, 12.0)
    assert lm.highpass == LEVEL_HIGHPASS and over == pytest.approx(15.0, abs=0.6) and levels is strong
    lm, over, _ = room_for_words(first, [], read, 12.0)  # no words: nothing to ask, the first reading stands, with no margin to show
    assert lm is first and over is None
    lm, over, _ = room_for_words(first, words, lambda hp: filtered if hp else _readings(fill=-48.0, spans=[(a, b, -20.0) for a, b in SPEECH]), 12.0)
    assert lm is first and over == pytest.approx(8.0, abs=0.6)  # a rumble over the whole band is not a way out


def test_room_for_words_reads_the_words_where_the_audio_is_when_it_starts_late():
    """A file whose audio starts 0.5 s after the picture: the readings count from the audio's first sample, the words from the file's."""
    words = [Word(a + 0.5, b + 0.5, "w") for a, b in SPEECH]
    clear = _readings(spans=[(a, b, -45.0) for a, b in SPEECH])
    first = read_level_map(lambda hp: clear, 11.5)
    lm, over, _ = room_for_words(first, words, lambda hp: clear, 11.5, offset=0.5)
    assert lm is first and over == pytest.approx(15.0, abs=0.6)  # the first reading tells them from the room
    assert room_for_words(first, words, lambda hp: clear, 11.5, offset=0.0)[1] < ffmpeg_ops.LEVEL_MIN_SEPARATION  # without it they are read in the room


def test_each_reading_is_cached_under_its_own_name_and_an_empty_one_is_read_again(clip_file, tmp_path, monkeypatch):
    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(filtered_speech=-52.0, whole_speech=-46.0)  # the whole band is asked for
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    cache = Cache(tmp_path / "names")
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=cache).clips[0]
    folder = tmp_path / "names" / clip.media.fingerprint
    assert sorted(p.name for p in folder.glob("*levels*")) == ["flatness-levels-v2.json", "levels10-v3.json", "levels10-whole-v1.json"]
    stored = json.loads((folder / "levels10-v3.json").read_text())["db"]
    assert any(v != round(v) for v in stored)  # in tenths of a dB: a whole dB moved a map across the line it is judged by
    analyze([clip_file / "talk.mov"], transcriber=None, cache=cache)
    assert calls["window_rms"] == 2  # the second run read nothing
    (folder / "levels10-whole-v1.json").write_text(json.dumps({"window": 0.01, "highpass": None, "db": []}))  # a read that failed, remembered
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=cache).clips[0]
    assert calls["window_rms"] == 3 and json.loads((folder / "levels10-whole-v1.json").read_text())["db"]  # asked for again, and kept
    assert "8." not in _kept(clip)


def test_readings_an_earlier_release_cached_are_not_trusted(clip_file, tmp_path, monkeypatch):
    """Whole-dB readings (levels10-v2.json) and a failed whole-band read (levels10-v1.json with no readings) are under names this
    release does not read."""
    from roughcut.media import fingerprint

    _sidecar(clip_file, WORDS_WITH_A_MADE_UP_NUMBER)
    filtered, whole = _scene(filtered_speech=-52.0, whole_speech=-46.0)
    cache = Cache(tmp_path / "old")
    fp = fingerprint(clip_file / "talk.mov")
    cache.put(fp, "levels10-v2.json", {"window": 0.01, "highpass": 250.0, "db": [-30] * 1200})
    cache.put(fp, "levels10-v1.json", {"window": 0.01, "db": []})
    calls = _rig(monkeypatch, whole, {}, high=filtered)
    clip = analyze([clip_file / "talk.mov"], transcriber=None, cache=cache).clips[0]
    assert "8." not in _kept(clip) and calls["window_rms"] == 2


def test_the_choice_through_real_audio_a_truck_over_a_rumble_does_not_cost_a_soft_word(tmp_path):
    """The case an independent review found, in sound: a steady rumble under 90 Hz, a talker whose last words of each sentence are
    6 dB softer, and a truck going by (more low sound, much louder) under most of the talking. Over the whole band the truck makes
    the talker look 33 dB over the room and the rumble's pause level holds his soft words; the filtered reading has them 12 dB over
    a room 14 dB quieter."""
    words, t = [], 1.0
    for sentence in range(8):
        for k in range(5):
            d = 0.30 + 0.05 * ((sentence + k) % 5)
            words.append((round(t, 3), round(t + d, 3), f"w{sentence}{k}"))
            t += d + 0.08
        t += 1.3
    seconds = 45
    gate = "+".join(f"between(t,{a},{b})" for a, b, _ in words)
    soft_gate = "+".join(f"between(t,{a},{b})" for i, (a, b, _) in enumerate(words) if i % 5 >= 3)
    voice = f"0.3*(sin(2*PI*700*t)+0.5*sin(2*PI*1400*t+1))*(0.7+0.3*sin(2*PI*5*t))*({gate})*(1-{1 - 10 ** (-6 / 20):.4f}*({soft_gate}))"

    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True)

    def rms_db(path):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        return float(re.findall(r"RMS level dB:\s*(-?[\d.]+)", out)[-1])

    def gain_to(name, source, filt, target_db):
        path = tmp_path / f"{name}.wav"
        ffmpeg("-f", "lavfi", "-i", source, "-af", filt, "-c:a", "pcm_s16le", str(path))
        return target_db - rms_db(path)

    duty = sum(b - a for a, b, _ in words) / seconds
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc=exprs='{voice}':s=48000:d={seconds}", "-c:a", "pcm_s16le", str(tmp_path / "voice.wav"))
    g_voice = -44.0 - (rms_db(tmp_path / "voice.wav") - 10 * math.log10(duty))
    g_rumble = gain_to("rumble", f"anoisesrc=c=brown:a=1:d={seconds}:r=48000:s=1", "lowpass=f=90,lowpass=f=90", -45.0)
    g_hiss = gain_to("hiss", f"anoisesrc=c=white:a=1:d={seconds}:r=48000:s=2", "anull", -57.0)
    g_truck = gain_to("truck", f"anoisesrc=c=brown:a=1:d={seconds}:r=48000:s=3", "lowpass=f=120,lowpass=f=120", -16.0) - 20 * math.log10(math.sqrt(0.375))
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc=exprs='{voice}':s=48000:d={seconds}",
           "-f", "lavfi", "-i", f"anoisesrc=c=brown:a=1:d={seconds}:r=48000:s=1",
           "-f", "lavfi", "-i", f"anoisesrc=c=white:a=1:d={seconds}:r=48000:s=2",
           "-f", "lavfi", "-i", f"anoisesrc=c=brown:a=1:d={seconds}:r=48000:s=3",
           "-filter_complex",
           f"[0:a]volume={g_voice}dB[v];[1:a]lowpass=f=90,lowpass=f=90,volume={g_rumble}dB[rum];[2:a]volume={g_hiss}dB[hiss];"
           f"[3:a]lowpass=f=120,lowpass=f=120,volume={g_truck}dB,volume='between(t,6,30)*(0.5-0.5*cos(2*PI*(t-6)/24))':eval=frame[truck];"
           "[v][rum][hiss][truck]amix=inputs=4:normalize=0[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(tmp_path / "mix.wav"))
    folder = tmp_path / "truck"
    folder.mkdir()
    ffmpeg("-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={seconds}", "-i", str(tmp_path / "mix.wav"), "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(folder / "talk.mov"))
    _sidecar(folder, words)
    clip = analyze([folder / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert clip.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB  # a noisy room
    assert _kept(clip) == [w for _, _, w in words]  # not one word of the talker's is left out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_deep_voice_in_wind_keeps_the_loudness_map_the_filter_would_have_lost(tmp_path):
    """White noise (wind) reaches every frequency, and a 250 Hz high-pass takes a deep voice's fundamental with it: the voice stands
    3 dB over the room in the filtered readings and 20 dB over it in the whole band. The map has to come from the whole band, or the
    number Whisper wrote into the room tone stays in the cut."""
    speech = [(1.0, 1.5), (2.0, 2.5), (6.0, 6.5), (7.5, 8.0)]
    gates = "+".join(f"between(t,{a},{b})" for a, b in speech)
    voice = f"0.2*sin(2*PI*140*t)*(0.7+0.3*sin(2*PI*3*t))*({gates})"
    path = tmp_path / "talk.mov"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{voice}':s=48000:d={SECONDS}",
         "-f", "lavfi", "-i", f"anoisesrc=c=white:a=0.04:d={SECONDS}:r=48000:s=3",
         "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )
    assert ffmpeg_ops.level_silences(ffmpeg_ops.window_rms(path, 0.01, highpass=250.0), float(SECONDS)) is None  # the filter lost the voice
    assert ffmpeg_ops.level_silences(ffmpeg_ops.window_rms(path, 0.01), float(SECONDS)) is not None  # the whole band has it
    _sidecar(tmp_path, WORDS_WITH_A_MADE_UP_NUMBER)
    clip = analyze([path], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    assert clip.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB  # a noisy room
    assert _kept(clip) == ["so", "this", "is", "arena"]  # the made-up number is the room's, and gone

"""A file whose audio starts after the picture: one clock for every time in the analysis, the file's own.

A trimmed copy (a stream copy cut at a keyframe, a QuickTime trim) can have its audio begin half a second or more after the
picture. ffmpeg's silencedetect and a transcript made by a person count from the file's clock; the speech engine's words and the
10 ms and 100 ms loudness readings count samples from the first one decoded. Left as they were, the pauses and the words of such
a file disagreed by the offset, and every cut landed that much early or late.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from roughcut import ffmpeg_ops, pauses
from roughcut.analyze import Cache, analyze
from roughcut.cutcheck import Edge, judge_detail, quiet_runs
from roughcut.ffmpeg_ops import level_summary
from roughcut.media import MediaInfo, probe
from roughcut.transcribe import Transcriber, Transcript, Word

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

OFFSET = 0.5
SENTENCES = [(1.0, 4.0, "So this is the new arena district coming to Anaheim."), (6.0, 9.5, "It's going to have restaurants and a music venue.")]


def _run(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _noisy_wav(path: Path, seconds=12) -> Path:
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    speech = f"0.5*sin(2*PI*220*t)*(0.7+0.3*sin(2*PI*3*t))*({gates})"
    _run("-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d={seconds}", "-f", "lavfi", "-i", f"anoisesrc=c=pink:a=0.06:d={seconds}:r=48000",
         "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(path))
    return path


def _movie(path: Path, wav: Path, offset: float, seconds=12.5) -> Path:
    """A picture from 0 and the audio from `offset`, as a trimmed copy leaves them."""
    _run("-f", "lavfi", "-i", f"color=c=gray:size=160x90:rate=25:duration={seconds}", "-itsoffset", str(offset), "-i", str(wav),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
    return path


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp("clock")
    wav = _noisy_wav(root / "base.wav")
    late = root / "late"
    late.mkdir()
    on_time = root / "ontime"
    on_time.mkdir()
    _movie(late / "talk.mov", wav, OFFSET)
    _movie(on_time / "talk.mov", wav, 0.0)
    return late, on_time


# ------------------------------------------------------------------ the probe

def test_the_probe_reads_when_the_audio_starts_and_says_so(files):
    late, on_time = files
    info = probe(late / "talk.mov")
    assert info.audio_start == pytest.approx(OFFSET, abs=0.03)
    assert any("audio starts" in w and "allowed for" in w for w in info.warnings)
    straight = probe(on_time / "talk.mov")
    assert straight.audio_start == 0.0 and not any("audio starts" in w for w in straight.warnings)
    assert MediaInfo.from_json(info.to_json()).audio_start == info.audio_start
    older = info.to_json()
    older.pop("audio_start")
    assert MediaInfo.from_json(older).audio_start == 0.0  # a reading made before the field existed


# ------------------------------------------------------------------ the words

def _stub(monkeypatch, words):
    asked = []

    def fake(self, p):
        asked.append(p)
        return Transcript(words=list(words), source="stub")

    monkeypatch.setattr(Transcriber, "transcribe", fake)
    return asked


def test_the_speech_engines_words_are_moved_onto_the_files_clock(files, tmp_path, monkeypatch):
    late, _ = files
    asked = _stub(monkeypatch, [Word(1.0, 1.4, "hello"), Word(2.0, 2.4, "there")])
    cache = Cache(tmp_path / "cache")
    for _ in range(2):  # and the same from the cache, which keeps the words as the engine made them
        clip = analyze([late / "talk.mov"], transcriber=Transcriber("faster-whisper", "x"), cache=cache).clips[0]
        starts = [round(w.start, 2) for s in clip.segments for w in s.words]
        assert starts == [pytest.approx(1.0 + OFFSET, abs=0.03), pytest.approx(2.0 + OFFSET, abs=0.03)]
    assert len(asked) == 1


def test_a_transcript_a_person_made_is_already_on_the_files_clock(files, tmp_path):
    late, _ = files
    (late / "talk.json").write_text(json.dumps({"words": [{"start": 1.5, "end": 1.9, "text": "hello", "prob": 0.9}]}))
    try:
        clip = analyze([late / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
        assert [w.start for s in clip.segments for w in s.words] == [1.5]
    finally:
        (late / "talk.json").unlink()


# ------------------------------------------------------------------ the pauses

def test_the_loudness_maps_pauses_are_on_the_files_clock(files, tmp_path, monkeypatch):
    """The speech sounds 1-4 and 6-9.5 s into the audio, so 1.5-4.5 and 6.5-10 s into the file; the pause between them is 4.5-6.5."""
    late, _ = files
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (-3.0, -30.0))  # silencedetect finds no pauses: the loudness map stands in
    (late / "talk.json").write_text(json.dumps({"words": [{"start": 1.5, "end": 1.9, "text": "So", "prob": 0.9}, {"start": 6.6, "end": 7.0, "text": "It's", "prob": 0.9}]}))
    try:
        clip = analyze([late / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    finally:
        (late / "talk.json").unlink()
    inner = [s for s in clip.silences if 4.0 < s[0] < 5.0]
    assert len(inner) == 1 and inner[0][0] == pytest.approx(4.5, abs=0.1) and inner[0][1] == pytest.approx(6.5, abs=0.1)


def test_silencedetect_and_the_loudness_map_agree_about_when_the_pause_begins(files, tmp_path, monkeypatch):
    late, on_time = files
    path = late / "talk.mov"
    sd = [s for s in ffmpeg_ops.detect_silences(path, 12.5, -25.0) if 4.0 < s[0] < 5.0]
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (-25.0, -30.0))
    clip = analyze([path], transcriber=None, cache=Cache(tmp_path / "cache")).clips[0]
    level = [s for s in clip.silences if 4.0 < s[0] < 5.0]
    assert sd and level and abs(sd[0][0] - level[0][0]) < 0.2


# ------------------------------------------------------------------ the loudness blocks

def test_the_loudness_summary_counts_from_the_files_clock():
    # The sound is 1.0-2.0 s into the audio: blocks of 100 ms, 11 s of audio, 400 ms momentary windows.
    m = [-120.0] * 110
    for i in range(10, 25):
        m[i] = -20.0
    blocks = {"step": 0.1, "m": m, "peak": [-6.0 if v > -100 else -120.0 for v in m]}
    plain = level_summary(blocks, spans=[(1.0, 2.0)], duration=20.0)
    late = level_summary(blocks, spans=[(1.5, 2.5)], duration=20.0, offset=0.5)
    assert plain["speech"] == late["speech"] is not None  # the same blocks, found by where the words are on the file's clock
    assert late["end"] == pytest.approx(plain["end"] + 0.5, abs=0.01)


# ------------------------------------------------------------------ the check

def test_roughcut_check_reads_the_audio_on_the_files_clock():
    levels = [-90.0] * 600
    for i in range(100, 150):
        levels[i] = -12.0  # sound 1.0-1.5 s into the audio, so 3.5-4.0 into the file when the audio starts at 2.5
    edge = Edge("ends", "/f/talk.mov", 3.7, Fraction(0), "talk")
    assert judge_detail([edge], levels).found == []  # read from sample 0 the edit would be in the quiet
    (found,) = judge_detail([edge], levels, offset=2.5).found
    assert found[0].source == 3.7 and found[1] == pytest.approx(0.3, abs=0.02)
    assert judge_detail([Edge("ends", "/f/talk.mov", 1.2, Fraction(0), "talk")], levels, offset=2.5).found == []  # before the audio began
    path = "/f/talk.mov"
    (run,) = quiet_runs([(path, 2.5, 6.0, Fraction(0))], {path: levels}, {path: -90.0}, offsets={path: 2.5}, at_least=1.0)
    assert run[0] == pytest.approx(2.0, abs=0.02) and run[1] == Fraction(3, 2) and run[3] == pytest.approx(4.0, abs=0.01)  # 1.5 s into the cut, 4.0 into the file

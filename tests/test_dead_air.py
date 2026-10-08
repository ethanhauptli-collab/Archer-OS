"""One clip in, "take out the dead space", and no cuts came back.

Two causes, both covered here: a fixed -35 dB silence level finds no pauses
when the background (room, wind, crowd) is louder than that, and Whisper
stretches words across the pauses after them, so the words' own times show
no gap to cut.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import make_clip, spread_words

from roughcut import ffmpeg_ops
from roughcut.analyze import Analysis, Cache, analyze, pause_warning
from roughcut.cli import main
from roughcut.segments import build_segments
from roughcut.timeline import STYLES, speech_pieces
from roughcut.transcribe import Word

SENTENCES = [
    (1.0, 4.0, "So this is the new arena district coming to Anaheim."),
    (6.0, 9.5, "It's going to have restaurants and a music venue."),
    (12.5, 14.0, "Pretty incredible."),
    (15.0, 16.5, "And honestly"),
    (18.0, 20.5, "it isn't even built yet."),
]
SECONDS = 22


def whisper_words(sentences) -> list[dict]:
    """Word times the way Whisper often gives them: each word runs up to the next one."""
    words = [w for s, e, text in sentences for w in spread_words(s, e, text)]
    for a, b in zip(words, words[1:]):
        a["end"] = round(b["start"] - 0.01, 3)
    return words


@pytest.fixture(scope="module")
def noisy_talk(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("noisy")
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    speech = f"0.5*sin(2*PI*220*t)*(0.55+0.45*sin(2*PI*4*t))*({gates})"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d={SECONDS}",
         "-f", "lavfi", "-i", f"anoisesrc=c=pink:a=0.06:d={SECONDS}:r=48000",  # a loud room: peaks near -30 dB
         "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(root / "talk.mov")],
        check=True,
    )
    (root / "talk.json").write_text(json.dumps({"words": whisper_words(SENTENCES)}))
    return root


@pytest.mark.parametrize(
    "floor,loud,check",
    [
        (-60, -6, lambda t: t == -35.0),  # clean recording: unchanged
        (-30, -6, lambda t: -27 < t < -18),  # loud room: above the background, under the speech
        (-65, -32, lambda t: -60 < t <= -44),  # very quiet recording: under the speech
        (-20, -15, lambda t: t == -35.0),  # background as loud as everything: can't tell, keep default
    ],
)
def test_silence_level_follows_the_background(floor, loud, check):
    peaks = [floor + (i % 3) * 0.5 for i in range(240)] + [loud - (i % 5) for i in range(360)]
    level, background = ffmpeg_ops.noise_threshold(peaks)
    assert check(level), level
    assert background is not None and abs(background - floor) <= 1.5


def test_too_short_to_judge_keeps_the_default():
    assert ffmpeg_ops.noise_threshold([-60.0] * 10) == (-35.0, None)


def test_a_pause_hidden_by_stretched_words_is_still_cut():
    clip = make_clip("V01", seconds=SECONDS, role="aroll")
    words = [Word(**w) for w in whisper_words([(15.0, 16.5, "And honestly"), (18.0, 20.5, "it isn't even built yet.")])]
    clip.segments = build_segments("V01", words)
    assert len(clip.segments) == 1  # no gap in the word times, so one sentence
    style = STYLES["medium"]
    assert len(speech_pieces(clip.segments[0], clip, style, 0)) == 1
    clip.silences = [(16.55, 17.95)]  # what the silence map hears
    first, second = speech_pieces(clip.segments[0], clip, style, 0)
    assert first.wb == pytest.approx(16.55)  # pulled back from the stretched end to where the sound stops
    assert second.wa == pytest.approx(18.0)
    assert second.a - first.b > 1.0  # the pause is gone


def test_noisy_clip_with_whisper_timing_gets_its_pauses_cut(noisy_talk, tmp_path, capsys):
    clip_path = noisy_talk / "talk.mov"
    # The bug: at the old fixed level this clip has no silence at all.
    assert ffmpeg_ops.detect_silences(clip_path, SECONDS, noise_db=-35.0) == []

    out = tmp_path / "out"
    assert main(["build", str(noisy_talk), "--provider", "none", "--transcriber", "none", "--no-menu",
                 "-o", str(out), "--cache-dir", str(tmp_path / "cache")]) == 0
    printed = capsys.readouterr().out
    clip = Analysis.load(out / "analysis.json").clips[0]
    assert clip.silence_db > -30 and clip.background_db > -35
    assert len(clip.silences) >= 4
    removed = next(line for line in printed.splitlines() if line.startswith("Dead air removed:"))
    pct = int(removed.rsplit("(", 1)[1].rstrip("%)"))
    assert pct >= 25, removed  # 12 s of speech in 22 s of footage
    assert "Dead air removed:" in (out / "edit_report.md").read_text()


def test_a_level_that_swallows_words_falls_back(noisy_talk, tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (-3.0, -30.0))  # absurd: above the speech
    analysis = analyze([noisy_talk / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache"))
    clip = analysis.clips[0]
    assert clip.silence_db == -35.0
    assert sum(len(s.words) for s in clip.segments) == len(whisper_words(SENTENCES))


def test_talking_clip_without_pauses_says_why():
    clip = make_clip("V01", seconds=30, sentences=[(0.5, 29.5, "word " * 60)])
    clip.background_db = -28.0
    warning = pause_warning(clip)
    assert "almost no pauses" in warning and "-28 dB" in warning
    clip.silences = [(10.0, 12.0)]
    assert pause_warning(clip) is None

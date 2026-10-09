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


def test_a_raised_level_never_deletes_or_cuts_real_words(noisy_talk, tmp_path, monkeypatch):
    """Promos with a music bed got levels near -14 dB: quiet words under the music must survive."""
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import build_timeline

    monkeypatch.setattr(ffmpeg_ops, "noise_threshold", lambda peaks: (-3.0, -30.0))  # absurd: above the speech
    analysis = analyze([noisy_talk / "talk.mov"], transcriber=None, cache=Cache(tmp_path / "cache"))
    clip = analysis.clips[0]
    assert clip.silence_db == -3.0
    assert sum(len(s.words) for s in clip.segments) == len(whisper_words(SENTENCES))  # nothing dropped
    tl = build_timeline(heuristic_plan(analysis), analysis, style=STYLES["medium"], name="t")
    kept = [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]
    for start, end, _ in SENTENCES:  # every sentence is still in the cut
        assert any(a <= start + 0.1 and b >= end - 0.1 for a, b in kept), (start, end, kept)


def test_quiet_word_inside_a_silence_is_not_cut():
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import build_timeline

    clip = make_clip("V01", seconds=8, role="aroll")
    # "all" is spoken softly under the music; a raised level hears 2.2-3.0 as silence.
    clip.segments = build_segments("V01", [
        Word(1.0, 1.4, "We"), Word(1.4, 1.8, "built"), Word(1.8, 2.2, "it"),
        Word(2.3, 2.9, "all"), Word(3.0, 3.6, "together."),
    ])
    clip.silences = [(2.2, 3.0)]
    analysis = Analysis(clips=[clip])
    tl = build_timeline(heuristic_plan(analysis), analysis, style=STYLES["medium"], name="t")
    kept = [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]
    assert any(a <= 2.3 and b >= 2.9 for a, b in kept), kept


def test_talking_clip_without_pauses_says_why():
    clip = make_clip("V01", seconds=30, sentences=[(0.5, 29.5, "word " * 60)])
    clip.background_db = -28.0
    warning = pause_warning(clip)
    assert "almost no pauses" in warning and "-28 dB" in warning
    clip.silences = [(10.0, 12.0)]
    assert pause_warning(clip) is None


# The report after the first fix: "a solid gap between 10-14 seconds, nothing is being cut".
# Whisper's word times are contiguous (no gaps at all), and the word next to a pause is
# stretched over it with its boundary drifting a little past the silence. The old clamps
# then failed and the two halves were merged back together.

GAP = (10.1, 14.0)  # what the silence map hears


def gap_clip(boundary: float, sentence_break: bool):
    a_text = "So this is the new arena district coming to Anaheim and the first phase opens next year"
    b_text = "And honestly it is not even built yet."
    a = [Word(**w) for w in spread_words(0.2, 9.9, a_text + ("." if sentence_break else ","))]
    b = [Word(**w) for w in spread_words(14.1, 19.0, b_text)]
    words = a + b
    for x, y in zip(words, words[1:]):  # Whisper: no gaps between words
        x.end = y.start
    a[-1].end = b[0].start = boundary  # where Whisper put the pause
    clip = make_clip("V01", seconds=20.5, role="aroll", silences=[(0.0, 0.18), GAP, (19.3, 20.5)])
    clip.segments = build_segments("V01", words)
    return clip


def kept_spans(tl) -> list[tuple[float, float]]:
    return [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]


def assert_gap_cut(spans):
    inside = [(a, b) for a, b in spans if a < 13.5 and b > 10.6]
    assert not inside, f"the 10-14 s pause is still in the cut: {spans}"
    assert any(b >= 9.9 for a, b in spans if a < 9.9) and any(a <= 14.1 for a, b in spans if b > 14.1)  # speech kept


@pytest.mark.parametrize("boundary", [10.0, 12.0, 14.1, 14.35])
@pytest.mark.parametrize("sentence_break", [True, False])
def test_a_pause_is_cut_wherever_whisper_put_it(boundary, sentence_break):
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import build_timeline

    analysis = Analysis(clips=[gap_clip(boundary, sentence_break)])
    tl = build_timeline(heuristic_plan(analysis), analysis, style=STYLES["medium"], name="t")
    assert_gap_cut(kept_spans(tl))


def test_a_timed_range_of_talking_footage_loses_its_dead_air():
    from roughcut.plan import normalize
    from roughcut.timeline import build_timeline

    analysis = Analysis(clips=[gap_clip(14.1, True), make_clip("V02", seconds=20, silences=[GAP])])
    plan = normalize({"sections": [{"name": "x", "items": ["V01@0-19.2"], "broll": []}]}, analysis)
    assert_gap_cut(kept_spans(build_timeline(plan, analysis, style=STYLES["medium"], name="t")))

    # A quiet moment chosen on purpose stays, and so does B-roll.
    for item in ("V01@10.5-13.5", "V02@5-18"):
        plan = normalize({"sections": [{"name": "x", "items": [item], "broll": []}]}, analysis)
        tl = build_timeline(plan, analysis, style=STYLES["medium"], name="t")
        assert len(tl.spine) == 1, item

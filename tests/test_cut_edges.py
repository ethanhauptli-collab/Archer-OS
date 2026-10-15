"""A cut never lands inside sound the silence map shows continuing, when the transcript's edge is off by a little more than the padding.

Whisper's word times are good to a few tens of milliseconds on clean speech and can be a hundred or more off in noise. Padding
(60/120 ms at tight, 100/200 at medium) is measured from where the silence map says the sound starts or stops, so an edge the
transcript puts too late (up to the padding plus `timeline.EDGE_SLACK`) or too early (up to `timeline.END_REACH`) still lands outside
the sound.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import SPEECH, ffmpeg, make_clip, spread_words

from roughcut.cli import main
from roughcut.segments import build_segments
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import EDGE_SLACK, END_REACH, FAKE_GAP, STYLES, _Spans, speech_pieces
from roughcut.transcribe import Word


# ------------------------------------------------------------------ the two lookups

SILENCES = [(0.0, 1.0), (2.0, 3.0)]


@pytest.mark.parametrize(
    "t,floor,least,within,expected",
    [
        (1.15, 0.0, 0.06, 0.16, 1.0),  # the word starts 150 ms after the sound did: beyond a 60 ms pad, within 160
        (1.05, 0.0, 0.06, 0.16, 1.05),  # 50 ms after: the padding already reaches back into the silence
        (1.06, 0.0, 0.06, 0.16, 1.06),  # exactly the padding: nothing to gain
        (1.30, 0.0, 0.06, 0.16, 1.30),  # too far: not a late start but another sound
        (1.15, 1.10, 0.06, 0.16, 1.15),  # a neighbouring word's sound ends at 1.10: it is not ours to take
        (0.5, 0.0, 0.06, 0.16, 0.5),  # inside a silence: clamp_start's case
        (-1.0, 0.0, 0.06, 0.16, -1.0),  # before every silence
    ],
)
def test_a_late_start_goes_back_to_where_the_sound_begins(t, floor, least, within, expected):
    assert _Spans(SILENCES).onset_before(t, floor, least, within) == pytest.approx(expected)


@pytest.mark.parametrize(
    "t,ceil,least,within,expected",
    [
        (1.85, 9.0, 0.12, 0.22, 2.0),  # the word stops 150 ms before the sound does
        (1.95, 9.0, 0.12, 0.22, 1.95),  # 50 ms: inside the padding
        (1.60, 9.0, 0.12, 0.22, 1.60),  # too far
        (1.85, 1.90, 0.12, 0.22, 1.85),  # the next word's sound starts at 1.90: not ours
        (2.5, 9.0, 0.12, 0.22, 2.5),  # inside a silence
        (3.5, 9.0, 0.12, 0.22, 3.5),  # after every silence
    ],
)
def test_an_early_end_goes_on_to_where_the_sound_stops(t, ceil, least, within, expected):
    assert _Spans(SILENCES).release_after(t, ceil, least, within) == pytest.approx(expected)


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (0.5, 0.9, 0.4),  # inside a silence
        (0.5, 1.5, 0.5),  # leaves it
        (1.2, 1.8, 0.0),  # between the two
        (0.5, 2.5, 1.0),  # the end of one and the start of the next: 0.5 + 0.5
        (-1.0, 4.0, 2.0),  # both whole
        (3.0, 3.5, 0.0),  # at the end of the last one
    ],
)
def test_how_much_of_a_stretch_is_silent(a, b, expected):
    assert _Spans(SILENCES).overlap(a, b) == pytest.approx(expected)


# ------------------------------------------------------------------ a run's pieces


def _words(*spec):
    return [Word(s, e, t) for s, e, t in spec]


@pytest.mark.parametrize("style", ["tight", "medium"])
def test_a_phrase_the_transcript_puts_late_is_cut_before_its_real_start(style):
    """The words really start at 5.0 and stop at 6.0 (the silences end and begin there); the transcript says 5.14 and 5.87."""
    pad = STYLES[style]
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.14, 5.45, "late"), (5.5, 5.87, "start.")))
    (piece,) = speech_pieces(clip.segments[0], clip, pad, 0)
    assert piece.a <= 5.0 and piece.b >= 6.0, (piece.a, piece.b)


@pytest.mark.parametrize("style,early", [("tight", 0.2), ("tight", 0.3), ("tight", 0.45), ("tight", 0.7), ("medium", 0.3), ("medium", 0.52), ("medium", 0.7)])
def test_a_last_word_the_transcript_ends_early_keeps_its_tail(style, early):
    """The sound really stops at 6.0 (the silence begins there); the transcript ends the last word `early` seconds before."""
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.0, 5.15, "so"), (5.2, 6.0 - early, "far.")))
    (piece,) = speech_pieces(clip.segments[0], clip, STYLES[style], 0)
    assert piece.b >= 6.0, (piece.a, piece.b)


def test_the_room_after_a_phrase_is_measured_from_where_the_sound_stops():
    """The transcript ends the last word 60 ms early, inside the padding: the cut still ends a full pad after the sound."""
    style = STYLES["tight"]
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.0, 5.4, "on"), (5.5, 5.94, "time.")))
    (piece,) = speech_pieces(clip.segments[0], clip, style, 0)
    assert piece.b == pytest.approx(6.0 + style.pad_out)


def test_the_room_before_a_phrase_is_measured_from_where_the_sound_begins():
    style = STYLES["tight"]
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.04, 5.4, "on"), (5.5, 6.0, "time.")))
    (piece,) = speech_pieces(clip.segments[0], clip, style, 0)
    assert piece.a == pytest.approx(5.0 - style.pad_in)


def test_a_transcript_that_is_right_changes_nothing():
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.0, 5.4, "on"), (5.5, 6.0, "time.")))
    (piece,) = speech_pieces(clip.segments[0], clip, STYLES["tight"], 0)
    assert piece.a == pytest.approx(5.0 - STYLES["tight"].pad_in) and piece.b == pytest.approx(6.0 + STYLES["tight"].pad_out)


def test_a_word_the_neighbour_claims_is_never_taken_back():
    """Sound starts at 5.0 but a cut-out filler ('um') owns 5.0 to 5.1: the next word's edge stays where it was."""
    clip = make_clip("V01", seconds=12, role="aroll", silences=[(0.0, 5.0), (6.0, 12.0)])
    clip.segments = build_segments("V01", _words((5.0, 5.1, "um"), (5.3, 5.8, "hello.")))
    style = STYLES["tight"]
    pieces = speech_pieces(clip.segments[0], clip, style, 0)
    assert all(p.a >= 5.1 for p in pieces), [(p.a, p.b) for p in pieces]


# ------------------------------------------------------------------ a pause only the transcript sees

SEEN = [(0.0, 4.9), (7.0, 12.0)]  # a clip whose pauses the silence map finds, with sound from 4.9 to 7.0


def _pair(gap, silences=SEEN, filler=False):
    """Two words (the first really ends 0.4 s after it starts) with `gap` seconds between them in the transcript."""
    clip = make_clip("V01", seconds=12, role="aroll", silences=silences)
    spec = [(5.0, 5.4, "honestly")]
    at = 5.4 + gap
    if filler:
        spec.append((5.45, at - 0.05, "um"))
    spec.append((at, at + 0.4, "good."))
    clip.segments = build_segments("V01", _words(*spec))
    return speech_pieces(clip.segments[0], clip, STYLES["tight"], 0)


def test_a_word_that_ended_early_does_not_split_the_phrase_through_its_sound():
    """Whisper ends 'honestly' at 5.4 and starts 'good' at 5.9, but the silence map has sound from 4.9 to 7.0: no pause there."""
    assert len(_pair(0.5)) == 1


def test_a_pause_the_silence_map_sees_still_splits_it():
    assert len(_pair(0.5, silences=[(0.0, 4.9), (5.45, 5.95), (7.0, 12.0)])) == 2


def test_without_a_silence_map_the_transcripts_gap_is_the_pause():
    assert len(_pair(0.5, silences=[])) == 2


def test_a_gap_longer_than_a_word_ends_early_is_a_pause():
    assert len(_pair(STYLES["tight"].max_gap + FAKE_GAP + 0.1 - 0.0)) == 2


def test_a_cut_out_filler_still_splits_the_phrase():
    assert len(_pair(0.5, filler=True)) == 2


def _two_sentences(gap, silences=SEEN):
    """Two one-word sentences ('honestly.' then 'good.') with `gap` seconds between them in the transcript; the cut's pieces."""
    from roughcut.analyze import Analysis
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import build_timeline

    clip = make_clip("V01", seconds=12, role="aroll", silences=silences)
    clip.segments = build_segments("V01", _words((5.0, 5.4, "honestly."), (5.4 + gap, 5.8 + gap, "good.")))
    assert len(clip.segments) == 2
    analysis = Analysis(clips=[clip])
    return build_timeline(heuristic_plan(analysis), analysis, style=STYLES["tight"], name="t", fill=False, fades=False).spine


def test_a_word_that_ended_early_does_not_cut_between_two_sentences_either():
    assert len(_two_sentences(0.5)) == 1


def test_a_pause_the_map_sees_still_cuts_between_two_sentences():
    assert len(_two_sentences(0.5, silences=[(0.0, 4.9), (5.45, 5.95), (7.0, 12.0)])) == 2


def test_the_sentences_of_a_clip_without_a_pause_map_keep_their_transcript_gap(monkeypatch):
    assert len(_two_sentences(0.5, silences=[])) == 2


# ------------------------------------------------------------------ the whole build

EDGE = 0.015  # a burst this much inside a cut is not clipped in any way an ear can tell


def _kept(fcpxml: Path) -> list[tuple[float, float]]:
    root = ET.parse(fcpxml).getroot()
    assets = {a.get("id"): a for a in root.iter("asset")}
    rough = next(p for p in root.iter("project") if "Rough Cut" in p.get("name", ""))
    out = []
    for el in rough.find("sequence/spine"):
        asset = assets[el.get("ref")]
        if asset.get("name") != "talk":
            continue
        a = float(T(el.get("start")) - T(asset.get("start")))
        out.append((a, a + float(T(el.get("duration")))))
    return sorted(out)


@pytest.mark.parametrize("late", [0.0, 0.10, 0.14])
@pytest.mark.parametrize("style", ["tight", "medium"])
def test_every_burst_of_speech_survives_a_transcript_that_runs_late(footage, tmp_path, style, late):
    """talk.mov is tone bursts where the sentences are, with a silence map to match: the transcript is shifted `late` seconds
    (every word's start and end), the dead air is cut as usual, and no burst may lose its first or last stretch."""
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    shutil.copy(footage / "talk.mov", shoot / "talk.mov")
    sidecar = json.loads((footage / "talk.json").read_text())
    for w in sidecar["words"]:
        w["start"] = round(w["start"] + late, 3)
        w["end"] = round(w["end"] + late, 3)
    (shoot / "talk.json").write_text(json.dumps(sidecar))
    out = tmp_path / "out"
    assert main(["build", str(shoot), "-o", str(out), "--provider", "none", "--transcriber", "none", "--style", style, "--cache-dir", str(tmp_path / "cache")]) == 0
    kept = _kept(next(out.glob("*.fcpxml")))
    for start, end, text in SPEECH:
        if {w.strip(",.").lower() for w in text.split()} & {"um", "uh"}:
            continue  # a sentence with a filler loses it on purpose
        inside = sum(max(0.0, min(end, b) - max(start, a)) for a, b in kept)
        if inside < 0.5 * (end - start):
            continue  # a sentence the cut dropped on purpose (a restart, a filler line)
        assert inside >= (end - start) - 2 * EDGE, f"{text!r}: {inside:.3f} s of {end - start:.3f} s kept at {style}, transcript {late * 1000:.0f} ms late"
    assert EDGE_SLACK > 0 and END_REACH > 0


# ------------------------------------------------------------------ words the transcript ends early

THREE_WORDS = [  # (what was said, start, end, where the transcript ends it): the transcript ends the first two half a second early
    ("one", 2.0, 3.4, 2.9),
    ("two", 3.5, 4.9, 4.4),
    ("three.", 5.0, 6.5, 6.4),
]


def _three_long_words(tmp_path: Path) -> Path:
    """Three long words with a 0.1 s breath between them (the sound runs on), then an ordinary sentence so that it is talking footage."""
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    sentence = "we climbed the ridge before the rain came and the kids led the way up."
    gates = "+".join([f"between(t,{s},{e})" for _, s, e, _ in THREE_WORDS] + ["between(t,9.5,13.0)"])
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30000/1001:duration=15",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*300*t)*({gates})':s=48000:d=15",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(shoot / "talk.mov"),
    )
    words = [{"start": s, "end": e, "text": t, "prob": 0.95} for t, s, _, e in THREE_WORDS] + spread_words(9.5, 13.0, sentence)
    (shoot / "talk.json").write_text(json.dumps({"words": words}))
    return shoot


def _played(tmp_path: Path, shoot: Path, style: str = "tight") -> float:
    """Seconds of the three words' 2.0 to 6.5 s that the cut keeps."""
    out = tmp_path / "out"
    assert main(["build", str(shoot), "-o", str(out), "--provider", "none", "--transcriber", "none", "--style", style, "--cache-dir", str(tmp_path / "cache")]) == 0
    return sum(max(0.0, min(6.5, b) - max(2.0, a)) for a, b in _kept(next(out.glob("*.fcpxml"))))


def test_words_the_transcript_ends_early_stay_whole_at_tight_pacing(tmp_path):
    """Two gaps of 0.6 s in the transcript with the audio running through them: at tight pacing (pauses over 0.35 s are cut) the
    gap rule keeps the phrase whole."""
    assert _played(tmp_path, _three_long_words(tmp_path)) >= 4.5 - 0.05


def test_the_gap_rule_is_what_keeps_them_whole(tmp_path, monkeypatch):
    from roughcut import timeline

    monkeypatch.setattr(timeline, "FAKE_GAP", 0.0)
    assert _played(tmp_path, _three_long_words(tmp_path)) < 4.5 - 0.5  # without it each early end cut half a second of sound out

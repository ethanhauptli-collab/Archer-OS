"""A cut never lands inside sound the silence map shows continuing, when the transcript's edge is off by a little more than the padding.

Whisper's word times are good to a few tens of milliseconds on clean speech and can be a hundred or more off in noise. Padding
(60/120 ms at tight, 100/200 at medium) covers an edge that is off by up to that much; `timeline.EDGE_SLACK` more is covered by
moving the edge to where the silence map says the sound starts or stops.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import SPEECH, make_clip

from roughcut.cli import main
from roughcut.segments import build_segments
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import EDGE_SLACK, STYLES, _Spans, speech_pieces
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
    assert EDGE_SLACK > 0

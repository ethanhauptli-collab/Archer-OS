"""`roughcut check`: the edits of a finished cut that go through a sound.

The judging is tried on made-up loudness readings, then the whole command on a cut of words made of tone bursts with a quiet gap
between each, first as built (nothing goes through a word) and then with one edit moved into a word.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest
from conftest import ffmpeg

from roughcut import cutcheck
from roughcut.cli import main
from roughcut.cutcheck import Edge, judge
from roughcut.timecode import fcpx_time, parse_fcpx_time

QUIET, LOUD = -90.0, -12.0


def _levels(spans, seconds=4.0):
    """10 ms loudness readings: quiet everywhere except the (start, end) spans."""
    n = int(seconds / 0.01)
    out = [QUIET] * n
    for a, b in spans:
        for i in range(int(a / 0.01), int(b / 0.01)):
            out[i] = LOUD
    return out


def _edge(kind, at):
    return Edge(kind, "/footage/talk.mov", at, Fraction(0), "talk")


# ------------------------------------------------------------------ the judging

def test_an_edit_in_the_quiet_between_two_words_is_clean():
    levels = _levels([(0.5, 0.9), (1.5, 1.9)])
    found, small, floor = judge([_edge("ends", 1.0), _edge("starts", 1.4)], levels)
    assert found == [] and small == 0 and floor is not None


def test_an_edit_right_at_the_end_of_a_sound_is_clean():
    assert judge([_edge("ends", 1.4)], _levels([(1.0, 1.4), (2.0, 3.0)]))[0] == []


def test_an_edit_inside_a_sound_is_found_with_how_much_is_cut_off():
    levels = _levels([(1.0, 1.6), (2.5, 3.5)])
    (found,), _, _ = judge([_edge("ends", 1.3)], levels)
    edge, cut_off, kept = found
    assert edge.kind == "ends" and cut_off == pytest.approx(0.3, abs=0.02) and kept == pytest.approx(0.3, abs=0.02)
    (found,), _, _ = judge([_edge("starts", 1.2)], levels)
    assert found[0].kind == "starts" and found[1] == pytest.approx(0.2, abs=0.02)


def test_how_long_the_sound_goes_on_is_looked_at_for_half_a_second_only():
    (found,), _, _ = judge([_edge("ends", 1.1)], _levels([(1.0, 3.9)]))
    assert found[1] == pytest.approx(cutcheck.LOOK, abs=0.02)


def test_a_sliver_of_sound_is_counted_but_not_listed():
    found, small, _ = judge([_edge("ends", 1.38)], _levels([(1.0, 1.4), (2.0, 3.0)]))
    assert found == [] and small == 1


def test_what_was_cut_away_being_a_spoken_filler_is_no_fault():
    levels = _levels([(1.0, 1.6), (2.5, 3.5)])
    assert judge([_edge("ends", 1.3)], levels, fillers=[(1.3, 1.6)])[0] == []
    assert len(judge([_edge("ends", 1.3)], levels, fillers=[(1.55, 1.6)])[0]) == 1  # a filler elsewhere in it changes nothing


def test_a_recording_whose_background_is_near_its_speech_is_not_judged():
    levels = [-30.0 + (i % 7) for i in range(2000)]  # 6 dB between the quiet and the loud
    assert judge([_edge("ends", 5.0)], levels) == ([], 0, None)


def test_every_quiet_stretch_inside_the_kept_dialogue_is_listed_with_where_it_is():
    path = "/footage/talk.mov"
    levels = _levels([(0.0, 1.0), (2.5, 3.9)])
    runs = cutcheck.quiet_runs([(path, 0.0, 4.0, Fraction(10))], {path: levels}, {path: QUIET}, at_least=1.0)
    (length, at, found_in, source), = runs
    assert length == pytest.approx(1.5, abs=0.02) and found_in == path and source == pytest.approx(1.0, abs=0.01)
    assert at == Fraction(11)  # ten seconds into the cut, a second into the kept piece
    short = cutcheck.quiet_runs([(path, 0.0, 4.0, Fraction(0))], {path: levels}, {path: QUIET})
    assert [round(r[0], 2) for r in short] == [1.5, 0.1]  # the last tenth of a second is quiet too
    assert cutcheck.pauses([(path, 0.0, 4.0, Fraction(0))], {path: levels}, {path: QUIET})[1] == 1  # one over a second


def test_the_report_says_where_the_long_pauses_are():
    frame = Fraction(1001, 30000)
    report = cutcheck.Report("/x/cut.fcpxml", "Trip Rough Cut", frame, edits=10, clips=1, seconds=60.0)
    report.longest_pause, report.long_pauses = (1.4, Fraction(30)), 2
    report.long_runs = [(1.4, Fraction(30), "/f.mov", 31.0), (1.1, Fraction(70), "/f.mov", 75.0)]
    text = cutcheck.render(report)
    assert f"Over a second: {cutcheck.timecode(Fraction(30), frame)} (1.4 s), {cutcheck.timecode(Fraction(70), frame)} (1.1 s)." in text
    report.long_pauses, report.long_runs = 1, report.long_runs[:1]
    assert "Over a second" not in cutcheck.render(report)  # the one pause is already named as the longest


# ------------------------------------------------------------------ the whole command

WORDS = [  # (start, end): a sentence of five words, a pause, a sentence of five more
    (1.0, 1.3), (1.4, 1.7), (1.8, 2.1), (2.2, 2.5), (2.6, 2.9),
    (5.0, 5.3), (5.4, 5.7), (5.8, 6.1), (6.2, 6.5), (6.6, 6.9),
    (9.5, 9.8), (9.9, 10.2), (10.3, 10.6), (10.7, 11.0), (11.1, 11.4),
]


@pytest.fixture(scope="module")
def word_by_word(tmp_path_factory):
    """tone bursts with a 100 ms gap between words, and the transcript to match."""
    root = tmp_path_factory.mktemp("words")
    gates = "+".join(f"between(t,{a},{b})" for a, b in WORDS)
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30000/1001:duration=13",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*300*t)*({gates})':s=48000:d=13",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mov"),
    )
    texts = "we climbed the ridge before. the kids led the way. it was a long day.".split()
    words = [{"start": a, "end": b, "text": t, "prob": 0.95} for (a, b), t in zip(WORDS, texts)]
    (root / "talk.json").write_text(json.dumps({"words": words}))
    return root


def _build(shoot, tmp_path, style="tight"):
    out = tmp_path / "out"
    assert main(["build", str(shoot), "-o", str(out), "--provider", "none", "--transcriber", "none", "--style", style, "--cache-dir", str(tmp_path / "cache"), "--no-menu"]) == 0
    return out


def test_a_cut_made_in_the_gaps_has_no_edit_through_a_word(word_by_word, tmp_path):
    report = cutcheck.check(_build(word_by_word, tmp_path))
    assert report.clips == 1 and report.edits >= 2
    assert report.flagged == []
    assert report.longest_pause is not None and report.longest_pause[0] < 1.0


def test_an_edit_moved_into_a_word_is_found_with_its_timecode_and_words(word_by_word, tmp_path):
    out = _build(word_by_word, tmp_path)
    fcpxml = next(out.glob("*.fcpxml"))
    tree = ET.parse(fcpxml)
    spine = next(p for p in tree.getroot().iter("project") if "Rough Cut" in p.get("name", "")).find("sequence/spine")
    second = spine.findall("asset-clip")[1]  # the second sentence: its start moves 150 ms into its first word
    original = parse_fcpx_time(second.get("start"))
    moved = original + Fraction(15, 100)
    second.set("start", fcpx_time(moved))
    tree.write(fcpxml, encoding="UTF-8", xml_declaration=True)
    report = cutcheck.check(fcpxml)
    starts = [f for f in report.flagged if f.edge.kind == "starts"]
    assert len(starts) == 1
    flag = starts[0]
    assert flag.cut_off >= 0.08 and "‖" in flag.words and "the" in flag.words
    assert cutcheck.timecode(flag.edge.timeline, report.frame_duration).count(":") == 3


def test_the_command_prints_the_report_and_the_json(word_by_word, tmp_path, capsys):
    out = _build(word_by_word, tmp_path)
    capsys.readouterr()
    assert main(["check", str(out)]) == 0
    text = capsys.readouterr().out
    assert "roughcut check:" in text and "0 go through sound" in text
    assert main(["check", str(out), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["through_sound"] == [] and data["edits"] >= 2 and data["files"] == 1
    assert data["long_pauses"] == [] and data["pauses_over_a_second"] == 0


def test_a_folder_with_no_cut_is_said_plainly(tmp_path, capsys):
    assert main(["check", str(tmp_path)]) == 1
    assert "no .fcpxml" in capsys.readouterr().err
    assert main(["check", str(tmp_path / "nowhere")]) == 1

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


# ------------------------------------------------------------------ what an independent review found

def test_an_edit_beside_a_cut_out_filler_is_listed_apart_not_dropped():
    """Whisper puts the end of the word before an um into the um, so a clipped word there can't be told from the um going."""
    levels = _levels([(1.0, 1.6), (2.5, 3.5)])
    detail = cutcheck.judge_detail([_edge("ends", 1.3)], levels, fillers=[(1.3, 1.6)])
    assert detail.found == [] and len(detail.beside_fillers) == 1
    edge, cut_off, kept = detail.beside_fillers[0]
    assert edge.kind == "ends" and cut_off == pytest.approx(0.3, abs=0.02)
    assert judge([_edge("ends", 1.3)], levels, fillers=[(1.3, 1.6)])[0] == []  # the old call still says what it did


def test_a_breath_next_to_a_sentence_is_not_a_sound_to_cut_through_in_a_quiet_room():
    """In a room at -90 dB a breath 40 dB under the words is 'sound' by the floor alone; it isn't a word."""
    levels = _levels([(2.0, 3.0)], seconds=5.0)
    for i in range(170, 198):
        levels[i] = -52.0  # an inhale ending 20 ms before the sentence
    detail = cutcheck.judge_detail([_edge("starts", 1.94)], levels)  # the engine's pad before the sentence lands in the breath
    assert detail.found == [] and detail.small == 0 and detail.floor is not None
    louder = list(levels)
    for i in range(170, 198):
        louder[i] = -25.0  # but a sound within 30 dB of the words is still one
    assert len(cutcheck.judge_detail([_edge("starts", 1.94)], louder).found) == 1


def test_the_loud_end_is_taken_from_the_kept_dialogue_not_the_whole_file():
    """90 s of nat sound and one 3 s line: a twentieth of the file isn't speech, so the whole file can't tell sound from background."""
    levels = [-50.0] * 9000
    for i in range(4000, 4300):
        levels[i] = -12.0
    edge = _edge("ends", 41.5)
    assert cutcheck.judge_detail([edge], levels).floor is None
    detail = cutcheck.judge_detail([edge], levels, kept=[(40.0, 43.0)])
    assert detail.floor is not None and len(detail.found) == 1


def test_a_cut_in_a_short_gap_inside_a_word_is_listed_as_a_tight_gap():
    """A stop consonant leaves 70 ms of quiet inside a word: the cut there reads 'clean' (sound doesn't touch it) but takes half the word."""
    levels = _levels([(1.0, 1.30), (1.37, 1.7), (2.5, 3.5)])
    detail = cutcheck.judge_detail([_edge("ends", 1.33)], levels)
    assert detail.found == [] and len(detail.tight_gaps) == 1
    edge, cut_off, kept, gap = detail.tight_gaps[0]
    assert gap == pytest.approx(0.07, abs=0.011) and cut_off > 0.2
    wide = _levels([(1.0, 1.30), (1.50, 1.8), (2.5, 3.5)])  # a 200 ms pause is a pause
    assert cutcheck.judge_detail([_edge("ends", 1.40)], wide).tight_gaps == []


def test_a_click_in_a_pause_does_not_end_it():
    path = "/footage/talk.mov"
    levels = _levels([(0.0, 0.2), (1.4, 1.6)], seconds=2.0)  # a 1.2 s pause
    for i in range(80, 82):
        levels[i] = QUIET + 10.0  # a 20 ms click in the middle, well under the speech
    runs = cutcheck.quiet_runs([(path, 0.0, 2.0, Fraction(0))], {path: levels}, {path: QUIET})
    assert [round(r[0], 1) for r in runs] == [1.2, 0.4]  # the pause, and the quiet after the last word
    # A lip smack is louder than the room but well under the words: given the speech level, the pause goes on through it.
    smack = list(levels)
    for i in range(80, 82):
        smack[i] = -40.0
    args = ([(path, 0.0, 2.0, Fraction(0))], {path: smack}, {path: QUIET})
    assert max(r[0] for r in cutcheck.quiet_runs(*args)) < 1.0
    assert max(r[0] for r in cutcheck.quiet_runs(*args, louds={path: LOUD})) == pytest.approx(1.2, abs=0.02)
    knock = list(smack)
    knock[80] = LOUD - 6.0  # within 12 dB of the words: a sound, not a click
    assert max(r[0] for r in cutcheck.quiet_runs([(path, 0.0, 2.0, Fraction(0))], {path: knock}, {path: QUIET}, louds={path: LOUD})) < 1.0


class _Fake:
    """A cut the check can read without a file: one dialogue file, its edits and ranges, the readings listen() would give."""

    path = "/footage/talk.mov"

    def __init__(self, monkeypatch, tmp_path, levels, words=None, edges=None, ranges=None):
        self.fcpxml = tmp_path / "cut.fcpxml"
        self.fcpxml.write_text("<fcpxml/>")
        frame = Fraction(1001, 30000)
        edges = edges if edges is not None else [Edge("ends", self.path, 5.0, Fraction(5), "talk")]
        ranges = ranges if ranges is not None else [(self.path, 0.0, 10.0, Fraction(0))]
        monkeypatch.setattr(cutcheck, "find_cut", lambda target: (self.fcpxml, tmp_path))
        monkeypatch.setattr(cutcheck, "spine_edges", lambda f: ("Trip Rough Cut", frame, edges, ranges))
        monkeypatch.setattr(cutcheck, "listen", lambda p: levels)
        self.words = cutcheck.Transcripts()
        for p, w in (words or {}).items():
            self.words.add(p, w)
        monkeypatch.setattr(cutcheck, "transcripts", lambda folder: self.words)


def test_um_and_uh_left_in_the_cut_are_listed_even_where_the_audio_isnt_judged(monkeypatch, tmp_path):
    from roughcut.transcribe import Word

    noisy = [-30.0 + (i % 7) for i in range(2000)]  # 6 dB between the quiet and the loud: not judged
    fake = _Fake(monkeypatch, tmp_path, noisy, words={_Fake.path: [Word(2.0, 2.3, "um,", 0.9), Word(3.0, 3.4, "so", 0.9)]})
    report = cutcheck.check(tmp_path)
    assert report.unjudged == ["talk.mov"]
    assert [w for w, _ in report.fillers] == ["um,"]
    assert "1 um/uh still in the cut" in cutcheck.render(report) and "No um/uh left" not in cutcheck.render(report)


def test_without_a_transcript_the_check_does_not_say_no_um_is_left(monkeypatch, tmp_path):
    levels = _levels([(0.0, 1.0), (2.5, 3.5)], seconds=10.0)
    _Fake(monkeypatch, tmp_path, levels, words={})
    text = cutcheck.render(cutcheck.check(tmp_path))
    assert "No um/uh left" not in text and "no transcript" in text.lower()


def test_words_are_found_for_footage_reached_by_another_path_when_the_name_is_unique():
    from roughcut.transcribe import Word

    words = cutcheck.Transcripts()
    words.add("/Volumes/Card/C0001.MP4", [Word(1.0, 1.2, "hi", 0.9)])
    assert words.for_file("/private/var/folders/x/C0001.MP4")  # a link or a mount that spells the path another way
    words.add("/Volumes/Other/C0001.MP4", [Word(2.0, 2.2, "ho", 0.9)])
    assert words.for_file("/private/var/folders/x/C0001.MP4") == []  # two files with that name: no guessing
    assert words.for_file("/Volumes/Card/C0001.MP4")[0].text == "hi"


def test_a_cut_copied_into_history_finds_the_run_s_analysis_json(tmp_path):
    out = tmp_path / "out"
    kept = out / "history" / "v1 2026-10-15 17.13"
    kept.mkdir(parents=True)
    (out / "analysis.json").write_text(json.dumps({"clips": [{"media": {"path": "/f/a.mov"}, "segments": [{"words": [{"start": 1.0, "end": 1.2, "text": "um", "prob": 0.9}]}]}]}))
    found = cutcheck.transcripts(kept)
    assert [w.text for w in found.for_file("/f/a.mov")] == ["um"]
    assert cutcheck.transcripts(tmp_path / "elsewhere") == {}


def test_a_drive_that_stopped_answering_is_not_waited_on_again_for_its_other_files(monkeypatch, tmp_path):
    from roughcut.textio import Stalled

    paths = ["/Volumes/Share/a.mov", "/Volumes/Share/b.mov", "/Users/me/c.mov"]
    frame = Fraction(1001, 30000)
    monkeypatch.setattr(cutcheck, "find_cut", lambda target: (tmp_path / "cut.fcpxml", tmp_path))
    monkeypatch.setattr(cutcheck, "spine_edges", lambda f: ("Cut", frame, [], [(p, 0.0, 5.0, Fraction(i * 5)) for i, p in enumerate(paths)]))
    monkeypatch.setattr(cutcheck, "transcripts", lambda folder: cutcheck.Transcripts())
    asked: list[tuple[str, float]] = []
    real_bounded = cutcheck.bounded

    def fake_bounded(fn, *args, seconds, what=""):
        if fn is not cutcheck.listen:  # finding the cut and the transcript are not what is being tried
            return real_bounded(fn, *args, seconds=seconds, what=what)
        path = args[0]
        asked.append((path, seconds))
        if path.startswith("/Volumes/Share"):
            raise Stalled(f"{path} didn't answer")
        return _levels([(1.0, 2.0)], seconds=5.0)

    monkeypatch.setattr(cutcheck, "bounded", fake_bounded)
    report = cutcheck.check(tmp_path)
    assert [p for p, _ in asked] == ["/Volumes/Share/a.mov", "/Users/me/c.mov"]  # b.mov was not tried: same drive
    assert asked[0][1] == cutcheck.LISTEN_SECONDS and asked[1][1] < cutcheck.LISTEN_SECONDS  # and the next file gets less time
    assert any("b.mov" in p and "skipped" in p for p in report.problems) and report.clips == 1


def test_an_unreadable_cut_is_said_plainly_not_with_a_traceback(monkeypatch, tmp_path, capsys):
    def refuse(target, **kw):
        raise PermissionError(13, "Permission denied", str(tmp_path / "cut.fcpxml"))

    monkeypatch.setattr(cutcheck, "check", refuse)
    assert main(["check", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err and "Traceback" not in err


def test_the_report_names_the_edits_beside_a_filler_and_in_tight_gaps():
    frame = Fraction(1001, 30000)
    edge = Edge("ends", "/f/talk.mov", 2.0, Fraction(20), "talk")
    report = cutcheck.Report("/x/cut.fcpxml", "Trip Rough Cut", frame, edits=10, clips=1, seconds=60.0)
    report.beside_fillers = [cutcheck.Flag(edge, 0.3, 0.4, "…ridge ‖ um before…")]
    report.tight_gaps = [cutcheck.Flag(edge, 0.3, 0.4, "…basket ‖ ball…", gap=0.07)]
    text = cutcheck.render(report)
    assert "beside an um/uh" in text and cutcheck.timecode(Fraction(20), frame) in text and "70 ms" in text
    data = cutcheck.as_json(report)
    assert data["beside_fillers"][0]["words"] == "…ridge ‖ um before…" and data["tight_gaps"][0]["gap_ms"] == 70

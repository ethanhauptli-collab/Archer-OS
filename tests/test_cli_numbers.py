"""Numbers typed on the command line that no cut can use (nan, infinity, absurd sizes) are refused up front.

They used to run the whole analysis first and then stop on a Python traceback, or be written into the FCPXML as
`amount="1e+09dB"`. The app never sends these options; the Terminal does.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from roughcut import timecode as tc
from roughcut.cli import build_parser, main
from roughcut.timeline import parse_format

COMMANDS = {"build": ["build", "folder"], "render": ["render", "out"], "revise": ["revise", "out", "tighter"]}
BAD_LEVELS = ["nan", "NaN", "inf", "-inf", "1e999", "-1e999", "1e9", "-1e300", "13", "-61", "12.01", "", "abc", "1,5"]
GOOD_LEVELS = [("-14", -14.0), ("-30", -30.0), ("-20.5", -20.5), ("0", 0.0), ("12", 12.0), ("-60", -60.0), ("+3", 3.0)]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("option", ["--music-db", "--broll-db"])
@pytest.mark.parametrize("value", BAD_LEVELS)
def test_a_db_level_nobody_can_mix_is_refused_by_the_parser(command, option, value, capsys):
    with pytest.raises(SystemExit) as stop:
        build_parser().parse_args([*COMMANDS[command], f"{option}={value}"])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert option in err and "-60 to 12" in err


@pytest.mark.parametrize("option,dest", [("--music-db", "music_db"), ("--broll-db", "broll_db")])
@pytest.mark.parametrize("text,value", GOOD_LEVELS)
def test_ordinary_db_levels_still_parse(option, dest, text, value):
    assert getattr(build_parser().parse_args(["build", "folder", f"{option}={text}"]), dest) == value
    assert getattr(build_parser().parse_args(["build", "folder", option, text]), dest) == value  # "-14" as a separate word
    assert getattr(build_parser().parse_args(["build", "folder"]), dest) is None  # not given: the defaults apply


# ---- --format: a frame rate a sequence can have


@pytest.mark.parametrize("rate", ["1e999", "1e999/1", "nan", "inf", "-30", "0", "1e-999", "0.5", "1e9", "99999", "1001", "abc", "30/0", "1/0"])
def test_a_frame_rate_no_sequence_has_is_refused(rate):
    with pytest.raises(ValueError, match="can't read frame rate"):
        parse_format(f"1080x1080@{rate}")


@pytest.mark.parametrize("rate", ["1e999", "1e999/1", "inf", "nan", "-1", "0", "0/0", float("inf"), float("nan")])
def test_snapping_an_impossible_rate_is_none_not_an_error(rate):
    assert tc.parse_rate(rate) is None or rate == "1e999"  # an exact huge number is a number; it just has no frame duration
    assert tc.snap_frame_duration(rate) is None


def test_snapping_a_probed_rate_is_unchanged():
    assert tc.snap_frame_duration("1e-999") == (Fraction(1), "1")  # as before: a rate under 1 fps is read as 1
    assert tc.snap_frame_duration("90000/1") == (Fraction(1, 90000), "90000")  # probes are not limited to what a sequence can have


@pytest.mark.parametrize(
    "spec,fd",
    [("1080p@23.98", Fraction(1001, 24000)), ("1080p@29.97", Fraction(1001, 30000)), ("vertical@25", Fraction(1, 25)), ("1080p@30000/1001", Fraction(1001, 30000)),
     ("4k@60", Fraction(1, 60)), ("1080p@1", Fraction(1)), ("1080p@120", Fraction(1, 120)), ("1080p@1000", Fraction(1, 1000))],
)
def test_ordinary_frame_rates_still_read(spec, fd):
    assert parse_format(spec)[2] == fd


@pytest.mark.parametrize("command", COMMANDS)
def test_a_format_that_cannot_be_read_stops_before_anything_is_made(command, tmp_path, capsys):
    folder = tmp_path / "folder"
    folder.mkdir()
    run = tmp_path / "run"  # what render and revise are pointed at
    run.mkdir()
    out = tmp_path / "out"
    args = {
        "build": ["build", str(folder), "-o", str(out), "--provider", "none", "--transcriber", "none", "--no-menu"],
        "render": ["render", str(run)],
        "revise": ["revise", str(run), "tighter"],
    }[command]
    for spec in ("1080x1080@1e999", "banana", "1080x1080@nan", "1080x1080@0.5"):
        assert main([*args, f"--format={spec}"]) == 1
        err = capsys.readouterr().err
        assert "can't read" in err and spec in err
    assert not out.exists() and not list(run.iterdir()), "nothing was made"


def test_progress_json_reports_an_unreadable_format_as_an_error_event(tmp_path, capsys):
    import json

    folder = tmp_path / "folder"
    folder.mkdir()
    assert main(["build", str(folder), "-o", str(tmp_path / "out"), "--progress-json", "--provider", "none", "--transcriber", "none", "--format=1080x1080@1e999"]) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert events[-1]["event"] == "error" and "frame rate" in events[-1]["message"]


def test_footage_format_words_are_not_checked_as_sizes(tmp_path, capsys):
    """`--format auto` means the footage's own format (the app's Re-cut sends it): no preflight stop, even without a run to render."""
    run = tmp_path / "run"
    run.mkdir()
    assert main(["render", str(run), "--format=auto"]) == 1
    err = capsys.readouterr().err
    assert err and "can't read format" not in err  # it stops for the missing plan, not for the format


def test_a_preset_cannot_save_an_unreadable_rate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    assert main(["preset", "save", "x", "--format", "1080p@1e999"]) == 1
    assert "frame rate" in capsys.readouterr().err


def test_a_build_with_an_absurd_music_level_is_refused_by_the_command_line(tmp_path, capsys):
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(SystemExit) as stop:
        main(["build", str(folder), "-o", str(tmp_path / "out"), "--provider", "none", "--music-db=nan"])
    assert stop.value.code == 2 and not (tmp_path / "out").exists()

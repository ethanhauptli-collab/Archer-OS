import json
from fractions import Fraction
from pathlib import Path

import pytest

from roughcut import timecode as tc

NTSC = Fraction(1001, 30000)


def test_fcpx_time_formats():
    assert tc.fcpx_time(Fraction(0)) == "0s"
    assert tc.fcpx_time(Fraction(5)) == "5s"
    assert tc.fcpx_time(3 * NTSC, 30000) == "3003/30000s"
    assert tc.fcpx_time(Fraction(1, 3)) == "1/3s"
    assert tc.parse_fcpx_time("3003/30000s") == 3 * NTSC
    assert tc.parse_fcpx_time("3600s") == 3600


@pytest.mark.parametrize(
    "rate,label",
    [("30000/1001", "29.97"), ("2997/100", "29.97"), ("24000/1001", "23.98"), ("25/1", "25"), ("30.02", "30"), ("60000/1001", "59.94")],
)
def test_snap_frame_duration(rate, label):
    fd, got = tc.snap_frame_duration(rate)
    assert got == label
    assert fd == tc.STANDARD_FRAME_DURATIONS[label]


def test_snap_rejects_garbage():
    assert tc.snap_frame_duration("0/0") is None
    assert tc.snap_frame_duration(None) is None


def test_timecode_ndf():
    # 1 hour at 29.97 NDF is 108000 frames.
    assert tc.timecode_to_seconds("01:00:00:00", NTSC) == 108000 * NTSC
    assert tc.timecode_to_seconds("00:00:01:00", Fraction(1, 25)) == 1


def test_timecode_drop_frame():
    # Drop-frame skips frame numbers 00 and 01 each minute except every tenth.
    assert tc.timecode_to_seconds("00:01:00;02", NTSC) == 1800 * NTSC
    assert tc.timecode_to_seconds("00:10:00;00", NTSC) == 17982 * NTSC
    assert tc.timecode_to_seconds("01:00:00;00", NTSC) == 107892 * NTSC


def test_fcp_format_names():
    assert tc.fcp_format_name(1920, 1080, NTSC) == "FFVideoFormat1080p2997"
    assert tc.fcp_format_name(3840, 2160, Fraction(1001, 24000)) == "FFVideoFormat3840x2160p2398"
    # Vertical video has no named format; FCP writes these without a name.
    assert tc.fcp_format_name(1080, 1920, NTSC) is None


@pytest.mark.parametrize("text,seconds", [("8m", 480), ("90s", 90), ("1:30", 90), ("1h5m", 3900), ("480", 480), ("2.5m", 150)])
def test_parse_duration(text, seconds):
    assert tc.parse_duration(text) == seconds


def test_seconds_to_clock():
    assert tc.seconds_to_clock(65) == "1:05"
    assert tc.seconds_to_clock(3725) == "1:02:05"
    assert tc.seconds_to_clock(12.34, millis=True) == "0:12.3"


@pytest.mark.parametrize("text,seconds", [("8 min", 480), ("8 m", 480), ("1 h 5 m", 3900), ("90 sec", 90), ("2 mins", 120)])
def test_parse_duration_accepts_spaced_units(text, seconds):
    assert tc.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["5m5m", "eight", "8x"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        tc.parse_duration(text)


# ---- a length the cut can be made to: more than a second, at most 24 hours, ASCII digits, never inf/nan
# The same lists the Mac app's Target length field is tested with (mac/Tests/RoughcutKitTests/Fixtures/target-lengths.json).

LENGTHS = json.loads((Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "target-lengths.json").read_text())
HUGE = "9" * 400  # float() turns a run of digits this long into inf
SPACED = "1h" + " " * 16000  # the old unit pattern took quadratic time on a long run of spaces
UNSAFE_LENGTHS = LENGTHS["reject"] + [HUGE, HUGE + ":0", "1:" + HUGE, HUGE + "s", HUGE + "m", HUGE + "h", SPACED + "x", SPACED + "30m", "1" + " " * 70 + "s", "9" * 65]


@pytest.mark.parametrize("text", UNSAFE_LENGTHS, ids=lambda t: repr(t if len(t) < 24 else t[:12] + "..." + t[-6:]))
def test_parse_duration_refuses_unsafe_lengths(text):
    with pytest.raises(ValueError) as caught:
        tc.parse_duration(text)
    assert text.strip() == "" or "should look like 8m, 90s or 8:30" in str(caught.value)  # says what a length looks like


@pytest.mark.parametrize("text,seconds", LENGTHS["accept"], ids=lambda v: repr(v))
def test_parse_duration_range_edges(text, seconds):
    assert tc.parse_duration(text) == seconds


@pytest.mark.parametrize("text", [HUGE, "9" * 100000, SPACED + "x", SPACED + "30m", " ".join(["1"] * 5000), "1:" * 30000])
def test_parse_duration_is_quick_and_its_message_short_for_any_input(text):
    import time

    start = time.perf_counter()
    with pytest.raises(ValueError) as caught:
        tc.parse_duration(text)
    assert time.perf_counter() - start < 2, "a hostile --target must not hang the command"
    assert len(str(caught.value)) < 200, "the message names the start of what was typed, not all of it"


def test_the_longest_length_text_is_stated_and_used():
    assert tc.MAX_DURATION_TEXT == 64
    assert tc.parse_duration("0" * 61 + "90s") == 90  # 64 characters
    with pytest.raises(ValueError):
        tc.parse_duration("0" * 62 + "90s")


@pytest.mark.parametrize("command", ["build", "revise"])
def test_target_help_states_the_range(command, capsys):
    from roughcut.cli import main

    with pytest.raises(SystemExit):
        main([command, "--help"])
    assert "1 second to 24 hours" in " ".join(capsys.readouterr().out.split())


def test_the_supported_range_is_stated():
    assert tc.MIN_DURATION_SECONDS == 1 and tc.MAX_DURATION_SECONDS == 24 * 3600


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 1e300, -1e300, 10**20])
def test_seconds_to_clock_never_raises(value):
    assert tc.seconds_to_clock(value) == "--:--"
    assert tc.seconds_to_clock(value, millis=True) == "--:--"


def test_seconds_to_clock_keeps_its_format_for_real_lengths():
    assert tc.seconds_to_clock(0) == "0:00"
    assert tc.seconds_to_clock(86400) == "24:00:00"
    assert tc.seconds_to_clock(-5) == "-0:05"
    assert tc.seconds_to_clock(Fraction(483)) == "8:03"


@pytest.mark.parametrize("text", ["1e999:0", "nan", "1:2:3:4", "-1:90", "0", "25h", "٨:٣٠"])
def test_build_refuses_an_unsafe_target_before_anything_is_made(text, tmp_path, capsys):
    from roughcut.cli import main

    footage = tmp_path / "footage"
    footage.mkdir()
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--no-menu", f"--target={text}"]) == 1
    err = capsys.readouterr().err
    assert "should look like 8m, 90s or 8:30" in err and "24 hours" in err
    assert not out.exists(), "stopped before the output folder was made"


def test_build_progress_json_reports_an_unsafe_target_as_an_error_event(tmp_path, capsys):
    from roughcut.cli import main

    footage = tmp_path / "footage"
    footage.mkdir()
    assert main(["build", str(footage), "-o", str(tmp_path / "out"), "--progress-json", "--provider", "none", "--transcriber", "none", "--target=1e999:0"]) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert events[-1]["event"] == "error" and "8:30" in events[-1]["message"]


@pytest.mark.parametrize("text", ["1e999:0", "nan", "-30", "0"])
def test_revise_refuses_an_unsafe_target(text, tmp_path, capsys):
    from roughcut.cli import main

    run = tmp_path / "run"
    run.mkdir()
    assert main(["revise", str(run), "tighter", f"--target={text}"]) == 1
    assert "should look like 8m, 90s or 8:30" in capsys.readouterr().err


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


def test_preset_target_checks_the_same_range(config, capsys):
    from roughcut import presets
    from roughcut.cli import main

    assert presets.check(presets.Preset(name="x", target="1e999:0")) is not None
    assert presets.check(presets.Preset(name="x", target="25h")) is not None
    assert presets.check(presets.Preset(name="x", target="8:30")) is None
    assert main(["preset", "save", "x", "-t", "nan"]) == 1
    assert "isn't a length" in capsys.readouterr().err
    assert presets.Preset(name="x", target="1e999:0").summary().startswith("1e999:0")  # shown as written, never raising


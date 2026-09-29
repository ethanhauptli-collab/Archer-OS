from fractions import Fraction

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

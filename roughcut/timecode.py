"""Exact time math for FCPXML.

Everything is a ``Fraction`` of seconds. FCPXML expresses time as rational
strings ("1001/30000s") and Final Cut rejects or warns about edits that do not
land on frame boundaries, so floats never make it into the XML.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction

# Frame durations for the rates Final Cut Pro natively supports.
STANDARD_FRAME_DURATIONS: dict[str, Fraction] = {
    "23.98": Fraction(1001, 24000),
    "24": Fraction(1, 24),
    "25": Fraction(1, 25),
    "29.97": Fraction(1001, 30000),
    "30": Fraction(1, 30),
    "47.95": Fraction(1001, 48000),
    "48": Fraction(1, 48),
    "50": Fraction(1, 50),
    "59.94": Fraction(1001, 60000),
    "60": Fraction(1, 60),
    "100": Fraction(1, 100),
    "119.88": Fraction(1001, 120000),
    "120": Fraction(1, 120),
}

# FCP's names for standard formats, keyed by (width, height, rate label).
_FORMAT_RATE_TOKENS = {
    "23.98": "2398",
    "24": "24",
    "25": "25",
    "29.97": "2997",
    "30": "30",
    "50": "50",
    "59.94": "5994",
    "60": "60",
}


# The frame rates a sequence can be asked for (--format WxH@fps); footage is measured, not limited.
MIN_SEQUENCE_FPS = 1
MAX_SEQUENCE_FPS = 1000


def parse_rate(rate: str | float | Fraction | None) -> Fraction | None:
    """Parse an ffprobe rate ("30000/1001", "29.97", 0/0) into frames/second."""
    if rate is None:
        return None
    if isinstance(rate, Fraction):
        return rate if rate > 0 else None
    if isinstance(rate, (int, float)):
        try:
            return Fraction(rate).limit_denominator(1001000) if rate > 0 else None
        except OverflowError:  # inf
            return None
    text = str(rate).strip()
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            if float(den) == 0:
                return None
            value = Fraction(int(float(num)), int(float(den)))
        else:
            value = Fraction(text)
    except (ValueError, ZeroDivisionError, OverflowError):  # "nan", "inf", "1e999/1" are not rates
        return None
    return value if value > 0 else None


def snap_frame_duration(rate: str | float | Fraction | None) -> tuple[Fraction, str] | None:
    """Map a measured frame rate to the nearest standard rate.

    Phone footage is often variable-frame-rate and reports rates like
    "2997/100" or "30.02"; Final Cut treats these as the nearest standard rate.
    Returns (frame_duration, label) or None when the rate is unusable (a number too large for a float, "1e999", is).
    """
    fps = parse_rate(rate)
    if fps is None:
        return None
    try:
        float(fps)
    except OverflowError:
        return None
    best_label, best_err = None, None
    for label, fd in STANDARD_FRAME_DURATIONS.items():
        err = abs(float(fps) - float(1 / fd)) / float(1 / fd)
        if best_err is None or err < best_err:
            best_label, best_err = label, err
    if best_label is not None and best_err is not None and best_err < 0.015:
        return STANDARD_FRAME_DURATIONS[best_label], best_label
    whole = max(1, round(float(fps)))
    return Fraction(1, whole), str(whole)


def rate_label(frame_duration: Fraction) -> str:
    for label, fd in STANDARD_FRAME_DURATIONS.items():
        if fd == frame_duration:
            return label
    return f"{float(1 / frame_duration):.3f}".rstrip("0").rstrip(".")


def nominal_fps(frame_duration: Fraction) -> int:
    """Timecode frame count per second (30 for 29.97, 24 for 23.976...)."""
    return round(1 / frame_duration)


def fcp_format_name(width: int, height: int, frame_duration: Fraction) -> str | None:
    """FCP's named video format (e.g. FFVideoFormat1080p2997), if one exists."""
    token = _FORMAT_RATE_TOKENS.get(rate_label(frame_duration))
    if token is None:
        return None
    sizes = {
        (1920, 1080): "1080p",
        (1280, 720): "720p",
        (3840, 2160): "3840x2160p",
        (4096, 2160): "4096x2160p",
        (1440, 1080): "1440x1080p",
        (2048, 1080): "2048x1080p",
        (5120, 2700): "5120x2700p",
    }
    size = sizes.get((width, height))
    if size is None:
        return None
    return f"FFVideoFormat{size}{token}"


def fcpx_time(t: Fraction, timebase: int | None = None) -> str:
    """Format seconds as an FCPXML time string.

    With a timebase (the denominator of the sequence frame duration, e.g. 30000)
    values are written the way Final Cut writes them ("2002/30000s") when they
    divide evenly; otherwise the reduced fraction is used.
    """
    t = Fraction(t)
    if t == 0:
        return "0s"
    if t.denominator == 1:
        return f"{t.numerator}s"
    if timebase and (t * timebase).denominator == 1:
        return f"{int(t * timebase)}/{timebase}s"
    return f"{t.numerator}/{t.denominator}s"


def parse_fcpx_time(text: str) -> Fraction:
    """Inverse of fcpx_time, for tests and re-reading our own output."""
    body = text.strip()
    if not body.endswith("s"):
        raise ValueError(f"not an FCPXML time: {text!r}")
    body = body[:-1]
    if "/" in body:
        num, den = body.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(body)


def floor_frames(t: Fraction, frame_duration: Fraction) -> int:
    return math.floor(Fraction(t) / frame_duration)


def ceil_frames(t: Fraction, frame_duration: Fraction) -> int:
    return math.ceil(Fraction(t) / frame_duration)


def round_frames(t: Fraction, frame_duration: Fraction) -> int:
    return round(Fraction(t) / frame_duration)


_TC_RE = re.compile(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})([:;.])(\d{2,3})$")


def timecode_to_seconds(tc: str, frame_duration: Fraction) -> Fraction | None:
    """Convert SMPTE timecode to seconds at the given frame duration.

    A ';' (or '.') before the frames field means drop-frame, which only exists
    for 29.97 and 59.94.
    """
    m = _TC_RE.match(tc.strip())
    if not m:
        return None
    hh, mm, ss, sep, ff = m.groups()
    h, mi, s, f = int(hh), int(mm), int(ss), int(ff)
    fps = nominal_fps(frame_duration)
    frames = ((h * 60 + mi) * 60 + s) * fps + f
    drop = sep in (";", ".") and frame_duration in (Fraction(1001, 30000), Fraction(1001, 60000))
    if drop:
        per_min = 2 if fps == 30 else 4
        total_minutes = h * 60 + mi
        frames -= per_min * (total_minutes - total_minutes // 10)
    return frames * frame_duration


# What a clock can show; beyond it (or not a number at all) the text is "--:--" rather than an error.
_CLOCK_LIMIT = 10**12
NO_CLOCK = "--:--"


def seconds_to_clock(seconds: float | Fraction, *, millis: bool = False) -> str:
    """Human clock string for reports: 1:02:03 / 2:03 / 2:03.4. Not a time (inf, nan, absurdly large): "--:--"."""
    total = float(seconds)
    if not math.isfinite(total) or abs(total) > _CLOCK_LIMIT:
        return NO_CLOCK
    sign = "-" if total < 0 else ""
    total = abs(total)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    if millis:
        sec = f"{s:04.1f}"
    else:
        sec = f"{int(s):02d}"
    if h:
        return f"{sign}{h}:{m:02d}:{sec}"
    return f"{sign}{m}:{sec}"


# The lengths a cut can be asked for (--target, a preset's target, the Mac app's Target length): from a second to a day.
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 24 * 3600

# ASCII digits only: float() and \d also read other scripts' digits, which the app's twin of this parser does not.
_NUMBER = r"[0-9]+(?:\.[0-9]+)?"
_NUMBER_ONLY = re.compile(_NUMBER)
_UNITS = re.compile(rf"(?:({_NUMBER})\s*h(?:rs?|ours?)?)?\s*(?:({_NUMBER})\s*m(?:ins?)?)?\s*(?:({_NUMBER})\s*s?(?:ec)?)?")


def _bad_duration(text: str) -> ValueError:
    return ValueError(f"can't read {text!r} as a length: it should look like 8m, 90s or 8:30 (from 1 second to 24 hours)")


def parse_duration(text: str) -> float:
    """Parse "8m", "90s", "8:30", "1h5m", "480" into seconds.

    A length is more than a second and at most 24 hours: no signs, exponents, "nan"/"inf", digits other than 0-9, or more
    than hours:minutes:seconds. Anything else (including numbers so large they overflow to infinity) is a ValueError.
    """
    raw = text.strip().lower()
    if not raw:
        raise ValueError("empty duration")
    if ":" in raw:
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) > 3 or not all(_NUMBER_ONLY.fullmatch(p) for p in parts):
            raise _bad_duration(text)
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
    else:
        m = _UNITS.fullmatch(raw)
        if not m or not any(m.groups()):
            raise _bad_duration(text)
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        total = h * 3600 + mi * 60 + s
    if not math.isfinite(total) or not MIN_DURATION_SECONDS <= total <= MAX_DURATION_SECONDS:
        raise _bad_duration(text)
    return total

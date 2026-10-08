"""The two ffmpeg jobs the pipeline needs: silence maps and thumbnail frames."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silencedetect(stderr: str, duration: float) -> list[tuple[float, float]]:
    """Turn ffmpeg silencedetect log lines into [(start, end)] intervals."""
    silences: list[tuple[float, float]] = []
    current: float | None = None
    for line in stderr.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            current = max(0.0, float(m.group(1)))
            continue
        m = _SILENCE_END.search(line)
        if m and current is not None:
            silences.append((current, min(duration, float(m.group(1)))))
            current = None
    if current is not None and current < duration:
        silences.append((current, duration))
    return silences


DEFAULT_NOISE_DB = -35.0


def window_peaks(path: Path, window: float = 0.05) -> list[float]:
    """Peak level (dBFS) of each 50 ms of the first audio track; [] if it can't be read."""
    af = (
        f"aformat=channel_layouts=mono,aresample=48000,asetnsamples=n={int(48000 * window)}:p=0,"
        "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    peaks = []
    for line in proc.stdout.splitlines():
        if "Peak_level=" in line:
            try:
                peaks.append(max(-120.0, float(line.split("=", 1)[1])))
            except ValueError:
                peaks.append(-120.0)
    return peaks


def noise_threshold(peaks: list[float], default: float = DEFAULT_NOISE_DB) -> tuple[float, float | None]:
    """(silencedetect level, background level) for one clip.

    A fixed -35 dB finds no pauses at all when the room, wind or crowd is
    louder than that, and marks everything as silence in a very quiet
    recording. So sit the level a little above this clip's own background
    and well below its loud parts; clean recordings keep the default.
    """
    if len(peaks) < 40:  # under two seconds: not enough to judge
        return default, None
    v = sorted(peaks)
    floor, loud = v[len(v) // 10], v[(len(v) * 9) // 10]
    if loud - floor < 10:  # background as loud as everything else: can't separate them
        return default, floor
    level = max(default, floor + min(12.0, max(4.0, 0.3 * (loud - floor))))
    return round(min(level, loud - 12.0), 1), floor


def detect_silences(
    path: Path, duration: float, noise_db: float = DEFAULT_NOISE_DB, min_silence: float = 0.35
) -> list[tuple[float, float]]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return parse_silencedetect(proc.stderr, duration)


def sound_regions(
    silences: list[tuple[float, float]], duration: float, min_len: float = 0.3
) -> list[tuple[float, float]]:
    """Complement of the silence map: where something audible happens."""
    regions = []
    cursor = 0.0
    for s, e in sorted(silences):
        if s - cursor >= min_len:
            regions.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor >= min_len:
        regions.append((cursor, duration))
    return regions


def extract_frame(src: Path, at: float, dest: Path, width: int = 512) -> Path | None:
    """Grab one JPEG frame (or scale a still) for visual description."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if at > 0:
        cmd += ["-ss", f"{at:.3f}"]
    cmd += [
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({width},iw)':-2",
        "-q:v",
        "5",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        return None
    return dest

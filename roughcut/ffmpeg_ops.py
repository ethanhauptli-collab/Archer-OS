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


def detect_silences(
    path: Path, duration: float, noise_db: float = -35.0, min_silence: float = 0.35
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

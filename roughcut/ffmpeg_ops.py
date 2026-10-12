"""The ffmpeg jobs the pipeline needs: silence maps, loudness and thumbnail frames."""

from __future__ import annotations

import math
import re
from pathlib import Path

from .textio import run_text

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
    proc = run_text(cmd)
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


LOUDNESS_STEP = 0.1  # seconds between loudness blocks (BS.1770's 75% overlap of 400 ms)


def loudness_blocks(path: Path) -> dict | None:
    """Momentary loudness (LUFS, 400 ms window) and sample peak (dBFS) every 100 ms.

    One pass of ffmpeg's ebur128 over the first audio track. Mono is measured
    as dual mono, the way it plays in a stereo timeline. Integrated loudness
    over any part of the clip is the gated energy mean of these blocks
    (`gated_loudness`), so speech-only loudness needs no second pass.
    None if the audio can't be read.
    """
    n = int(48000 * LOUDNESS_STEP)
    af = (
        f"aresample=48000,asetnsamples=n={n}:p=0,"
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level,"
        "ebur128=metadata=1:dualmono=1,ametadata=mode=print:file=-"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    proc = run_text(cmd)
    if proc.returncode != 0:
        return None
    return parse_loudness(proc.stdout)


def _db(text: str) -> float:
    try:
        return round(max(-120.0, float(text)), 1)
    except ValueError:  # "-inf", "nan"
        return -120.0


def parse_loudness(stdout: str) -> dict | None:
    m: list[float] = []
    peak: list[float] = []
    for line in stdout.splitlines():
        if line.startswith("frame:"):
            m.append(-120.0)
            peak.append(-120.0)
        elif not m:
            continue
        elif line.startswith("lavfi.r128.M="):
            m[-1] = _db(line.split("=", 1)[1])
        elif line.startswith("lavfi.astats.Overall.Peak_level="):
            peak[-1] = _db(line.split("=", 1)[1])
    if not m:
        return None
    return {"step": LOUDNESS_STEP, "m": m, "peak": peak}


ABSOLUTE_GATE = -70.0


def _energy_mean(values: list[float]) -> float:
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def gated_loudness(blocks: list[float]) -> float | None:
    """BS.1770 integrated loudness of momentary blocks: -70 LUFS absolute gate, -10 LU relative gate."""
    above = [v for v in blocks if v > ABSOLUTE_GATE]
    if not above:
        return None
    relative = _energy_mean(above) - 10.0
    gated = [v for v in above if v > relative]
    return round(_energy_mean(gated), 1)


def _percentile(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


def level_summary(blocks: dict, spans: list[tuple[float, float]] | None = None, duration: float | None = None) -> dict:
    """Loudness facts for one clip from its `loudness_blocks`.

    lufs: integrated loudness of the whole clip (what its nat sound or music
    plays at). speech: the same inside `spans` (the words), None when they
    cover under half a second. floor: the background (10th percentile of the
    momentary loudness). peak / speech_peak: sample peak of the clip / of
    the words (99.5th percentile of 100 ms blocks, so one bump doesn't
    count). end: where the last sound fades out (for back-timing music).
    """
    step = blocks.get("step", LOUDNESS_STEP)
    m, peaks = blocks.get("m") or [], blocks.get("peak") or []
    out = {"lufs": None, "floor": None, "peak": None, "end": None, "speech": None, "speech_peak": None}
    if not m:
        return out

    def peak_of(idx: list[int]) -> float | None:
        values = [peaks[i] for i in idx if i < len(peaks)]
        return round(_percentile(values, 0.995), 1) if values else None

    out["lufs"] = gated_loudness(m)
    out["floor"] = round(_percentile(m, 0.10), 1)
    out["peak"] = peak_of(list(range(len(m))))
    if spans:
        # A momentary block covers the 400 ms before its frame ends: centre = (i + 1) * step - 0.2.
        spans = sorted(spans)
        inside, j = [], 0
        for i in range(len(m)):
            centre = (i + 1) * step - 0.2
            while j < len(spans) and spans[j][1] < centre:
                j += 1
            if j < len(spans) and spans[j][0] <= centre:
                inside.append(i)
        if len(inside) >= 5:  # half a second of speech or more
            out["speech"] = gated_loudness([m[i] for i in inside])
            out["speech_peak"] = peak_of(inside)
    if out["lufs"] is not None:
        loud_enough = max(ABSOLUTE_GATE, out["lufs"] - 30.0)
        last = max((i for i, v in enumerate(m) if v > loud_enough), default=None)
        if last is not None:
            end = round((last + 1) * step, 2)
            out["end"] = min(end, duration) if duration is not None else end
    return out


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
    proc = run_text(cmd)
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


def extract_frame(src: Path, at: float, dest: Path, width: int = 512, zoom: float = 1.0) -> Path | None:
    """Grab one JPEG frame (or scale a still) for visual description.

    `width` caps the longest side, so a portrait frame costs no more image tokens
    than a landscape one. Rotated phone video arrives upright (ffmpeg autorotates).
    `zoom`: the middle of the frame, as a punch-in shows it (1.12: 1/1.12 of the width and height).
    """
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
        (f"crop=iw/{zoom:g}:ih/{zoom:g}," if zoom != 1 else "") + f"scale='if(gte(iw,ih),min({width},iw),-2)':'if(gte(iw,ih),-2,min({width},ih))'",
        "-q:v",
        "5",
        str(dest),
    ]
    proc = run_text(cmd)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        return None
    return dest

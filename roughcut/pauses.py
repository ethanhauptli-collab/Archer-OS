"""What plays in the pauses of talking footage.

A silence level raised above the default means something sounds under the
speech. In a finished promo that's a music bed: cutting every breath there
makes the music stutter, so those clips keep their pauses longer and the
planner hears that it's a finished edit. Room tone, wind and crowds are
noise, and their pauses are dead air like any other.

Music is tonal and noise isn't: the spectral flatness of the pauses (near 0
for tones and chords, 0.5-0.9 for noise) tells them apart.
"""

from __future__ import annotations

import bisect
import math
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpeg_ops
from .analyze import Cache, Clip, log

MUSIC_FLATNESS = 0.25  # median flatness of the pauses below this: a music bed
MIN_BED_SECONDS = 2.0  # pauses needed to judge
FINISHED_EDIT_CUTS = 4.0  # picture cuts a minute that mark a finished edit, not raw footage


def pause_flatness(path: Path, silences: list[tuple[float, float]]) -> float | None:
    """Median spectral flatness of the audio inside `silences` (None if too little to judge)."""
    if sum(e - s for s, e in silences) < MIN_BED_SECONDS:
        return None
    af = (
        "aformat=channel_layouts=mono,aresample=16000,highpass=f=150,"  # below 150 Hz: hum and rumble, not music
        "aspectralstats=win_size=1024:measure=flatness,ametadata=mode=print:file=-"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    spans = sorted(silences)
    starts = [s for s, _ in spans]
    t = None
    inside: list[float] = []
    for line in proc.stdout.splitlines():
        if line.startswith("frame:") and "pts_time:" in line:
            try:
                t = float(line.rsplit("pts_time:", 1)[1].split()[0])
            except ValueError:
                t = None
        elif ".flatness=" in line and t is not None:
            i = bisect.bisect_right(starts, t) - 1
            if i >= 0 and spans[i][0] + 0.05 <= t <= spans[i][1] - 0.1:
                try:
                    value = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                if math.isfinite(value):
                    inside.append(value)
    if len(inside) < 10:
        return None
    return round(statistics.median(inside), 4)


def cuts_per_minute(path: Path, duration: float) -> float | None:
    """Picture cuts a minute (one low-res scene-change pass); None for audio or unreadable video."""
    if duration < 5:
        return None
    vf = "fps=10,scale=160:-2,scdet=threshold=10,metadata=mode=print:key=lavfi.scd.time:file=-"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-vf", vf, "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    cuts = sum(1 for line in proc.stdout.splitlines() if line.startswith("lavfi.scd.time="))
    return round(cuts * 60 / duration, 1)


def mark_beds(clips: list[Clip], cache: Cache, workers: int = 4) -> list[str]:
    """Set clip.bed ('music' / 'noise') and cuts_per_min on talking clips with a raised silence level."""
    todo = [
        c for c in clips
        if c.role in ("aroll", "voiceover") and not c.hidden and c.media.has_audio
        and c.silence_db is not None and c.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB
    ]

    def measure(clip: Clip) -> None:
        m = clip.media
        key = f"bed-v1-{clip.silence_db:g}.json"
        found = cache.get(m.fingerprint, key)
        if found is None:
            found = {"flatness": pause_flatness(Path(m.path), clip.silences)}
            cache.put(m.fingerprint, key, found)
        flat = found.get("flatness")
        clip.bed = None if flat is None else ("music" if flat < MUSIC_FLATNESS else "noise")
        if clip.bed == "music" and m.kind == "video":
            cuts = cache.get(m.fingerprint, "scdet-v1.json")
            if cuts is None:
                cuts = {"cuts_per_min": cuts_per_minute(Path(m.path), clip.duration)}
                cache.put(m.fingerprint, "scdet-v1.json", cuts)
            clip.cuts_per_min = cuts.get("cuts_per_min")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(measure, todo))
    notes = []
    for clip in todo:
        if clip.bed != "music":
            continue
        edit = (clip.cuts_per_min or 0) >= FINISHED_EDIT_CUTS
        note = (
            f"{clip.media.name}: speech over a music bed{' in a finished edit' if edit else ''}, "
            "so its pauses are kept longer and the music flows; edits that jump the music get to-do markers"
        )
        log(note)
        notes.append(note)
    return notes

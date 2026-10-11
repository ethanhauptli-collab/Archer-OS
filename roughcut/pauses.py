"""What plays in the pauses of talking footage.

A silence level raised above the default means something sounds under the
speech. In a finished promo that's a music bed: cutting every breath there
makes the music stutter, so those clips keep their pauses longer and the
planner hears that it's a finished edit. Room tone, wind and crowds are
noise, and their pauses are dead air like any other.

Music is tonal and noise isn't: the spectral flatness of the pauses (near 0
for tones and chords, 0.5-0.9 for noise) tells them apart.

A breath or a mouth click in the middle of a pause splits it in two on the
silence map, and each half can be shorter than the style's longest pause,
so the whole pause stayed. `mark_breaths` finds those short, quiet islands
(no transcribed word in them, well below the speech level) so the timeline
can treat the pause as one.
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
            "so its pauses are kept longer and the music flows"
        )
        log(note)
        notes.append(note)
    return notes


# ------------------------------------------------------------------ breaths

BREATH_MAX = 0.4  # longest sound between two silences that can be a breath or a click
BREATH_BELOW_SPEECH = 12.0  # dB under the clip's speech level
WINDOW = 0.05  # seconds per level reading


def window_levels(path: Path) -> list[float]:
    """RMS level (dBFS) of each 50 ms of the first audio track; [] if it can't be read."""
    af = (
        f"aformat=channel_layouts=mono,aresample=48000,asetnsamples=n={int(48000 * WINDOW)}:p=0,"
        "astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-af", af, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    levels = []
    for line in proc.stdout.splitlines():
        if "RMS_level=" in line:
            try:
                value = float(line.split("=", 1)[1])
            except ValueError:
                value = -120.0
            levels.append(max(-120.0, value) if math.isfinite(value) else -120.0)
    return levels


EDGE = 0.02  # word times and the silence map disagree by about this much
OUTSIDE = 0.05  # a word sounding less than this outside a pause has no sound of its own there


def word_in_pause(words, starts: list[float], s1: float, e1: float, s2: float, e2: float) -> bool:
    """Is a transcribed word said in the sound between silences (s1, e1) and (s2, e2)?

    Either a word starts in it, or a word over it has no sound outside the
    two silences. Whisper stretches a quiet word across the pauses either
    side of its sound, so it starts in the first silence and ends in the
    second, and that sound is all there is of it. A neighbour stretched over
    a pause still sounds before or after it.
    """
    lo = bisect.bisect_left(starts, s1 - OUTSIDE)  # only words starting near the pause can be in it
    for ws, we in words[lo : bisect.bisect_left(starts, s2)]:
        if ws >= e1 - EDGE or (we > e1 - EDGE and we <= e2 + OUTSIDE):
            return True
    return False


def breath_candidates(silences: list[tuple[float, float]], words: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Short sounds between two silences with no transcribed word in them (see word_in_pause)."""
    spans = sorted(silences)
    words = sorted(words)
    starts = [ws for ws, _ in words]
    out = []
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        if not 0 < s2 - e1 <= BREATH_MAX:
            continue
        if word_in_pause(words, starts, s1, e1, s2, e2):
            continue
        out.append((e1, s2))
    return out


def breath_islands(silences, words: list[tuple[float, float]], levels: list[float]) -> list[tuple[float, float]]:
    """The candidates quiet enough to be a breath or click: BREATH_BELOW_SPEECH under the speech."""
    candidates = breath_candidates(silences, words)
    if not candidates or not levels:
        return []
    spans = sorted(words)
    in_words = [v for i, v in enumerate(levels) if _within((i + 0.5) * WINDOW, spans)]
    speech = statistics.median(in_words) if len(in_words) >= 20 else sorted(levels)[(len(levels) * 9) // 10]
    out = []
    for a, b in candidates:
        first, last = int(a / WINDOW), min(len(levels) - 1, int(b / WINDOW))
        windows = [levels[i] for i in range(first, last + 1) if a <= (i + 0.5) * WINDOW <= b]
        if not windows:  # shorter than a window: the one it falls in
            windows = [levels[min(len(levels) - 1, int((a + b) / 2 / WINDOW))]]
        if max(windows) <= speech - BREATH_BELOW_SPEECH:
            out.append((a, b))
    return out


def _within(t: float, spans: list[tuple[float, float]]) -> bool:
    i = bisect.bisect_right(spans, (t, math.inf)) - 1
    return i >= 0 and spans[i][0] <= t <= spans[i][1]


def mark_breaths(clips: list[Clip], cache: Cache, workers: int = 4) -> None:
    """Set clip.breaths on talking clips: the breaths and clicks that split a pause in two."""
    todo = []
    for c in clips:
        c.breaths = []
        if c.role not in ("aroll", "voiceover") or c.hidden or not c.media.has_audio:
            continue
        words = sorted((w.start, w.end) for s in c.segments for w in s.words)
        if words and breath_candidates(c.silences, words):
            todo.append((c, words))

    def measure(item) -> None:
        clip, words = item
        m = clip.media
        found = cache.get(m.fingerprint, "levels-v1.json")
        if found is None:
            found = {"window": WINDOW, "rms": window_levels(Path(m.path))}
            cache.put(m.fingerprint, "levels-v1.json", found)
        clip.breaths = breath_islands(clip.silences, words, found.get("rms") or [])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(measure, todo))

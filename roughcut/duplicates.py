"""Spot footage exported more than once, and repeated narration takes.

Real projects mix sources and finished exports: the same promo in 16:9,
9:16 and 4:3, a B-roll clip saved under two names, three reads of the same
voiceover script. Left alone, the planner and the stringout use all of
them, so the same lines play two or three times.

- Versions (same video, different export) get `duplicate_of`: hidden from
  the planner and the stringout. One is kept, matching the sequence shape.
- Takes (same script, read again) get `take_of`: shown to the planner as
  alternatives for each line; the stringout uses one.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path

from .analyze import Cache, Clip

VERSION_SIMILARITY = 0.9  # same words, same length: an alternate export
TAKE_SIMILARITY = 0.6  # most of the same script read again


def _words(clip: Clip) -> list[str]:
    text = " ".join(s.text for s in clip.segments).lower()
    return re.findall(r"[a-z0-9']+", text)


def similarity(a: Clip, b: Clip) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _dhash(path: Path, at: float) -> int | None:
    """64-bit difference hash of one frame (tiny grayscale thumbnail)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, at):.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    data = proc.stdout
    if proc.returncode != 0 or len(data) < 72:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (data[row * 9 + col] > data[row * 9 + col + 1])
    return bits


def frame_signature(clip: Clip, cache: Cache) -> list[int] | None:
    m = clip.media
    cached = cache.get(m.fingerprint, "dhash.json")
    if cached is not None:
        return cached or None
    dur = clip.duration
    sig = [_dhash(Path(m.path), dur * f) for f in (0.25, 0.5, 0.75)]
    result = [s for s in sig if s is not None] if all(s is not None for s in sig) else []
    cache.put(m.fingerprint, "dhash.json", result)
    return result or None


def _same_frames(a: list[int], b: list[int], max_bits: int = 10) -> bool:
    return len(a) == len(b) and all(bin(x ^ y).count("1") <= max_bits for x, y in zip(a, b))


def _aspect(clip: Clip) -> float:
    m = clip.media
    return m.width / m.height if m.width and m.height else 0.0


def _canonical(group: list[Clip], prefer_vertical: bool) -> Clip:
    def score(c: Clip) -> tuple:
        aspect = _aspect(c)
        if prefer_vertical:
            shape = -abs(aspect - 9 / 16)
        else:
            shape = -abs(aspect - 16 / 9)
        return (shape, c.media.width * c.media.height, -group.index(c))

    return max(group, key=score)


def _groups(clips: list[Clip], same) -> list[list[Clip]]:
    """Connected components of the `same(a, b)` relation."""
    parent = {c.id: c.id for c in clips}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(clips):
        for b in clips[i + 1 :]:
            if find(a.id) != find(b.id) and same(a, b):
                parent[find(b.id)] = find(a.id)
    by_root: dict[str, list[Clip]] = {}
    for c in clips:
        by_root.setdefault(find(c.id), []).append(c)
    return [g for g in by_root.values() if len(g) > 1]


def mark_duplicates(clips: list[Clip], cache: Cache, *, prefer_vertical: bool = False) -> list[str]:
    """Set duplicate_of / take_of on clips. Returns human-readable notes."""
    notes: list[str] = []
    for c in clips:
        c.duplicate_of = None
        c.take_of = None
        c.same_words = 0.0

    # 1. Talking videos with the same words and length: alternate exports.
    talking = [c for c in clips if c.media.kind == "video" and c.has_transcript]

    def same_version(a: Clip, b: Clip) -> bool:
        return abs(a.duration - b.duration) <= 1.0 and similarity(a, b) >= VERSION_SIMILARITY

    # 2. Silent videos with the same length and the same pictures.
    silent = [c for c in clips if c.media.kind == "video" and not c.has_transcript]

    def same_footage(a: Clip, b: Clip) -> bool:
        if abs(a.duration - b.duration) > 0.1 or abs(_aspect(a) - _aspect(b)) > 0.02:
            return False
        sa, sb = frame_signature(a, cache), frame_signature(b, cache)
        return bool(sa and sb and _same_frames(sa, sb))

    for group in _groups(talking, same_version) + _groups(silent, same_footage):
        keep = _canonical(group, prefer_vertical)
        others = [c for c in group if c is not keep]
        for c in others:
            c.duplicate_of = keep.id
        notes.append(f"{', '.join(c.id for c in others)} {'is' if len(others) == 1 else 'are'} the same footage as {keep.id} ({keep.media.name}); using {keep.id} only")

    # 3. Voiceover read more than once.
    narration = [c for c in clips if c.role == "voiceover" and c.has_transcript]
    for group in _groups(narration, lambda a, b: similarity(a, b) >= TAKE_SIMILARITY):
        keep = max(group, key=lambda c: (len(_words(c)), c.media.creation_time or ""))
        for c in group:
            if c is keep:
                continue
            c.take_of = keep.id
            c.same_words = round(similarity(c, keep), 2)
        notes.append(
            f"{', '.join(c.id for c in group if c is not keep)} and {keep.id} are takes of the same narration script; "
            "the planner picks one read per line and the stringout uses " + keep.id
        )
    return notes

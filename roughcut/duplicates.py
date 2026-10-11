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
from collections import Counter
from pathlib import Path

from .analyze import Cache, Clip

VERSION_SIMILARITY = 0.9  # same words, same length: an alternate export
TAKE_SIMILARITY = 0.6  # most of the same script read again


def _words(clip: Clip) -> list[str]:
    text = " ".join(s.text for s in clip.segments).lower()
    return re.findall(r"[a-z0-9']+", text)


LINE_REPEAT_IN_TAKES = 0.8  # another read or export of the same script: any length
LINE_REPEAT = 0.85  # anywhere else, for lines of six words or more
LINE_REPEAT_MIN_WORDS = 6


def line_words(text: str) -> list[str]:
    """A line's words for comparing reads: lowercased, no punctuation, no um/uh."""
    from .transcribe import FILLERS

    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in FILLERS]


def take_group(clip: Clip) -> str:
    """Clips with the same group are reads (take_of) or exports (duplicate_of) of one thing."""
    return clip.take_of or clip.duplicate_of or clip.id


def same_line(a: list[str], b: list[str], same_takes: bool) -> bool:
    """Is line `a` the same line as `b`, said again? `same_takes`: from two reads/exports of one script."""
    if not a or not b:
        return False
    if not same_takes and len(a) < LINE_REPEAT_MIN_WORDS:
        return False  # "Thank you." twice in different interviews is fine
    need = LINE_REPEAT_IN_TAKES if same_takes else LINE_REPEAT
    if 2.0 * min(len(a), len(b)) / (len(a) + len(b)) < need:
        return False  # real_quick_ratio, without building a matcher for every pair of lines
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.quick_ratio() >= need and sm.ratio() >= need


LINE_CONTAINED = 0.85  # of the shorter line's words found in order inside the longer one...
LINE_SPREAD = 1.3  # ...within this many times its length (not common words scattered through a long line)
LINE_CONTAINED_MIN_WORDS = 4  # the shorter line, in two reads/exports of one script (LINE_REPEAT_MIN_WORDS elsewhere)


def contains_line(long: list[str], short: list[str], same_takes: bool) -> bool:
    """Is line `short` said again inside line `long`?

    Whisper's punctuation, so where sentences split, differs between reads
    of one script and between a clean voiceover and the same one over
    music: one read's sentence is often half of the other's.
    """
    if len(short) < (LINE_CONTAINED_MIN_WORDS if same_takes else LINE_REPEAT_MIN_WORDS) or len(long) < len(short):
        return False
    if sum((Counter(short) & Counter(long)).values()) < LINE_CONTAINED * len(short):
        return False  # cheap check first, without building a matcher for every pair of lines
    blocks = [m for m in difflib.SequenceMatcher(None, short, long, autojunk=False).get_matching_blocks() if m.size]
    if sum(m.size for m in blocks) < LINE_CONTAINED * len(short):
        return False
    return blocks[-1].b + blocks[-1].size - blocks[0].b <= LINE_SPREAD * len(short)


def found_in(long: list[str], short: list[str]) -> set[int]:
    """Positions of `long` that `short` matches, in order."""
    blocks = difflib.SequenceMatcher(None, short, long, autojunk=False).get_matching_blocks()
    return {m.b + i for m in blocks for i in range(m.size)}


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

"""Group words into sentence-sized segments with stable IDs.

The language model never deals in raw timestamps: it picks segments by ID
("V01.S014") and the deterministic code works out exact frame-accurate cuts.
That split is what keeps the output reliable.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .transcribe import Word

SENTENCE_END = (".", "?", "!", "…")


@dataclass
class Segment:
    id: str
    clip_id: str
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "clip_id": self.clip_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "words": [{"start": w.start, "end": w.end, "text": w.text, "prob": w.prob} for w in self.words],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Segment":
        return cls(
            id=d["id"],
            clip_id=d["clip_id"],
            start=d["start"],
            end=d["end"],
            text=d["text"],
            words=[Word(**w) for w in d.get("words", [])],
        )


def split_words_in_silence(words: list[Word], silences: list[tuple[float, float]], tolerance: float = 0.05) -> tuple[list[Word], list[Word]]:
    """(the words kept, the words dropped): Whisper sometimes invents words over silence, so any word fully inside one goes."""
    if not silences:
        return words, []
    spans = sorted(silences)
    starts = [s for s, _ in spans]
    kept: list[Word] = []
    dropped: list[Word] = []
    for w in words:
        # Silences don't overlap: only the last one to start by the word's start (give or take the tolerance), or the one before it
        # for a word too short to tell the two apart, can hold the word.
        i = bisect.bisect_right(starts, w.start + tolerance) - 1
        inside = any(s - tolerance <= w.start and w.end <= e + tolerance for s, e in spans[max(0, i - 1) : i + 1])
        (dropped if inside else kept).append(w)
    return kept, dropped


def _word_readings(levels: list[float], w: Word, window: float, offset: float = 0.0) -> list[float]:
    """The readings under a word. `offset` is how far into the file the first reading is (a file whose audio starts late)."""
    i = max(0, int((w.start - offset) / window))
    return levels[i : max(i + 1, int((w.end - offset) / window))]


def speech_stands_above_room(
    words: list[Word], levels: list[float], floor: float, window: float = 0.01, min_separation: float = 12.0, offset: float = 0.0
) -> bool:
    """Is the transcript's own speech clearly louder than the room? The median reading inside the words, against the room's floor.

    The loudest twentieth of a clip says nothing about its talker when a horn, a door or a laugh is that loud; the words are where
    the talker is. Within `min_separation` of the room, loudness can't tell the speech from the room and proves nothing.
    """
    inside = sorted(v for w in words for v in _word_readings(levels, w, window, offset))
    return bool(inside) and inside[len(inside) // 2] - floor >= min_separation


def split_invented_words(
    words: list[Word],
    silences: list[tuple[float, float]],
    levels: list[float],
    floor: float,
    pause_db: float,
    window: float = 0.01,
    tolerance: float = 0.05,
    min_separation: float = 12.0,
    offset: float = 0.0,
) -> tuple[list[Word], list[Word]]:
    """(the words kept, the words Whisper wrote over nothing but the room) by the clip's own 10 ms loudness.

    A word is a candidate when it lies fully inside a loudness pause (split_words_in_silence). It is dropped only if the words of the
    clip stand clearly above the room (speech_stands_above_room), so the map can tell speech from the room at all, and if its own
    readings (their 90th percentile) are under the pause level. The second test is made on the word itself, not on the pause, which
    a click rule closed over a short soft word ("a", "I") that sounds a little over the room.
    """
    kept, candidates = split_words_in_silence(words, silences, tolerance)
    if not candidates:
        return words, []
    if not speech_stands_above_room(words, levels, floor, window, min_separation, offset):
        return words, []
    invented: list[Word] = []
    for w in candidates:
        r = sorted(_word_readings(levels, w, window, offset))
        if r and r[int(0.9 * (len(r) - 1))] <= pause_db:
            invented.append(w)
        else:
            kept.append(w)
    kept.sort(key=lambda w: (w.start, w.end))
    return kept, invented


def drop_words_in_silence(words: list[Word], silences: list[tuple[float, float]], tolerance: float = 0.05) -> list[Word]:
    """Whisper sometimes invents words over silence; remove any fully inside one."""
    return split_words_in_silence(words, silences, tolerance)[0]


def build_segments(
    clip_id: str,
    words: list[Word],
    *,
    gap_split: float = 1.0,
    max_words: int = 45,
) -> list[Segment]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        if current:
            gap = w.start - current[-1].end
            ends_sentence = current[-1].text.rstrip().endswith(SENTENCE_END)
            too_long = len(current) >= max_words
            soft_break = too_long and current[-1].text.rstrip().endswith((",", ";", ":"))
            if ends_sentence or gap >= gap_split or soft_break or len(current) >= max_words * 2:
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)

    segments = []
    for i, g in enumerate(groups, 1):
        text = " ".join(w.text.strip() for w in g).strip()
        segments.append(Segment(id=f"{clip_id}.S{i:03d}", clip_id=clip_id, start=g[0].start, end=g[-1].end, text=text, words=g))
    return segments


def segments_from_regions(clip_id: str, regions: list[tuple[float, float]], max_len: float = 12.0) -> list[Segment]:
    """Fallback when there is no transcript: each audible region is a segment."""
    segments = []
    n = 0
    for start, end in regions:
        cursor = start
        while cursor < end - 0.05:
            stop = min(end, cursor + max_len)
            n += 1
            segments.append(Segment(id=f"{clip_id}.S{n:03d}", clip_id=clip_id, start=cursor, end=stop, text=""))
            cursor = stop
    return segments

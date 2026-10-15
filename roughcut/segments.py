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

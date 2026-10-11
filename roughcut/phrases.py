"""Find the words a plan names in the transcript, and where they play in the cut.

The planner never writes times for speech. A graphic or a cutaway names the
exact words it belongs to; these helpers find them in a segment's words and
map a source time to a timeline frame. Shared by `graphics.place` and the
B-roll placement in `timeline.py`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .timeline import Timeline


def tokens(text: str) -> list[str]:
    """Lowercased words with punctuation stripped ("Center," -> "center"; "$325M" stays)."""
    return [t for t in (re.sub(r"[^\w$%]", "", w.lower()) for w in text.split()) if t]


def _word_tokens(words) -> list[str]:
    return [(tokens(w.text) or [""])[0] for w in words]


def exact_matches(words, phrase: str) -> list[tuple[int, int]]:
    """Every (first, last) word index where the whole phrase is spoken, in order."""
    want = tokens(phrase)
    have = _word_tokens(words)
    n = len(want)
    if not n:
        return []
    return [(i, i + n - 1) for i in range(len(have) - n + 1) if have[i : i + n] == want]


def partial_match(words, phrase: str) -> tuple[int, int] | None:
    """The longest run of the phrase's opening words found in order (first wins a tie)."""
    want = tokens(phrase)
    have = _word_tokens(words)
    n = len(want)
    best = None
    for i in range(len(have)):
        k = 0
        while i + k < len(have) and k < n and have[i + k] == want[k]:
            k += 1
        if k and (best is None or k > best[1] - best[0] + 1):
            best = (i, i + k - 1)
    return best


def find_phrase(words, phrase: str) -> tuple[int, int]:
    """Indexes of the first and last segment word matching `phrase` (whole segment if not found)."""
    exact = exact_matches(words, phrase)
    if exact:
        return exact[0]
    return partial_match(words, phrase) or (0, len(words) - 1)


def frame_at(tl: "Timeline", clip_id: str, t: float) -> int | None:
    """Timeline frame showing source time `t` of a clip, or None if it was cut."""
    fd = tl.frame_duration
    for s in tl.spine:
        if s.clip_id != clip_id:
            continue
        a = float(s.src_in)
        b = a + float(s.frames * fd)
        if a <= t < b:
            return s.offset + int((t - a) / float(fd))
    return None

"""Captions for a cut: the words it keeps, timed to the timeline, as SubRip (.srt).

Final Cut imports them onto the open project with File > Import > Captions, and they
move with the clips when you trim. Times come from the transcript's words mapped
through the cut (never from the model), on whole frames. Only what's spoken on camera
or in narration is captioned, without fillers; B-roll nat sound isn't. Names the planner
found misspelled by speech recognition (the plan's "spellings") are written as the brief spells them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .analyze import Analysis
    from .timeline import Timeline

SPOKEN_ROLES = ("aroll", "voiceover")
LINE_CHARS = 42  # per line, two lines a caption: the usual broadcast and YouTube limit
MAX_SECONDS = 6.0  # longest a caption stays up
MIN_SECONDS = 1.0  # shortest, if the next caption leaves room
PAUSE = 0.8  # a pause this long starts a new caption
SENTENCE_END = (".", "?", "!", "…")


@dataclass
class _Word:
    text: str
    a: int  # timeline frames
    b: int


@dataclass
class Caption:
    a: int  # timeline frames, end exclusive
    b: int
    lines: list[str]


def kept_words(tl: "Timeline", analysis: "Analysis") -> list[_Word]:
    """Every transcribed word the cut plays on the spine, in timeline order, fillers left out."""
    fd = tl.frame_duration
    clips = {c.id: c for c in analysis.clips}
    out: list[_Word] = []
    for piece in sorted(tl.spine, key=lambda s: s.offset):
        clip = clips.get(piece.clip_id)
        if clip is None or clip.role not in SPOKEN_ROLES:
            continue
        a = float(piece.src_in)
        b = a + float(piece.frames * fd)
        for seg in clip.segments:
            if seg.end <= a or seg.start >= b:
                continue
            for w in seg.words:
                mid = (w.start + w.end) / 2
                text = w.text.strip()
                if not text or w.is_filler or not a <= mid < b:
                    continue
                fa = piece.offset + round(Fraction(max(w.start, a) - a) / fd)
                fb = piece.offset + round(Fraction(min(w.end, b) - a) / fd)
                out.append(_Word(text, fa, max(fb, fa + 1)))
    return out


def _bare(word: str) -> tuple[str, str]:
    """("roy", "") for "Roy", ("rousseau", "'s") for "Rousseau's," ."""
    core = re.sub(r"[^\w']", "", word.lower().replace("\u2019", "'"))
    if core.endswith("'s"):
        return core[:-2], "'s"
    return core.replace("'", ""), ""


def fix_names(words: list[_Word], spellings: list[dict]) -> list[_Word]:
    """Words with each misheard name ("Roy Choy") replaced by its spelling ("Roy Choi"): one caption word,
    so a line never breaks inside it, keeping the run's timing and its punctuation or possessive."""
    fixes = sorted(
        ((tokens, str(f["correct"]).strip()) for f in spellings or [] if (tokens := [t for t in (_bare(w)[0] for w in str(f.get("heard") or "").split()) if t]) and str(f.get("correct") or "").strip()),
        key=lambda f: -len(f[0]),  # longest first: "Chef Remy Lovan" before "Remy"
    )
    if not fixes:
        return words
    out: list[_Word] = []
    i = 0
    while i < len(words):
        for tokens, correct in fixes:
            run = words[i : i + len(tokens)]
            bare = [_bare(w.text) for w in run]
            if len(run) == len(tokens) and [b for b, _ in bare] == tokens and not any(p for _, p in bare[:-1]):
                last = run[-1].text
                tail = bare[-1][1] + (last[len(last.rstrip(".,;:!?…")):] if last.rstrip(".,;:!?…") != last else "")
                out.append(_Word(correct + tail, run[0].a, run[-1].b))
                i += len(tokens)
                break
        else:
            out.append(words[i])
            i += 1
    return out


def _lines(words: list[str]) -> list[str]:
    """One line, or two: split where the halves are most even, after a comma if one is near, never inside a name."""
    text = " ".join(words)
    if len(text) <= LINE_CHARS or len(words) < 2:
        return [text]

    def cost(k: int) -> float:
        top, bottom = " ".join(words[:k]), " ".join(words[k:])
        c = abs(len(top) - len(bottom)) + 100 * (max(len(top), len(bottom)) > LINE_CHARS)
        if words[k - 1][:1].isupper() and words[k][:1].isupper() and not words[k - 1].endswith((",", ".", ";", ":", "?", "!")):
            c += 15  # "Golden / Hour Bar"
        if words[k - 1].endswith((",", ";", ":")):
            c -= 12
        return c

    best = min(range(1, len(words)), key=cost)
    return [" ".join(words[:best]), " ".join(words[best:])]


def captions(tl: "Timeline", analysis: "Analysis", spellings: list[dict] | None = None) -> list[Caption]:
    fps = 1 / float(tl.frame_duration)
    groups: list[list[_Word]] = []
    for w in fix_names(kept_words(tl, analysis), spellings or []):
        cur = groups[-1] if groups else None
        if cur is not None:
            prev = cur[-1]
            text = " ".join(x.text for x in cur + [w])
            if (
                (w.a - prev.b) / fps >= PAUSE
                or prev.text.endswith(SENTENCE_END)
                or len(text) > 2 * LINE_CHARS
                or (w.b - cur[0].a) / fps > MAX_SECONDS
                or (prev.text.endswith((",", ";", ":")) and len(text) > LINE_CHARS)
            ):
                cur = None
        if cur is None:
            groups.append([w])
        else:
            cur.append(w)
    out: list[Caption] = []
    for n, g in enumerate(groups):
        a, b = g[0].a, g[-1].b
        nxt = groups[n + 1][0].a if n + 1 < len(groups) else tl.total_frames
        b = min(max(b, a + round(MIN_SECONDS * fps)), nxt, tl.total_frames)
        if b > a:
            out.append(Caption(a, b, _lines([w.text for w in g])))
    return out


def _clock(frame: int, fd: Fraction) -> str:
    ms = round(frame * fd * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt(tl: "Timeline", analysis: "Analysis", spellings: list[dict] | None = None) -> str:
    """The cut's captions as SubRip text, or "" when nothing in it is spoken."""
    fd = tl.frame_duration
    blocks = [
        f"{n}\n{_clock(c.a, fd)} --> {_clock(c.b, fd)}\n" + "\n".join(c.lines) + "\n"
        for n, c in enumerate(captions(tl, analysis, spellings), 1)
    ]
    return "\n".join(blocks)

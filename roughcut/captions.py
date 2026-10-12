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
MIN_KEPT = 0.12  # seconds of a word's sound the cut must play for it to be captioned (or 40% of a short word)
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


def _sounding(clip, w) -> tuple[float, float]:
    """Where a word is heard: Whisper stretches a word over the pause next to it, and a cut takes that pause out."""
    from .timeline import _index

    pauses = _index(clip).silences
    a, b = pauses.clamp_start(w.start, w.end), pauses.clamp_end(w.end, w.start)
    return (a, b) if b > a else (w.start, w.end)


def kept_words(tl: "Timeline", analysis: "Analysis") -> list[_Word]:
    """Every transcribed word the cut plays on the spine, in timeline order, fillers left out.

    On camera and narration clips every word in a kept piece counts; on any other clip, the words
    of the lines the plan picked. A word goes with the piece that plays most of its sound, and only
    when the cut plays a real part of it: the end of a cut retake can sit a few milliseconds inside a piece.
    """
    fd = tl.frame_duration
    clips = {c.id: c for c in analysis.clips}
    best: dict[tuple[str, int, int], tuple[float, _Word]] = {}  # (clip, segment, word) -> (sound kept, caption word)
    played: dict[tuple[str, int, int], tuple[float, float]] = {}  # (clip, segment, word) -> (sound kept in all, its sound)
    for piece in tl.spine:
        clip = clips.get(piece.clip_id)
        if clip is None or not clip.segments or (clip.role not in SPOKEN_ROLES and not piece.seg_ids):
            continue
        a = float(piece.src_in)
        b = a + float(piece.frames * fd)
        for n, seg in enumerate(clip.segments):
            if seg.end <= a or seg.start >= b or (clip.role not in SPOKEN_ROLES and seg.id not in piece.seg_ids):
                continue
            for i, w in enumerate(seg.words):
                text = w.text.strip()
                if not text or w.is_filler:
                    continue
                sa, sb = _sounding(clip, w)
                kept = min(sb, b) - max(sa, a)
                if kept <= 0:
                    continue
                key = (clip.id, n, i)
                played[key] = (played.get(key, (0.0, 0.0))[0] + kept, sb - sa)
                if key in best and best[key][0] >= kept:
                    continue
                fa = piece.offset + round(Fraction(max(sa, a) - a) / fd)
                fb = piece.offset + round(Fraction(min(sb, b) - a) / fd)
                best[key] = (kept, _Word(text, fa, max(fb, fa + 1)))
    heard = (w for key, (_, w) in best.items() if played[key][0] >= min(MIN_KEPT, 0.4 * played[key][1]))
    return sorted(heard, key=lambda w: (w.a, w.b))


def _bare(word: str) -> tuple[str, str]:
    """("roy", "") for "Roy", ("rousseau", "'s") for "Rousseau's," ."""
    core = re.sub(r"[^\w']", "", word.lower().replace("\u2019", "'"))
    if core.endswith("'s"):
        return core[:-2], "'s"
    return core.replace("'", ""), ""


_PUNCT = ".,;:!?…"
_POSSESSIVE = re.compile(r"['\u2019][sS]$")
# The last word of a name whose period is its own: an initialism ("U.S.", "Ph.D.", "Psy.D.", "B.Com.", "a.m.": parts of up
# to three letters, the last a capital or a single letter) or a short form ("Jr.", "Inc.", "Blvd."). Not a web address,
# whose last part is a lowercase name ("cnn.com.", "t.co.", "x.ai."), or any other dotted word: those end a sentence.
_ABBREVIATION = re.compile(
    r"(?:^|\s)(?:(?:[A-Za-z]{1,3}\.)+(?-i:[A-Z][A-Za-z]{0,2}|[a-z])|Jr|Sr|Inc|Co|Corp|Ltd|Bros|St|Ave|Blvd|Rd|Mt|Ft|Dr|Mr|Mrs|Ms|Esq)\.$",
    re.IGNORECASE,
)


def _unpunctuated(text: str) -> str:
    """`text` without the punctuation after it, except an abbreviation's period ("Roy Choi Jr.")."""
    bare = text.rstrip(_PUNCT).rstrip()
    return bare + "." if text[len(bare):].lstrip().startswith(".") and _ABBREVIATION.search(bare + ".") else bare


def clean_spelling(heard: str, correct: str) -> tuple[str, str]:
    """A name fix without what the planner copied from the transcript around the name, on either side: the
    punctuation after it ("Marco Belini." -> "Marco Bellini.") and a possessive ("Gina Rousseau's" -> "Gina
    Russo's", "Roy Choy" -> "Roy Choi's"). Those are the transcript's, and each caption keeps its own."""
    heard, correct = _unpunctuated(heard.strip()), _unpunctuated(correct.strip())
    if _POSSESSIVE.search(heard) and correct:
        if _POSSESSIVE.search(correct):
            heard, correct = heard[:-2], correct[:-2]
        elif _bare(correct.split()[-1])[0] != _bare(heard.split()[-1])[0] + "s":  # unless it's the name: "Wahoo's" -> "Wahoos"
            heard = heard[:-2]
    elif _POSSESSIVE.search(correct) and heard:
        if _bare(correct.split()[-1])[0] + "s" != _bare(heard.split()[-1])[0]:  # unless it's the name: "Trader Joes" -> "Trader Joe's"
            correct = _unpunctuated(correct[:-2])
    return heard, correct


def _same(run: list[tuple[str, str]], name: list[tuple[str, str]]) -> bool:
    """`run` (bare words) says `name`: a possessive inside it as the name has it ("Nona's Table"), any at the end."""
    return (
        len(run) == len(name)
        and all(c + p.replace("'", "") == nc + np.replace("'", "") for (c, p), (nc, np) in zip(run[:-1], name[:-1]))
        and run[-1][0] == name[-1][0]
    )


def _respelled(texts: list[str], spellings: list[dict], keep: list[dict] | None = None) -> list[tuple[int, int, str]]:
    """Each misheard name in `texts` (one word each): (its first word, past its last, the name as the brief
    spells it, with a possessive the transcript gives it and what's around it: quote marks, brackets, punctuation).

    `keep`: pairs whose right forms stay as they are ("Chef Rémy Lovain" while an undone "Rémy" is reversed)."""
    from .transcribe import FILLERS

    fixes = []
    for f in spellings or []:
        heard, correct = clean_spelling(str(f.get("heard") or ""), str(f.get("correct") or ""))
        name = [b for b in map(_bare, heard.split()) if b[0] and b[0] not in FILLERS]  # captions leave out "um"
        if name and correct:
            # A possessive the pair itself has ("Wahoo's" -> "Wahoos") replaces the transcript's. A period
            # `heard` ends with is its own ("Roy Choi Jr."), not the sentence's.
            fixes.append((
                name, [_bare(w) for w in correct.split()], correct, not name[-1][1] and not _POSSESSIVE.search(correct), heard.endswith("."),
            ))
    fixes.sort(key=lambda f: -len(f[0]))  # longest first: "Chef Remy Lovan" before "Remy"
    kept = [[_bare(w) for w in clean_spelling(str(f.get("heard") or ""), str(f.get("correct") or ""))[1].split()] for f in keep or []]
    kept = sorted((run for run in kept if run), key=len, reverse=True)
    bare = [_bare(t) for t in texts]
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(texts):
        # Spelled by a pair still in force: left as it is, unless a longer name starts here.
        guard = next((len(r) for r in kept if _same(bare[i : i + len(r)], r)), 0)
        for name, right, correct, possessive, own_period in fixes:
            if len(right) > max(len(name), guard) and _same(bare[i : i + len(right)], right):
                i += len(right)  # already spelled out: "Golden Hour Bar" for "Golden Hour"
                break
            if len(name) > guard and _same(bare[i : i + len(name)], name):
                j = i + len(name)
                lead = re.match(r"\W*", texts[i]).group()  # an opening quote mark or bracket
                last = texts[j - 1]
                tail = re.search(r"\W*$", last).group()  # punctuation, a closing quote mark or bracket
                said = last[: len(last) - len(tail)]
                text = correct + (said[-2] + "s" if possessive and _POSSESSIVE.search(said) else "")  # its own apostrophe
                if own_period and tail.startswith(".") and not tail.startswith("..") and not _sentence_starts(texts, j):
                    tail = tail[1:]  # "Jr." mid-sentence or on its own: the period goes with the abbreviation (an ellipsis stays)
                tail = tail[next((k for k in range(len(tail), 0, -1) if text.endswith(tail[:k])), 0) :]  # "Jr." then ".": once
                out.append((i, j, lead + text + tail))
                i = j
                break
        else:
            i += guard or 1
    return out


# Title Case leaves these lowercase: "Head of Sales" and "Founder and Chef" aren't sentences.
_SMALL_WORDS = frozenset(("of", "and", "at", "for", "the", "in", "on", "to", "a", "an", "&", "with", "by"))


def _sentence_starts(texts: list[str], j: int) -> bool:
    """Does a sentence start at word `j`? A capital letter or a number, then a lowercase word ("Today we eat", "2024
    was big"; not a small word Title Case keeps lowercase), or a capital in text that ends its sentences elsewhere
    too. Title Case alone ("Acme Inc. CEO", "Jr. Head Chef", "Inc. Head of Sales") isn't one."""
    first = re.sub(r"^\W+", "", texts[j])[:1] if j < len(texts) else ""
    if not (first.isupper() or first.isdigit()):
        return False
    after = next((w for w in (re.sub(r"^\W+", "", t) for t in texts[j + 1 :]) if w.lower() not in _SMALL_WORDS), "")
    if after[:1].islower():
        return True
    ends = (w.rstrip("\"')]\u201d\u2019") for k, w in enumerate(texts) if k != j - 1)
    return any(w.endswith(("?", "!", "\u2026")) or (w.endswith(".") and not _ABBREVIATION.search(w)) for w in ends)


def respell(text: str, spellings: list[dict], keep: list[dict] | None = None) -> str:
    """`text` with each misheard name ("Roy Choy") spelled as the brief spells it ("Roy Choi"), its line breaks kept.
    `keep`: pairs in force whose right forms `spellings` mustn't change (undoing a pair inside a longer name)."""
    parts = re.split(r"(\s+)", text)
    words, gaps = parts[0::2], parts[1::2] + [""]  # each word and the space after it
    found = _respelled(words, spellings, keep)
    if not found:
        return text
    out, at = [], 0
    for i, j, name in found:
        out += [w + g for w, g in zip(words[at:i], gaps[at:i])] + [name + gaps[j - 1]]
        at = j
    return "".join(out + [w + g for w, g in zip(words[at:], gaps[at:])])


def respell_timed(words: list[tuple[str, float]], spellings: list[dict]) -> list[tuple[str, float]]:
    """(word, seconds) with each misheard name as one entry, spelled as the brief spells it, at its first word's time."""
    out: list[tuple[str, float]] = []
    at = 0
    for i, j, name in _respelled([w for w, _ in words], spellings):
        out += words[at:i] + [(name, words[i][1])]
        at = j
    return out + words[at:]


def fix_names(words: list[_Word], spellings: list[dict]) -> list[_Word]:
    """Words with each misheard name ("Roy Choy") replaced by its spelling ("Roy Choi"): one caption word,
    so a line never breaks inside it, keeping the run's timing, its possessive and the quote marks or punctuation around it."""
    out: list[_Word] = []
    at = 0
    for i, j, name in _respelled([w.text for w in words], spellings):
        out += words[at:i] + [_Word(name, words[i].a, words[j - 1].b)]
        at = j
    return out + words[at:]


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


def _fits(words: list[_Word], fps: float) -> bool:
    """One caption holds `words`: two lines of 42 at most, up no longer than MAX_SECONDS."""
    texts = [w.text for w in words]
    return (
        len(" ".join(texts)) <= 2 * LINE_CHARS
        and all(len(line) <= LINE_CHARS for line in _lines(texts))
        and (words[-1].b - words[0].a) / fps <= MAX_SECONDS
    )


_TITLE = re.compile(r"(?:^|\s)(?:Mr|Mrs|Ms)\.$", re.IGNORECASE)  # always before a name: "Mr. Lee"
# Before a name too ("Dr. Lee", "Mt. Baldy"), but these can also end a sentence ("We live on Ocean Dr."):
# then the next word is one a sentence starts with.
_PLACE_TITLE = re.compile(r"(?:^|\s)(?:Dr|Mt|Ft)\.$", re.IGNORECASE)
_OPENERS = {
    "the", "then", "we", "i", "it", "it's", "this", "that", "and", "but", "so", "they", "he", "she", "you", "there",
    "here", "now", "our", "my", "a", "an", "what", "when", "if", "after", "next", "first", "also", "plus",
}


def _ends_sentence(words: list[_Word], i: int) -> bool:
    """Does `words[i]` end its sentence? Its punctuation says so, but an abbreviation's own period doesn't when the
    sentence goes on: a lowercase word after it ("Roy Choi Jr. and his wife"), or a name after a title ("Dr. Lee")."""
    text = words[i].text
    if not text.endswith(SENTENCE_END):
        return False
    if not text.endswith(".") or text.endswith("..") or i + 1 >= len(words) or not _ABBREVIATION.search(text):
        return True
    nxt = re.sub(r"^\W+", "", words[i + 1].text)
    after = nxt[:1]
    if after.islower():
        return False
    named = _TITLE.search(text) or (_PLACE_TITLE.search(text) and re.sub(r"[^\w']", "", nxt.lower()) not in _OPENERS)
    return not (after.isupper() and named)


def _rest_of_sentence(words: list[_Word], k: int, fps: float) -> list[_Word]:
    """`words[k]` and the rest of its sentence, up to a pause long enough to start a caption anyway."""
    end = k
    while end + 1 < len(words) and not _ends_sentence(words, end) and (words[end + 1].a - words[end].b) / fps < PAUSE:
        end += 1
    return words[k : end + 1]


def _few(words: list[_Word]) -> bool:
    """Too little to stand as a caption of its own: three words at most, or under 20 characters."""
    return len(words) <= 3 or len(" ".join(w.text for w in words)) < 20


def _break_back(cur: list[_Word], rest: list[_Word], fps: float) -> int | None:
    """Where the full caption `cur` ends instead when the rest of its sentence (`rest`) is only a word or two: after
    `cur`'s last comma when what follows it and `rest` fit one caption, else where `cur` and `rest` split most evenly
    into two that fit. Neither side is left with a few words alone. None when `rest` is longer, or nothing fits."""
    if not _few(rest):
        return None
    splits = [m for m in range(1, len(cur)) if not _few(cur[:m]) and not _few(cur[m:] + rest) and _fits(cur[:m], fps) and _fits(cur[m:] + rest, fps)]
    commas = [m for m in splits if cur[m - 1].text.endswith((",", ";", ":"))]
    if commas:
        return commas[-1]
    size = lambda ws: len(" ".join(w.text for w in ws))  # noqa: E731
    return min(splits, key=lambda m: abs(size(cur[:m]) - size(cur[m:] + rest)), default=None)


def captions(tl: "Timeline", analysis: "Analysis", spellings: list[dict] | None = None) -> list[Caption]:
    fps = 1 / float(tl.frame_duration)
    groups: list[list[_Word]] = []
    words = fix_names(kept_words(tl, analysis), spellings or [])
    for k, w in enumerate(words):
        cur = groups[-1] if groups else None
        if cur is not None:
            prev = cur[-1]
            if (w.a - prev.b) / fps >= PAUSE or _ends_sentence(words, k - 1):
                cur = None
            elif not _fits(cur + [w], fps) or (
                # Past one line, a comma starts the next caption, unless this one holds the rest of the sentence
                # (no "closes it out." left on its own).
                prev.text.endswith((",", ";", ":")) and len(" ".join(x.text for x in cur + [w])) > LINE_CHARS
                and not _fits(cur + _rest_of_sentence(words, k, fps), fps)
            ):
                # Full. When the rest of the sentence is a word or two ("different." up on its own for a
                # second), this caption ends earlier, so the next one holds them with the words before them.
                m = _break_back(cur, _rest_of_sentence(words, k, fps), fps)
                if m is None:
                    cur = None
                else:
                    groups[-1], cur = cur[:m], cur[m:]
                    groups.append(cur)
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

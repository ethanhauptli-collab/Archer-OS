"""Sound-effect accents, part two: placing the pool's sounds on the cut's own frames.

Every accent lands on a frame the cut already computed; the model never gives a time (it only
says how many accents the brief asks for: the plan's `sfx`, none / soft / full). The events:

- each section start after the first: a whoosh;
- each graphic attached to the cut (after graphics are made, from the timeline itself, so one that
  never rendered gets nothing): a pop on a lower third, a hit on a title card or a stat;
- with "full" only: the first B-roll cut in each stretch of 1.5 s or more without speech (a
  whoosh), and a riser that ends on the last section's first frame when that's 20 s or more in.

Accents keep about SPACING apart: when two compete, a hit beats a pop, a pop a section whoosh,
that a montage whoosh, and that a riser. A riser builds into its event, so an accent on that
frame or after it doesn't compete with it. The same file never plays twice within
REPLAY_SECONDS, and each class takes its sounds in turn (by path, in the order the events
come), so the same cut always gets the same sounds.

Each accent is a connected audio clip on a lane below every music lane, attached to the spine
clip under its first frame. Its hit (the start of its loudest 50 ms; a riser's end) lands on the
event frame; it is whole frames of its file, trimmed at the cut's start and end, with a short
de-click fade (whole samples) only where it was trimmed. It never plays at its recorded level:
SFX_DB under the dialogue level, UNDER_WORDS_DB lower where a kept word sounds under it, never
past -1 dBFS peaks, and a fixed gain when levels are kept as recorded.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction

from . import timecode as tc
from .analyze import Analysis, Clip
from .sfx import Pool, Sound, frames_of
from .timeline import DECLICK, DUCK_GAP, PEAK_CEILING, REPLAY_SECONDS, ConnectedClip, Timeline, _is_speech, _round_db, _samples

AMOUNTS = ("none", "soft", "full")
SPACING = Fraction(2)  # seconds between accents
MONTAGE_GAP = DUCK_GAP  # a stretch without speech this long gets a whoosh on its first B-roll cut (as the music comes up there)
RISER_FROM = 20  # seconds: the last section starts at least this far in for a riser into it
# Under the dialogue level, by amount (to tune on the owner's Mac, like the other levels).
SFX_DB = {"soft": -12.0, "full": -9.0}
UNDER_WORDS_DB = -6.0  # further down where a kept word sounds under it
CREST_DB = 12.0  # a sound too short to measure is levelled as if this far under its peak
MAX_BOOST_DB = 12.0
MAX_CUT_DB = 40.0
GRAPHIC_SOUNDS = {"lower_third": "pop", "title_card": "hit", "stat": "hit"}
# Which accent wins when two compete (lower first).
RANK = {"hit": 0, "pop": 1, "section": 2, "montage": 3, "riser": 4}


@dataclass
class Event:
    frame: int  # where the sound lands
    why: str  # graphic / section / montage / riser
    kind: str  # the sound it wants: whoosh / riser / pop / hit
    label: str

    @property
    def rank(self) -> int:
        return RANK[self.kind] if self.why == "graphic" else RANK[self.why]


def events(tl: Timeline, analysis: Analysis, amount: str, graphic_kinds: dict[str, str] | None = None) -> list[Event]:
    """The frames this cut could take an accent on, for `amount` (soft: sections and graphics; full: all)."""
    if amount not in ("soft", "full") or not tl.spine:
        return []
    fd = tl.frame_duration
    out = [Event(frame, "section", "whoosh", f"into {name!r}") for frame, name in tl.section_starts[1:] if 0 < frame < tl.total_frames]
    for cc in tl.connected:
        if cc.kind != "graphic":
            continue
        kind = (graphic_kinds or {}).get(cc.clip_id, "")
        sound = GRAPHIC_SOUNDS.get(kind)
        if sound and 0 <= cc.start < tl.total_frames:
            out.append(Event(cc.start, "graphic", sound, f"{kind.replace('_', ' ')} {cc.note!r}" if cc.note else kind.replace("_", " ")))
    if amount == "full":
        speech: list[list[int]] = []
        for sc in tl.spine:
            if _is_speech(sc, analysis.clip(sc.clip_id)):
                if speech and sc.offset <= speech[-1][1]:
                    speech[-1][1] = max(speech[-1][1], sc.end)
                else:
                    speech.append([sc.offset, sc.end])
        gaps, cursor = [], 0
        for s0, s1 in speech:
            if s0 > cursor:
                gaps.append((cursor, s0))
            cursor = max(cursor, s1)
        if cursor < tl.total_frames:
            gaps.append((cursor, tl.total_frames))
        cuts = sorted(
            {c.start for c in tl.connected if c.kind == "broll"}
            | {sc.offset for sc in tl.spine if not _is_speech(sc, analysis.clip(sc.clip_id))}
        )
        least = tc.ceil_frames(MONTAGE_GAP, fd)
        for g0, g1 in gaps:
            if g1 - g0 < least:
                continue
            first = next((f for f in cuts if g0 <= f < g1 and f > 0), None)
            if first is not None:
                out.append(Event(first, "montage", "whoosh", "into the B-roll"))
        if len(tl.section_starts) > 1:
            frame, name = tl.section_starts[-1]
            if frame * fd >= RISER_FROM:
                out.append(Event(frame, "riser", "riser", f"building into {name!r}"))
    return out


def _compete(a: Event, b: Event, spacing: int) -> bool:
    """Too close to both play. A riser builds into its frame, so only an accent shortly before that frame competes with it."""
    if a.why == "riser" and b.why == "riser":
        return True
    if a.why == "riser" or b.why == "riser":
        riser, other = (a, b) if a.why == "riser" else (b, a)
        return riser.frame - spacing < other.frame < riser.frame
    return abs(a.frame - b.frame) < spacing


def choose(found: list[Event], fd: Fraction) -> list[Event]:
    """The accents that play: by rank (hit, pop, section, montage, riser), each kept clear of the ones before it. In time order."""
    spacing = tc.round_frames(SPACING, fd)
    kept: list[Event] = []
    for e in sorted(found, key=lambda e: (e.rank, e.frame)):
        if not any(_compete(e, k, spacing) for k in kept):
            kept.append(e)
    return sorted(kept, key=lambda e: (e.frame, e.rank))


def _word_spans(tl: Timeline, analysis: Analysis) -> list[tuple[int, int]]:
    """Timeline frames where a kept word sounds."""
    fd = tl.frame_duration
    out = []
    for sc in tl.spine:
        if not sc.seg_ids:
            continue
        a, b = sc.src_in, sc.src_in + sc.frames * fd
        for seg_id in sc.seg_ids:
            seg = analysis.segment(seg_id)
            if seg is None:
                continue
            spans = [(w.start, w.end) for w in seg.words] or [(seg.start, seg.end)]
            for s, e in spans:
                ws, we = Fraction(s).limit_denominator(1000000), Fraction(e).limit_denominator(1000000)
                if we <= a or ws >= b:
                    continue
                out.append((sc.offset + tc.floor_frames(max(ws, a) - a, fd), sc.offset + tc.ceil_frames(min(we, b) - a, fd)))
    return out


def level(sound: Sound, dialogue_level: float | None, amount: str, under_words: bool) -> tuple[float, str]:
    """(clip volume in dB, why) for one accent. Never None: an accent never plays at its recorded level."""
    base = SFX_DB.get(amount, SFX_DB["soft"])
    lower = UNDER_WORDS_DB if under_words else 0.0
    notes = []
    if dialogue_level is None:
        volume = base + lower
        notes.append(f"a fixed {base:+g} dB (levels are as recorded)")
    else:
        lufs = sound.lufs
        if lufs is None:
            lufs = (sound.peak if sound.peak is not None else 0.0) - CREST_DB
            notes.append("too short to measure its loudness, so levelled by its peak")
        volume = max(-MAX_CUT_DB, min(MAX_BOOST_DB, dialogue_level + base + lower - lufs))
        notes.insert(0, f"{-base:g} dB under the dialogue")
    if under_words:
        notes.append(f"{-UNDER_WORDS_DB:g} dB lower: a word sounds under it")
    volume = _round_db(volume)
    if sound.peak is not None and volume + sound.peak > PEAK_CEILING:
        volume = math.floor((PEAK_CEILING - sound.peak) * 2) / 2 + 0.0
        notes.append("held so its peaks stay under -1 dBFS")
    return volume, "; ".join(notes)


def _span(sound: Sound, frame: int, tl: Timeline) -> tuple[int, int, int, bool, bool] | None:
    """(start, frames trimmed off its head, frames, head trimmed, tail trimmed) with its hit on `frame`; None if nothing fits."""
    fd = tl.frame_duration
    n = frames_of(sound, fd)
    if n < 1:
        return None
    anchor = n if sound.kind == "riser" else min(n - 1, tc.floor_frames(Fraction(sound.hit).limit_denominator(1000000), fd))
    start = frame - anchor
    head = max(0, -start)
    start = max(0, start)
    end = min(start + n - head, tl.total_frames)
    frames = end - start
    if frames < 1:
        return None
    return start, head, frames, head > 0, head + frames < n


def clear(tl: Timeline) -> None:
    """Take the accents off (before placing them again, with the graphics' events)."""
    tl.connected = [c for c in tl.connected if c.kind != "sfx"]
    tl.levels = [r for r in tl.levels if not str(r.get("use", "")).startswith("sound effect")]
    tl.accents = []


def place(
    tl: Timeline, analysis: Analysis, pool: Pool | None, amount: str, *, graphic_kinds: dict[str, str] | None = None, heuristic: bool = False,
) -> dict:
    """Put the pool's accents on `tl` (replacing any placed before). `heuristic`: a cut made without AI gets no more than soft.

    Returns what didn't get one: {"no_sound": {class: events}} for a class the pool has none of, and
    {"replayed": n} for events whose class had no sound left that hadn't played in the last REPLAY_SECONDS.
    """
    clear(tl)
    missed: dict = {"no_sound": {}, "replayed": 0}
    if pool is None or not pool.sounds or not tl.spine:
        return missed
    if heuristic and amount == "full":
        amount = "soft"
    fd = tl.frame_duration
    found = []
    for e in events(tl, analysis, amount, graphic_kinds):
        if pool.of(e.kind):
            found.append(e)
        else:
            missed["no_sound"][e.kind] = missed["no_sound"].get(e.kind, 0) + 1
    words = _word_spans(tl, analysis)
    replay = tc.round_frames(Fraction(REPLAY_SECONDS), fd)
    top = min((c.lane for c in tl.connected if c.kind == "music"), default=0) - 1  # below every music lane
    lanes: dict[int, list[tuple[int, int]]] = {}
    turns: Counter = Counter()
    played: list[tuple[int, str]] = []
    rows: dict[tuple[str, float], dict] = {}
    for e in choose(found, fd):
        sounds = pool.of(e.kind)
        turn = turns[e.kind]
        turns[e.kind] += 1
        pick = span = None
        for j in range(len(sounds)):
            s = sounds[(turn + j) % len(sounds)]
            if any(path == s.path and abs(f - e.frame) < replay for f, path in played):
                continue
            span = _span(s, e.frame, tl)
            if span is not None:
                pick = s
                break
        if pick is None:
            missed["replayed"] += 1
            continue
        start, head, frames, cut_head, cut_tail = span
        end = start + frames
        played.append((e.frame, pick.path))
        lane = top
        while any(not (end <= s0 or start >= s1) for s0, s1 in lanes.get(lane, [])):
            lane -= 1
        lanes.setdefault(lane, []).append((start, end))
        under = any(w0 < end and w1 > start for w0, w1 in words)
        volume, why = level(pick, tl.dialogue_level, amount, under)
        length = frames * fd
        cc = ConnectedClip(
            pick.id, head * fd, frames, start, lane, kind="sfx", volume_db=volume, note=f"{pick.kind} {e.label}",
            fade_in=_samples(min(DECLICK, length / 2)) if cut_head else Fraction(0),
            fade_out=_samples(min(DECLICK, length / 2)) if cut_tail else Fraction(0),
        )
        tl.connected.append(cc)
        tl.accents.append({
            "frame": e.frame, "start": start, "kind": pick.kind, "event": e.why, "label": e.label, "clip": pick.id, "file": pick.name,
            "volume": volume, "under_words": under, "origin": pick.origin, "provider": pick.provider, "license": pick.license,
        })
        rows.setdefault((pick.id, volume), {
            "clip": pick.id, "use": f"sound effect ({pick.kind})", "lufs": pick.lufs, "peak": pick.peak, "words": False, "volume": volume, "why": why,
        })
    if tl.dialogue_level is not None:
        tl.levels += list(rows.values())
    return missed


def clips(tl: Timeline, pool: Pool | None) -> list[Clip]:
    """The clips of the sounds `tl` uses (only those reach Final Cut's browser, never the whole pool)."""
    if pool is None:
        return []
    used = {c.clip_id for c in tl.connected if c.kind == "sfx"}
    return [s.clip() for s in pool.sounds if s.id in used]


def with_clips(analysis: Analysis, tl: Timeline, pool: Pool | None, extra: list[Clip] | None = None) -> Analysis:
    """A copy of `analysis` with the accents' clips (and `extra`, e.g. graphics): never saved, so analysis.json's digest stays."""
    more = clips(tl, pool) + list(extra or [])
    return replace(analysis, clips=analysis.clips + more) if more else analysis


def summary(tl: Timeline) -> dict | None:
    """For the end of a run: how many accents, by class."""
    if not tl.accents:
        return None
    by = Counter(a["kind"] for a in tl.accents)
    return {"accents": len(tl.accents), "by_kind": dict(by)}


def describe(counts: dict[str, int]) -> str:
    """{'whoosh': 3, 'pop': 1} -> '3 whooshes, 1 pop'."""
    order = ("whoosh", "riser", "pop", "hit")
    return ", ".join(f"{counts[k]} {k}{'es' if k == 'whoosh' and counts[k] != 1 else 's' if counts[k] != 1 else ''}" for k in order if counts.get(k))

"""Stage 3: turn a validated plan into a frame-accurate timeline.

All timeline positions are integer frame counts at the sequence rate. Source
in-points are exact Fractions (clip-local seconds, snapped to the source's own
frame grid). The FCPXML writer converts to Final Cut's parent-relative
coordinates; nothing here knows about XML.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction

from . import timecode as tc
from .analyze import Analysis, Clip
from .plan import Plan
from .segments import Segment


@dataclass(frozen=True)
class Style:
    pad_in: float  # seconds kept before the first word of a run
    pad_out: float  # seconds kept after the last word
    max_gap: float  # pauses longer than this inside kept speech are cut out
    remove_fillers: bool = True
    min_broll: float = 1.5  # shortest cutaway worth placing
    fill_shot: float = 4.0  # shot length when filling narration with B-roll
    max_still: float = 6.0  # longest a single still holds


STYLES = {
    "tight": Style(pad_in=0.06, pad_out=0.12, max_gap=0.35, fill_shot=3.0, max_still=5.0),
    "medium": Style(pad_in=0.10, pad_out=0.20, max_gap=0.60),
    "loose": Style(pad_in=0.20, pad_out=0.40, max_gap=1.20, fill_shot=5.0, max_still=7.0),
}


@dataclass
class Marker:
    frame: int  # absolute timeline frame
    value: str
    kind: str = "standard"  # "standard" | "todo" | "chapter"
    note: str = ""


@dataclass
class SpineClip:
    clip_id: str
    src_in: Fraction  # clip-local seconds (asset start not included)
    frames: int
    offset: int  # timeline frame
    section: int
    seg_ids: list[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.offset + self.frames


@dataclass
class ConnectedClip:
    clip_id: str
    src_in: Fraction
    frames: int
    start: int  # absolute timeline frame
    lane: int
    kind: str  # "broll" | "music"
    volume_db: float | None = None
    note: str = ""

    @property
    def end(self) -> int:
        return self.start + self.frames


@dataclass
class Timeline:
    name: str
    frame_duration: Fraction
    width: int
    height: int
    spine: list[SpineClip] = field(default_factory=list)
    connected: list[ConnectedClip] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    section_starts: list[tuple[int, str]] = field(default_factory=list)
    section_frames: dict[int, int] = field(default_factory=dict)  # plan section index -> first frame
    seg_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return self.spine[-1].end if self.spine else 0

    @property
    def seconds(self) -> float:
        return float(self.total_frames * self.frame_duration)

    def spine_at(self, frame: int) -> SpineClip | None:
        for sc in self.spine:
            if sc.offset <= frame < sc.end:
                return sc
        return self.spine[-1] if self.spine and frame >= self.spine[-1].end else None


@dataclass
class _Piece:
    clip_id: str
    a: float  # padded bounds, clip-local seconds
    b: float
    wa: float  # content bounds (first word start / last word end)
    wb: float
    section: int
    seg_ids: list[str]
    # Clip-wide word indexes of the first/last kept word. Two pieces may only be
    # re-joined when nothing was cut between them (w0 == previous w1 + 1).
    w0: int | None = None
    w1: int | None = None
    # True when an edge was limited by a neighbouring (cut) word, so frame
    # rounding must go inward rather than grab a frame of that word.
    a_tight: bool = False
    b_tight: bool = False


# ------------------------------------------------------------------ formats


def parse_format(spec: str) -> tuple[int, int, Fraction | None]:
    """'1080p', '4k', 'vertical', '1080x1920', '3840x2160@23.98' -> (w, h, fd)."""
    presets = {"1080p": (1920, 1080), "4k": (3840, 2160), "uhd": (3840, 2160), "720p": (1280, 720), "vertical": (1080, 1920), "square": (1080, 1080)}
    size, _, rate = spec.lower().partition("@")
    if size in presets:
        w, h = presets[size]
    else:
        try:
            w_s, h_s = size.split("x")
            w, h = int(w_s), int(h_s)
        except ValueError as e:
            raise ValueError(f"can't read format {spec!r}; try 1080p, 4k, vertical, or 1920x1080@29.97") from e
    fd = None
    if rate:
        snapped = tc.snap_frame_duration(rate)
        if snapped is None:
            raise ValueError(f"can't read frame rate in {spec!r}")
        fd = snapped[0]
    return w, h, fd


def choose_format(pieces: list[_Piece], analysis: Analysis, override: str | None) -> tuple[int, int, Fraction]:
    weight: Counter = Counter()
    for p in pieces:
        m = analysis.clip(p.clip_id).media
        if m.kind == "video" and m.frame_duration:
            weight[(m.width, m.height, m.frame_duration)] += p.b - p.a
    if not weight:
        for c in analysis.clips:
            m = c.media
            if m.kind == "video" and m.frame_duration:
                weight[(m.width, m.height, m.frame_duration)] += float(m.duration)
    w, h, fd = weight.most_common(1)[0][0] if weight else (1920, 1080, Fraction(1001, 30000))
    if override:
        ow, oh, ofd = parse_format(override)
        w, h, fd = ow, oh, ofd or fd
    return w, h, fd


# ------------------------------------------------------------- speech runs


def _silence_clamp_end(t: float, word_start: float, silences: list[tuple[float, float]], eps: float = 0.05) -> float:
    """Whisper word ends often run into the following silence; pull them back.

    Only a silence that covers the word's tail counts. A pause in the middle
    of a stretched word must not cut the real audio after it.
    """
    for s, e in silences:
        if word_start < s < t and e >= t - eps:
            return s
    return t


def _silence_clamp_start(t: float, word_end: float, silences: list[tuple[float, float]], eps: float = 0.05) -> float:
    """Mirror of _silence_clamp_end: skip silence covering the word's head."""
    for s, e in silences:
        if s <= t + eps and t < e < word_end:
            return e
    return t


def speech_pieces(seg: Segment, clip: Clip, style: Style, section: int) -> list[_Piece]:
    dur = clip.duration
    if not seg.words:
        # Silence-map segment: its position in the clip stands in for word indexes.
        idx = next((i for i, s in enumerate(clip.segments) if s.id == seg.id), 0)
        a, b = max(0.0, seg.start - style.pad_in), min(dur, seg.end + style.pad_out)
        return [_Piece(clip.id, a, b, seg.start, seg.end, section, [seg.id], idx, idx)]

    all_words = seg.words
    runs: list[list[int]] = []  # indexes into all_words
    current: list[int] = []
    removed_since_last = False
    for i, w in enumerate(all_words):
        if style.remove_fillers and w.is_filler:
            removed_since_last = True
            continue
        if current:
            prev = all_words[current[-1]]
            gap = w.start - prev.end
            if gap > style.max_gap or (removed_since_last and gap > 0.25):
                runs.append(current)
                current = []
        current.append(i)
        removed_since_last = False
    if current:
        runs.append(current)

    # Neighbouring words outside this segment, so padding never grabs the tail
    # of a cut retake or the start of the next sentence.
    idx = next((i for i, s in enumerate(clip.segments) if s.id == seg.id), -1)
    base = sum(len(s.words) for s in clip.segments[:idx]) if idx > 0 else 0
    prev_end = clip.segments[idx - 1].words[-1].end if idx > 0 and clip.segments[idx - 1].words else 0.0
    next_start = (
        clip.segments[idx + 1].words[0].start
        if 0 <= idx < len(clip.segments) - 1 and clip.segments[idx + 1].words
        else dur
    )

    pieces = []
    for run in runs:
        first_i, last_i = run[0], run[-1]
        first, last = all_words[first_i], all_words[last_i]
        wa = _silence_clamp_start(first.start, first.end, clip.silences)
        wb = _silence_clamp_end(last.end, last.start, clip.silences)
        # Padding never reaches into a neighbouring word (including cut fillers).
        lo = (all_words[first_i - 1].end if first_i > 0 else prev_end) + 0.02
        hi = (all_words[last_i + 1].start if last_i + 1 < len(all_words) else next_start) - 0.02
        a = max(0.0, wa - style.pad_in, min(lo, wa))
        b = min(dur, wb + style.pad_out, max(hi, wb))
        if b - a > 0.05:
            pieces.append(
                _Piece(
                    clip.id, a, b, wa, wb, section, [seg.id], base + first_i, base + last_i,
                    a_tight=min(lo, wa) >= wa - style.pad_in and a > 0.0,
                    b_tight=max(hi, wb) <= wb + style.pad_out and b < dur,
                )
            )
    return pieces


def _merge(pieces: list[_Piece], max_gap: float, warnings: list[str] | None = None) -> list[_Piece]:
    """Join back-to-back sentences from the same take when the pause between them is natural.

    Only pieces with nothing cut between them are joined, so removed fillers
    and long pauses stay removed.
    """
    merged: list[_Piece] = []
    for p in pieces:
        if merged:
            q = merged[-1]
            adjacent = q.w1 is not None and p.w0 is not None and p.w0 == q.w1 + 1
            if adjacent and q.clip_id == p.clip_id and q.section == p.section and 0 <= p.wa - q.wb <= max_gap:
                q.b = max(q.b, p.b)
                q.wb = max(q.wb, p.wb)
                q.w1 = p.w1
                q.seg_ids.extend(s for s in p.seg_ids if s not in q.seg_ids)
                continue
            if q.clip_id == p.clip_id and p.a < q.b and p.a >= q.a:
                # The next pick starts inside the previous one: split the overlap,
                # but never into either side's words.
                mid = min(max((q.b + p.a) / 2, q.wb), max(q.wb, p.wa))
                q.b = min(q.b, mid)
                q.b_tight = True
                p.a = max(p.a, q.b)
                p.a_tight = True
                if p.b - p.a < 0.05:
                    if warnings is not None:
                        warnings.append(f"{p.clip_id}: {', '.join(p.seg_ids) or 'range'} repeats the previous pick; skipped")
                    continue
        merged.append(p)
    return merged


# --------------------------------------------------------------- resolve


def build_timeline(
    plan: Plan,
    analysis: Analysis,
    *,
    style: Style,
    name: str,
    format_override: str | None = None,
    broll_db: float | None = -20.0,
    music_db: float = -14.0,
    fill: bool = True,
) -> Timeline:
    warnings: list[str] = []
    raw_pieces: list[_Piece] = []
    for s_idx, section in enumerate(plan.sections):
        for item in section.items:
            clip = analysis.clip(item.clip_id)
            if item.is_segment:
                seg = analysis.segment(item.seg_id)
                raw_pieces.extend(speech_pieces(seg, clip, style, s_idx))
            else:
                raw_pieces.append(_Piece(clip.id, item.start, item.end, item.start, item.end, s_idx, []))
    pieces = _merge(raw_pieces, style.max_gap, warnings)

    width, height, seq_fd = choose_format(pieces, analysis, format_override)
    tl = Timeline(name=name, frame_duration=seq_fd, width=width, height=height)

    offset = 0
    last_section = -1
    for p in pieces:
        clip = analysis.clip(p.clip_id)
        m = clip.media
        if m.kind == "image":
            src_in = Fraction(0)
            frames = max(1, tc.round_frames(Fraction(p.b - p.a).limit_denominator(100000), seq_fd))
        else:
            grid = m.frame_duration if m.kind == "video" and m.frame_duration else seq_fd
            a = Fraction(p.a).limit_denominator(1000000)
            b = Fraction(p.b).limit_denominator(1000000)
            # Round outward for breathing room, inward where a cut word is next door.
            in_frames = tc.ceil_frames(a, grid) if p.a_tight else tc.floor_frames(a, grid)
            src_in = in_frames * grid
            prev = tl.spine[-1] if tl.spine else None
            if prev is not None and prev.clip_id == p.clip_id:
                prev_end = prev.src_in + prev.frames * seq_fd
                if prev.src_in <= src_in < prev_end:
                    # Never replay a frame the previous edit already showed.
                    src_in = tc.ceil_frames(prev_end, grid) * grid
            span = b - src_in
            frames = tc.floor_frames(span, seq_fd) if p.b_tight else tc.ceil_frames(span, seq_fd)
            available = tc.floor_frames(m.duration - src_in, seq_fd)
            frames = min(frames, available)
        if frames < 1:
            warnings.append(f"{p.clip_id}: piece at {p.a:.2f}s too short to place")
            continue
        prev = tl.spine[-1] if tl.spine else None
        if (
            prev is not None
            and prev.clip_id == p.clip_id
            and prev.section == p.section
            and m.kind != "image"
            and prev.src_in + prev.frames * seq_fd == src_in
        ):
            prev.frames += frames
            prev.seg_ids.extend(s for s in p.seg_ids if s not in prev.seg_ids)
            sc = prev
        else:
            sc = SpineClip(clip_id=p.clip_id, src_in=src_in, frames=frames, offset=offset, section=p.section, seg_ids=list(p.seg_ids))
            tl.spine.append(sc)
        if p.section != last_section:
            tl.section_starts.append((offset, plan.sections[p.section].name))
            tl.section_frames.setdefault(p.section, offset)
            last_section = p.section
        # Map each segment's content to timeline frames through this clip.
        for seg_id in p.seg_ids:
            seg = analysis.segment(seg_id)
            seg_a = max(Fraction(seg.start).limit_denominator(1000000), sc.src_in)
            seg_b = Fraction(seg.end).limit_denominator(1000000)
            f0 = sc.offset + max(0, tc.floor_frames(seg_a - sc.src_in, seq_fd))
            f1 = min(sc.end, sc.offset + tc.ceil_frames(seg_b - sc.src_in, seq_fd))
            f1 = max(f1, f0 + 1)
            if seg_id in tl.seg_ranges:
                o0, o1 = tl.seg_ranges[seg_id]
                f0, f1 = min(f0, o0), max(f1, o1)
            tl.seg_ranges[seg_id] = (f0, f1)
        offset += frames

    lanes = _Lanes()
    _place_broll(tl, plan, analysis, style, broll_db, warnings, lanes)
    if fill:
        _fill_picture(tl, analysis, style, broll_db, lanes)
    _place_music(tl, plan, analysis, music_db, warnings)
    _place_markers(tl, plan, analysis)
    tl.warnings = warnings
    return tl


class _Lanes:
    """Which frames each connected lane already holds."""

    def __init__(self) -> None:
        self.used: dict[int, list[tuple[int, int]]] = {}

    def free(self, start: int, end: int) -> int:
        lane = 1
        while any(not (end <= s or start >= e) for s, e in self.used.get(lane, [])):
            lane += 1
        self.used.setdefault(lane, []).append((start, end))
        return lane


def _video_window(m, want: int, fd: Fraction, hint: Fraction | None) -> tuple[Fraction, int]:
    """Source in-point and length (frames) for a cutaway of `want` frames."""
    grid = m.frame_duration or fd
    dur = m.duration
    need = want * fd
    if hint is not None:
        # Back off a late in-point so the cutaway still fills its spot.
        src = min(hint, max(Fraction(0), dur - need))
    else:
        src = min(dur * Fraction(15, 100), max(Fraction(0), dur - need))
    src = max(Fraction(0), min(src, dur - grid))
    src_in = tc.floor_frames(src, grid) * grid
    return src_in, min(want, tc.floor_frames(dur - src_in, fd))


def _place_broll(
    tl: Timeline, plan: Plan, analysis: Analysis, style: Style, volume_db: float | None, warnings: list[str], lanes: _Lanes
) -> None:
    fd = tl.frame_duration
    min_frames = tc.ceil_frames(Fraction(style.min_broll).limit_denominator(1000), fd)
    max_still = tc.floor_frames(Fraction(style.max_still).limit_denominator(1000), fd)
    for section in plan.sections:
        # Consecutive entries over the same lines play one after another, so a
        # long narrated sentence can get several shots.
        groups: list[list] = []
        for b in section.broll:
            if groups and set(groups[-1][0].over) == set(b.over):
                groups[-1].append(b)
            else:
                groups.append([b])
        for group in groups:
            ranges = [tl.seg_ranges[s] for s in group[0].over if s in tl.seg_ranges]
            if not ranges:
                continue
            start = min(r[0] for r in ranges)
            end = max(r[1] for r in ranges)
            if end - start < min_frames:
                end = min(tl.total_frames, start + min_frames)
            cursor = start
            for k, b in enumerate(group):
                remaining = end - cursor
                if remaining < 1:
                    warnings.append(f"B-roll {b.clip_id}: no time left over {', '.join(b.over)}; skipped")
                    continue
                want = remaining if k == len(group) - 1 else max(1, remaining // (len(group) - k))
                clip = analysis.clip(b.clip_id)
                m = clip.media
                if m.kind == "image":
                    src_in, frames = Fraction(0), min(want, max_still)
                else:
                    hint = Fraction(b.source_in).limit_denominator(1000000) if b.source_in >= 0 else None
                    src_in, frames = _video_window(m, want, fd, hint)
                if frames < min(min_frames, want):
                    warnings.append(f"B-roll {b.clip_id} is too short for its spot; skipped")
                    continue
                tl.connected.append(
                    ConnectedClip(
                        clip_id=b.clip_id,
                        src_in=src_in,
                        frames=frames,
                        start=cursor,
                        lane=lanes.free(cursor, cursor + frames),
                        kind="broll",
                        volume_db=volume_db if m.has_audio else None,
                        note=b.reason,
                    )
                )
                cursor += frames


# ---------------------------------------------------------- picture fill


def _extend_shot_before(tl: Timeline, analysis: Analysis, g0: int, g1: int) -> bool:
    """Close a sliver of a gap by holding the previous shot a little longer."""
    for c in tl.connected:
        m = analysis.clip(c.clip_id).media
        if c.end != g0 or not m.has_video:
            continue
        if m.kind == "image" or c.src_in + (c.frames + g1 - g0) * tl.frame_duration <= m.duration:
            c.frames += g1 - g0
            return True
    return False

_STOP = set(
    """the and for are but not you all any can had her was one our out day get has him his how man new now old see two
    way who boy did its let put say she too use this that with have from they will what when your there their them than
    then these those some into over also just like been were more most much very about after before where which while
    here being because through could would should going really right okay yeah well even still only other each such
    own same both few many make made thing things people time year years jpg png mov mp4 final export copy""".split()
)


def _tokens(text: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # HondaCenter -> Honda Center
    words = re.findall(r"[a-zA-Z]+", spaced.lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


_NAME = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:[ -](?:[A-Z][a-z]+|[A-Z]{2,}))+")


def _names(text: str) -> list[set[str]]:
    """Multi-word proper names ("Orange County", "Honda Center") as token sets."""
    names = []
    for match in _NAME.finditer(text):
        toks = _tokens(match.group(0))
        if len(toks) >= 2:
            names.append(toks)
    return names


def _match_score(clip_toks: set[str], said: set[str], names: list[set[str]], idf: dict[str, float]) -> float:
    """How well a clip fits what's being said. Matching a whole name counts
    double; matching one word of a name ("orange" from "Orange County") counts
    for little."""
    score = 0.0
    for t in clip_toks & said:
        weight = idf.get(t, 1.0)
        for name in names:
            if t in name:
                weight *= 2.0 if name <= clip_toks else 0.3
                break
        score += weight
    return score


def _clip_tokens(clip: Clip) -> set[str]:
    text = clip.media.stem
    if clip.visual:
        text += " " + clip.visual.get("description", "") + " " + " ".join(clip.visual.get("tags", []))
    return _tokens(text)


def _gaps_without_picture(tl: Timeline, analysis: Analysis) -> list[tuple[int, int]]:
    """Frames where nothing visual is on screen: audio-only storyline, no cutaway."""
    blank = [(sc.offset, sc.end) for sc in tl.spine if not analysis.clip(sc.clip_id).media.has_video]
    covered = sorted((c.start, c.end) for c in tl.connected if analysis.clip(c.clip_id).media.has_video)
    gaps: list[tuple[int, int]] = []
    for start, end in blank:
        cursor = start
        for c0, c1 in covered:
            if c1 <= cursor or c0 >= end:
                continue
            if c0 > cursor:
                gaps.append((cursor, c0))
            cursor = max(cursor, c1)
        if cursor < end:
            gaps.append((cursor, end))
    merged: list[tuple[int, int]] = []
    for g in sorted(gaps):
        if merged and g[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], g[1]))
        else:
            merged.append(g)
    return merged


MIN_MATCH = 2.0  # roughly one distinctive shared word


def _fill_picture(tl: Timeline, analysis: Analysis, style: Style, volume_db: float | None, lanes: _Lanes) -> None:
    """Cover narration the plan left without picture, so no frame is black.

    Shots are chosen by matching what's being said to clip names and visual
    descriptions (rare words count most), then by least used.
    """
    gaps = _gaps_without_picture(tl, analysis)
    if not gaps:
        return
    pool = [c for c in analysis.clips if c.media.has_video and c.role in ("broll", "still") and not c.hidden]
    if not pool:
        tl.warnings.append("Narration has no picture and there are no B-roll clips or stills to fill it with.")
        return
    fd = tl.frame_duration
    shot = max(1, tc.round_frames(Fraction(style.fill_shot).limit_denominator(1000), fd))
    tiny = tc.round_frames(Fraction(1, 2), fd)
    order = {c.id: i for i, c in enumerate(analysis.clips)}

    docs = {c.id: _clip_tokens(c) for c in pool}
    df: dict[str, int] = {}
    for toks in docs.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((1 + len(pool)) / (1 + n)) + 1 for t, n in df.items()}

    uses = {c.id: 0 for c in analysis.clips}
    for c in tl.connected:
        uses[c.clip_id] = uses.get(c.clip_id, 0) + 1
    video_cursor: dict[str, Fraction] = {}
    placed_frames = 0
    placed_shots = 0
    last_clip = None

    def said_during(f0: int, f1: int) -> tuple[set[str], list[set[str]]]:
        text = []
        for seg_id, (s0, s1) in tl.seg_ranges.items():
            if s1 > f0 - shot and s0 < f1:  # include the line leading in
                seg = analysis.segment(seg_id)
                if seg:
                    text.append(seg.text)
        joined = " ".join(text)
        names = _names(joined)
        # "OC Vibe" should also match files named OCVIBE_...
        said = _tokens(joined) | {re.sub(r"[^a-z]", "", m.group(0).lower()) for m in _NAME.finditer(joined)}
        return said, names

    for g0, g1 in gaps:
        if not pool:
            break
        if g1 - g0 < tiny and _extend_shot_before(tl, analysis, g0, g1):
            placed_frames += g1 - g0
            continue
        cursor = g0
        while cursor < g1 and pool:
            remaining = g1 - cursor
            want = remaining if remaining < shot + tiny else shot
            said, names = said_during(cursor, cursor + want)

            def rank(c: Clip) -> tuple:
                # Long videos can be revisited (a new part each time); a still
                # shown again reads as a mistake, so stills wear out fast.
                wear = uses.get(c.id, 0) * (0.5 if c.media.kind == "video" else 10.0)
                fit = _match_score(docs[c.id], said, names, idf) / (1 + wear)
                if fit < MIN_MATCH:
                    fit = 0.0  # a stray shared word isn't a reason to pick a shot
                # With nothing to match, moving footage beats a random still.
                return (fit, c.media.kind == "video", -uses.get(c.id, 0), -order[c.id])

            candidates = [c for c in pool if c.id != last_clip] or pool
            best = max(candidates, key=rank)
            m = best.media
            if m.kind == "image":
                src_in, frames = Fraction(0), want
            else:
                grid = m.frame_duration or fd
                start_at = video_cursor.get(best.id, m.duration * Fraction(1, 10))
                if start_at + want * fd > m.duration:
                    start_at = m.duration * Fraction(1, 10)
                src_in, frames = _video_window(m, want, fd, start_at)
                video_cursor[best.id] = src_in + frames * fd + 1
            if frames < 1:
                pool = [c for c in pool if c is not best]
                continue
            tl.connected.append(
                ConnectedClip(
                    clip_id=best.id,
                    src_in=src_in,
                    frames=frames,
                    start=cursor,
                    lane=lanes.free(cursor, cursor + frames),
                    kind="broll",
                    volume_db=volume_db if m.has_audio else None,
                    note="auto-fill",
                )
            )
            uses[best.id] = uses.get(best.id, 0) + 1
            last_clip = best.id
            cursor += frames
            placed_frames += frames
            placed_shots += 1
    if placed_shots:
        tl.notes.append(
            f"Filled {tc.seconds_to_clock(float(placed_frames * fd))} of narration with {placed_shots} B-roll shots "
            "the plan didn't specify, matched to what's being said by clip names and descriptions."
        )


def _place_music(tl: Timeline, plan: Plan, analysis: Analysis, volume_db: float, warnings: list[str]) -> None:
    fd = tl.frame_duration
    placed = sorted(tl.section_frames.items())
    for cue in plan.music:
        inside = [f for i, f in placed if cue.first_section <= i <= cue.last_section]
        if not inside:
            warnings.append(f"music {cue.clip_id}: sections {cue.first_section + 1}-{cue.last_section + 1} aren't on the timeline; skipped")
            continue
        start = min(inside)
        after = [f for i, f in placed if i > cue.last_section]
        end = min(after) if after else tl.total_frames
        m = analysis.clip(cue.clip_id).media
        src_in = Fraction(cue.source_in).limit_denominator(1000)
        src_in = tc.floor_frames(src_in, fd) * fd
        if src_in >= m.duration:
            src_in = Fraction(0)
        frames = min(end - start, tc.floor_frames(m.duration - src_in, fd))
        if frames < 1:
            warnings.append(f"music {cue.clip_id} has no room; skipped")
            continue
        tl.connected.append(ConnectedClip(cue.clip_id, src_in, frames, start, lane=-1 - sum(1 for c in tl.connected if c.kind == "music"), kind="music", volume_db=volume_db))


def _place_markers(tl: Timeline, plan: Plan, analysis: Analysis) -> None:
    for frame, name in tl.section_starts:
        tl.markers.append(Marker(frame=frame, value=name, kind="chapter"))
    for alt in plan.alternates:
        rng = tl.seg_ranges.get(alt["chosen"])
        if not rng or not alt["others"]:
            continue
        others = []
        for o in alt["others"]:
            seg = analysis.segment(o)
            clip = analysis.clip(seg.clip_id)
            others.append(f'{o} ({clip.media.name} @ {tc.seconds_to_clock(seg.start)}): "{seg.text[:60]}"')
        note = (alt.get("note", "") + " | " if alt.get("note") else "") + "; ".join(others)
        tl.markers.append(Marker(frame=rng[0], value=f"ALT takes: {', '.join(alt['others'])}", kind="standard", note=note))
    for flag in plan.flags:
        rng = tl.seg_ranges.get(flag["ref"])
        if rng is None:
            # Flags about clips on the timeline land on the clip's first use.
            clip_first = next((sc.offset for sc in tl.spine if sc.clip_id == flag["ref"]), None)
            if clip_first is None:
                continue
            frame = clip_first
        else:
            frame = rng[0]
        tl.markers.append(Marker(frame=frame, value=flag["note"][:120], kind="todo", note=flag["note"]))
    tl.markers.sort(key=lambda mk: mk.frame)


def timeline_stats(tl: Timeline) -> dict:
    return {
        "frames": tl.total_frames,
        "seconds": tl.seconds,
        "spine_clips": len(tl.spine),
        "broll": sum(1 for c in tl.connected if c.kind == "broll"),
        "music": sum(1 for c in tl.connected if c.kind == "music"),
        "rate": tc.rate_label(tl.frame_duration),
        "size": f"{tl.width}x{tl.height}",
    }

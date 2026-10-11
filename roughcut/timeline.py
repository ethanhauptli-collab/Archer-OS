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
from dataclasses import dataclass, field, replace
from fractions import Fraction

from . import timecode as tc
from .analyze import Analysis, Clip
from .plan import MusicCue, Plan
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
    volume_db: float | None = None  # clip volume (level matching); None = as recorded
    # First word start / last word end (clip-local seconds) when the edges are speech,
    # so a de-click fade never reaches into a word. None: not known.
    wa: float | None = None
    wb: float | None = None
    fade_in: Fraction = Fraction(0)  # seconds of audio fade at each end
    fade_out: Fraction = Fraction(0)

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
    fade_in: Fraction = Fraction(0)
    fade_out: Fraction = Fraction(0)
    # Volume automation: (frames into this clip, dB). Empty: volume_db throughout.
    keyframes: list[tuple[int, float]] = field(default_factory=list)

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
    dialogue_level: float | None = None  # LUFS speech was matched to (None: levels as recorded)
    levels: list[dict] = field(default_factory=list)  # one row per clip whose volume was set (see _mix)

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


def _pause_between(prev_start: float, next_end: float, silences: list[tuple[float, float]], eps: float = 0.05) -> float:
    """Longest measured silence between two words.

    Whisper often stretches a word across the pause after it (or starts the
    next word early), so the words' own times can show no gap at all. The
    silence map still sees the pause.
    """
    return max((e - s for s, e in silences if s >= prev_start + eps and e <= next_end - eps), default=0.0)


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
            gap = max(w.start - prev.end, _pause_between(prev.start, w.end, clip.silences))
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
    sil = clip.silences

    def end_of(w) -> float:  # where a neighbouring word's sound really stops / starts
        return _silence_clamp_end(w.end, w.start, sil)

    def start_of(w) -> float:
        return _silence_clamp_start(w.start, w.end, sil)

    prev_end = end_of(clip.segments[idx - 1].words[-1]) if idx > 0 and clip.segments[idx - 1].words else 0.0
    next_start = (
        start_of(clip.segments[idx + 1].words[0])
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
        lo = (end_of(all_words[first_i - 1]) if first_i > 0 else prev_end) + 0.02
        hi = (start_of(all_words[last_i + 1]) if last_i + 1 < len(all_words) else next_start) - 0.02
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


def _cut_silences(pieces: list[_Piece], analysis: Analysis, style: Style) -> list[_Piece]:
    """Remove long silences inside kept talking footage, wherever they ended up.

    Whisper stretches a word over the pause beside it, before or after, by
    any amount, so a pause can hide inside a word, between two sentences that
    were joined, or inside a timed range. Cutting the silence itself never
    removes sound, so this runs last. Only silences with sound on both sides
    inside the piece go: a quiet reaction shot or B-roll stays as chosen.
    A "silence" that holds a whole transcribed word is quiet speech under a
    raised level (music bed, loud room), not dead air, so it stays too.
    """
    out: list[_Piece] = []
    for p in pieces:
        clip = analysis.clip(p.clip_id)
        spoken = [(w.start, w.end) for seg in clip.segments for w in seg.words]
        if p.seg_ids:
            longest = style.max_gap
        elif clip.role in ("aroll", "voiceover"):  # a timed range of talking footage
            longest = max(1.0, 2 * style.max_gap)
        else:
            out.append(p)
            continue
        cur = p
        for s, e in sorted(clip.silences):
            if e - s > longest and cur.wa < s and e < cur.wb and not any(s <= ws and we <= e for ws, we in spoken):
                out.append(replace(cur, b=min(cur.b, s + style.pad_out), wb=s, w1=None, b_tight=False, seg_ids=list(cur.seg_ids)))
                cur = replace(cur, a=max(cur.a, e - style.pad_in), wa=e, w0=None, a_tight=False, seg_ids=list(cur.seg_ids))
        out.append(cur)
    return out


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
    dialogue_level: float | None = None,
    fades: bool = True,
    music_beds: bool = True,
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
    pieces = _cut_silences(_merge(raw_pieces, style.max_gap, warnings), analysis, style)

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
            prev.wb = p.wb if p.seg_ids else None
            sc = prev
        else:
            sc = SpineClip(clip_id=p.clip_id, src_in=src_in, frames=frames, offset=offset, section=p.section, seg_ids=list(p.seg_ids))
            sc.wa, sc.wb = (p.wa, p.wb) if p.seg_ids else (None, None)
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
    _place_music(tl, plan, analysis, music_db, warnings, beds=music_beds)
    _place_markers(tl, plan, analysis)
    _mix(tl, analysis, dialogue_level=dialogue_level, broll_db=broll_db, music_db=music_db)
    _duck_music(tl, analysis)
    _music_fades(tl, analysis)
    if fades:
        _fades(tl, analysis)
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


def _music_cues(plan: Plan, analysis: Analysis, beds: bool, notes: list[str]) -> list[MusicCue]:
    """The plan's music cues, or the --music song under the whole cut when the plan didn't use it."""
    songs = [c for c in analysis.clips if c.music_bed] if beds else []
    if not songs or not plan.sections or any(analysis.clip(cue.clip_id).music_bed for cue in plan.music):
        return list(plan.music)
    song = songs[0]
    dropped = sorted({analysis.clip(cue.clip_id).media.name for cue in plan.music})
    notes.append(
        f"Music: {song.media.name} (--music) runs under the whole cut"
        + (f" instead of the plan's choice ({', '.join(dropped)})" if dropped else "")
        + (f"; {', '.join(c.media.name for c in songs[1:])} not used" if len(songs) > 1 else "")
        + "."
    )
    return [MusicCue(song.id, 0, len(plan.sections) - 1, 0.0)]


def _last_sound(clip: Clip) -> Fraction:
    """Where the song really ends (its last audible moment), for back-timing."""
    if clip.sound_end is not None:
        end = Fraction(clip.sound_end).limit_denominator(1000)
    else:
        from .ffmpeg_ops import sound_regions

        regions = sound_regions(clip.silences, clip.duration)
        end = Fraction(regions[-1][1]).limit_denominator(1000) if regions else clip.media.duration
    return min(end, clip.media.duration)


def _place_music(tl: Timeline, plan: Plan, analysis: Analysis, volume_db: float, warnings: list[str], *, beds: bool = True) -> None:
    """Music cues under their sections. The cue that runs to the end of the cut is
    back-timed: it starts wherever it must so the song's own ending lands on the
    cut's last frame."""
    fd = tl.frame_duration
    placed = sorted(tl.section_frames.items())
    for cue in _music_cues(plan, analysis, beds, tl.notes):
        inside = [f for i, f in placed if cue.first_section <= i <= cue.last_section]
        if not inside:
            warnings.append(f"music {cue.clip_id}: sections {cue.first_section + 1}-{cue.last_section + 1} aren't on the timeline; skipped")
            continue
        start = min(inside)
        after = [f for i, f in placed if i > cue.last_section]
        end = min(after) if after else tl.total_frames
        clip = analysis.clip(cue.clip_id)
        m = clip.media
        src_in = Fraction(cue.source_in).limit_denominator(1000)
        src_in = tc.floor_frames(src_in, fd) * fd
        if src_in >= m.duration:
            src_in = Fraction(0)
        if end == tl.total_frames and end > start:
            last = _last_sound(clip)
            lead = last - (end - start) * fd
            if lead >= 0:
                src_in = tc.floor_frames(lead, fd) * fd
                tl.notes.append(
                    f"Music: {m.name} is back-timed so its ending lands on the last frame; it starts "
                    f"{tc.seconds_to_clock(float(src_in))} into the track."
                )
            else:
                src_in = Fraction(0)
                warnings.append(
                    f"music {m.name} ({tc.seconds_to_clock(float(last))}) is shorter than the "
                    f"{tc.seconds_to_clock(float((end - start) * fd))} it covers, so it ends before the cut does"
                )
        frames = min(end - start, tc.floor_frames(m.duration - src_in, fd))
        if frames < 1:
            warnings.append(f"music {cue.clip_id} has no room; skipped")
            continue
        tl.connected.append(ConnectedClip(cue.clip_id, src_in, frames, start, lane=-1 - sum(1 for c in tl.connected if c.kind == "music"), kind="music", volume_db=volume_db))


# ------------------------------------------------------------------- audio

DIALOGUE_LEVEL = -16.0  # LUFS: what speech is matched to (a common level for online video)
MAX_BOOST = 12.0  # dB
MAX_CUT = 12.0  # dB
PEAK_CEILING = -1.0  # dBFS: a boost never pushes a clip's peaks past this
SPINE_BROLL_DB = -6.0  # nat sound of B-roll in the main storyline, relative to the dialogue
BUSY_BACKGROUND = 20.0  # dB: a boosted talker whose background is closer than this under the words gets a note


def _measured(clip: Clip, speech: bool) -> tuple[float | None, float | None]:
    """(loudness, peak) to match: the words when `speech` and they were measured, else the whole clip."""
    if speech and clip.speech_loudness is not None:
        return clip.speech_loudness, clip.speech_peak
    return clip.loudness, clip.peak_db


def level_match(clip: Clip, target: float, base: float = 0.0, *, speech: bool = False) -> tuple[float, str] | None:
    """(clip volume in dB, a note on it) so `clip` plays at `target` + `base` LUFS.

    None when the clip wasn't measured. Speech is matched by the loudness of
    its words, everything else by the whole clip. The correction is held to
    +/-12 dB, and a boost stops before the peaks pass PEAK_CEILING (a limit
    never turns a boost into a cut). A talker's background comes up with
    the words, so the clip keeps its own balance, whether that is a music
    bed under a finished promo or a busy room; the note says so when the
    background sits close under the words.
    """
    lufs, peak = _measured(clip, speech)
    if lufs is None:
        return None
    want = target - lufs
    match = max(-MAX_CUT, min(MAX_BOOST, want))
    notes = [] if match == want else [f"held to {MAX_BOOST if want > 0 else -MAX_CUT:+g} dB"]
    if match > 0 and peak is not None:
        cap = max(0.0, math.floor((PEAK_CEILING - peak - base) * 2) / 2)  # in the half-dB steps volumes use
        if cap < match:
            match = cap
            notes = [
                f"peaks would clip, so it plays {want - cap:.0f} dB under the dialogue level; a Limiter effect on it lets it come up"
                if speech
                else "boost limited by peaks"
            ]
    if speech and match >= 3.0 and clip.loudness_floor is not None and lufs - clip.loudness_floor < BUSY_BACKGROUND:
        notes.append(f"its background, {lufs - clip.loudness_floor:.0f} dB under the words, comes up too")
    return base + match, "; ".join(notes)


def _round_db(v: float) -> float:
    return round(v * 2) / 2 + 0.0  # half-dB steps; + 0.0 turns -0.0 into 0.0


def _mix(tl: Timeline, analysis: Analysis, *, dialogue_level: float | None, broll_db: float | None, music_db: float) -> None:
    """Clip volumes that bring every source to one dialogue level.

    Speech plays at `dialogue_level`, B-roll in the main storyline 6 dB
    under it, cutaway nat sound at `broll_db` and the music bed at
    `music_db` relative to it. Unmeasured clips keep the plain gains the
    placement gave them.
    """
    tl.dialogue_level = dialogue_level
    if dialogue_level is None:
        return
    rows: dict[tuple[str, str], dict] = {}

    def row(clip: Clip, use: str, speech: bool, volume: float | None, why: str) -> None:
        lufs = _measured(clip, speech)[0]
        words = speech and clip.speech_loudness is not None
        rows.setdefault((clip.id, use), {"clip": clip.id, "use": use, "lufs": lufs, "words": words, "volume": volume, "why": why})

    for sc in tl.spine:
        clip = analysis.clip(sc.clip_id)
        if not clip.media.has_audio:
            continue
        # A line from a clip classified as B-roll is still dialogue when the plan uses it as one.
        speech = _is_speech(sc, clip)
        got = level_match(clip, dialogue_level, 0.0 if speech else SPINE_BROLL_DB, speech=speech)
        if got is None:
            continue
        volume = _round_db(got[0])
        sc.volume_db = volume if abs(volume) >= 1.0 else None
        use = ("narration" if clip.role == "voiceover" else "dialogue") if speech else "nat sound"
        row(clip, use, speech, sc.volume_db, got[1])
    for cc in tl.connected:
        clip = analysis.clip(cc.clip_id)
        base = music_db if cc.kind == "music" else broll_db if cc.kind == "broll" else None
        if clip is None or not clip.media.has_audio or base is None:
            continue
        got = level_match(clip, dialogue_level, base)
        if got is None:
            continue
        cc.volume_db = _round_db(got[0])
        row(clip, "music bed" if cc.kind == "music" else "cutaway nat sound", False, cc.volume_db, got[1])
    tl.levels = list(rows.values())


DUCK_LIFT = 8.0  # dB the music bed comes up where nobody speaks
DUCK_GAP = Fraction(3, 2)  # seconds without speech before it does
DUCK_RAMP = Fraction(1, 2)  # seconds to come up or go back down


def _is_speech(sc: SpineClip, clip: Clip) -> bool:
    return clip.media.has_audio and (clip.role in ("aroll", "voiceover") or bool(sc.seg_ids))


def _duck_music(tl: Timeline, analysis: Analysis) -> None:
    """Music sits at its bed level under speech and comes up DUCK_LIFT dB in
    stretches of DUCK_GAP or more where nobody speaks (B-roll montages,
    stills, the open and close), as volume keyframes in the music clip,
    never above the dialogue level. In a cut where nobody speaks at all the
    song is the programme: it plays at the dialogue level, without keyframes."""
    fd = tl.frame_duration
    speech: list[list[int]] = []
    for sc in tl.spine:
        if _is_speech(sc, analysis.clip(sc.clip_id)):
            if speech and sc.offset <= speech[-1][1]:
                speech[-1][1] = max(speech[-1][1], sc.end)
            else:
                speech.append([sc.offset, sc.end])
    min_gap = tc.ceil_frames(DUCK_GAP, fd)
    ramp = max(1, tc.round_frames(DUCK_RAMP, fd))

    def level_row(cc: ConnectedClip) -> dict:
        return next((r for r in tl.levels if r["clip"] == cc.clip_id and r["use"] == "music bed"), {})

    for cc in tl.connected:
        if cc.kind != "music":
            continue
        clip = analysis.clip(cc.clip_id)
        # The volume at which the song plays as loud as the dialogue.
        full = level_match(clip, tl.dialogue_level) if tl.dialogue_level is not None else None
        if not speech:
            cc.keyframes = []
            cc.volume_db = _round_db(full[0]) if full else None
            if cc.volume_db is not None and abs(cc.volume_db) < 1.0:
                cc.volume_db = None
            row = level_row(cc)
            if row:
                row["volume"] = cc.volume_db
                row["why"] = "; ".join(filter(None, ["nobody speaks in this cut, so it plays at the dialogue level", full[1] if full else ""]))
            continue
        low = cc.volume_db if cc.volume_db is not None else 0.0
        high = low + DUCK_LIFT
        if full is not None:
            high = min(high, _round_db(full[0]))
        if high - low < 1.0:
            cc.keyframes = []
            continue
        gaps, cursor = [], cc.start
        for s0, s1 in speech:
            if s1 <= cursor or s0 >= cc.end:
                continue
            if s0 > cursor:
                gaps.append((cursor, s0))
            cursor = max(cursor, s1)
        if cursor < cc.end:
            gaps.append((cursor, cc.end))
        points: list[tuple[int, float]] = []
        for g0, g1 in gaps:
            if g1 - g0 < min_gap:
                continue
            r = max(1, min(ramp, (g1 - g0) // 4))
            points += [(g0, high)] if g0 == cc.start else [(g0, low), (g0 + r, high)]
            if g1 < cc.end:
                points += [(g1 - r, high), (g1, low)]  # back down by the time the voice comes in
        cc.keyframes = [(f - cc.start, v) for f, v in points]
        row = level_row(cc)
        if points and row:
            row["why"] = "; ".join(filter(None, [row["why"], f"comes up {high - low:g} dB where nobody speaks"]))


MUSIC_FADE_IN = Fraction(1)  # seconds
MUSIC_FADE_OUT = Fraction(2)
DECLICK = Fraction(15, 1000)  # seconds: fade at a speech edit, too short to hear, long enough to stop a tick
SPINE_BROLL_FADE = 6  # frames of fade on B-roll nat sound in the main storyline...
CUTAWAY_FADE = Fraction(3, 10)  # ...and seconds on connected B-roll; both at most a quarter of the clip
AUDIO_GRID = 48000  # fades are whole samples at 48 kHz


def _samples(t: Fraction) -> Fraction:
    return Fraction(math.floor(max(Fraction(0), t) * AUDIO_GRID), AUDIO_GRID)


def _continuous(a: SpineClip | None, b: SpineClip | None, fd: Fraction) -> bool:
    """True when b's audio carries straight on from a's (same clip, no gap in the source)."""
    return a is not None and b is not None and a.clip_id == b.clip_id and a.src_in + a.frames * fd == b.src_in


def _fades(tl: Timeline, analysis: Analysis) -> None:
    """Short fades where the audio jumps, so the first pass plays without ticks.

    Speech edits get a 15 ms de-click that ends before the first word and
    starts after the last one (no fade where a cut sits right on a word).
    B-roll nat sound eases in and out over a few frames. Fades are Final
    Cut fade handles, so they stay draggable.
    """
    fd = tl.frame_duration
    for i, sc in enumerate(tl.spine):
        clip = analysis.clip(sc.clip_id)
        if not clip.media.has_audio or clip.media.kind == "image":
            continue
        length = sc.frames * fd
        head = not _continuous(tl.spine[i - 1] if i > 0 else None, sc, fd)
        tail = not _continuous(sc, tl.spine[i + 1] if i + 1 < len(tl.spine) else None, fd)
        if clip.role in ("aroll", "voiceover") or sc.seg_ids:
            room_in = DECLICK if sc.wa is None else Fraction(sc.wa).limit_denominator(1000000) - sc.src_in
            room_out = DECLICK if sc.wb is None else sc.src_in + length - Fraction(sc.wb).limit_denominator(1000000)
            fade_in, fade_out = min(DECLICK, room_in), min(DECLICK, room_out)
        else:  # nat sound under B-roll (or music) in the main storyline
            fade_in = fade_out = min(SPINE_BROLL_FADE * fd, length / 4)
        sc.fade_in = _samples(min(fade_in, length / 2)) if head else Fraction(0)
        sc.fade_out = _samples(min(fade_out, length / 2)) if tail else Fraction(0)
    for cc in tl.connected:
        clip = analysis.clip(cc.clip_id)
        if cc.kind == "broll" and clip is not None and clip.media.has_audio:
            cc.fade_in = cc.fade_out = _samples(min(CUTAWAY_FADE, cc.frames * fd / 4))


def _music_fades(tl: Timeline, analysis: Analysis) -> None:
    """The music bed fades in over a second and out over two, unless it plays
    through its own ending. Part of the bed, like the ducking, so `--fades
    off` (which is about edits) leaves it."""
    fd = tl.frame_duration
    for cc in tl.connected:
        clip = analysis.clip(cc.clip_id)
        if cc.kind != "music" or clip is None or not clip.media.has_audio:
            continue
        quarter = cc.frames * fd / 4
        cc.fade_in = _samples(min(MUSIC_FADE_IN, quarter))
        # A song that plays through its own ending (sound dying away before the
        # file ends) needs no fade-out; one cut off mid-song, or a file that stops dead, does.
        last = _last_sound(clip)
        ends_itself = cc.src_in + (cc.frames + 1) * fd >= last and last <= clip.media.duration - Fraction(1, 4)
        cc.fade_out = Fraction(0) if ends_itself else _samples(min(MUSIC_FADE_OUT, quarter))


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


def dead_air(clips, stringout: "Timeline | None") -> tuple[float, float] | None:
    """(talking footage seconds, stringout seconds): how much pause and filler the cut removed."""
    if stringout is None or not stringout.spine:
        return None
    by_id = {c.id: c for c in clips}
    talk = sum(by_id[cid].duration for cid in {s.clip_id for s in stringout.spine} if cid in by_id)
    return talk, stringout.seconds


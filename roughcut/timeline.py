"""Stage 3: turn a validated plan into a frame-accurate timeline.

All timeline positions are integer frame counts at the sequence rate. Source
in-points are exact Fractions (clip-local seconds, snapped to the source's own
frame grid). The FCPXML writer converts to Final Cut's parent-relative
coordinates; nothing here knows about XML.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from fractions import Fraction

from . import timecode as tc
from .analyze import Analysis, Clip
from .pauses import word_in_pause
from .phrases import exact_matches, frame_at, partial_match
from .phrases import tokens as phrase_tokens
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
    scale: float = 1.0  # a punch-in on a jump cut (Final Cut's Transform > Scale); 1: as shot

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
    target_seconds: float | None = None  # the length the brief asked for
    review: "Review | None" = None  # length vs target, uncovered jump cuts, fill, repeated stills

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


class _Spans:
    """(start, end) spans sorted by start, looked up by bisection.

    A long talking clip has thousands of silences and words, and every
    segment and piece asks about the few near it; scanning them all each
    time made a three-hour recording take minutes.
    """

    def __init__(self, spans) -> None:
        self.spans: list[tuple[float, float]] = sorted(spans)
        self.starts = [s for s, _ in self.spans]
        self.longest = max((e - s for s, e in self.spans), default=0.0)

    def __iter__(self):
        return iter(self.spans)

    def __len__(self) -> int:
        return len(self.spans)

    def clamp_end(self, t: float, word_start: float, eps: float = 0.05) -> float:
        """Whisper word ends often run into the following silence; pull them back.

        Only a silence that covers the word's tail counts. A pause in the middle
        of a stretched word must not cut the real audio after it.
        """
        i = bisect.bisect_right(self.starts, word_start)
        while i < len(self.spans) and self.starts[i] < t:
            s, e = self.spans[i]
            if e >= t - eps:
                return s
            i += 1
        return t

    def clamp_start(self, t: float, word_end: float, eps: float = 0.05) -> float:
        """Mirror of clamp_end: skip silence covering the word's head."""
        i = bisect.bisect_left(self.starts, t - self.longest - 1e-6)  # a span ending after t starts after this
        while i < len(self.spans) and self.starts[i] <= t + eps:
            e = self.spans[i][1]
            if t < e < word_end:
                return e
            i += 1
        return t

    def pause_between(self, prev_start: float, next_end: float, eps: float = 0.05) -> float:
        """Longest measured silence between two words.

        Whisper often stretches a word across the pause after it (or starts the
        next word early), so the words' own times can show no gap at all. The
        silence map still sees the pause.
        """
        longest = 0.0
        i = bisect.bisect_left(self.starts, prev_start + eps)
        while i < len(self.spans) and self.starts[i] <= next_end - eps:
            s, e = self.spans[i]
            if e <= next_end - eps:
                longest = max(longest, e - s)
            i += 1
        return longest

    def holds(self, s: float, e: float) -> bool:
        """Does a span lie wholly inside s..e? (For words: is a whole word said in it?)"""
        i = bisect.bisect_left(self.starts, s)
        while i < len(self.spans) and self.starts[i] <= e:
            if self.spans[i][1] <= e:
                return True
            i += 1
        return False


def _spans(silences) -> _Spans:
    return silences if isinstance(silences, _Spans) else _Spans(silences)


# One-off forms taking a plain list of silences. The timeline itself uses a clip's _index().
def _silence_clamp_end(t: float, word_start: float, silences, eps: float = 0.05) -> float:
    return _spans(silences).clamp_end(t, word_start, eps)


def _silence_clamp_start(t: float, word_end: float, silences, eps: float = 0.05) -> float:
    return _spans(silences).clamp_start(t, word_end, eps)


def _pause_between(prev_start: float, next_end: float, silences, eps: float = 0.05) -> float:
    return _spans(silences).pause_between(prev_start, next_end, eps)


BREATH_MARGIN = 0.05  # kept either side of a breath in narration
MIN_TRIM = 0.1  # a shorter trim isn't worth an edit


@dataclass
class _ClipIndex:
    """A talking clip's pauses and words, sorted once (speech_pieces asks once per segment,
    _cut_silences once per piece)."""

    silences: _Spans
    clusters: list[tuple[float, float, list[tuple[float, float]]]]  # see _pause_clusters
    pauses: _Spans  # the clusters' spans
    spoken: _Spans  # every transcribed word
    seg_pos: dict[str, tuple[int, int]]  # segment ID -> (its index in the clip, clip-wide index of its first word)


def _index(clip: Clip) -> _ClipIndex:
    # Kept on the clip and rebuilt when its silences, breaths or segments change.
    key = (id(clip.segments), len(clip.segments), tuple(clip.silences), tuple(clip.breaths))
    memo = clip.__dict__.get("_timeline_index")
    if memo is not None and memo[0] == key:
        return memo[1]
    silences = _Spans(clip.silences)
    spoken = _Spans((w.start, w.end) for seg in clip.segments for w in seg.words)
    breaths = sorted(clip.breaths)

    def breath_between(a: float, b: float) -> bool:
        i = bisect.bisect_left(breaths, (a - 1e-3,))
        return i < len(breaths) and abs(breaths[i][0] - a) < 1e-3 and abs(breaths[i][1] - b) < 1e-3

    clusters: list[tuple[float, float, list[tuple[float, float]]]] = []
    for s, e in silences:
        if (
            clusters
            and breaths
            and breath_between(clusters[-1][1], s)
            and not spoken.holds(s, e)
            and not spoken.holds(*clusters[-1][2][-1])
            # A quiet word whose only sound is the "breath" (marked by an older analysis) stays.
            and not word_in_pause(spoken.spans, spoken.starts, clusters[-1][0], clusters[-1][1], s, e)
        ):
            clusters[-1] = (clusters[-1][0], e, clusters[-1][2] + [(s, e)])
            continue
        clusters.append((s, e, [(s, e)]))
    seg_pos: dict[str, tuple[int, int]] = {}
    base = 0
    for i, seg in enumerate(clip.segments):
        seg_pos.setdefault(seg.id, (i, base))
        base += len(seg.words)
    ix = _ClipIndex(silences, clusters, _Spans((s, e) for s, e, _ in clusters), spoken, seg_pos)
    clip.__dict__["_timeline_index"] = (key, ix)
    return ix


def _pause_clusters(clip: Clip) -> list[tuple[float, float, list[tuple[float, float]]]]:
    """Silences joined across the breaths and clicks between them: (start, end, the silences).

    A breath splits one pause in two on the silence map, and each half can be
    shorter than the style's longest pause, so the pause stayed. Analysis
    marks the breaths (short, quiet, no transcribed word); silences that hold
    a whole word (quiet speech under a raised level) are never joined, nor
    two around a quiet word whose only sound is the "breath" between them.
    """
    return _index(clip).clusters


def _gap_limit(clip: Clip, style: Style) -> float:
    """Longest pause kept inside speech. Over a music bed (a finished promo) the pauses carry
    the music, and cutting every breath makes it stutter, so they stay longer."""
    return max(1.0, 2 * style.max_gap) if clip.bed == "music" else style.max_gap


def _gap_limits(analysis: Analysis, style: Style) -> dict[str, float]:
    return {c.id: _gap_limit(c, style) for c in analysis.clips if c.bed == "music"}


def speech_pieces(seg: Segment, clip: Clip, style: Style, section: int) -> list[_Piece]:
    dur = clip.duration
    ix = _index(clip)
    if not seg.words:
        # Silence-map segment: its position in the clip stands in for word indexes.
        idx = ix.seg_pos.get(seg.id, (0, 0))[0]
        a, b = max(0.0, seg.start - style.pad_in), min(dur, seg.end + style.pad_out)
        return [_Piece(clip.id, a, b, seg.start, seg.end, section, [seg.id], idx, idx)]

    all_words = seg.words
    runs: list[list[int]] = []  # indexes into all_words
    current: list[int] = []
    removed_since_last = False
    # On camera, cutting out a very short um/uh jumps the picture, so it stays. Narration
    # has no picture of its own (B-roll covers it), so every um/uh can go.
    filler_floor = 0.25 if clip.media.has_video else 0.0
    max_gap = _gap_limit(clip, style)
    # On camera a pause split by a breath is one pause, cut in one edit. Narration keeps its
    # breaths (sound is never removed there); _cut_silences trims the silence around them.
    sil = ix.pauses if clip.media.has_video else ix.silences
    for i, w in enumerate(all_words):
        if style.remove_fillers and w.is_filler:
            removed_since_last = True
            continue
        if current:
            prev = all_words[current[-1]]
            gap = max(w.start - prev.end, sil.pause_between(prev.start, w.end))
            if gap > max_gap or (removed_since_last and gap > filler_floor):
                runs.append(current)
                current = []
        current.append(i)
        removed_since_last = False
    if current:
        runs.append(current)

    # Neighbouring words outside this segment, so padding never grabs the tail
    # of a cut retake or the start of the next sentence.
    idx, base = ix.seg_pos.get(seg.id, (-1, 0))

    def end_of(w) -> float:  # where a neighbouring word's sound really stops / starts
        return sil.clamp_end(w.end, w.start)

    def start_of(w) -> float:
        return sil.clamp_start(w.start, w.end)

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
        wa = sil.clamp_start(first.start, first.end)
        wb = sil.clamp_end(last.end, last.start)
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


def _fillers_left_on_camera(pieces: list[_Piece], analysis: Analysis) -> int:
    """How many um/uh still play inside kept on-camera speech (too short to cut cleanly)."""
    left = set()
    for p in pieces:
        clip = analysis.clip(p.clip_id)
        if not clip.media.has_video or not p.seg_ids:
            continue
        for seg_id in p.seg_ids:
            for w in analysis.segment(seg_id).words:
                if w.is_filler and p.a <= w.start and w.end <= p.b:
                    left.add((clip.id, w.start))
    return len(left)


def _cut_silences(pieces: list[_Piece], analysis: Analysis, style: Style) -> list[_Piece]:
    """Remove long silences inside kept talking footage, wherever they ended up.

    Whisper stretches a word over the pause beside it, before or after, by
    any amount, so a pause can hide inside a word, between two sentences that
    were joined, or inside a timed range. Cutting the silence itself never
    removes sound, so this runs last. Only silences with sound on both sides
    inside the piece go: a quiet reaction shot or B-roll stays as chosen.
    A "silence" that holds a whole transcribed word is quiet speech under a
    raised level (music bed, loud room), not dead air, so it stays too.

    A pause split by a breath or click counts as one (_pause_clusters). On
    camera it goes in one edit, breath and all, rather than leaving a
    word-less flash of the breath; in narration only the silence around the
    breath goes.
    """
    out: list[_Piece] = []
    for p in pieces:
        clip = analysis.clip(p.clip_id)
        ix = _index(clip)
        if p.seg_ids:
            longest = _gap_limit(clip, style)
        elif clip.role in ("aroll", "voiceover"):  # a timed range of talking footage
            longest = max(1.0, 2 * style.max_gap)
        else:
            out.append(p)
            continue
        cur = p
        # Only pauses starting inside the piece's speech can qualify (cur.wa only moves later).
        first = bisect.bisect_right(ix.pauses.starts, p.wa)
        last = bisect.bisect_left(ix.pauses.starts, p.wb)
        for s, e, members in ix.clusters[first:last]:
            if e - s > longest and cur.wa < s and e < cur.wb and not ix.spoken.holds(s, e):
                if clip.media.has_video or len(members) == 1:
                    cuts = [(s, e, style.pad_out, style.pad_in)]
                else:
                    last = len(members) - 1
                    cuts = [
                        (ms, me, style.pad_out if i == 0 else BREATH_MARGIN, style.pad_in if i == last else BREATH_MARGIN)
                        for i, (ms, me) in enumerate(members)
                    ]
                    cuts = [c for c in cuts if (c[1] - c[3]) - (c[0] + c[2]) >= MIN_TRIM]
                for ms, me, pad_out, pad_in in cuts:
                    out.append(replace(cur, b=min(cur.b, ms + pad_out), wb=ms, w1=None, b_tight=False, seg_ids=list(cur.seg_ids)))
                    cur = replace(cur, a=max(cur.a, me - pad_in), wa=me, w0=None, a_tight=False, seg_ids=list(cur.seg_ids))
        out.append(cur)
    return out


def _merge(pieces: list[_Piece], max_gap: float, warnings: list[str] | None = None, limits: dict[str, float] | None = None) -> list[_Piece]:
    """Join back-to-back sentences from the same take when the pause between them is natural.

    Only pieces with nothing cut between them are joined, so removed fillers
    and long pauses stay removed. `limits` sets a longer pause per clip (music beds).
    """
    merged: list[_Piece] = []
    for p in pieces:
        if merged:
            q = merged[-1]
            adjacent = q.w1 is not None and p.w0 is not None and p.w0 == q.w1 + 1
            if adjacent and q.clip_id == p.clip_id and q.section == p.section and 0 <= p.wa - q.wb <= (limits or {}).get(p.clip_id, max_gap):
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
    target_seconds: float | None = None,
    punch_in: bool = False,
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
    pieces = _cut_silences(_merge(raw_pieces, style.max_gap, warnings, _gap_limits(analysis, style)), analysis, style)

    width, height, seq_fd = choose_format(pieces, analysis, format_override)
    tl = Timeline(name=name, frame_duration=seq_fd, width=width, height=height)
    left = _fillers_left_on_camera(pieces, analysis) if style.remove_fillers else 0
    if left:
        tl.notes.append(
            f"Left {left} short um/uh on camera: cutting one under a quarter second would jump the picture. "
            "Trim them in Final Cut, or cover them with B-roll, if they bother you."
        )

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
    tl.target_seconds = target_seconds
    tl.review = review(tl, plan, analysis, target_seconds)
    _place_markers(tl, plan, analysis)  # first, so a jump on a line the plan flagged keeps one to-do
    punched: set[int] = set()
    if punch_in:  # also a cut made without AI (--provider none), which is all jump cuts
        punched = _punch_in(tl, tl.review, analysis)
        tl.review.punched = len(punched)
        tl.review.not_punched = len(tl.review.jump_cuts) - len(punched)
    if plan.source != "heuristic":  # a stringout is all jump cuts by design
        _mark_bed_jumps(tl, analysis, warnings)
        _mark_jump_cuts(tl, tl.review, skip=punched)
        tl.markers.sort(key=lambda mk: mk.frame)
        if punch_in:  # which of the cuts left as shot got a to-do (the most important ones do, up to MAX_JUMP_MARKERS)
            todo = {m.frame for m in tl.markers if m.kind == "todo"}
            tl.review.not_punched_marked = sum(1 for j in tl.review.jump_cuts if j.frame not in punched and j.frame in todo)
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

    def sync(self, tl: "Timeline") -> None:
        """Re-read what each lane holds after shots were stretched or moved."""
        self.used = {}
        for c in tl.connected:
            self.used.setdefault(c.lane, []).append((c.start, c.end))


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


REPLAY_SECONDS = 10  # the same footage again within this reads as a mistake, not a callback


def _replays_near(
    tl: Timeline, clip_id: str, src_in: Fraction, frames: int, start: int, skip: "ConnectedClip | None" = None
) -> list[tuple[ConnectedClip, Fraction]]:
    """Shots of the clip a moment away (REPLAY_SECONDS) from a shot at `start` that show some of
    the same footage, with the seconds of it they share."""
    fd = tl.frame_duration
    gap = tc.round_frames(Fraction(REPLAY_SECONDS), fd)
    a, b = src_in, src_in + frames * fd
    out = []
    for o in tl.connected:
        if o is skip or o.clip_id != clip_id or o.end < start - gap or o.start > start + frames + gap:
            continue
        shared = min(b, o.src_in + o.frames * fd) - max(a, o.src_in)
        if shared > 0:
            out.append((o, shared))
    return out


def _place_broll(
    tl: Timeline, plan: Plan, analysis: Analysis, style: Style, volume_db: float | None, warnings: list[str], lanes: _Lanes
) -> None:
    fd = tl.frame_duration
    min_frames = tc.ceil_frames(Fraction(style.min_broll).limit_denominator(1000), fd)
    max_still = tc.floor_frames(Fraction(style.max_still).limit_denominator(1000), fd)
    reach = tc.round_frames(Fraction(1, 2), fd)
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
            start, end = _snap_to_edits(tl, min(r[0] for r in ranges), max(r[1] for r in ranges), reach)
            if end - start < min_frames:
                end = min(tl.total_frames, start + min_frames)
            # A shot named on words comes in as they're spoken; the others share the time between.
            runs = _broll_runs(tl, analysis, group, start, end, min_frames, reach, warnings)
            if runs[0][0] > start and _extend_shot_before(tl, analysis, start, runs[0][0], max_still):
                lanes.sync(tl)  # the shot before holds until the words
            for run_start, run_end, run in runs:
                cursor = run_start
                for k, b in enumerate(run):
                    remaining = run_end - cursor
                    if remaining < 1:
                        warnings.append(f"B-roll {b.clip_id}: no time left over {', '.join(b.over)}; skipped")
                        continue
                    want = remaining if k == len(run) - 1 else max(1, remaining // (len(run) - k))
                    clip = analysis.clip(b.clip_id)
                    m = clip.media
                    if m.kind == "image":
                        src_in, frames = Fraction(0), min(want, max_still)
                    else:
                        hint = Fraction(b.source_in).limit_denominator(1000000) if b.source_in >= 0 else None
                        if hint is None:  # "let the software choose": the logged moment that fits the line
                            said = " ".join(seg.text for seg in (analysis.segment(o) for o in b.over) if seg)
                            on_screen = next((o for o in tl.connected if o.end == cursor and o.clip_id == clip.id), None)
                            at = _pick_moment(tl, clip, _tokens(said), _tokens(b.words), on_screen=on_screen, length=float(want * fd))
                            hint = Fraction(at).limit_denominator(1000) if at is not None else None
                            if hint is None and on_screen is not None:
                                # The same clip again, straight after itself: carry on from where it got to
                                # (or a part well away), never the same footage twice.
                                hint = on_screen.src_in + on_screen.frames * fd
                                if hint + want * fd > m.duration:
                                    hint = _away_from(tl, clip, on_screen, want)
                            if hint is None:  # a part the cut hasn't shown yet, from 15% in
                                hint = _fresh_in(tl, clip, want, m.duration * Fraction(15, 100))
                        src_in, frames = _video_window(m, want, fd, hint)
                        near = [o for o, _ in _replays_near(tl, clip.id, src_in, frames, cursor)]
                        if near:
                            # It would replay a shot of this clip from a moment away (the planner's in-point,
                            # where a late one backed off to, or all a short clip has left): carry on from
                            # where that got to instead, shorter if need be, and let the next shot (or the
                            # fill) have the rest.
                            grid = m.frame_duration or fd
                            start_at = min(hint, m.duration) if b.source_in >= 0 else Fraction(0)
                            while near:
                                start_at = max([start_at] + [o.src_in + o.frames * fd for o in near])
                                src_in = tc.ceil_frames(start_at, grid) * grid
                                frames = max(0, min(want, tc.floor_frames(m.duration - src_in, fd)))
                                near = [o for o, _ in _replays_near(tl, clip.id, src_in, frames, cursor)] if frames > 0 else []
                            why = f"B-roll {b.clip_id}" + (f" from {b.source_in:g}s" if b.source_in >= 0 else "") + " would replay a shot of it a moment away"
                            if frames < min(min_frames, want):
                                warnings.append(f"{why}, and too little of it is left after that; skipped")
                                continue
                            if b.source_in >= 0:  # the planner's in-point moved
                                warnings.append(f"{why}, so it carries on from {float(src_in):.1f}s")
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


def _snap_to_edits(tl: Timeline, start: int, end: int, reach: int) -> tuple[int, int]:
    """Start and end a cutaway on the edit next to its lines, not a few frames of padding away.

    Starting three frames after a cut would flash the speaker (or show the
    jump the cutaway is there to hide). The same goes for the very start and
    end of the timeline: a cutaway over the first line starts on frame 0
    rather than after the padding, and one over the last line runs to the end.
    """
    head = tl.spine_at(start)
    if head is not None and 0 < start - head.offset <= reach:
        start = head.offset
    tail = tl.spine_at(end - 1)
    if tail is not None and 0 < tail.end - end <= reach:
        end = tail.end
    return start, end


def _is_jump(a: SpineClip, b: SpineClip, analysis: Analysis, fd: Fraction) -> bool:
    """Is the edit from a to b a jump cut: the same camera clip, picking up somewhere else?"""
    return a.clip_id == b.clip_id and analysis.clip(b.clip_id).media.kind == "video" and a.src_in + a.frames * fd != b.src_in


BROLL_LEAD = 0.07  # a cutaway named on words comes in about two frames before them


def _word_frame(tl: Timeline, analysis: Analysis, b, after: int, start: int, end: int) -> int | None:
    """First timeline frame (after `after`) where the cutaway's words are heard, or None."""
    lead = max(1, round(Fraction(BROLL_LEAD).limit_denominator(1000) / tl.frame_duration))
    segs = [s for s in (analysis.segment(o) for o in b.over) if s is not None and s.words]

    def frame_of(seg, i: int) -> int | None:
        w = seg.words[i]
        clip = analysis.clip(seg.clip_id)
        # Whisper often starts a word early, over the pause before it; the sound starts later.
        f = frame_at(tl, seg.clip_id, _index(clip).silences.clamp_start(w.start, w.end))
        if f is None:
            f = frame_at(tl, seg.clip_id, w.start)
        if f is None or not (start <= f < end) or f - lead <= after:
            return None
        return f - lead

    for seg in segs:
        for i, _ in exact_matches(seg.words, b.words):
            f = frame_of(seg, i)
            if f is not None:
                return f
    # Close enough: most of the opening words ("Honda Centre" for "Honda Center").
    want = phrase_tokens(b.words)
    for seg in segs:
        hit = partial_match(seg.words, b.words)
        if hit is None:
            continue
        got = phrase_tokens(" ".join(w.text for w in seg.words[hit[0] : hit[1] + 1]))
        if len(got) * 2 >= len(want) and any(len(t) >= 4 for t in got):
            f = frame_of(seg, hit[0])
            if f is not None:
                return f
    return None


def _broll_runs(
    tl: Timeline, analysis: Analysis, group: list, start: int, end: int, min_frames: int, reach: int, warnings: list[str]
) -> list[tuple[int, int, list]]:
    """Split a group of cutaways over the same lines at the shots anchored on words.

    A shot whose words are found starts as they're spoken (and ends the shot
    before it); shots without words share the time up to the next anchored
    one evenly. The first shot starts with the lines unless its words come
    later (over half a second in): then the shot before holds, or the
    speaker, or auto-fill under narration, has the lines until them. It never
    comes in after a jump cut it covers: it starts on that edit instead. Words
    that aren't in the cut fall back to the even split, with a warning.
    """
    fd = tl.frame_duration
    firsts: list[int] = []
    at: dict[int, int] = {}
    prev, count = start, 0  # last fixed start, and how many shots share the time since it
    for k, b in enumerate(group):
        anchor = None
        if b.words:
            # Words said twice: the time after the shots before it have had their minimum first.
            anchor = _word_frame(tl, analysis, b, prev + min_frames * count - 1 if k else -1, start, end)
            if anchor is None and k:
                anchor = _word_frame(tl, analysis, b, prev, start, end)
            if anchor is None:
                warnings.append(f'B-roll {b.clip_id}: "{b.words}" isn\'t heard in {", ".join(b.over)} (or was cut); shared the time evenly instead')
            else:
                lo, hi = prev + min_frames * count, end - min_frames
                if lo > hi:
                    warnings.append(f'B-roll {b.clip_id}: no room to start on "{b.words}"; shared the time evenly instead')
                    anchor = None
                else:
                    anchor = min(max(anchor, lo), hi)
                    if k == 0:
                        # A jump cut before the words would show again: come in on it instead.
                        jump = next(
                            (b2.offset for b1, b2 in zip(tl.spine, tl.spine[1:]) if start < b2.offset < anchor and _is_jump(b1, b2, analysis, fd)),
                            None,
                        )
                        anchor = anchor if jump is None else jump
                        if anchor - start < reach:
                            anchor = start  # the words open the line: start with it
        if k == 0 and anchor is None:
            anchor = start
        if anchor is None:
            count += 1
            continue
        firsts.append(k)
        at[k] = anchor
        prev, count = anchor, 1
    runs = []
    for n, k in enumerate(firsts):
        nxt = firsts[n + 1] if n + 1 < len(firsts) else len(group)
        runs.append((at[k], at[nxt] if nxt in at else end, group[k:nxt]))
    return runs


# ---------------------------------------------------------- picture fill


def _lane_busy(tl: Timeline, c: ConnectedClip, f0: int, f1: int) -> bool:
    """Does another clip on c's lane hold any of frames f0..f1?"""
    return any(o is not c and o.lane == c.lane and o.start < f1 and o.end > f0 for o in tl.connected)


def _replay(tl: Timeline, c: ConnectedClip, src_in: Fraction, frames: int, start: int) -> Fraction:
    """Seconds of footage c, placed so, would repeat from the shots of its clip a moment away."""
    return sum((shared for _, shared in _replays_near(tl, c.clip_id, src_in, frames, start, skip=c)), Fraction(0))


def _extend_shot_before(tl: Timeline, analysis: Analysis, g0: int, g1: int, max_still: int | None = None, *, slip: bool = False) -> bool:
    """Close a sliver of a gap by holding the previous shot a little longer.

    A video plays on, as long as that doesn't show footage again that a shot
    of the same clip a moment away shows (one picking up where this one ends,
    say). With `slip`, a video that can't play on starts that sliver earlier
    in its source instead.
    """
    fd = tl.frame_duration
    k = g1 - g0
    for c in tl.connected:
        m = analysis.clip(c.clip_id).media
        if c.end != g0 or not m.has_video:
            continue
        if m.kind == "image" and max_still is not None and c.frames + k > max_still:
            continue
        if _lane_busy(tl, c, g0, g1):
            continue
        if m.kind == "image":
            if slip:
                continue  # a still was already offered the time
            c.frames += k
            return True
        grid = m.frame_duration or fd
        now = _replay(tl, c, c.src_in, c.frames, c.start)
        src_in = tc.floor_frames(c.src_in - k * fd, grid) * grid if slip else c.src_in
        if 0 <= src_in and src_in + (c.frames + k) * fd <= m.duration and _replay(tl, c, src_in, c.frames + k, c.start) <= now:
            c.src_in, c.frames = src_in, c.frames + k
            return True
    return False


def _extend_shot_after(tl: Timeline, analysis: Analysis, g0: int, g1: int, max_still: int) -> bool:
    """Close a sliver of a gap by starting the next shot a little earlier.

    A video starts earlier in its source (or, already at the head of its
    source or picking up where the same clip's shot before it ends, plays on
    a little longer from the same in-point), never showing footage again
    that a shot of it a moment away shows; a still grows, or, already at its
    longest, moves earlier when nothing follows it (the fill covers its tail).
    """
    fd = tl.frame_duration
    for c in tl.connected:
        m = analysis.clip(c.clip_id).media
        if c.start != g1 or not m.has_video or c.kind != "broll" or _lane_busy(tl, c, g0, g1):
            continue
        if m.kind == "image":
            if c.frames + g1 - g0 <= max_still:
                c.frames += g1 - g0
            elif any(o is not c and o.start <= c.end < o.end and analysis.clip(o.clip_id).media.has_video for o in tl.connected):
                continue
            c.start = g0
            return True
        grid = m.frame_duration or fd
        k = g1 - g0
        now = _replay(tl, c, c.src_in, c.frames, c.start)
        for src_in in (max(Fraction(0), tc.floor_frames(c.src_in - k * fd, grid) * grid), c.src_in):
            if src_in + (c.frames + k) * fd <= m.duration and _replay(tl, c, src_in, c.frames + k, g0) <= now:
                c.src_in, c.start, c.frames = src_in, g0, c.frames + k
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
        text += " " + " ".join(mo.get("note", "") for mo in _usable_moments(clip))
    return _tokens(text)


def _usable_moments(clip: Clip) -> list[dict]:
    return [mo for mo in ((clip.visual or {}).get("moments") or []) if isinstance(mo, dict) and mo.get("usable", True)]


def _pick_moment(
    tl: Timeline,
    clip: Clip,
    said: set[str],
    words: set[str] = frozenset(),
    *,
    on_screen: "ConnectedClip | None" = None,
    length: float = 0.0,
    fresh_only: bool = False,
) -> float | None:
    """Where to start inside a long clip: the usable logged moment that fits what's being said
    (the cutaway's own words count double), one not shown yet first.

    Never one whose `length` seconds would replay the part of this clip on
    screen just before (`on_screen`); with `fresh_only`, never one shown
    already. None when nothing logged qualifies.
    """
    fd = tl.frame_duration
    shown = [(float(c.src_in), float(c.src_in + c.frames * fd)) for c in tl.connected if c.clip_id == clip.id]

    def seen(at: float) -> bool:
        return any(a - 0.5 <= at < b for a, b in shown)

    def replays(at: float) -> bool:
        if on_screen is None:
            return False
        a = float(on_screen.src_in)
        return at < a + float(on_screen.frames * fd) and at + length > a - 0.5

    moments = [mo for mo in _usable_moments(clip) if not replays(mo["at"]) and not (fresh_only and seen(mo["at"]))]
    if not moments:
        return None

    def rank(mo: dict) -> tuple:
        toks = _tokens(mo.get("note", ""))
        return (not seen(mo["at"]), 2 * len(toks & words) + len(toks & said), -mo["at"])

    return float(max(moments, key=rank)["at"])


def _shows_unusable(clip: Clip, src_in: Fraction, frames: int, fd: Fraction) -> bool:
    """Would this part of the clip show a moment the visual log called unusable (shaky, blurred...)?"""
    a, b = float(src_in), float(src_in + frames * fd)
    moments = (clip.visual or {}).get("moments") or []
    return any(isinstance(mo, dict) and not mo.get("usable", True) and a <= mo.get("at", -1) < b for mo in moments)


def _shown(tl: Timeline, clip_id: str) -> list[tuple[Fraction, Fraction]]:
    """The parts of a clip already in the cut (cutaways and main-storyline uses), merged, in source seconds."""
    fd = tl.frame_duration
    spans = sorted(
        [(c.src_in, c.src_in + c.frames * fd) for c in tl.connected if c.clip_id == clip_id]
        + [(sc.src_in, sc.src_in + sc.frames * fd) for sc in tl.spine if sc.clip_id == clip_id]
    )
    merged: list[tuple[Fraction, Fraction]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _unseen(tl: Timeline, clip: Clip, want: int) -> bool:
    """Does the clip still have `want` frames in a row that the cut hasn't shown?"""
    need = want * tl.frame_duration
    edges = [Fraction(0)] + [x for span in _shown(tl, clip.id) for x in span] + [clip.media.duration]
    return any(edges[i + 1] - edges[i] >= need for i in range(0, len(edges), 2))


def _fresh_in(tl: Timeline, clip: Clip, want: int, prefer: Fraction) -> Fraction:
    """In-point for `want` frames of a clip that shows as little as possible of what's already in the cut.

    `prefer` when that part is all new; else the first all-new part after it,
    then from the head; failing that, the part that repeats least. A part the
    visual log calls unusable only when nothing else will do.
    """
    m, fd = clip.media, tl.frame_duration
    grid = m.frame_duration or fd
    need = want * fd
    last = max(Fraction(0), m.duration - need)
    shown = _shown(tl, clip.id)
    starts = {min(max(Fraction(0), prefer), last), Fraction(0), last}
    for a, b in shown:  # the least repeat starts or ends against a part already shown
        starts.update(s for s in (tc.ceil_frames(b, grid) * grid, a - need) if 0 <= s <= last)

    def repeat(s: Fraction) -> Fraction:
        return sum((max(Fraction(0), min(b, s + need) - max(a, s)) for a, b in shown), Fraction(0))

    return min(starts, key=lambda s: (_shows_unusable(clip, s, want, fd), repeat(s), s < prefer, s))


def _away_from(tl: Timeline, clip: Clip, on_screen: ConnectedClip, want: int) -> Fraction:
    """In-point for a new shot from the clip already on screen.

    Well away from what's showing (5 s, or a quarter of the clip) so it reads
    as another shot rather than a jump: the first such part after it that
    hasn't been shown yet, wrapping round to the head, and never a part the
    visual log calls unusable. Failing that, such a part shown before; for a
    clip too short for that, the part that repeats least (_fresh_in).
    """
    m, fd = clip.media, tl.frame_duration
    need = want * fd
    a, b = on_screen.src_in, on_screen.src_in + on_screen.frames * fd
    away = max(Fraction(5), m.duration / 4)
    starts: list[Fraction] = []
    s = b + away
    while s + need <= m.duration:  # after the shot on screen...
        starts.append(s)
        s += need
    s = Fraction(0)
    while s + need <= a - away:  # ...then from the head
        starts.append(s)
        s += need
    good = [s for s in starts if not _shows_unusable(clip, s, want, fd)]
    shown = [(c.src_in, c.src_in + c.frames * fd) for c in tl.connected if c.clip_id == clip.id]
    new = [s for s in good if not any(s < y and s + need > x for x, y in shown)]
    if new or good:
        return (new or good)[0]
    return _fresh_in(tl, clip, want, Fraction(0))


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
    max_still = tc.floor_frames(Fraction(style.max_still).limit_denominator(1000), fd)

    # A few frames of another clip would flash: close slivers with the shots beside them first.
    placed_frames = 0
    for g0, g1 in gaps:
        if g1 - g0 < tiny and (
            _extend_shot_before(tl, analysis, g0, g1)
            or _extend_shot_after(tl, analysis, g0, g1, max_still)
            or _extend_shot_before(tl, analysis, g0, g1, slip=True)
        ):
            placed_frames += g1 - g0
    gaps = _gaps_without_picture(tl, analysis)
    lanes.sync(tl)

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
    placed_shots = 0

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

    def take(c: Clip, cursor: int, want: int, said: set[str]) -> tuple[Fraction, int]:
        """The part of a clip a fill shot at `cursor` shows: (source in-point, frames)."""
        m = c.media
        if m.kind == "image":
            return Fraction(0), want
        on_screen = next((o for o in tl.connected if o.end == cursor and o.clip_id == c.id), None)
        length = float(want * fd)
        # A good moment from the visual log that fits the line and hasn't been shown yet.
        at = _pick_moment(tl, c, said, on_screen=on_screen, length=length, fresh_only=True)
        if at is not None:
            return _video_window(m, want, fd, Fraction(at).limit_denominator(1000))
        if on_screen is not None:
            # The only fit is the clip already on screen: cut to a part well away from it.
            start_at = _away_from(tl, c, on_screen, want)
        else:
            # On from the last fill shot of it (or a tenth in), never over what the cut already
            # shows (planned shots too) while an unseen part is left.
            start_at = _fresh_in(tl, c, want, video_cursor.get(c.id, m.duration * Fraction(1, 10)))
        src_in, frames = _video_window(m, want, fd, start_at)
        if _shows_unusable(c, src_in, frames, fd):
            # Every good logged moment has been shown: one again (never the one on screen)
            # beats a part the log calls unusable.
            at = _pick_moment(tl, c, said, on_screen=on_screen, length=length)
            if at is not None:
                return _video_window(m, want, fd, Fraction(at).limit_denominator(1000))
        return src_in, frames

    def rounds():
        """The gaps, and again until none is left: moving a capped still earlier
        (_extend_shot_after) uncovers a few frames at its tail after the list was made,
        and the next round closes such a sliver by holding the still a little longer."""
        nonlocal gaps
        for _ in range(4):
            yield from gaps
            gaps = _gaps_without_picture(tl, analysis)
            if not gaps:
                return
            lanes.sync(tl)

    for g0, g1 in rounds():
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

            # Videos with a part the cut hasn't shown yet (all of them, until they run short).
            new_video = {c.id for c in pool if c.media.kind == "video" and (not uses.get(c.id, 0) or _unseen(tl, c, want))}

            def rank(c: Clip) -> tuple:
                # Long videos can be revisited (a new part each time); a still shown again, or
                # footage shown again, reads as a mistake, so those wear out fast.
                wear = uses.get(c.id, 0) * (0.5 if c.id in new_video else 10.0)
                fit = _match_score(docs[c.id], said, names, idf) / (1 + wear)
                if fit < MIN_MATCH:
                    fit = 0.0  # a stray shared word isn't a reason to pick a shot
                # With nothing to match, moving footage beats a random still.
                return (fit, c.id in new_video, -uses.get(c.id, 0), -order[c.id])

            # Never cut to the shot already on screen (or the one coming next), and never show
            # a still or footage twice: another part of a video that's next door reads better.
            beside = {c.clip_id for c in tl.connected if c.end == cursor or (want == remaining and c.start == g1)}
            fresh = [c for c in pool if not (c.media.kind == "image" and uses.get(c.id, 0))]
            new = [c for c in fresh if c.media.kind == "image" or c.id in new_video]
            tiers = [[c for c in new if c.id not in beside], new, [c for c in fresh if c.id not in beside], fresh, [c for c in pool if c.id not in beside], pool]
            best = max(next(t for t in tiers if t), key=rank)
            src_in, frames = take(best, cursor, want, said)
            if frames < 1:
                pool = [c for c in pool if c is not best]
                continue
            short = remaining - frames
            if 0 < short < tiny:
                # The pick is a little shorter than the rest of the gap, which would leave a few
                # frames for a flash. Hold the shot before it longer (or start the next one
                # earlier); failing that, use a clip that covers the whole rest.
                if _extend_shot_before(tl, analysis, cursor, cursor + short, max_still):
                    cursor += short
                    placed_frames += short
                    lanes.sync(tl)
                elif _extend_shot_after(tl, analysis, g1 - short, g1, max_still):
                    g1 -= short
                    placed_frames += short
                    lanes.sync(tl)
                else:
                    for tier in tiers:
                        whole = [c for c in tier if c is not best and take(c, cursor, want, said)[1] == remaining]
                        if whole:
                            best = max(whole, key=rank)
                            src_in, frames = take(best, cursor, want, said)
                            break
            m = best.media
            if m.kind == "video":
                video_cursor[best.id] = src_in + frames * fd + 1
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
EDGE_DECLICK = Fraction(4, 1000)  # ...and at an edit on (or inside) a word: stops the tick without softening the word
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
    starts after the last one. Where there's less room than that (a cut
    um/uh or retake right against the word, and frame rounding that puts
    the edit inside it) the fade is shorter, never under 4 ms: a hard cut
    in a voiced sound is the tick itself. B-roll nat sound eases in and out
    over a few frames. Fades are Final Cut fade handles, so they stay
    draggable.
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
            fade_in, fade_out = (max(EDGE_DECLICK, min(DECLICK, room)) for room in (room_in, room_out))
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
            # Flags about clips on the timeline land on the clip's first use, in the main storyline
            # or (B-roll, usually) as a cutaway.
            uses = [sc.offset for sc in tl.spine if sc.clip_id == flag["ref"]] + [c.start for c in tl.connected if c.clip_id == flag["ref"]]
            if not uses:
                continue
            frame = min(uses)
        else:
            frame = rng[0]
        tl.markers.append(Marker(frame=frame, value=flag["note"][:120], kind="todo", note=flag["note"]))
    tl.markers.sort(key=lambda mk: mk.frame)


# ------------------------------------------------------------------ review


def kept_length(seg: Segment, clip: Clip, analysis: Analysis, style: Style) -> tuple[float, int]:
    """(seconds, cuts): how long a segment plays on its own in the cut, and the edits inside it.

    The same pause and filler trimming the timeline does, so the planner's
    sums match what gets built.
    """
    pieces = _cut_silences(_merge(speech_pieces(seg, clip, style, 0), style.max_gap, None, _gap_limits(analysis, style)), analysis, style)
    return sum(p.b - p.a for p in pieces), max(0, len(pieces) - 1)


@dataclass
class JumpCut:
    frame: int  # timeline frame of the edit
    clip_id: str
    before: list[str]  # segment IDs on each side
    after: list[str]
    src_from: float  # clip-local seconds where the picture jumps from / to
    src_to: float

    @property
    def between_lines(self) -> bool:
        return set(self.before) != set(self.after)


@dataclass
class Review:
    seconds: float
    target: float | None
    sections: list[tuple[str, float]]
    jump_cuts: list[JumpCut]  # same-clip edits on camera with no B-roll over them
    filled_seconds: float
    filled_shots: int
    repeated_stills: dict[str, int]  # clip ID -> times shown
    punched: int = 0  # jump cuts smoothed with a punch-in
    not_punched: int = 0  # with punch-ins on: jump cuts on a clip that doesn't fill the frame
    not_punched_marked: int | None = None  # how many of those have a to-do marker; None: a cut made without AI marks none

    def length_line(self) -> str:
        line = tc.seconds_to_clock(self.seconds)
        if self.target:
            off = (self.seconds - self.target) / self.target
            how = "on target" if abs(off) < 0.005 else f"{abs(off):.0%} {'over' if off > 0 else 'under'}"
            line += f", target {tc.seconds_to_clock(self.target)} ({how})"
        return line

    def lines(self, frame_duration: Fraction) -> list[str]:
        out = [f"Length: {self.length_line()}" + ("" if self.target else ", no target given")]
        if len(self.sections) > 1:
            out.append("Sections: " + ", ".join(f"{name} {tc.seconds_to_clock(sec)}" for name, sec in self.sections))
        if self.jump_cuts:
            shown = [
                f"{tc.seconds_to_clock(float(j.frame * frame_duration))} {j.clip_id} "
                f"({', '.join(j.before) or 'range'} \u2192 {', '.join(j.after) or 'range'})"
                for j in self.jump_cuts[:12]
            ]
            more = f" and {len(self.jump_cuts) - 12} more" if len(self.jump_cuts) > 12 else ""
            out.append(f"Jump cuts on camera with no B-roll over them: {len(self.jump_cuts)}: " + "; ".join(shown) + more)
            if self.punched:
                out.append(
                    f"Punched in: {self.punched} of those cuts alternate between the shot as framed and {PUNCH_IN:.0%} "
                    "(Transform > Scale in the inspector; select the clips and reset Transform to undo)"
                )
            if self.not_punched:
                marked = self.not_punched_marked
                if marked is None:
                    said = "; a cut made without AI is all jump cuts, so none are marked"
                elif marked == self.not_punched:
                    said = "; they're to-do markers"
                elif marked:
                    said = f"; {marked} of them are to-do markers (the jump cuts that matter most get one, {MAX_JUMP_MARKERS} at most)"
                else:
                    said = ""
                out.append(
                    f"Not punched in: {self.not_punched} of those cuts, on a clip letterboxed or pillarboxed in this format "
                    f"(the black bars would jump in size){said}"
                )
        else:
            out.append("Jump cuts on camera with no B-roll over them: none")
        if self.filled_shots:
            out.append(f"Auto-filled: {tc.seconds_to_clock(self.filled_seconds)} of narration ({self.filled_shots} shot{'s' if self.filled_shots != 1 else ''} the plan didn't name)")
        if self.repeated_stills:
            out.append("Stills shown more than once: " + ", ".join(f"{cid} ({n}x)" for cid, n in sorted(self.repeated_stills.items())))
        return out


def review(tl: Timeline, plan: Plan, analysis: Analysis, target_seconds: float | None = None) -> Review:
    """Measure a built cut: length against the target, and what an editor would fix first."""
    fd = tl.frame_duration
    starts = list(tl.section_starts) + [(tl.total_frames, "")]
    sections = [(name, float((nxt - frame) * fd)) for (frame, name), (nxt, _) in zip(starts, starts[1:])]
    pictures = [c for c in tl.connected if c.kind == "broll" and analysis.clip(c.clip_id).media.has_video]
    jumps = []
    for a, b in zip(tl.spine, tl.spine[1:]):
        if not _is_jump(a, b, analysis, fd):
            continue
        if any(c.start <= b.offset <= c.end for c in pictures):
            continue  # a cutaway hides it
        jumps.append(JumpCut(b.offset, b.clip_id, list(a.seg_ids), list(b.seg_ids), float(a.src_in + a.frames * fd), float(b.src_in)))
    shown: Counter = Counter()
    for sc in tl.spine:
        if analysis.clip(sc.clip_id).media.kind == "image":
            shown[sc.clip_id] += 1
    for c in pictures:
        if analysis.clip(c.clip_id).media.kind == "image":
            shown[c.clip_id] += 1
    filled = [c for c in pictures if c.note == "auto-fill"]
    return Review(
        seconds=tl.seconds,
        target=target_seconds,
        sections=sections,
        jump_cuts=jumps,
        filled_seconds=float(sum(c.frames for c in filled) * fd),
        filled_shots=len(filled),
        repeated_stills={cid: n for cid, n in shown.items() if n > 1},
    )


MAX_JUMP_MARKERS = 10
# How far a punch-in zooms: enough to read as a new framing, little enough to keep 4K sharp in HD and the framing intact.
PUNCH_IN = 1.12


def _punch_in(tl: Timeline, rv: Review, analysis: Analysis) -> set[int]:
    """At each jump cut no B-roll covers, alternate the framing: one piece as shot, the next punched in,
    the next as shot again. A new shot, or a cut B-roll hides, starts as shot. A clip that doesn't fill
    the frame (16:9 in a vertical cut, which Final Cut letterboxes) stays as shot: punched in, its black
    bars would grow and shrink at every cut. Returns the frames of the cuts punched."""
    jumps = {j.frame for j in rv.jump_cuts}
    punched: set[int] = set()
    prev = None
    for sc in tl.spine:
        if prev is not None and sc.offset in jumps and sc.clip_id == prev.clip_id and _fills_frame(analysis.clip(sc.clip_id).media, tl):
            sc.scale = 1.0 if prev.scale != 1.0 else PUNCH_IN
            punched.add(sc.offset)
        else:
            sc.scale = 1.0
        prev = sc
    return punched


def _fills_frame(media, tl: Timeline) -> bool:
    """Whether a picture has the sequence's shape (within 1%), so Final Cut's Fit leaves no black bars."""
    if not (media.width and media.height and tl.width and tl.height):
        return True
    return abs((media.width * tl.height) / (media.height * tl.width) - 1) <= 0.01


def _mark_jump_cuts(tl: Timeline, rv: Review, skip: set[int] | None = None) -> None:
    """To-do markers on the uncovered jump cuts that matter most: between lines first, then pause trims.
    `skip`: the cuts a punch-in already smooths."""
    marked = {m.frame for m in tl.markers if m.kind == "todo"} | (skip or set())  # one to-do per edit
    jumps = [j for j in rv.jump_cuts if j.frame not in marked]
    worst = sorted(jumps, key=lambda j: (not j.between_lines, -(abs(j.src_to - j.src_from))))[:MAX_JUMP_MARKERS]
    for j in sorted(worst, key=lambda j: j.frame):
        what = f"{', '.join(j.before) or 'range'} \u2192 {', '.join(j.after) or 'range'}"
        tl.markers.append(
            Marker(
                frame=j.frame,
                value="Jump cut: cover with B-roll or smooth it",
                kind="todo",
                note=f"{j.clip_id} jumps from {tc.seconds_to_clock(j.src_from, millis=True)} to {tc.seconds_to_clock(j.src_to, millis=True)} ({what})",
            )
        )


MAX_BED_MARKERS = 20


def _mark_bed_jumps(tl: Timeline, analysis: Analysis, warnings: list[str]) -> None:
    """To-do markers where an edit jumps the music under a finished promo, and a warning
    when the plan's music plays over one (two songs at once)."""
    fd = tl.frame_duration
    bed = {c.id for c in analysis.clips if c.bed == "music"}
    if not bed:
        return
    jumps = []
    for a, b in zip(tl.spine, tl.spine[1:]):
        if not ({a.clip_id, b.clip_id} & bed):
            continue
        if a.clip_id == b.clip_id and a.src_in + a.frames * fd == b.src_in:
            continue
        jumps.append((b.offset, a, b))
    marked = {m.frame for m in tl.markers if m.kind == "todo"}  # one to-do per edit: the plan's flag stays
    for frame, a, b in jumps[:MAX_BED_MARKERS]:
        if frame in marked:
            continue
        name = analysis.clip(b.clip_id if b.clip_id in bed else a.clip_id).media.name
        tl.markers.append(
            Marker(
                frame=frame,
                value="Music jumps: smooth this edit",
                kind="todo",
                note=f"The music bed in {name} jumps at this edit ({a.clip_id} \u2192 {b.clip_id}). Try a crossfade, a J/L-cut, or a music cue over it.",
            )
        )
    if jumps:
        more = f" (markers on the first {MAX_BED_MARKERS})" if len(jumps) > MAX_BED_MARKERS else ""
        one = len(jumps) == 1
        tl.notes.append(
            f"{len(jumps)} {'edit jumps' if one else 'edits jump'} the music under a finished promo; "
            f"{'it has' if one else 'each has'} a to-do marker{more}."
        )
    for cue in (c for c in tl.connected if c.kind == "music"):
        under = sorted({sc.clip_id for sc in tl.spine if sc.clip_id in bed and sc.offset < cue.end and sc.end > cue.start})
        if under:
            warnings.append(f"music {cue.clip_id} plays over {', '.join(under)}, which already {'has' if len(under) == 1 else 'have'} music under the speech; the two will clash")


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


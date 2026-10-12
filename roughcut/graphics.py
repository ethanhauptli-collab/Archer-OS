"""Motion graphics: designed by the model as HTML animations, rendered to transparent video.

The planner picks the moments: which segment, the exact words a graphic
belongs to, and what it says. As with the cut itself, the model never
writes a timestamp; the transcript gives the frames. Only one graphic is
on screen at a time.

A second request designs every graphic as a self-contained HTML/CSS
animation in one consistent style (the first batch sets the look, the
rest are designed in parallel against it), knowing the shot it sits over
and, on vertical video, where the platform's buttons are.

A headless browser renders each one frame by frame (every animation is
paused and seeked to the frame's time) and ffmpeg encodes ProRes 4444
with alpha, which Final Cut layers above the footage. Every browser step
has a deadline: a design that freezes is left out with a warning instead
of hanging the run. Each render is checked (script errors, missing fonts,
blank, never animating off, outside the safe area) and composited over
its shot in graphics/preview.html (graphics_preview.py).

The designs and graphics.json are saved before rendering starts, and each
render is recorded as it finishes, so `roughcut render` can finish a run
that stopped there without asking the model again. Edit a design's HTML
and run `roughcut render` to re-render only what changed. Designs are
matched to graphics by their position in the plan ("index"), never by ID:
IDs follow timeline order and can change with the cut. Designs no graphic
uses any more move to graphics/old/. A designed graphic a re-cut can't
place (it would cover another, or its words aren't in that cut) keeps its
design in graphics/left-out/, named by its position in the plan, and the
next re-cut places it again.

Each saved design ends with a note of the timing it was made for (its
length and when its words come). A re-cut that moves the words (another
pacing, a revision that keeps the design) renders it on a warped clock, so
its words stay on the words and its exit lands at the end.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from pathlib import Path

from . import brand_fonts, ffmpeg_ops, progress
from .analyze import ROLE_LABELS, Analysis, Clip, log
from .captions import respell, respell_timed
from .plan import DEFAULT_SECONDS, GraphicSpec
from .timeline import ConnectedClip, Timeline

LEAD = 0.12  # a graphic starts animating in just before its words (never before the cut its words open)
GAP_FRAMES = 2  # between one graphic leaving and the next arriving: one graphic on screen at a time
MIN_SECONDS = 1.5  # shortest a graphic is held when it's shortened to make room for the next
LATE_SECONDS = 1.0  # how far a graphic may wait past its lead, while its words are said, so the one before can hold
MANIFEST = "graphics.json"

# ProRes 4444 with alpha, which Final Cut layers above the footage. A fixed quantizer encodes
# about 5x faster than the default rate control at ~80 dB PSNR; alpha is lossless either way.
ENCODER = ["-c:v", "prores_ks", "-profile:v", "4444", "-qscale:v", "4", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]

# Every browser step has a deadline, so a design that freezes is left out instead of hanging the run.
FRAME_TIMEOUT = 15.0  # seconds one frame's seek may take
STEP_TIMEOUT = 30.0  # loading the design, drawing a frame
BUDGET_MIN, BUDGET_PER_FRAME = 120.0, 3.0  # a whole graphic: max(BUDGET_MIN, BUDGET_PER_FRAME x frames) seconds
PROGRESS_EVERY = 30.0  # seconds between "frame n of N" lines on a long render
REFERENCE_CHARS = 40_000  # longest finished design sent along as the reference for the other batches
PICTURE_WIDTH = 512  # frame of the shot under each graphic, shown to the designer

# On a vertical (9:16) video, Reels, TikTok and Shorts cover these strips of the frame with their
# buttons, caption and header (fractions of the frame). Designs keep out of them and the render
# check flags anything under them. Pass keep_out= to make() to use other strips.
VERTICAL_KEEP_OUT = {"top": 0.14, "bottom": 0.20, "right": 0.12}

# Sets the page to time t (seconds); called for every frame, in order, from 0. Animations that exist at
# the start play on the graphic's clock; ones that window.seek starts later (a class it adds, an element
# it inserts, element.animate()) play from the frame they start, as in a browser. A design that rebuilds
# its elements every frame would restart them every frame, so for those new animations keep the
# graphic's clock. SVG animations (<animate>, <animateTransform>, <animateMotion>, <set>) aren't in
# getAnimations(): each outermost <svg> has its own clock, paused and set the same way (one inserted later
# starts from the frame it appears; one rebuilt every frame keeps the graphic's clock). Returns the error
# window.seek threw, or null.
_SEEK_JS = """t => {
  const rc = window.__roughcut || (window.__roughcut = {last: [], streak: 0, shared: false});
  const svgs = (adopt) => {
    const fresh = [];
    for (const s of document.querySelectorAll('svg')) {
      if (s.ownerSVGElement || typeof s.pauseAnimations !== 'function') continue;
      if (s.__t0 === undefined) {
        if (!adopt) continue;
        const svgLast = rc.svgLast || [];
        if (t > 0 && svgLast.length > 0 && svgLast.every(o => !o.isConnected)) rc.svgShared = true;
        s.__t0 = t > 0 && !rc.svgShared ? t : 0;
        fresh.push(s);
      }
      s.pauseAnimations();
      s.setCurrentTime(Math.max(0, t - s.__t0));
    }
    if (adopt) rc.svgLast = t > 0 ? fresh : [];
  };
  for (const a of document.getAnimations()) {
    if (a.__t0 !== undefined) { a.pause(); a.currentTime = Math.max(0, t - a.__t0) * 1000; }
  }
  svgs(false);
  let error = null;
  if (typeof window.seek === 'function') {
    try { window.seek(t); } catch (e) { error = String((e && e.message) || e); }
  }
  svgs(true);
  const fresh = document.getAnimations().filter(a => a.__t0 === undefined);
  if (t > 0 && fresh.length) {
    const rebuilt = rc.last.length > 0 && rc.last.every(a => a.effect && a.effect.target && !a.effect.target.isConnected);
    rc.streak += 1;
    if (rebuilt || rc.streak >= 6) rc.shared = true;
  } else {
    rc.streak = 0;
  }
  for (const a of fresh) {
    a.__t0 = rc.shared ? 0 : t;
    a.pause();
    a.currentTime = (t - a.__t0) * 1000;
  }
  rc.last = t > 0 ? fresh : [];
  return error;
}"""

# Font families that text asks for first and this computer doesn't have (canvas width test).
_FONTS_JS = """(() => {
  const generic = new Set(['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui', 'ui-serif', 'ui-sans-serif',
    'ui-monospace', 'ui-rounded', 'math', 'emoji', 'fangsong', '-apple-system', 'blinkmacsystemfont', 'inherit', 'initial']);
  const ctx = document.createElement('canvas').getContext('2d');
  const width = font => { ctx.font = '72px ' + font; return ctx.measureText('mmmmmmmmmmlli1WQ@#&').width; };
  const missing = new Set(), seen = new Set();
  for (const el of document.querySelectorAll('body, body *')) {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    const family = getComputedStyle(el).fontFamily;
    if (seen.has(family)) continue;
    seen.add(family);
    const first = family.split(',')[0].trim().replace(/^["']|["']$/g, '');
    if (!first || generic.has(first.toLowerCase())) continue;
    const q = JSON.stringify(first);
    if (width(q + ', monospace') === width('monospace') && width(q + ', serif') === width('serif')) missing.add(first);
  }
  return [...missing];
})()"""


class GraphicsError(RuntimeError):
    pass


@dataclass
class PlacedGraphic:
    id: str
    spec: GraphicSpec
    index: int  # position in the plan's graphics list (stable across re-cuts)
    start: int  # timeline frame
    frames: int
    word_times: list[tuple[str, float]] = field(default_factory=list)  # (word, seconds from the graphic's start)
    over: str = ""  # what's on screen under it, for the designer
    under: tuple[str, float] | None = None  # (clip id, source seconds) of that picture at its middle
    zoom: float = 1.0  # that picture's punch-in (Transform > Scale; 1: as shot)
    # A design made for other timing (an earlier cut) plays re-timed: (seconds here, seconds in the design).
    retime: list[tuple[float, float]] | None = None
    shortened_for: int | None = None  # plan index of the graphic it was shortened to make way for
    full_frames: int = 0  # its length before that
    latest_start: int | None = None  # the latest frame it may start (inside its words and its piece), when the one before needs room
    waited_for: int | None = None  # plan index of the graphic it starts late for
    on_time: tuple[int, list[tuple[str, float]]] | None = None  # its start and word times before that

    @property
    def slug(self) -> str:
        return f"{self.id}-{self.spec.kind.replace('_', '-')}"

    def timing(self, fd: Fraction) -> dict:
        """Its length and word times in this cut: what a design made for it is made for."""
        return {"seconds": round(float(self.frames * fd), 3), "words": [[w, t] for w, t in self.word_times]}


# ------------------------------------------------------------------ placement


# Shared with B-roll placement, which anchors cutaways on words the same way.
from .phrases import find_phrase as _find_phrase, frame_at as _frame_at  # noqa: E402


def _first_kept(tl: Timeline, clip_id: str, t0: float, t1: float) -> int | None:
    """First timeline frame of the clip between source times t0 and t1."""
    hit = _frame_at(tl, clip_id, t0)
    if hit is not None:
        return hit
    later = [s for s in tl.spine if s.clip_id == clip_id and t0 <= float(s.src_in) < t1]
    return min((s.offset for s in later), default=None)


def place(specs: list[GraphicSpec], tl: Timeline, analysis: Analysis, warnings: list[str]) -> list[PlacedGraphic]:
    fd = tl.frame_duration
    fps = 1 / float(fd)
    placed: list[PlacedGraphic] = []
    for index, spec in enumerate(specs):
        seg = analysis.segment(spec.segment) if spec.segment else None
        if seg is None or not spec.text:
            if spec.text:
                warnings.append(f"graphic {spec.text!r}: segment {spec.segment!r} not found; skipped")
            continue
        clip_id = spec.segment.split(".")[0]
        words = seg.words
        if words:
            i, j = _find_phrase(words, spec.words)
            t0, t1 = words[i].start, words[j].end
        else:  # silence-map segment: no words, use its span
            i = j = 0
            t0, t1 = seg.start, seg.end
        hit = _first_kept(tl, clip_id, t0, max(t1, seg.end))
        if hit is None:
            warnings.append(f"graphic {spec.text!r}: its words aren't in the cut; skipped")
            continue
        # The lead never reaches back over a cut: Final Cut connects a graphic to the clip under its
        # first frame, and it should move (and be deleted) with the clip its words are in.
        piece = tl.spine_at(hit)
        start = max(piece.offset if piece is not None else 0, hit - round(LEAD * fps))
        times = []
        for w in words[i : j + 1]:
            f = _frame_at(tl, clip_id, w.start)
            if f is not None and f >= start:
                times.append((w.text, round((f - start) * float(fd), 3)))
        end_frame = _frame_at(tl, clip_id, max(t0, t1 - 0.01))
        spoken = ((end_frame - start) / fps) if end_frame is not None else 0.0
        seconds = spec.seconds if spec.seconds > 0 else DEFAULT_SECONDS.get(spec.kind, 4.0)
        if spec.kind == "kinetic_caption":
            seconds = max(seconds, spoken + 0.6)
        seconds = min(max(seconds, 1.5), 12.0)
        frames = min(round(seconds * fps), tl.total_frames - start)
        if frames < round(fps):  # less than a second left on the timeline
            warnings.append(f"graphic {spec.text!r}: too close to the end of the cut; skipped")
            continue
        # Room to wait a moment for the graphic before it: while its words are still being said, and never
        # past the end of the clip its words are in (Final Cut connects it to the clip under its first frame).
        piece_end = piece.offset + piece.frames if piece is not None else tl.total_frames
        latest = min(
            start + round(LATE_SECONDS * fps), end_frame if end_frame is not None else start, piece_end - 1,
            start + frames - round(MIN_SECONDS * fps),  # it keeps its end, so it still holds MIN_SECONDS
        )
        placed.append(PlacedGraphic(id="", spec=spec, index=index, start=start, frames=frames, word_times=times, latest_start=max(start, latest)))
    placed = one_at_a_time(placed, fps, warnings)
    for n, g in enumerate(placed, 1):  # numbered in the order they appear
        g.id = f"G{n:02d}"
    return placed


def one_at_a_time(placed: list[PlacedGraphic], fps: float, warnings: list[str]) -> list[PlacedGraphic]:
    """Never two graphics on screen at once: shorten the earlier one, or leave the later one out.

    The earlier one ends GAP_FRAMES before the next starts, if that leaves it at least
    MIN_SECONDS and (for a kinetic caption) time to show its last word. When it doesn't,
    the later one may start up to LATE_SECONDS later, while its words are still being said
    (names read back to back each get their graphic), before it is left out.
    """
    kept: list[PlacedGraphic] = []
    for g in sorted(placed, key=lambda g: (g.start, g.index)):
        prev = kept[-1] if kept else None
        if prev is None or g.start >= prev.start + prev.frames:
            kept.append(g)
            continue
        last_word = prev.word_times[-1][1] if prev.spec.kind == "kinetic_caption" and prev.word_times else 0.0
        need = max(round(MIN_SECONDS * fps), math.ceil((last_word + 0.5) * fps) if last_word else 0)
        wait = prev.start + need + GAP_FRAMES - g.start
        if wait > 0 and g.spec.kind != "kinetic_caption" and g.latest_start is not None and g.start + wait <= g.latest_start:
            g.waited_for, g.on_time = prev.index, (g.start, list(g.word_times))
            g.start += wait
            g.frames -= wait
            g.word_times = [(w, round(t - wait / fps, 3)) for w, t in g.word_times if t - wait / fps >= 0]
        room = g.start - GAP_FRAMES - prev.start
        if room >= round(MIN_SECONDS * fps) and room / fps >= last_word + 0.5:
            warnings.append(shortened_warning(prev, g, room / fps))
            prev.full_frames = prev.full_frames or prev.frames
            prev.frames, prev.shortened_for = room, g.index
            kept.append(g)
        else:
            warnings.append(f"graphic {g.spec.text!r} would cover {prev.spec.text!r}; left out (one graphic at a time)")
    return kept


def shortened_warning(prev: PlacedGraphic, g: PlacedGraphic, seconds: float) -> str:
    return f"graphic {prev.spec.text!r} shortened to {seconds:.1f}s so it's gone before {g.spec.text!r}"


def make_way_back(placed: list[PlacedGraphic], present: set[str]) -> dict[str, tuple[int, int]]:
    """Where graphics that made way for one that isn't there after all (no design, or it didn't render) go back to.

    One that waited for it starts on its words again (keeping its end), once the graphic before it is
    gone; one shortened for it holds longer, as far as the next graphic that is there.
    Returns {graphic ID: (start, frames)} for those that change.
    """
    there = sorted((g for g in placed if g.id in present), key=lambda g: g.start)
    indexes = {g.index for g in there}
    starts, end = {}, None
    for g in there:
        starts[g.id] = g.start
        if g.on_time is not None and g.waited_for not in indexes:
            earliest = g.on_time[0] if end is None else max(g.on_time[0], end + GAP_FRAMES)
            starts[g.id] = min(g.start, earliest)
        end = g.start + g.frames
    back = {}
    for n, g in enumerate(there):
        start = starts[g.id]
        frames = g.frames + g.start - start
        if g.shortened_for is not None and g.shortened_for not in indexes:
            full = g.full_frames + g.start - start
            limit = starts[there[n + 1].id] - GAP_FRAMES - start if n + 1 < len(there) else full
            frames = max(frames, min(full, limit))
        if (start, frames) != (g.start, g.frames):
            back[g.id] = (start, frames)
    return back


def _move_back(g: PlacedGraphic, start: int, frames: int, fps: float) -> None:
    """Put g where make_way_back says: starting at `start` (on its words again, keeping its end) and holding `frames`."""
    shift = g.start - start
    if shift and g.on_time is not None:
        early, words = g.on_time
        late = (start - early) / fps
        g.word_times = [(w, round(t - late, 3)) for w, t in words if t - late >= 0]
        if g.full_frames:
            g.full_frames += shift
        g.start, g.waited_for, g.on_time = start, None, None
    if frames > g.frames + shift:
        g.shortened_for = None
    g.frames = frames


def _said_back(g: PlacedGraphic, was: PlacedGraphic, placed: list[PlacedGraphic], fps: float, warnings: list[str]) -> None:
    """g (placed as `was`) didn't have to make way after all: say so, and drop the warning that said it did."""
    if g.start < was.start:
        gone = next((o for o in placed if o.index == was.waited_for), None)
        if gone is not None:
            log(f"{g.slug}: starts {(was.start - g.start) / fps:.1f}s sooner, on its words, since {gone.spec.text!r} isn't on the timeline")
    if was.shortened_for is not None and g.shortened_for is None:
        gone = next((o for o in placed if o.index == was.shortened_for), None)
        if gone is not None:
            stale = shortened_warning(g, gone, was.frames / fps)
            if stale in warnings:
                warnings.remove(stale)
            log(f"{g.slug}: back to {g.frames / fps:.1f}s, since {gone.spec.text!r} isn't on the timeline")


# ------------------------------------------------------------------ re-timing a design for another cut
#
# Designs bake in the timing they were made for: when each word appears, when the exit starts. A
# re-cut (another pacing, a revision that keeps a design) moves the words and changes the length,
# so every saved design carries a note of the timing it was made for, and the renderer plays it on
# a warped clock: its words land on this cut's words, and its exit plays at full speed at the end.

_TIMING_NOTE = re.compile(r"\n?<!--\s*roughcut-timing\b[^{\n]*(\{[^\n]*?\})\s*-->\n?")
EXIT_SECONDS = 0.8  # the last part of a design (its exit) always plays at full speed when it's re-timed


def without_timing(page: str) -> str:
    """The design without its timing note (what a designer is shown)."""
    return _TIMING_NOTE.sub("", page)


def with_timing(page: str, timing: dict) -> str:
    """The design with a note of the timing it was made for, at the end (replacing any earlier note)."""
    data = json.dumps(timing, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    page = without_timing(page)
    note = f"<!-- roughcut-timing: the length and word times this design was made for; a re-cut re-times it from them. Keep this line. {data} -->"
    return page + ("" if page.endswith("\n") else "\n") + note + "\n"


def design_timing(page: str, fd: Fraction) -> dict | None:
    """{"seconds": s, "words": [(word, s), ...] or None} from a design's timing note; None without one."""
    found = _TIMING_NOTE.findall(page)
    try:
        data = json.loads(found[-1]) if found else None
        if not isinstance(data, dict):
            return None
        seconds = float(data["seconds"]) if "seconds" in data else float(int(data["frames"]) * fd)
        words = data.get("words")
        if words is not None:
            words = [(str(w), float(t)) for w, t in words]
    except (KeyError, TypeError, ValueError):
        return None
    return {"seconds": seconds, "words": words} if seconds > 0 else None


def _token(word: str) -> str:
    return re.sub(r"[^\w$%]", "", word.lower())


def time_map(g: PlacedGraphic, made: dict, fd: Fraction) -> list[tuple[float, float]] | None:
    """How to play a design made for other timing in this cut: (seconds here, seconds in the design) anchors.

    Each word the design was timed to lands on that word here; the last EXIT_SECONDS (the exit)
    play at full speed at the end; in between, time stretches or squeezes. None when the design
    was made for this timing (within half a frame), so it plays as it is.
    """
    fps = 1 / float(fd)
    now_end, made_end = float(g.frames * fd), made["seconds"]
    pairs: list[tuple[float, float]] = []
    if made["words"]:
        k = 0
        for word, t in g.word_times:
            j = next((j for j in range(k, len(made["words"])) if _token(made["words"][j][0]) == _token(word)), None)
            if j is not None:
                pairs.append((t, made["words"][j][1]))
                k = j + 1
    anchors = [(0.0, 0.0)]
    for a, b in pairs:
        if anchors[-1][0] < a < now_end and anchors[-1][1] < b < made_end:
            anchors.append((a, b))
    tail = min(EXIT_SECONDS, now_end / 2, made_end / 2)
    if now_end - tail > anchors[-1][0] and made_end - tail > anchors[-1][1]:
        anchors.append((now_end - tail, made_end - tail))
    anchors.append((now_end, made_end))
    if all(abs(a - b) <= 0.5 / fps for a, b in anchors):
        return None
    return [(round(a, 4), round(b, 4)) for a, b in anchors]


def note_new_designs(placed: list[PlacedGraphic], html: dict[str, str], fd: Fraction) -> None:
    """A design without a timing note was just made, for this cut: note the timing it was made for."""
    for g in placed:
        if g.id in html and not _TIMING_NOTE.search(html[g.id]):
            html[g.id] = with_timing(html[g.id], g.timing(fd))


def retime(placed: list[PlacedGraphic], html: dict[str, str], fd: Fraction, warnings: list[str]) -> None:
    """Set how each design plays in this cut, from the timing it was made for (none when that's this cut's)."""
    by_length = []
    for g in placed:
        made = design_timing(html[g.id], fd) if g.id in html else None
        g.retime = time_map(g, made, fd) if made else None
        if not g.retime:
            continue
        log(f"{g.slug}: design made for {made['seconds']:.2f}s, re-timed to {float(g.frames * fd):.2f}s in this cut")
        if made["words"] is None:
            by_length.append(g.id)
    if by_length:
        warnings.append(
            f"{'graphics' if len(by_length) > 1 else 'graphic'} {', '.join(by_length)}: the saved design has no note of when its words "
            "come (it's from an earlier version of roughcut, or the note was deleted), so it was fitted to this cut by its length "
            "only: check that its words land in time"
        )


def design_time(t: float, anchors: list[tuple[float, float]] | None) -> float:
    """The time in the design to show at time t of the graphic."""
    if not anchors:
        return t
    for (a0, b0), (a1, b1) in zip(anchors, anchors[1:]):
        if t <= a1:
            return b0 + (t - a0) * (b1 - b0) / (a1 - a0)
    a, b = anchors[-1]
    return b + (t - a)


# ------------------------------------------------------------------ design

DESIGN_SYSTEM = """\
You are a motion designer making broadcast-quality animated graphics for a video edit. Each graphic \
is an HTML document that a headless browser renders frame by frame, with a transparent background, \
and that is layered over the footage in Final Cut Pro.

Technical contract (follow exactly; anything else will not render):
- One complete, self-contained HTML document per graphic: inline <style> and <script> only. No \
external URLs, web fonts, images, libraries or network requests. Inline SVG is fine.
- Fonts: the canvas block lists the font families installed for rendering. Use those, by the exact \
name given, or generic families (sans-serif, serif, monospace); any other name falls back to a \
system face.
- The viewport is the canvas size given below, in CSS pixels. html and body: margin 0, \
background transparent, overflow hidden. Never paint a full-frame background except on a title card.
- Each graphic lasts exactly its given seconds. Animate in over the first 0.3-0.7 s, hold, and be \
completely gone (opacity 0 or off-frame) by the end. The last frame is drawn one frame before the end, \
so finish the exit at least 0.1 s before the given seconds.
- Animate only with CSS @keyframes/transitions (explicit durations and delays in seconds, \
animation-fill-mode both), the Web Animations API, or SVG <animate>/<animateTransform> inside \
inline SVG. The renderer pauses every animation and seeks it to each frame's time, so never rely \
on setTimeout, setInterval, requestAnimationFrame, Date or performance.now. Animations that start later (a class or element your window.seek adds, or \
element.animate() called from it) play from the frame they start, as they would in a browser. \
For what CSS can't do (counting a number up, typing text out), define \
window.seek = function (t) { ... } that sets the DOM for time t in seconds; it is called for \
every frame, in order, starting at 0, and must be deterministic.
- Words come with the time they are spoken, in seconds from the graphic's start. Kinetic captions \
reveal each word at its time; other kinds may use the times to land emphasis.
- Keep everything inside the title-safe area (5% in from every edge). Where the brief or a \
graphic's direction says to put graphics (e.g. "right center"), put them there. Otherwise lower \
thirds and stats sit in the lower third or a corner, away from the center of frame where faces \
usually are. Only title cards may fill the frame. On a vertical video the canvas block lists the areas the app's buttons \
and captions cover: keep all text and plates out of them (a lower third sits just above the bottom one).
- Each graphic says what it sits over ("over"), and there may be a frame of that picture: make the \
text read clearly over it, and keep plates off faces and the main subject.
- Legibility over unknown footage: main text at least 48 px on a 1080-line canvas (scale for other \
sizes), with a solid or semi-opaque plate, or a strong shadow.
- Fonts: the font named in the style direction if there is one (it is installed on the editor's \
Mac), otherwise -apple-system, "SF Pro Display", "Helvetica Neue", Arial, sans-serif.

Design: one consistent system across every graphic (type, colors, corner radii, plate style, \
easing, timing). Follow the style direction and the brief. With no direction: clean, modern and \
confident, white type, one accent color, smooth ease-out motion, no gimmicks. Use exactly the \
text given; fix only obvious typos.

Return JSON: "style" (one paragraph describing the design system, reused for consistency) and \
"graphics" (one entry per requested id, with the complete HTML document)."""

DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "style": {"type": "string"},
        "graphics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "html": {"type": "string"}},
                "required": ["id", "html"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["style", "graphics"],
    "additionalProperties": False,
}


def canvas(width: int, height: int) -> tuple[int, int, float]:
    """CSS viewport and scale: designs are made at 1080 lines and rendered at the sequence size."""
    scale = min(width, height) / 1080
    return round(width / scale), round(height / scale), scale


def platform_zones(width: int, height: int) -> dict[str, float]:
    """Strips of the frame that platform buttons and captions cover: VERTICAL_KEEP_OUT on 9:16-like video, else none."""
    return dict(VERTICAL_KEEP_OUT) if width and height >= 1.6 * width else {}


def _canvas_block(width: int, height: int, fd: Fraction, zones: dict[str, float]) -> str:
    cw, ch, _ = canvas(width, height)
    lines = [f"{cw}x{ch} CSS pixels, rendered at {width}x{height}, {1 / float(fd):.3f} fps."]
    families = brand_fonts.families()
    if families:
        lines.append("Fonts installed for rendering: " + ", ".join(families) + ".")
    if zones:
        lines.append(
            "This is a vertical video for Reels, TikTok and Shorts. The app's buttons, caption and header cover "
            "these areas, so keep every graphic's text and plates out of them:"
        )
        for side, part in zones.items():
            px = round((ch if side in ("top", "bottom") else cw) * part)
            span = {"top": f"y 0-{px}", "bottom": f"y {ch - px}-{ch}", "left": f"x 0-{px}", "right": f"x {cw - px}-{cw}"}.get(side)
            if span:
                lines.append(f"- {side} {px} px ({span})")
    return "<canvas>\n" + "\n".join(lines) + "\n</canvas>"


# ------------------------------------------------------------------ what's under each graphic


def picture_under(tl: Timeline, analysis: Analysis, frame: int) -> tuple[Clip | None, float]:
    """The clip on screen at a timeline frame (the topmost B-roll, else the spine clip) and the source second shown."""
    clip, at, _ = shot_under(tl, analysis, frame)
    return clip, at


def shot_under(tl: Timeline, analysis: Analysis, frame: int) -> tuple[Clip | None, float, float]:
    """picture_under, and how far that picture is punched in (a spine piece's scale; B-roll plays as shot)."""
    fd = tl.frame_duration
    covering = sorted((c for c in tl.connected if c.kind == "broll" and c.lane > 0 and c.start <= frame < c.end), key=lambda c: -c.lane)
    for c in covering:
        clip = analysis.clip(c.clip_id)
        if clip is not None and clip.media.has_video:
            return clip, float(c.src_in + (frame - c.start) * fd), 1.0
    sc = tl.spine_at(frame)
    clip = analysis.clip(sc.clip_id) if sc is not None else None
    if clip is not None and clip.media.has_video:
        return clip, float(sc.src_in + (frame - sc.offset) * fd), sc.scale
    return None, 0.0, 1.0


def describe_picture(clip: Clip | None) -> str:
    """'V05 drone_pier.mov (B-Roll): <visual description>; <shot>', for the designer."""
    if clip is None:
        return "black: no picture under it"
    shape = ", vertical" if clip.media.height > clip.media.width else ""
    v = clip.visual or {}
    detail = "; ".join(str(x) for x in (v.get("description"), v.get("shot")) if x)
    return f"{clip.id} {clip.media.name} ({ROLE_LABELS.get(clip.role, clip.role)}{shape})" + (f": {detail[:300]}" if detail else "")


def look_under(placed: list[PlacedGraphic], tl: Timeline, analysis: Analysis) -> None:
    """Note what each graphic sits over, at its middle frame."""
    for g in placed:
        clip, at, g.zoom = shot_under(tl, analysis, g.start + g.frames // 2)
        g.over = describe_picture(clip)
        g.under = (clip.id, 0.0 if clip.media.kind == "image" else at) if clip is not None else None


def _pictures(placed: list[PlacedGraphic], analysis: Analysis, workers: int = 4) -> dict[str, bytes]:
    """A JPEG of the picture under each graphic (where one can be grabbed), by graphic ID."""
    todo = [g for g in placed if g.under is not None and analysis.clip(g.under[0]) is not None]
    if not todo:
        return {}
    with tempfile.TemporaryDirectory(prefix="roughcut-under-") as tmp:

        def grab(g: PlacedGraphic):
            clip = analysis.clip(g.under[0])
            return g.id, ffmpeg_ops.extract_frame(Path(clip.media.path), g.under[1], Path(tmp) / f"{g.id}.jpg", width=PICTURE_WIDTH, zoom=g.zoom)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return {gid: path.read_bytes() for gid, path in pool.map(grab, todo) if path is not None}


def _design_request(
    placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int,
    house_style: str = "", reference: tuple[str, str, str] | None = None, keep_out: dict[str, float] | None = None,
    spellings: list[dict] | None = None,
) -> str:
    """The design request for `placed`. Names in what's said and in the words a kinetic caption reveals are spelled as
    the brief spells them (`spellings`, as the text already is); the timing note keeps the transcript's words."""
    items = []
    for g in placed:
        seg = analysis.segment(g.spec.segment)
        item = {
            "id": g.id,
            "kind": g.spec.kind,
            "seconds": round(float(g.frames * fd), 3),
            "text": g.spec.text,
            "subtext": g.spec.subtext,
            "direction": g.spec.direction,
            "said": respell(seg.text, spellings or []) if seg is not None else "",
            "words": [{"word": w, "at": t} for w, t in respell_timed(g.word_times, spellings or [])],
        }
        if g.over:
            item["over"] = g.over
        items.append(item)
    # Saved designs shown as examples (an approved look, a revision's reference) lose their timing notes:
    # those describe another graphic's timing, and a new design is made for its own.
    style = without_timing(style)
    parts = [
        _canvas_block(width, height, fd, platform_zones(width, height) if keep_out is None else keep_out),
        "<brief>\n" + (brief.strip() or "No brief given.") + "\n</brief>",
        "<style_direction>\n" + (style.strip() or "None given: use the default look.") + "\n</style_direction>",
    ]
    if house_style:
        parts.append("<design_system>\nAlready established for this video; match it exactly:\n" + house_style + "\n</design_system>")
    if reference:
        rid, kind, page = reference
        page = without_timing(page)
        parts.append(
            f'<reference_design id="{rid}" kind="{kind}">\nA finished graphic from this video. Reuse its type, colors, plates, '
            f"easing and timing so the set looks like one system.\n{page[:REFERENCE_CHARS]}\n</reference_design>"
        )
    parts.append("<graphics>\n" + json.dumps(items, indent=1) + "\n</graphics>")
    parts.append(f"Design these {len(items)} graphics: {', '.join(g.id for g in placed)}.")
    return "\n\n".join(parts)


def design(
    provider, placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int,
    warnings: list[str], batch: int = 4, workers: int = 3, keep_out: dict[str, float] | None = None,
    house_style: str = "", found: dict | None = None, spellings: list[dict] | None = None,
) -> dict[str, str]:
    """HTML for each graphic, by ID.

    The first batch is designed on its own and sets the look; the other batches are then
    designed in parallel, each given that house style and one finished design to match.
    `house_style` is a design system already established for this video (a revision's new
    graphics match it); the one in use at the end goes into found["house_style"].
    """
    from .providers import ImagePart, ProviderAuthError, ProviderError, TextPart

    html: dict[str, str] = {}
    if not placed:
        return html
    pictures = _pictures(placed, analysis) if getattr(provider, "supports_vision", False) else {}
    groups = [placed[n : n + batch] for n in range(0, len(placed), batch)]
    t_all = time.monotonic()
    tried = 0
    log(f"designing {len(placed)} graphics in {len(groups)} request(s)" + (f", with a frame of the shot under {len(pictures)}" if pictures else ""))
    progress.stage("graphics", f"Designing {len(placed)} graphics", current=0, total=len(placed))

    def ask(group: list[PlacedGraphic], house: str, reference: tuple[str, str, str] | None):
        request = _design_request(
            group, analysis, fd, brief=brief, style=style, width=width, height=height, house_style=house, reference=reference, keep_out=keep_out,
            spellings=spellings,
        )
        parts: list = []
        for g in group:  # the shot under each graphic, then the request
            if g.id in pictures:
                parts += [TextPart(f"{g.id} sits over {g.over}"), ImagePart(pictures[g.id], "image/jpeg")]
        t0 = time.monotonic()
        try:
            raw = provider.complete_json(DESIGN_SYSTEM, [*parts, TextPart(request)], DESIGN_SCHEMA, schema_name="graphics", purpose="graphics design", max_tokens=64000)
        except ProviderAuthError:
            raise
        except ProviderError as e:
            return group, None, str(e), time.monotonic() - t0
        return group, raw, None, time.monotonic() - t0

    def take(group: list[PlacedGraphic], raw: dict | None, error: str | None, seconds: float) -> dict[str, str]:
        nonlocal tried
        tried += len(group)
        progress.stage("graphics", f"Designing {len(placed)} graphics", current=tried, total=len(placed))
        if raw is None:
            warnings.append(f"graphics design failed for {', '.join(g.id for g in group)}: {error}")
            log(f"designing {_ids(group)} failed after {_took(seconds)}: {error}")
            return {}
        wanted = {g.id for g in group}
        got = {}
        for entry in raw.get("graphics") or []:
            gid = str(entry.get("id", "")).upper() if isinstance(entry, dict) else ""
            if gid in wanted and "<" in str(entry.get("html", "")):
                got[gid] = without_timing(str(entry["html"]))[:300_000]  # a new design is made for this cut's timing
        html.update(got)
        missing = [g.id for g in group if g.id not in got]
        if missing:
            warnings.append(f"no design came back for {', '.join(missing)}; skipped")
        log(f"designed {_ids(group)} in {_took(seconds)}" + (f" ({', '.join(missing)} missing)" if missing else ""))
        return got

    # The look is set by the first batch that comes back; then the rest go out together.
    house, reference = house_style, None
    pending = list(groups)
    while pending:
        group = pending.pop(0)
        answer = ask(group, house_style, None)
        got = take(*answer)
        if got:
            house = house_style or str(answer[1].get("style") or "")
            first = next((g for g in group if g.id in got and g.spec.kind != "title_card"), None) or next(g for g in group if g.id in got)
            reference = (first.id, first.spec.kind, got[first.id])
            break
    for answer in _in_background(lambda group: ask(group, house, reference), pending, workers):
        take(*answer)
    log(f"designed {len(html)} of {len(placed)} graphics in {_took(time.monotonic() - t_all)}")
    if found is not None and house:
        found["house_style"] = house
    return html


def _ids(group: list[PlacedGraphic]) -> str:
    return group[0].id if len(group) == 1 else f"{group[0].id}-{group[-1].id}"


def _in_background(fn, items: list, workers: int):
    """Yield fn(item) for each item as it finishes, running at most `workers` at once in daemon threads.

    For model requests that take minutes: Ctrl-C stops the wait at once, and the process can
    exit without finishing (and paying for) the requests still in flight, where a
    ThreadPoolExecutor would wait for them. An exception from fn is raised here.
    """
    if not items:
        return
    todo: queue.SimpleQueue = queue.SimpleQueue()
    for item in items:
        todo.put(item)
    finished: queue.SimpleQueue = queue.SimpleQueue()
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            try:
                item = todo.get_nowait()
            except queue.Empty:
                return
            try:
                finished.put((True, fn(item)))
            except BaseException as e:  # noqa: BLE001 - raised in the caller's thread
                finished.put((False, e))

    for _ in range(max(1, min(workers, len(items)))):
        threading.Thread(target=work, name="roughcut-design", daemon=True).start()
    try:
        for _ in items:
            while True:
                try:
                    ok, value = finished.get(timeout=0.2)  # wake often, so Ctrl-C is handled at once
                    break
                except queue.Empty:
                    continue
            if not ok:
                raise value
            yield value
    finally:
        stop.set()  # stopped or failed: start no more requests


# ------------------------------------------------------------------ rendering


def _chromium_paths() -> list[str]:
    paths = [os.environ.get("ROUGHCUT_CHROMIUM", "")]
    paths += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if bp:
        paths.append(str(Path(bp) / "chromium"))
    return [p for p in paths if p and os.path.isfile(p) and os.access(p, os.X_OK)]


def _launch_options():
    """Playwright's own Chromium, then an installed Chrome/Edge, then known paths."""
    yield {}
    yield {"channel": "chrome"}
    yield {"channel": "msedge"}
    for path in _chromium_paths():
        yield {"executable_path": path}


def _no_browser() -> GraphicsError:
    return GraphicsError(f"No browser to render graphics with. Install Google Chrome, or run:  {_PY} -m playwright install chromium")


def launch_browser(p):
    for kwargs in _launch_options():
        try:
            return p.chromium.launch(**kwargs)
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    raise _no_browser()


async def _launch_browser_async(p):
    for kwargs in _launch_options():
        try:
            return await p.chromium.launch(**kwargs)
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    raise _no_browser()


# Spelled with this interpreter's path: `pip` alone isn't on PATH when the venv isn't activated.
_PY = shlex.quote(sys.executable)
INSTALL_HELP = f"Motion graphics need Playwright. Install it with:  {_PY} -m pip install 'playwright>=1.40'"


def renderer_problem() -> str | None:
    """Why graphics can't be rendered on this machine, or None when they can."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return INSTALL_HELP
    try:
        with sync_playwright() as p:
            launch_browser(p).close()
    except GraphicsError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"The graphics renderer didn't start ({str(e).splitlines()[0][:200]})."
    return None


class _Stuck(Exception):
    """A browser step didn't finish in time."""


class _Stopped(Exception):
    """The run was interrupted (Ctrl-C)."""


_STOP = threading.Event()  # set on Ctrl-C: every render stops at its next frame


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


def _note(notes: list[str], text: str) -> None:
    text = " ".join(str(text).split())[:200]
    if text and text not in notes and len(notes) < 20:
        notes.append(text)


async def _bounded(awaitable, seconds: float, what: str):
    """Await with a deadline kept on our side, so a page whose main thread is stuck can't hang us."""
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except Exception as e:
        if isinstance(e, asyncio.TimeoutError) or type(e).__name__ == "TimeoutError":  # ours, or Playwright's own deadline
            raise _Stuck(what) from None
        raise


async def _evaluate(cdp, expression: str, seconds: float, what: str):
    """Run JS in the page. Chrome stops it after `seconds`; if the page never answers, so do we."""
    try:
        reply = await _bounded(
            cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "timeout": int(seconds * 1000)}), seconds + 5, what
        )
    except _Stuck:
        raise
    except Exception as e:  # noqa: BLE001 - Chrome reports the stopped script as a protocol error
        if "terminated" in str(e).lower():
            raise _Stuck(what) from None
        raise
    if reply.get("exceptionDetails"):
        d = reply["exceptionDetails"]
        raise RuntimeError(((d.get("exception") or {}).get("description") or d.get("text") or "script error").splitlines()[0][:200])
    return (reply.get("result") or {}).get("value")


_LOAD_FONTS_JS = "() => Promise.all([...document.fonts].map(f => f.load().catch(() => null))).then(() => document.fonts.ready).then(() => true)"


async def load_fonts(page) -> None:
    """Load every font the design declares before frame 0.

    The page's load event doesn't wait for web fonts, and a face is only fetched once text
    uses it, so without this the first frames (or the first frames of text a later animation
    reveals) draw in a fallback face and then jump to the brand font.
    """
    await _bounded(page.evaluate(_LOAD_FONTS_JS), STEP_TIMEOUT, "never finished loading its fonts")


async def _frame_png(page, cdp, canvas_: tuple[int, int, float], fast: bool, what: str) -> bytes:
    """One frame as a transparent PNG at the sequence size.

    The fast path asks Chrome directly (about twice as quick as page.screenshot, same pixels);
    `scale` makes it capture device pixels rather than CSS pixels.
    """
    cw, ch, scale = canvas_
    if fast:
        clip = {"x": 0, "y": 0, "width": cw, "height": ch, "scale": scale}
        reply = await _bounded(cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True, "clip": clip}), STEP_TIMEOUT, what)
        return base64.b64decode(reply["data"])
    return await _bounded(page.screenshot(omit_background=True, type="png", caret="hide"), STEP_TIMEOUT + 5, what)


def _png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24]) if png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) >= 24 else (0, 0)


def _blocker(notes: list[str]):
    async def block(route) -> None:  # designs may not fetch anything
        _note(notes, f"tried to load {route.request.url[:120]} (blocked: designs must be self-contained)")
        await route.abort()

    return block


def _console(notes: list[str], message) -> None:
    if message.type == "error" and not message.text.startswith("Failed to load resource"):  # blocked loads are noted by URL
        _note(notes, f"console error: {message.text}")


async def _render_async(job: dict) -> dict:
    from playwright.async_api import async_playwright

    gid, frames, fps = job["id"], job["frames"], job["fps"]
    out = Path(job["out"])
    tmp = out.with_suffix(".partial.mov")
    cw, ch, scale = job["canvas"]
    frame_timeout = job.get("frame_timeout") or FRAME_TIMEOUT
    budget = job.get("budget") or max(BUDGET_MIN, BUDGET_PER_FRAME * frames)
    notes: list[str] = []  # script errors from the page, deduplicated
    result = {"id": gid, "error": None, "notes": notes, "seconds": 0.0, "fast": True}
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", job["rate"], "-i", "-", *ENCODER, "-r", job["rate"], str(tmp)]
    t0 = time.monotonic()
    ff = None
    # A file, not a pipe: ffmpeg can't block on a full stderr pipe while we're still feeding it frames.
    with tempfile.TemporaryFile() as ff_err:
        try:
            async with async_playwright() as p:
                browser = await _launch_browser_async(p)
                try:
                    page = await browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=scale)
                    page.set_default_timeout(STEP_TIMEOUT * 1000)
                    await page.route("**/*", _blocker(notes))
                    page.on("pageerror", lambda e: _note(notes, f"script error: {e}"))
                    page.on("console", lambda m: _console(notes, m))
                    cdp = await page.context.new_cdp_session(page)
                    # Transparent page background, set once (page.screenshot sets and resets it every frame).
                    await _bounded(cdp.send("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}}), STEP_TIMEOUT, "didn't start")
                    await _bounded(page.set_content(brand_fonts.embed(job["html"]), wait_until="load"), STEP_TIMEOUT + 5, "never finished loading")
                    await load_fonts(page)
                    size = tuple(job.get("size") or (round(cw * scale), round(ch * scale)))
                    fast = True
                    seek_error, fonts = None, []
                    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=ff_err)
                    last_note = t0
                    for frame in range(frames):
                        if _STOP.is_set():
                            raise _Stopped()
                        now = time.monotonic()
                        if now - t0 > budget:
                            raise _Stuck(f"was still rendering after {budget:.0f}s, at frame {frame} of {frames}")
                        if now - last_note >= PROGRESS_EVERY:
                            log(f"{job.get('slug', gid)}: frame {frame} of {frames} ({_took(now - t0)})")
                            last_note = now
                        t = design_time(frame / fps, job.get("retime"))  # a design made for other timing plays re-timed
                        error = await _evaluate(cdp, f"({_SEEK_JS})({t!r})", frame_timeout, f"froze at frame {frame} (window.seek never returned)")
                        if error and seek_error is None:
                            seek_error = f"its window.seek failed at {t:.2f}s ({' '.join(str(error).split())[:160]}); rendered anyway, check it"
                        if frame == frames // 2:  # most of the graphic is on screen by now
                            for font in await _evaluate(cdp, _FONTS_JS, STEP_TIMEOUT, "froze while checking fonts") or []:
                                fonts.append(f"asks for the font {str(font)[:60]}, which isn't installed on this computer, so it rendered in a fallback font")
                        drawing = f"froze at frame {frame} (the page stopped drawing)"
                        png = await _frame_png(page, cdp, (cw, ch, scale), fast, drawing)
                        if fast and frame == 0 and _png_size(png) != size:
                            fast = result["fast"] = False  # the quick capture came out the wrong size: use Playwright's screenshot
                            png = await _frame_png(page, cdp, (cw, ch, scale), fast, drawing)
                        try:
                            ff.stdin.write(png)
                        except (BrokenPipeError, OSError):
                            raise RuntimeError("ffmpeg stopped taking frames") from None
                finally:
                    try:
                        await _bounded(browser.close(), 20, "didn't close")
                    except Exception:  # noqa: BLE001 - leaving async_playwright stops the browser anyway
                        pass
            try:
                ff.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            ff.wait(timeout=300)
            if ff.returncode != 0 or not tmp.exists():
                ff_err.seek(0)
                raise RuntimeError(f"ffmpeg failed: {ff_err.read().decode(errors='replace').strip()[:300]}")
            tmp.replace(out)
            for note in ([seek_error] if seek_error else []) + fonts + inspect(out, job.get("kind", ""), size, job.get("keep_out")):
                _note(notes, note)
        except _Stuck as e:
            result["error"] = f"{e}; left out. Fix graphics/{job.get('slug') or out.stem}.html and run `roughcut render`"
        except _Stopped:
            result["error"] = "stopped"
        except Exception as e:  # noqa: BLE001 - report, don't kill the run
            result["error"] = f"didn't render: {_first_line(e)}"
        finally:
            if ff is not None and ff.poll() is None:
                ff.kill()
                ff.wait()
            if result["error"]:
                tmp.unlink(missing_ok=True)
    result["seconds"] = time.monotonic() - t0
    return result


def _render_one(job: dict) -> dict:
    """Render one graphic to ProRes 4444 with alpha.

    Runs in a worker thread with its own event loop, Playwright and browser (one per
    thread; never processes). Every browser step has a deadline, so a design that
    freezes is reported instead of hanging the run.
    """
    try:
        return asyncio.run(_render_async(job))
    except Exception as e:  # noqa: BLE001 - e.g. no browser could start
        return {"id": job["id"], "error": f"didn't render: {_first_line(e)}", "notes": [], "seconds": 0.0}


def _digest(html: str, frames: int, rate: str, width: int, height: int, retime: list | None = None) -> str:
    """Identifies a render: the design, its length and size, its re-timing (if any), and how it's rendered and encoded."""
    how = " ".join(ENCODER) + "|" + _SEEK_JS
    timed = f"|retime={json.dumps(retime)}" if retime else ""
    return hashlib.sha256(f"{frames}|{rate}|{width}x{height}|{how}{timed}|{html}".encode()).hexdigest()[:16]


def graphic_digest(g: PlacedGraphic, page: str, tl: Timeline) -> str:
    fd = tl.frame_duration
    return _digest(page, g.frames, f"{fd.denominator}/{fd.numerator}", tl.width, tl.height, g.retime)


def unchanged(placed: list[PlacedGraphic], html: dict[str, str], folder: Path, tl: Timeline, previous: dict) -> dict[str, Path]:
    """Rendered clips that still match their design and length, by graphic ID.

    A clip's file name carries the digest of the design, length, size and renderer it was made
    from, and a render only gets that name once it's complete, so an existing file is reusable.
    Graphics are numbered in timeline order, so one added or dropped earlier renumbers the rest:
    a clip rendered from the same design under another number gets this graphic's name too (a
    hard link, else a copy) instead of rendering again. Clips from before digest names re-render
    once (their old files stay for cuts already in Final Cut).
    """
    found = {}
    for g in placed:
        if g.id not in html:
            continue
        digest = graphic_digest(g, html[g.id], tl)
        out = clip_path(folder, g, digest)
        if out.exists() or _same_render_as(folder, g, digest, out):
            found[g.id] = out
    return found


def _same_render_as(folder: Path, g: PlacedGraphic, digest: str, out: Path) -> bool:
    """Give `out` the clip already rendered from this design under another graphic number, if there is one."""
    pattern = re.compile(r"G\d+-" + re.escape(g.slug.split("-", 1)[1]) + "-" + re.escape(digest[:6]) + r"\.mov")
    try:
        twins = sorted(p for p in folder.glob(f"G*-{digest[:6]}.mov") if pattern.fullmatch(p.name) and p.is_file())
    except OSError:
        return False
    for src in twins:
        try:
            os.link(src, out)
        except FileExistsError:
            return True
        except OSError:
            partial = out.with_suffix(".partial.mov")
            try:
                shutil.copy2(src, partial)
                partial.replace(out)
            except OSError:
                partial.unlink(missing_ok=True)
                continue
        log(f"{g.slug}: same design and length as {src.name}, reused")
        return True
    return False


def clip_path(folder: Path, g: PlacedGraphic, digest: str) -> Path:
    """The digest is in the name: a changed graphic is a new file, so a cut already imported into
    Final Cut keeps the clip it was made with."""
    return folder / f"{g.slug}-{digest[:6]}.mov"


def save_designs(placed: list[PlacedGraphic], html: dict[str, str], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for g in placed:
        if g.id in html:
            (folder / f"{g.slug}.html").write_text(html[g.id], encoding="utf-8")


DESIGN_FILE = re.compile(r"G\d+-[a-z0-9-]+\.html")  # a graphic's saved design, e.g. G01-lower-third.html
LEFT_OUT = "left-out"  # graphics/left-out/: designs of planned graphics a cut couldn't place, kept for the next re-cut


def keep_left_out(specs: list[GraphicSpec], placed: list[PlacedGraphic], saved_html: dict[int, str], out_dir: Path) -> list[dict]:
    """graphics.json entries for the planned graphics this cut doesn't place (one would have covered another,
    or its words aren't in this cut), so the next re-cut can place them again without the model.

    A designed one keeps its design in graphics/left-out/, named by its place in the plan (01 is the
    plan's first graphic): renumbering never writes over it, and it isn't put aside as unused.
    """
    there = {g.index for g in placed}
    entries = []
    for index, spec in enumerate(specs):
        if not spec.text or index in there:
            continue
        entry = {"index": index, **asdict(spec), "placed": False}
        if index in saved_html:
            rel = f"graphics/{LEFT_OUT}/{index + 1:02d}-{spec.kind.replace('_', '-')}.html"
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_text(saved_html[index], encoding="utf-8")
            entry["html"] = rel
        entries.append(entry)
    return entries


def _put_aside_old_designs(folder: Path, placed: list[PlacedGraphic], html: dict[str, str], left_out: list[dict] | None = None) -> None:
    """Move designs this run doesn't use into graphics/old/, so the folder only holds the live ones.

    Graphics are numbered in timeline order, so a re-cut (or an older folder) can leave
    G01-quote.html beside the live G02-quote.html; editing it would do nothing. Their content is
    kept (it may hold hand edits). Rendered .mov files stay where they are: a project already
    imported into Final Cut may still use them. A design in graphics/left-out/ goes too once its
    graphic is placed again (its live copy is then the numbered one) or leaves the plan.
    """
    live = {f"{g.slug}.html" for g in placed if g.id in html}
    stale = [p for p in folder.glob("G*.html") if DESIGN_FILE.fullmatch(p.name) and p.name not in live]
    waiting = {Path(str(e["html"])).name for e in left_out or [] if e.get("html")}
    stale += [p for p in (folder / LEFT_OUT).glob("*.html") if p.name not in waiting]
    if not stale:
        return
    old = folder / "old"
    moved = 0
    try:  # tidying up; never worth failing the run over
        old.mkdir(exist_ok=True)
        for p in stale:
            target, n = old / p.name, 2
            while target.exists():
                target, n = old / f"{p.stem} ({n}).html", n + 1
            p.replace(target)
            moved += 1
    except OSError:
        pass
    if moved:
        log(f"moved {moved} design(s) no graphic uses any more to {old}")


def render(
    placed: list[PlacedGraphic], html: dict[str, str], folder: Path, tl: Timeline, *, workers: int, warnings: list[str],
    previous: dict | None = None, notes: dict[str, list[str]] | None = None, keep_out: dict[str, float] | None = None,
    on_rendered=None,
) -> dict[str, Path]:
    """Render what's new or changed; reuse clips whose design and length are the same.

    `previous` is the manifest from before this run (read from disk when not given);
    `notes` collects what each graphic's render reported, for the manifest;
    `on_rendered(done)` is called with every clip so far each time one finishes.
    """
    save_designs(placed, html, folder)
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    if previous is None:
        previous = _read_manifest(folder.parent)
    notes = {} if notes is None else notes
    done = unchanged(placed, html, folder, tl, previous)
    notes.update(_old_notes(previous, placed, html, tl, done))
    jobs = [
        {"id": g.id, "slug": g.slug, "kind": g.spec.kind, "html": html[g.id], "frames": g.frames, "fps": 1 / float(fd), "rate": rate,
         "canvas": canvas(tl.width, tl.height), "size": (tl.width, tl.height), "keep_out": keep_out, "retime": g.retime,
         "out": str(clip_path(folder, g, graphic_digest(g, html[g.id], tl)))}
        for g in placed
        if g.id in html and g.id not in done
    ]
    if jobs:
        t0 = time.monotonic()
        log(f"rendering {len(jobs)} graphic(s) at {tl.width}x{tl.height}" + (f" ({len(done)} unchanged)" if done else ""))
        _STOP.clear()
        # One Playwright instance and browser per thread (Playwright's rule); the browsers do the work.
        pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs))))
        try:
            futures = {pool.submit(_render_one, job): job for job in jobs}
            for n, future in enumerate(as_completed(futures), 1):
                job, result = futures[future], future.result()
                progress.stage("graphics", f"Rendering {len(jobs)} graphics", current=n, total=len(jobs))
                notes[job["id"]] = list(result.get("notes") or [])
                if result.get("error"):
                    warnings.append(f"graphic {job['id']} {result['error']}")
                    log(f"{job['slug']}: {result['error']}")
                else:
                    done[job["id"]] = Path(job["out"])
                    if on_rendered is not None:  # recorded before it's announced
                        on_rendered(done)
                    log(f"rendered {job['slug']} ({job['frames']} frames) in {_took(result.get('seconds') or 0.0)} [{n}/{len(jobs)}]")
        except KeyboardInterrupt:
            _STOP.set()  # the workers stop at their next frame and clean up
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
        log(f"rendered {sum(1 for j in jobs if j['id'] in done)} of {len(jobs)} graphic(s) in {_took(time.monotonic() - t0)}")
    return done


def note_warnings(placed: list[PlacedGraphic], notes: dict[str, list[str]]) -> list[str]:
    """One warning per distinct note (up to 3 per graphic), naming every graphic it applies to."""
    grouped: dict[str, list[str]] = {}
    for g in placed:
        said = notes.get(g.id) or []
        for note in said[:3] + ([f"{len(said) - 3} more messages from its scripts"] if len(said) > 3 else []):
            grouped.setdefault(note, []).append(g.id)
    return [f"{'graphics' if len(ids) > 1 else 'graphic'} {', '.join(ids)}: {note}" for note, ids in grouped.items()]


# ------------------------------------------------------------------ checking a render

TITLE_SAFE = 0.05  # title-safe margin, as a fraction of the frame
QC_GRACE = 0.01  # soft shadows and glows may reach this much past it


QC_WIDTH = 320  # the alpha channel is checked at this width
QC_ALPHA = 8  # of 255: anything more opaque than this counts as visible
QC_LAST_ALPHA = 64  # of 255: more than this on the last frame is a visible cut, not the tail of a fade


def _alpha_frames(path: Path, size: tuple[int, int]) -> tuple[int, int, list[bytes]] | None:
    """(width, height, frames) of the alpha channel, area-averaged down to QC_WIDTH so thin strokes still count."""
    w = QC_WIDTH
    h = max(2, round(w * size[1] / size[0] / 2) * 2) if size[0] and size[1] else w * 9 // 16
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"alphaextract,scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    n = w * h
    return w, h, [proc.stdout[i * n : (i + 1) * n] for i in range(len(proc.stdout) // n)]


def _box(frame: bytes, w: int, h: int) -> tuple[float, float, float, float] | None:
    """The box holding everything visible (x0, y0, x1, y1 as fractions of the frame), or None when blank."""
    rows = [r for r in range(h) if max(frame[r * w : (r + 1) * w]) > QC_ALPHA]
    if not rows:
        return None
    x0 = next(c for c in range(w) if max(frame[c::w]) > QC_ALPHA)
    x1 = next(c for c in range(w - 1, -1, -1) if max(frame[c::w]) > QC_ALPHA)
    return x0 / w, rows[0] / h, (x1 + 1) / w, (rows[-1] + 1) / h


def inspect(path: Path, kind: str, size: tuple[int, int], keep_out: dict[str, float] | None = None) -> list[str]:
    """What a viewer would notice: blank, never animating off, outside the title-safe area, under platform buttons."""
    read = _alpha_frames(path, size)
    if not read or not read[2]:
        return []
    w, h, frames = read
    boxes = [_box(f, w, h) for f in frames]
    if not any(boxes):
        return ["came out blank: nothing is visible in any frame"]
    notes = []
    if max(frames[-1]) > QC_LAST_ALPHA:  # a fade that's nearly done on the last frame is fine
        notes.append("is still on screen in its last frame, so it cuts off instead of animating out")
    n = len(boxes)
    hold = [b for b in boxes[n // 3 : max(n // 3 + 1, 2 * n // 3)] if b]  # the middle third: not entering or leaving
    if hold:
        m = TITLE_SAFE - QC_GRACE
        outside = [b for b in hold if b[0] < m or b[1] < m or b[2] > 1 - m or b[3] > 1 - m]
        if kind != "title_card" and len(outside) * 2 > len(hold):  # title cards may fill the frame
            notes.append("reaches outside the title-safe area (5% in from the edges)")
        # A title card that fills the frame can't be judged from its outline (its text may sit anywhere
        # on the background), but one whose words sit in a strip is as covered as any other graphic.
        fills = (lambda b, side: _spans(b, side)) if kind == "title_card" else (lambda b, side: False)
        zones = platform_zones(*size) if keep_out is None else keep_out
        under = [side for side, part in zones.items() if len([b for b in hold if _covers(b, side, part) and not fills(b, side)]) * 2 > len(hold)]
        if under:
            notes.append(f"sits under the app's buttons and captions on a vertical video ({', '.join(f'{s} {zones[s]:.0%}' for s in under)})")
    return notes


def _spans(box: tuple[float, float, float, float], side: str) -> bool:
    """Whether a visible box runs edge to edge across a strip (a full-frame background), rather than sitting in it."""
    x0, y0, x1, y1 = box
    m = TITLE_SAFE - QC_GRACE
    return (y0 < m and y1 > 1 - m) if side in ("top", "bottom") else (x0 < m and x1 > 1 - m)


def _covers(box: tuple[float, float, float, float], side: str, part: float) -> bool:
    """Whether a visible box reaches into a strip along one side of the frame."""
    x0, y0, x1, y1 = box
    g = QC_GRACE
    return {"top": y0 < part - g, "bottom": y1 > 1 - part + g, "left": x0 < part - g, "right": x1 > 1 - part + g}.get(side, False)


def _took(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# ------------------------------------------------------------------ timeline + manifest


def attach(tl: Timeline, placed: list[PlacedGraphic], files: dict[str, Path], warnings: list[str]) -> list[Clip]:
    """Probe the rendered clips and connect them above everything else on the timeline."""
    from .media import probe

    clips = []
    base = max((c.lane for c in tl.connected if c.lane > 0), default=0)
    lanes: dict[int, list[tuple[int, int]]] = {}
    for g in placed:
        path = files.get(g.id)
        if path is None:
            continue
        try:
            info = probe(path)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"graphic {g.id}: rendered file unreadable ({e})")
            continue
        lane = base + 1
        while any(not (g.start + g.frames <= s or g.start >= e) for s, e in lanes.get(lane, [])):
            lane += 1
        lanes.setdefault(lane, []).append((g.start, g.start + g.frames))
        clip = Clip(id=g.id, media=info, role="graphic", transcript_source="graphic")
        clips.append(clip)
        tl.connected.append(ConnectedClip(clip_id=g.id, src_in=Fraction(0), frames=g.frames, start=g.start, lane=lane, kind="graphic", note=g.spec.text))
    return clips


def _read_manifest(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / MANIFEST).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(
    out_dir: Path, placed: list[PlacedGraphic], files: dict[str, Path], html: dict[str, str], tl: Timeline, style: str,
    notes: dict[str, list[str]] | None = None, house_style: str = "", left_out: list[dict] | None = None,
) -> None:
    """graphics.json: every placed graphic, its design, and the clip rendered from it (if any).

    `house_style` is the designer's description of its design system, for later designs to match.
    "retime" is how a design made for other timing plays in this cut ((seconds here, seconds in the design)).
    `left_out`: entries for the planned graphics this cut couldn't place (keep_left_out), listed after the placed ones."""
    entries = []
    for g in placed:
        entry = {"id": g.id, "index": g.index, **asdict(g.spec), "start_frame": g.start, "frames": g.frames}
        if g.over:
            entry["over"] = g.over
        if g.id in html:
            entry["html"] = f"graphics/{g.slug}.html"
            entry["digest"] = graphic_digest(g, html[g.id], tl)
            if g.retime:
                entry["retime"] = [list(a) for a in g.retime]
        if g.id in files:
            entry["file"] = f"graphics/{files[g.id].name}"
            entry["rendered"] = entry.get("digest")  # the design that file was rendered from
        if notes and notes.get(g.id):
            entry["notes"] = notes[g.id]
        entries.append(entry)
    entries += left_out or []
    from .versions import atomic_write

    data = {"style": style, **({"house_style": house_style} if house_style else {}), "graphics": entries}
    atomic_write(out_dir / MANIFEST, json.dumps(data, indent=1))


def saved_designs(out_dir: Path) -> tuple[list[GraphicSpec], dict[int, str], str]:
    """Specs and (possibly hand-edited) HTML from an earlier run, for `roughcut render`.

    Each spec sits at its position in the plan ("index"), and HTML is keyed by it. Positions don't
    move when a re-cut drops a graphic, and graphics.json lists entries in timeline order, not
    plan order, so entries are placed by index (gaps become empty specs, which place() skips).

    A design without its timing note (made before designs carried one, or the note was deleted
    by hand) is taken as made for the length graphics.json gives it, so a re-cut can at least
    fit its exit to the new length.
    """
    data = _read_manifest(out_dir)
    by_index: dict[int, GraphicSpec] = {}
    html: dict[int, str] = {}
    entries = [g for g in data.get("graphics", []) if isinstance(g, dict)]
    for position, g in enumerate(entries):
        try:
            index = int(g.get("index", position))
        except (TypeError, ValueError):
            index = position
        if index < 0 or index in by_index:
            continue
        try:
            seconds = float(g.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        by_index[index] = GraphicSpec(
            kind=str(g.get("kind", "quote")), segment=str(g.get("segment", "")), words=str(g.get("words", "")),
            text=str(g.get("text", "")), subtext=str(g.get("subtext", "")), seconds=seconds, direction=str(g.get("direction", "")),
        )
        page = out_dir / str(g.get("html") or "")
        if g.get("html") and page.is_file():
            html[index] = page.read_text(encoding="utf-8")
            frames = g.get("frames")
            if not _TIMING_NOTE.search(html[index]) and isinstance(frames, int) and frames > 0:
                html[index] = with_timing(html[index], {"frames": frames})
    empty = GraphicSpec(kind="quote", segment="", words="", text="")
    specs = [by_index.get(i, empty) for i in range(max(by_index, default=-1) + 1)]
    return specs, html, str(data.get("style", ""))


def with_planned(specs: list[GraphicSpec], planned: list[GraphicSpec]) -> list[GraphicSpec]:
    """Saved specs, with the plan's graphics that the saved cut left out back at their positions.

    A build's graphics.json (or an older folder's) holds only the graphics its cut placed. One
    left out then (it would have covered another, or its words weren't in that cut) may fit this
    cut: `render --design-missing` can design it, and the re-cut after that show it.
    """
    empty = GraphicSpec(kind="quote", segment="", words="", text="")
    out = list(specs) + [empty] * max(0, len(planned) - len(specs))
    have = {(g.kind, g.segment, g.words, g.text) for g in specs if g.text}
    for i, spec in enumerate(planned):
        if not out[i].text and spec.text and (spec.kind, spec.segment, spec.words, spec.text) not in have:
            out[i] = spec
    return out


def make(
    provider, specs: list[GraphicSpec], tl: Timeline, analysis: Analysis, out_dir: Path, *, brief: str, style: str, workers: int,
    saved_html: dict[int, str] | None = None, keep_out: dict[str, float] | None = None, logo: str | None = None, look=(),
    house_style: str = "", planned: list[GraphicSpec] | None = None, spellings: list[dict] | None = None,
) -> tuple[list[Clip], list[PlacedGraphic], list[str]]:
    """Place, design (unless saved designs are given), render and attach. Returns (clips, placed, warnings).

    keep_out overrides the strips of a vertical frame that designs stay out of (VERTICAL_KEEP_OUT).
    With a `logo`, designs may show it as <img src="brand:logo">; the saved HTML carries it inline.
    `look` is approved designs (.html) from an earlier run for new ones to match.
    `house_style`: the design system described when this video's graphics were first designed (kept in graphics.json).
    `planned`: with saved designs, every graphic of the plan at its index (default: `specs`). The ones this cut
    doesn't place stay in graphics.json, with their designs, for the next re-cut to place again.
    `spellings`: the plan's name fixes, for the words the designer is shown.
    """
    from .presets import design_style, inline_logo

    warnings: list[str] = []
    placed = place(specs, tl, analysis, warnings)
    folder = out_dir / "graphics"
    if not placed:
        waiting = keep_left_out(planned if planned is not None else specs, [], saved_html, out_dir) if saved_html is not None else []
        if waiting:
            write_manifest(out_dir, [], {}, {}, tl, style, house_style=house_style, left_out=waiting)
            _put_aside_old_designs(folder, [], {}, waiting)
        return [], [], warnings
    look_under(placed, tl, analysis)
    found = {"house_style": house_style}
    if saved_html is not None:
        progress.stage("graphics", f"Re-timing {len(placed)} graphics")
        html = {g.id: saved_html[g.index] for g in placed if g.index in saved_html}
        for g in placed:
            if g.id not in html:
                warnings.append(f"graphic {g.id} {g.spec.text!r} has no saved design; left out")
    else:
        progress.stage("graphics", f"Designing {len(placed)} graphics")
        html = design(
            provider, placed, analysis, tl.frame_duration, brief=brief, style=design_style(style, logo, look), width=tl.width, height=tl.height,
            warnings=warnings, keep_out=keep_out, house_style=house_style, found=found, spellings=spellings,
        )
        html = {k: inline_logo(v, logo) for k, v in html.items()}
    # Each design notes the timing it was made for (a new one: this cut's), and plays re-timed in a
    # cut whose timing differs, so its words stay on the words and its exit lands at the end.
    fd = tl.frame_duration
    fps = 1 / float(fd)
    note_new_designs(placed, html, fd)
    # Made way for a graphic that has no design: start on its words, and hold for the full length, after all.
    back = make_way_back(placed, set(html))
    for g in placed:
        if g.id in back:
            was = replace(g)
            _move_back(g, *back[g.id], fps)
            _said_back(g, was, placed, fps, warnings)
    retime(placed, html, fd, warnings)
    # Every design is on disk before rendering starts, and each render is recorded as it finishes:
    # if rendering fails or is stopped, `roughcut render` renders only what's missing, without
    # asking the model again.
    previous = _read_manifest(out_dir)
    save_designs(placed, html, folder)
    waiting = keep_left_out(planned if planned is not None else specs, placed, saved_html, out_dir) if saved_html is not None else []
    kept = unchanged(placed, html, folder, tl, previous)
    notes: dict[str, list[str]] = _old_notes(previous, placed, html, tl, kept)
    write_manifest(out_dir, placed, kept, html, tl, style, notes=notes, house_style=found["house_style"], left_out=waiting)
    files: dict[str, Path] = {}

    def record(done: dict[str, Path]) -> None:
        write_manifest(out_dir, placed, {**files, **done}, html, tl, style, notes=notes, house_style=found["house_style"], left_out=waiting)

    files = render(placed, html, folder, tl, workers=workers, warnings=warnings, previous=previous, notes=notes, keep_out=keep_out, on_rendered=record)
    # Made way for a graphic that didn't render: render it again on its words and at its full length (the old clip stays if that fails).
    back = make_way_back(placed, set(files))
    if back:
        redo = [g for g in placed if g.id in back]
        before = {g.id: (replace(g), files.pop(g.id), notes.get(g.id)) for g in redo}
        for g in redo:
            _move_back(g, *back[g.id], fps)
        retime(redo, html, fd, warnings)
        again = render(redo, html, folder, tl, workers=workers, warnings=warnings, previous=previous, notes=notes, keep_out=keep_out, on_rendered=record)
        for g in redo:
            was, clip, said = before[g.id]
            if g.id in again:
                _said_back(g, was, placed, fps, warnings)
                files[g.id] = again[g.id]
            else:  # keep the clip it has
                vars(g).update(vars(was))
                files[g.id], notes[g.id] = clip, said or []
    warnings += note_warnings(placed, notes)
    clips = attach(tl, placed, files, warnings)
    write_manifest(out_dir, placed, files, html, tl, style, notes=notes, house_style=found["house_style"], left_out=waiting)
    _put_aside_old_designs(folder, placed, html, waiting)
    log(f"{len(clips)} of {len(placed)} graphics on the timeline")
    try:
        from .graphics_preview import write_previews

        write_previews(placed, files, tl, analysis, out_dir, workers=workers, notes=notes, warnings=warnings, keep_out=keep_out)
    except Exception as e:  # noqa: BLE001 - a preview is a convenience; never lose the cut over it
        warnings.append(f"graphics preview page wasn't written: {_first_line(e)}")
    return clips, placed, warnings


def _old_notes(manifest: dict, placed: list[PlacedGraphic], html: dict[str, str], tl: Timeline, reused: dict[str, Path]) -> dict[str, list[str]]:
    """What the earlier render of each reused clip reported, matched by what was rendered (IDs can change with the cut)."""
    said = {
        str(g["rendered"]): [str(n) for n in g["notes"]]
        for g in manifest.get("graphics", [])
        if isinstance(g, dict) and g.get("rendered") and isinstance(g.get("notes"), list) and g["notes"]
    }
    return {g.id: said[d] for g in placed if g.id in reused and (d := graphic_digest(g, html[g.id], tl)) in said}

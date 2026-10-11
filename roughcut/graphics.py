"""Motion graphics: designed by the model as HTML animations, rendered to transparent video.

The planner picks the moments: which segment, the exact words a graphic
belongs to, and what it says. As with the cut itself, the model never
writes a timestamp; the transcript gives the frames. A second request
designs every graphic as a self-contained HTML/CSS animation in one
consistent style. A headless browser then renders each one frame by frame
(every animation is paused and seeked to the frame's time) and ffmpeg
encodes ProRes 4444 with alpha, which Final Cut layers above the footage.
Every browser step has a deadline: a design that freezes is left out with
a warning instead of hanging the run. The designs and graphics.json are
saved before rendering starts, so `roughcut render` can finish a run that
stopped there without asking the model again.

The HTML is saved next to each rendered clip. Edit it and run
`roughcut render` to re-render only what changed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

from . import progress
from .analyze import Analysis, Clip, log
from .plan import DEFAULT_SECONDS, GraphicSpec
from .timeline import ConnectedClip, Timeline

LEAD = 0.12  # a graphic starts animating in just before its words
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
# Sets the page to time t (seconds); called for every frame, in order, from 0. Animations that exist at
# the start play on the graphic's clock; ones that window.seek starts later (a class it adds, an element
# it inserts, element.animate()) play from the frame they start, as in a browser. A design that rebuilds
# its elements every frame would restart them every frame, so for those new animations keep the
# graphic's clock. Returns the error window.seek threw, or null.
_SEEK_JS = """t => {
  const rc = window.__roughcut || (window.__roughcut = {last: [], streak: 0, shared: false});
  for (const a of document.getAnimations()) {
    if (a.__t0 !== undefined) { a.pause(); a.currentTime = Math.max(0, t - a.__t0) * 1000; }
  }
  let error = null;
  if (typeof window.seek === 'function') {
    try { window.seek(t); } catch (e) { error = String((e && e.message) || e); }
  }
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

    @property
    def slug(self) -> str:
        return f"{self.id}-{self.spec.kind.replace('_', '-')}"


# ------------------------------------------------------------------ placement


def _tokens(text: str) -> list[str]:
    return [t for t in (re.sub(r"[^\w$%]", "", w.lower()) for w in text.split()) if t]


def _find_phrase(words, phrase: str) -> tuple[int, int]:
    """Indexes of the first and last segment word matching `phrase` (whole segment if not found)."""
    want = _tokens(phrase)
    have = [(_tokens(w.text) or [""])[0] for w in words]
    if want:
        n = len(want)
        for i in range(len(have) - n + 1):
            if have[i : i + n] == want:
                return i, i + n - 1
        # Partial: the longest run of the phrase's words found in order.
        best = None
        for i in range(len(have)):
            k = 0
            while i + k < len(have) and k < n and have[i + k] == want[k]:
                k += 1
            if k and (best is None or k > best[1] - best[0] + 1):
                best = (i, i + k - 1)
        if best:
            return best
    return 0, len(words) - 1


def _frame_at(tl: Timeline, clip_id: str, t: float) -> int | None:
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
        start = _first_kept(tl, clip_id, t0, max(t1, seg.end))
        if start is None:
            warnings.append(f"graphic {spec.text!r}: its words aren't in the cut; skipped")
            continue
        start = max(0, start - round(LEAD * fps))
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
        placed.append(PlacedGraphic(id=f"G{len(placed) + 1:02d}", spec=spec, index=index, start=start, frames=frames, word_times=times))
    return placed


# ------------------------------------------------------------------ design

DESIGN_SYSTEM = """\
You are a motion designer making broadcast-quality animated graphics for a video edit. Each graphic \
is an HTML document that a headless browser renders frame by frame, with a transparent background, \
and that is layered over the footage in Final Cut Pro.

Technical contract (follow exactly; anything else will not render):
- One complete, self-contained HTML document per graphic: inline <style> and <script> only. No \
external URLs, web fonts, images, libraries or network requests. Inline SVG is fine.
- The viewport is the canvas size given below, in CSS pixels. html and body: margin 0, \
background transparent, overflow hidden. Never paint a full-frame background except on a title card.
- Each graphic lasts exactly its given seconds. Animate in over the first 0.3-0.7 s, hold, and be \
completely gone (opacity 0 or off-frame) by the end.
- Animate only with CSS @keyframes/transitions (explicit durations and delays in seconds, \
animation-fill-mode both) or the Web Animations API. The renderer pauses every animation and \
seeks it to each frame's time, so never rely on setTimeout, setInterval, requestAnimationFrame, \
Date or performance.now. Animations that start later (a class or element your window.seek adds, or \
element.animate() called from it) play from the frame they start, as they would in a browser. \
For what CSS can't do (counting a number up, typing text out), define \
window.seek = function (t) { ... } that sets the DOM for time t in seconds; it is called for \
every frame, in order, starting at 0, and must be deterministic.
- Words come with the time they are spoken, in seconds from the graphic's start. Kinetic captions \
reveal each word at its time; other kinds may use the times to land emphasis.
- Keep everything inside the title-safe area (5% in from every edge). Lower thirds and stats sit \
in the lower third or a corner, away from the center of frame where faces usually are. Only title \
cards may fill the frame.
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


def _design_request(
    placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int,
    house_style: str = "", reference: tuple[str, str, str] | None = None,
) -> str:
    cw, ch, _ = canvas(width, height)
    items = []
    for g in placed:
        seg = analysis.segment(g.spec.segment)
        items.append(
            {
                "id": g.id,
                "kind": g.spec.kind,
                "seconds": round(float(g.frames * fd), 3),
                "text": g.spec.text,
                "subtext": g.spec.subtext,
                "direction": g.spec.direction,
                "said": seg.text if seg is not None else "",
                "words": [{"word": w, "at": t} for w, t in g.word_times],
            }
        )
    parts = [
        f"<canvas>{cw}x{ch} CSS pixels, rendered at {width}x{height}, {1 / float(fd):.3f} fps</canvas>",
        "<brief>\n" + (brief.strip() or "No brief given.") + "\n</brief>",
        "<style_direction>\n" + (style.strip() or "None given: use the default look.") + "\n</style_direction>",
    ]
    if house_style:
        parts.append("<design_system>\nAlready established for this video; match it exactly:\n" + house_style + "\n</design_system>")
    if reference:
        rid, kind, page = reference
        parts.append(
            f'<reference_design id="{rid}" kind="{kind}">\nA finished graphic from this video. Reuse its type, colors, plates, '
            f"easing and timing so the set looks like one system.\n{page[:REFERENCE_CHARS]}\n</reference_design>"
        )
    parts.append("<graphics>\n" + json.dumps(items, indent=1) + "\n</graphics>")
    parts.append(f"Design these {len(items)} graphics: {', '.join(g.id for g in placed)}.")
    return "\n\n".join(parts)


def design(
    provider, placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int,
    warnings: list[str], batch: int = 4, workers: int = 3,
) -> dict[str, str]:
    """HTML for each graphic, by ID.

    The first batch is designed on its own and sets the look; the other batches are then
    designed in parallel, each given that house style and one finished design to match.
    """
    from .providers import ProviderAuthError, ProviderError, TextPart

    html: dict[str, str] = {}
    if not placed:
        return html
    groups = [placed[n : n + batch] for n in range(0, len(placed), batch)]
    t_all = time.monotonic()
    tried = 0
    log(f"designing {len(placed)} graphics in {len(groups)} request(s)")
    progress.stage("graphics", f"Designing {len(placed)} graphics", current=0, total=len(placed))

    def ask(group: list[PlacedGraphic], house: str, reference: tuple[str, str, str] | None):
        request = _design_request(group, analysis, fd, brief=brief, style=style, width=width, height=height, house_style=house, reference=reference)
        t0 = time.monotonic()
        try:
            raw = provider.complete_json(DESIGN_SYSTEM, [TextPart(request)], DESIGN_SCHEMA, schema_name="graphics", purpose="graphics design", max_tokens=64000)
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
                got[gid] = str(entry["html"])[:300_000]
        html.update(got)
        missing = [g.id for g in group if g.id not in got]
        if missing:
            warnings.append(f"no design came back for {', '.join(missing)}; skipped")
        log(f"designed {_ids(group)} in {_took(seconds)}" + (f" ({', '.join(missing)} missing)" if missing else ""))
        return got

    # The look is set by the first batch that comes back; then the rest go out together.
    house, reference = "", None
    pending = list(groups)
    while pending:
        group = pending.pop(0)
        answer = ask(group, "", None)
        got = take(*answer)
        if got:
            house = str(answer[1].get("style") or "")
            first = next((g for g in group if g.id in got and g.spec.kind != "title_card"), None) or next(g for g in group if g.id in got)
            reference = (first.id, first.spec.kind, got[first.id])
            break
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            for future in as_completed([pool.submit(ask, group, house, reference) for group in pending]):
                take(*future.result())
    log(f"designed {len(html)} of {len(placed)} graphics in {_took(time.monotonic() - t_all)}")
    return html


def _ids(group: list[PlacedGraphic]) -> str:
    return group[0].id if len(group) == 1 else f"{group[0].id}-{group[-1].id}"


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
                    await _bounded(page.set_content(job["html"], wait_until="load"), STEP_TIMEOUT + 5, "never finished loading")
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
                        error = await _evaluate(cdp, f"({_SEEK_JS})({frame / fps!r})", frame_timeout, f"froze at frame {frame} (window.seek never returned)")
                        if error and seek_error is None:
                            seek_error = f"its window.seek failed at {frame / fps:.2f}s ({' '.join(str(error).split())[:160]}); rendered anyway, check it"
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
            for note in ([seek_error] if seek_error else []) + fonts + inspect(out, job.get("kind", ""), size):
                _note(notes, note)
        except _Stuck as e:
            result["error"] = f"{e}; left out. Fix graphics/{out.with_suffix('.html').name} and run `roughcut render`"
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


def _digest(html: str, frames: int, rate: str, width: int, height: int) -> str:
    """Identifies a render: the design, its length and size, and how it's rendered and encoded."""
    how = " ".join(ENCODER) + "|" + _SEEK_JS
    return hashlib.sha256(f"{frames}|{rate}|{width}x{height}|{how}|{html}".encode()).hexdigest()[:16]


def _rendered_digest(entry: dict | None) -> str | None:
    """Digest of the design a manifest entry's .mov was rendered from (None if it wasn't)."""
    if not entry:
        return None
    if "rendered" in entry:
        return entry["rendered"]
    return entry.get("digest") if entry.get("file") else None  # manifests written only after rendering


def unchanged(placed: list[PlacedGraphic], html: dict[str, str], folder: Path, tl: Timeline, previous: dict) -> dict[str, Path]:
    """Rendered clips that still match their design and length, by graphic ID."""
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    old = {g.get("id"): g for g in previous.get("graphics", []) if isinstance(g, dict)}
    found = {}
    for g in placed:
        out = folder / f"{g.slug}.mov"
        if g.id in html and out.exists() and _rendered_digest(old.get(g.id)) == _digest(html[g.id], g.frames, rate, tl.width, tl.height):
            found[g.id] = out
    return found


def save_designs(placed: list[PlacedGraphic], html: dict[str, str], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for g in placed:
        if g.id in html:
            (folder / f"{g.slug}.html").write_text(html[g.id], encoding="utf-8")


def render(
    placed: list[PlacedGraphic], html: dict[str, str], folder: Path, tl: Timeline, *, workers: int, warnings: list[str],
    previous: dict | None = None, notes: dict[str, list[str]] | None = None,
) -> dict[str, Path]:
    """Render what's new or changed; reuse clips whose design and length are the same.

    `previous` is the manifest from before this run (read from disk when not given);
    `notes` collects what each graphic's render reported, for the manifest.
    """
    save_designs(placed, html, folder)
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    if previous is None:
        previous = _read_manifest(folder.parent)
    old = {g.get("id"): g for g in previous.get("graphics", []) if isinstance(g, dict)}
    notes = {} if notes is None else notes
    done = unchanged(placed, html, folder, tl, previous)
    for gid in done:
        notes[gid] = [str(n) for n in old.get(gid, {}).get("notes") or []]
    jobs = [
        {"id": g.id, "slug": g.slug, "kind": g.spec.kind, "html": html[g.id], "frames": g.frames, "fps": 1 / float(fd), "rate": rate,
         "canvas": canvas(tl.width, tl.height), "size": (tl.width, tl.height), "out": str(folder / f"{g.slug}.mov")}
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
                notes[job["id"]] = result["notes"]
                if result["error"]:
                    warnings.append(f"graphic {job['id']} {result['error']}")
                    log(f"{job['slug']}: {result['error']}")
                else:
                    done[job["id"]] = Path(job["out"])
                    log(f"rendered {job['slug']} ({job['frames']} frames) in {_took(result['seconds'])} [{n}/{len(jobs)}]")
        except KeyboardInterrupt:
            _STOP.set()  # the workers stop at their next frame and clean up
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
        log(f"rendered {sum(1 for j in jobs if j['id'] in done)} of {len(jobs)} graphic(s) in {_took(time.monotonic() - t0)}")
    warnings += note_warnings(placed, notes)
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


def visible_boxes(path: Path, size: tuple[int, int]) -> list[tuple[float, float, float, float] | None] | None:
    """Per frame, the box holding everything visible (x0, y0, x1, y1 as fractions of the frame), or None when blank.

    One ffmpeg pass reads the alpha channel, area-averaged down to QC_WIDTH so thin
    strokes still count. None when the file can't be read.
    """
    w = QC_WIDTH
    h = max(2, round(w * size[1] / size[0] / 2) * 2) if size[0] and size[1] else w * 9 // 16
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"alphaextract,scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    boxes: list[tuple[float, float, float, float] | None] = []
    size_ = w * h
    for n in range(len(proc.stdout) // size_):
        frame = proc.stdout[n * size_ : (n + 1) * size_]
        rows = [r for r in range(h) if max(frame[r * w : (r + 1) * w]) > QC_ALPHA]
        if not rows:
            boxes.append(None)
            continue
        x0 = next(c for c in range(w) if max(frame[c::w]) > QC_ALPHA)
        x1 = next(c for c in range(w - 1, -1, -1) if max(frame[c::w]) > QC_ALPHA)
        boxes.append((x0 / w, rows[0] / h, (x1 + 1) / w, (rows[-1] + 1) / h))
    return boxes


def inspect(path: Path, kind: str, size: tuple[int, int]) -> list[str]:
    """What a viewer would notice: blank, never animating off, or outside the title-safe area."""
    boxes = visible_boxes(path, size)
    if not boxes:
        return []
    if not any(boxes):
        return ["came out blank: nothing is visible in any frame"]
    notes = []
    if boxes[-1] is not None:
        notes.append("is still on screen in its last frame, so it cuts off instead of animating out")
    n = len(boxes)
    hold = [b for b in boxes[n // 3 : max(n // 3 + 1, 2 * n // 3)] if b]  # the middle third: not entering or leaving
    if kind != "title_card" and hold:  # title cards may fill the frame
        m = TITLE_SAFE - QC_GRACE
        outside = [b for b in hold if b[0] < m or b[1] < m or b[2] > 1 - m or b[3] > 1 - m]
        if len(outside) * 2 > len(hold):
            notes.append("reaches outside the title-safe area (5% in from the edges)")
    return notes


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
    notes: dict[str, list[str]] | None = None,
) -> None:
    """graphics.json: every placed graphic, its design, and the clip rendered from it (if any)."""
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    entries = []
    for g in placed:
        entry = {"id": g.id, "index": g.index, **asdict(g.spec), "start_frame": g.start, "frames": g.frames}
        if g.id in html:
            entry["html"] = f"graphics/{g.slug}.html"
            entry["digest"] = _digest(html[g.id], g.frames, rate, tl.width, tl.height)
        if g.id in files:
            entry["file"] = f"graphics/{files[g.id].name}"
            entry["rendered"] = entry.get("digest")  # the design that file was rendered from
        if notes and notes.get(g.id):
            entry["notes"] = notes[g.id]
        entries.append(entry)
    tmp = out_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps({"style": style, "graphics": entries}, indent=1))
    tmp.replace(out_dir / MANIFEST)


def saved_designs(out_dir: Path) -> tuple[list[GraphicSpec], dict[int, str], str]:
    """Specs and (possibly hand-edited) HTML from an earlier run, for `roughcut render`.

    HTML is keyed by the graphic's position in the plan, which doesn't move when a re-cut drops one.
    """
    data = _read_manifest(out_dir)
    specs: list[GraphicSpec] = []
    html: dict[int, str] = {}
    for g in data.get("graphics", []):
        index = int(g.get("index", len(specs)))
        while len(specs) < index:  # keep positions aligned if entries were dropped
            specs.append(GraphicSpec(kind="quote", segment="", words="", text=""))
        try:
            seconds = float(g.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        specs.append(
            GraphicSpec(
                kind=str(g.get("kind", "quote")), segment=str(g.get("segment", "")), words=str(g.get("words", "")),
                text=str(g.get("text", "")), subtext=str(g.get("subtext", "")), seconds=seconds, direction=str(g.get("direction", "")),
            )
        )
        page = out_dir / str(g.get("html") or "")
        if g.get("html") and page.is_file():
            html[index] = page.read_text(encoding="utf-8")
    return specs, html, str(data.get("style", ""))


def make(provider, specs: list[GraphicSpec], tl: Timeline, analysis: Analysis, out_dir: Path, *, brief: str, style: str, workers: int, saved_html: dict[int, str] | None = None) -> tuple[list[Clip], list[PlacedGraphic], list[str]]:
    """Place, design (unless saved designs are given), render and attach. Returns (clips, placed, warnings)."""
    warnings: list[str] = []
    placed = place(specs, tl, analysis, warnings)
    if not placed:
        return [], [], warnings
    folder = out_dir / "graphics"
    if saved_html is not None:
        progress.stage("graphics", f"Re-timing {len(placed)} graphics")
        html = {g.id: saved_html[g.index] for g in placed if g.index in saved_html}
    else:
        progress.stage("graphics", f"Designing {len(placed)} graphics")
        html = design(provider, placed, analysis, tl.frame_duration, brief=brief, style=style, width=tl.width, height=tl.height, warnings=warnings)
    # Every design is on disk before rendering starts: if rendering fails or is stopped,
    # `roughcut render` re-renders them without asking the model again.
    previous = _read_manifest(out_dir)
    save_designs(placed, html, folder)
    kept = unchanged(placed, html, folder, tl, previous)
    write_manifest(out_dir, placed, kept, html, tl, style, notes={k: v for k, v in _old_notes(previous).items() if k in kept})
    notes: dict[str, list[str]] = {}
    files = render(placed, html, folder, tl, workers=workers, warnings=warnings, previous=previous, notes=notes)
    clips = attach(tl, placed, files, warnings)
    write_manifest(out_dir, placed, files, html, tl, style, notes=notes)
    log(f"{len(clips)} of {len(placed)} graphics on the timeline")
    return clips, placed, warnings


def _old_notes(manifest: dict) -> dict[str, list[str]]:
    return {g["id"]: list(g["notes"]) for g in manifest.get("graphics", []) if isinstance(g, dict) and g.get("id") and g.get("notes")}

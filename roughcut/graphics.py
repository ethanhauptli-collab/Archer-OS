"""Motion graphics: designed by the model as HTML animations, rendered to transparent video.

The planner picks the moments: which segment, the exact words a graphic
belongs to, and what it says. As with the cut itself, the model never
writes a timestamp; the transcript gives the frames. A second request
designs every graphic as a self-contained HTML/CSS animation in one
consistent style. A headless browser then renders each one frame by frame
(every animation is paused and seeked to the frame's time) and ffmpeg
encodes ProRes 4444 with alpha, which Final Cut layers above the footage.

The HTML is saved next to each rendered clip. Edit it and run
`roughcut render` to re-render only what changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

from . import progress
from .analyze import Analysis, Clip, log
from .plan import DEFAULT_SECONDS, GraphicSpec
from .timeline import ConnectedClip, Timeline

LEAD = 0.12  # a graphic starts animating in just before its words
MANIFEST = "graphics.json"
_SEEK_JS = "t => { document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; }); if (typeof window.seek === 'function') window.seek(t); }"


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
Date or performance.now. For what CSS can't do (counting a number up, typing text out), define \
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


def _design_request(placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int, house_style: str = "") -> str:
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
    parts.append("<graphics>\n" + json.dumps(items, indent=1) + "\n</graphics>")
    parts.append(f"Design these {len(items)} graphics: {', '.join(g.id for g in placed)}.")
    return "\n\n".join(parts)


def design(provider, placed: list[PlacedGraphic], analysis: Analysis, fd: Fraction, *, brief: str, style: str, width: int, height: int, warnings: list[str], batch: int = 8) -> dict[str, str]:
    from .providers import ProviderAuthError, ProviderError, TextPart

    html: dict[str, str] = {}
    house = ""
    for n in range(0, len(placed), batch):
        group = placed[n : n + batch]
        progress.stage("graphics", f"Designing {len(placed)} graphics", current=n, total=len(placed))
        request = _design_request(group, analysis, fd, brief=brief, style=style, width=width, height=height, house_style=house)
        try:
            raw = provider.complete_json(DESIGN_SYSTEM, [TextPart(request)], DESIGN_SCHEMA, schema_name="graphics", purpose="graphics design", max_tokens=64000)
        except ProviderAuthError:
            raise
        except ProviderError as e:
            warnings.append(f"graphics design failed for {', '.join(g.id for g in group)}: {e}")
            continue
        house = house or str(raw.get("style") or "")
        for entry in raw.get("graphics") or []:
            if isinstance(entry, dict) and "<" in str(entry.get("html", "")):
                html[str(entry.get("id", "")).upper()] = str(entry["html"])[:300_000]
        missing = [g.id for g in group if g.id not in html]
        if missing:
            warnings.append(f"no design came back for {', '.join(missing)}; skipped")
    return html


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


def launch_browser(p):
    """Playwright's own Chromium, then an installed Chrome/Edge, then known paths."""
    errors = []
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return p.chromium.launch(**kwargs)
        except Exception as e:  # noqa: BLE001 - try the next browser
            errors.append(str(e).strip().splitlines()[0][:160])
    for path in _chromium_paths():
        try:
            return p.chromium.launch(executable_path=path)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e).strip().splitlines()[0][:160])
    raise GraphicsError("No browser to render graphics with. Install Google Chrome, or run: playwright install chromium")


INSTALL_HELP = "Motion graphics need Playwright: pip install -e '.[graphics]' (and Google Chrome, or: playwright install chromium)."


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


def _render_one(job: dict) -> dict:
    """Render one graphic to ProRes 4444 with alpha. Runs in a worker thread with its own browser."""
    from playwright.sync_api import sync_playwright

    out = Path(job["out"])
    tmp = out.with_suffix(".partial.mov")
    console: list[str] = []
    cw, ch, scale = job["canvas"]
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "image2pipe", "-framerate", job["rate"], "-i", "-",
        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
        "-r", job["rate"], str(tmp),
    ]
    try:
        with sync_playwright() as p:
            browser = launch_browser(p)
            try:
                page = browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=scale)
                page.route("**/*", lambda route: route.abort())  # designs may not fetch anything
                page.on("pageerror", lambda e: console.append(str(e)[:200]))
                page.set_content(job["html"], wait_until="load")
                ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for i in range(job["frames"]):
                        page.evaluate(_SEEK_JS, i / job["fps"])
                        ff.stdin.write(page.screenshot(omit_background=True, type="png", caret="hide"))
                finally:
                    ff.stdin.close()
                    err = ff.stderr.read().decode(errors="replace")
                    ff.wait()
            finally:
                browser.close()
        if ff.returncode != 0 or not tmp.exists():
            return {"id": job["id"], "error": f"ffmpeg failed: {err.strip()[:300]}", "console": console}
        tmp.replace(out)
        return {"id": job["id"], "error": None, "console": console}
    except Exception as e:  # noqa: BLE001 - report, don't kill the run
        tmp.unlink(missing_ok=True)
        return {"id": job["id"], "error": str(e).strip().splitlines()[0][:300], "console": console}


def _digest(html: str, frames: int, rate: str, width: int, height: int) -> str:
    return hashlib.sha256(f"{frames}|{rate}|{width}x{height}|{html}".encode()).hexdigest()[:16]


def render(placed: list[PlacedGraphic], html: dict[str, str], folder: Path, tl: Timeline, *, workers: int, warnings: list[str]) -> dict[str, Path]:
    """Render what's new or changed; reuse clips whose design and length are the same."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    old = {g["id"]: g for g in _read_manifest(folder.parent).get("graphics", [])}
    jobs, done = [], {}
    for g in placed:
        if g.id not in html:
            continue
        digest = _digest(html[g.id], g.frames, rate, tl.width, tl.height)
        out = folder / f"{g.slug}.mov"
        (folder / f"{g.slug}.html").write_text(html[g.id], encoding="utf-8")
        if out.exists() and old.get(g.id, {}).get("digest") == digest:
            done[g.id] = out
            continue
        jobs.append(
            {"id": g.id, "html": html[g.id], "frames": g.frames, "fps": 1 / float(fd), "rate": rate,
             "canvas": canvas(tl.width, tl.height), "out": str(out), "digest": digest}
        )
    if jobs:
        log(f"rendering {len(jobs)} graphic(s) at {tl.width}x{tl.height}")
        # One Playwright instance and browser per thread (Playwright's rule); the browsers do the work.
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            for n, result in enumerate(pool.map(_render_one, jobs), 1):
                progress.stage("graphics", f"Rendering {len(jobs)} graphics", current=n, total=len(jobs))
                job = next(j for j in jobs if j["id"] == result["id"])
                if result["console"]:
                    warnings.append(f"graphic {result['id']}: script error in the design ({result['console'][0]})")
                if result["error"]:
                    warnings.append(f"graphic {result['id']} didn't render: {result['error']}")
                else:
                    done[result["id"]] = Path(job["out"])
    return done


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


def write_manifest(out_dir: Path, placed: list[PlacedGraphic], files: dict[str, Path], html: dict[str, str], tl: Timeline, style: str) -> None:
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
        entries.append(entry)
    (out_dir / MANIFEST).write_text(json.dumps({"style": style, "graphics": entries}, indent=1))


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
    progress.stage("graphics", f"Designing {len(placed)} graphics")
    if saved_html is not None:
        html = {g.id: saved_html[g.index] for g in placed if g.index in saved_html}
    else:
        html = design(provider, placed, analysis, tl.frame_duration, brief=brief, style=style, width=tl.width, height=tl.height, warnings=warnings)
    files = render(placed, html, out_dir / "graphics", tl, workers=workers, warnings=warnings)
    clips = attach(tl, placed, files, warnings)
    write_manifest(out_dir, placed, files, html, tl, style)
    log(f"{len(clips)} of {len(placed)} graphics on the timeline")
    return clips, placed, warnings

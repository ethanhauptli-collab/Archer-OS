"""graphics/preview.html: every rendered graphic, animating over the shot it sits on in the cut.

Each preview is the real ProRes render composited by ffmpeg over a still of the
picture under the graphic at its middle frame (the topmost B-roll, else the spine
clip, fitted to the sequence like Final Cut's default "Fit"). Small H.264 files
any browser plays, so the whole set can be checked before importing anything.
"""

from __future__ import annotations

import hashlib
import html
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .analyze import Analysis, Clip, log
from .ffmpeg_ops import extract_frame
from .timecode import seconds_to_clock
from .textio import run_text
from .timeline import Timeline

PAGE = "preview.html"
FOLDER = "previews"
LONG_EDGE = 960  # preview size: the sequence scaled so its long edge is this


def preview_size(width: int, height: int) -> tuple[int, int]:
    s = LONG_EDGE / max(width, height, 1)
    return max(2, round(width * s / 2) * 2), max(2, round(height * s / 2) * 2)


def write_previews(
    placed: list, files: dict[str, Path], tl: Timeline, analysis: Analysis, out_dir: Path, *,
    workers: int = 4, notes: dict[str, list[str]] | None = None, warnings: list[str] | None = None, keep_out: dict[str, float] | None = None,
) -> Path | None:
    """Write graphics/previews/<id>.mp4 + .jpg for each rendered graphic and the contact sheet. Returns the page.

    On vertical video the page can show the strips platform buttons cover (keep_out, else graphics.VERTICAL_KEEP_OUT).
    """
    from .graphics import describe_picture, picture_under, platform_zones

    folder = out_dir / "graphics"
    page = folder / PAGE
    rendered = [g for g in placed if g.id in files]
    if not rendered:
        page.unlink(missing_ok=True)  # don't leave a page showing graphics this cut no longer has
        return None
    previews = folder / FOLDER
    previews.mkdir(parents=True, exist_ok=True)
    fd = tl.frame_duration
    rate = f"{fd.denominator}/{fd.numerator}"
    size = preview_size(tl.width, tl.height)
    jobs = []
    for g in rendered:
        clip, at = picture_under(tl, analysis, g.start + g.frames // 2)
        if clip is not None and clip.media.kind == "image":
            at = 0.0
        mov = files[g.id]
        stat = mov.stat()
        key = f"{mov.name}|{stat.st_size}|{stat.st_mtime_ns}|{clip.media.path if clip else ''}|{at:.3f}|{size}|{rate}"
        jobs.append({"g": g, "clip": clip, "at": at, "mov": mov, "key": hashlib.sha1(key.encode()).hexdigest(), "over": describe_picture(clip)})
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        errors = list(pool.map(lambda job: _preview(job, previews, size, rate, float(fd)), jobs))
    for job, error in zip(jobs, errors):
        if error and warnings is not None:
            warnings.append(f"graphic {job['g'].id}: no preview ({error})")
    wanted = {f"{job['g'].id}{ext}" for job in jobs for ext in (".mp4", ".jpg", ".key")}
    for f in previews.iterdir():
        if f.is_file() and f.name not in wanted:
            f.unlink()
    unrendered = [g for g in placed if g.id not in files]
    zones = platform_zones(tl.width, tl.height) if keep_out is None else keep_out
    page.write_text(_page(tl, jobs, errors, unrendered, size, notes or {}, zones), encoding="utf-8")
    log(f"graphics preview: {page}")
    return page


def _preview(job: dict, previews: Path, size: tuple[int, int], rate: str, fd: float) -> str | None:
    """Composite one graphic over its background. Returns an error, or None."""
    g = job["g"]
    mp4, jpg, keyfile = (previews / f"{g.id}{ext}" for ext in (".mp4", ".jpg", ".key"))
    if mp4.is_file() and jpg.is_file() and keyfile.is_file() and keyfile.read_text() == job["key"]:
        return None
    pw, ph = size
    with tempfile.TemporaryDirectory(prefix="roughcut-preview-") as tmp:
        background = ["-f", "lavfi", "-i", f"color=c=black:s={pw}x{ph}:r={rate}"]
        clip: Clip | None = job["clip"]
        if clip is not None:
            still = extract_frame(Path(clip.media.path), job["at"], Path(tmp) / "under.jpg", width=2 * LONG_EDGE)
            if still is not None:
                background = ["-loop", "1", "-framerate", rate, "-i", str(still)]
        fit = f"scale={pw}:{ph}:force_original_aspect_ratio=decrease,pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        graph = f"[0:v]{fit}[bg];[1:v]scale={pw}:{ph}[fg];[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[v]"
        # Written next to the final files (a rename can't cross from a temp folder to an external drive).
        partial, poster = previews / f"{g.id}.partial.mp4", previews / f"{g.id}.partial.jpg"
        try:
            error = ""
            for codec in (["libx264", "-preset", "veryfast", "-crf", "20"], ["h264_videotoolbox", "-b:v", "4M"]):
                cmd = ["ffmpeg", "-v", "error", "-y", *background, "-i", str(job["mov"]), "-filter_complex", graph, "-map", "[v]", "-an",
                       "-c:v", *codec, "-r", rate, "-movflags", "+faststart", str(partial)]
                proc = run_text(cmd)
                if proc.returncode == 0 and partial.is_file():
                    break
                error = (proc.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:200]
            else:
                return error
            middle = (g.frames // 2) * fd
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{middle:.3f}", "-i", str(partial), "-frames:v", "1", "-q:v", "3", str(poster)], capture_output=True)
            if not poster.is_file():
                return "couldn't grab a poster frame"
            partial.replace(mp4)
            poster.replace(jpg)
        finally:
            partial.unlink(missing_ok=True)
            poster.unlink(missing_ok=True)
    keyfile.write_text(job["key"])
    return None


def _zone_boxes(zones: dict[str, float]) -> str:
    """The strips platform buttons cover, drawn over a preview (shown or hidden by the page's checkbox)."""
    places = {
        "top": "left:0;right:0;top:0;height:{:.2%}", "bottom": "left:0;right:0;bottom:0;height:{:.2%}",
        "left": "top:0;bottom:0;left:0;width:{:.2%}", "right": "top:0;bottom:0;right:0;width:{:.2%}",
    }
    return "".join(f'<div class="zone" title="{side}: covered by the app" style="{places[side].format(part)}"></div>' for side, part in zones.items() if side in places)


def _page(
    tl: Timeline, jobs: list[dict], errors: list[str | None], unrendered: list, size: tuple[int, int], notes: dict[str, list[str]],
    zones: dict[str, float] | None = None,
) -> str:
    e = html.escape
    fd = tl.frame_duration
    pw, ph = size
    overlay = _zone_boxes(zones or {})
    cards = []
    for job, error in zip(jobs, errors):
        g = job["g"]
        text = g.spec.text + (f" / {g.spec.subtext}" if g.spec.subtext else "")
        media = (
            f'<div class="missing" style="aspect-ratio:{pw}/{ph}">No preview: {e(error)}</div>' if error else
            # Controls only on hover: the bar would cover the bottom of the frame, where lower thirds sit.
            f'<video src="{FOLDER}/{g.id}.mp4" poster="{FOLDER}/{g.id}.jpg" width="{pw}" height="{ph}" autoplay muted loop playsinline'
            ' onmouseenter="this.controls=true" onmouseleave="this.controls=false"></video>' + overlay
        )
        flagged = "".join(f"<li>{e(n)}</li>" for n in notes.get(g.id, []))
        cards.append(
            f'<figure><div class="frame">{media}</div><figcaption><div class="head"><b>{g.id}</b> {e(g.spec.kind.replace("_", " "))}'
            f'<span class="tc">{seconds_to_clock(float(g.start * fd))}</span></div>'
            f'<div class="text">{e(text)}</div>'
            f'<div class="meta">Over {e(job["over"])}<br>Edit <code>{e(g.slug)}.html</code>, then run <code>roughcut render</code></div>'
            + (f'<ul class="notes">{flagged}</ul>' if flagged else "")
            + "</figcaption></figure>"
        )
    for g in unrendered:
        cards.append(
            f'<figure><div class="missing" style="aspect-ratio:{pw}/{ph}">Not rendered (see the report\'s warnings)</div>'
            f'<figcaption><div class="head"><b>{g.id}</b> {e(g.spec.kind.replace("_", " "))}'
            f'<span class="tc">{seconds_to_clock(float(g.start * fd))}</span></div><div class="text">{e(g.spec.text)}</div></figcaption></figure>'
        )
    column = 380 if pw >= ph else 240
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Graphics preview</title>
<style>
:root {{ color-scheme: light dark; --bg: #f5f5f3; --fg: #1d1d1f; --muted: #6e6e73; --card: #fff; --line: #e2e2df; --warn: #a15c00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161617; --fg: #f5f5f7; --muted: #a1a1a6; --card: #1f1f21; --line: #333336; --warn: #ffb340; }} }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }}
header {{ padding: 24px 24px 4px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
header p {{ color: var(--muted); margin: 0; max-width: 70ch; }}
main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, {column}px), 1fr)); gap: 20px; padding: 20px 24px 40px; }}
figure {{ margin: 0; background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }}
video {{ display: block; width: 100%; height: auto; background: #000; }}
.frame {{ position: relative; }}
.zone {{ position: absolute; background: rgba(255, 59, 48, 0.22); box-shadow: inset 0 0 0 1px rgba(255, 59, 48, 0.8); pointer-events: none; }}
body:has(#zones:not(:checked)) .zone {{ display: none; }}
label {{ display: inline-flex; gap: 6px; align-items: center; margin-top: 10px; color: var(--muted); }}
.missing {{ display: grid; place-items: center; background: #000; color: #bbb; padding: 12px; text-align: center; }}
figcaption {{ padding: 10px 12px 12px; }}
.head {{ display: flex; gap: 8px; align-items: baseline; }}
.tc {{ margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; }}
.text {{ margin: 2px 0 6px; }}
.meta {{ color: var(--muted); font-size: 13px; }}
.notes {{ color: var(--warn); font-size: 13px; margin: 8px 0 0; padding-left: 18px; }}
code {{ font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }}
</style></head>
<body>
<header><h1>{e(tl.name)}: graphics</h1>
<p>Each graphic plays over a still of the shot it sits on, taken at its middle. These are the rendered clips Final Cut will get.</p>
{'<label><input type="checkbox" id="zones" checked> Show where Reels, TikTok and Shorts put their buttons and captions</label>' if overlay else ''}</header>
<main>
{chr(10).join(cards)}
</main>
</body></html>
"""

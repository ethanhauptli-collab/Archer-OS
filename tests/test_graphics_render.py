"""Motion graphics robustness and quality: bounded renders, saved designs, logging, QC, placement, previews."""

from __future__ import annotations

import json
import re
import subprocess
import time

import pytest
from conftest import CANNED_PLAN, GRAPHIC_HTML, SPEECH, FakeProvider, ffmpeg, make_clip

from roughcut import graphics
from roughcut.analyze import Analysis
from roughcut.plan import GraphicSpec, normalize
from roughcut.timeline import STYLES, build_timeline

needs_renderer = pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")

TWO_GRAPHICS = [
    {"kind": "lower_third", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2, "direction": ""},
    {"kind": "stat", "segment": "V01.S005", "words": "asking about this trip", "text": "Weeks of asking", "subtext": "", "seconds": 2, "direction": ""},
]

FREEZES = GRAPHIC_HTML.replace("<script>window.seek=function(t){", "<script>window.seek=function(t){if(t>0.1){while(true){}}")
# Blocks the page's main thread from a timer, outside window.seek. The timer is set by the first seek,
# so the stall always starts once the page and its fonts are loaded and frames are being drawn,
# however slow the machine is.
STALLS_IN_A_TIMER = GRAPHIC_HTML.replace(
    "<script>window.seek=function(t){", "<script>window.seek=function(t){if(!window.__stall){window.__stall=1;setTimeout(function(){while(true){}},0)}"
)


class DesignsBy(FakeProvider):
    """A fake model that returns a chosen HTML document per graphic ID (the default design otherwise)."""

    def __init__(self, designs: dict[str, str] | None = None, plan: dict | None = None, delay: float = 0.0):
        super().__init__(plan)
        self.designs = designs or {}
        self.delay = delay
        self.spans: list[tuple[float, float]] = []

    def complete_json(self, system, parts, schema, *, schema_name, purpose, effort=None, max_tokens=64000):
        t0 = time.monotonic()
        reply = super().complete_json(system, parts, schema, schema_name=schema_name, purpose=purpose, effort=effort, max_tokens=max_tokens)
        if schema_name == "graphics":
            time.sleep(self.delay)
            for entry in reply["graphics"]:
                entry["html"] = self.designs.get(entry["id"], entry["html"])
            self.spans.append((t0, time.monotonic()))
        return reply


def small_cut(graphics_raw=TWO_GRAPHICS, fmt="640x360"):
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S003", "V01.S004", "V01.S005"], "broll": []}], "graphics": graphics_raw}, a)
    return a, plan, build_timeline(plan, a, style=STYLES["medium"], name="t", format_override=fmt)


def job_for(tmp_path, html, *, frames=12, gid="G01", **extra):
    return {"id": gid, "slug": f"{gid}-test", "html": html, "frames": frames, "fps": 30.0, "rate": "30/1",
            "canvas": graphics.canvas(320, 180), "out": str(tmp_path / f"{gid}-test.mov"), **extra}


# ------------------------------------------------------------------ N1: a broken graphic can't hang the run


@needs_renderer
def test_a_frozen_design_is_left_out_and_the_build_finishes(footage, tmp_path, monkeypatch):
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut import pipeline

    monkeypatch.setattr(graphics, "FRAME_TIMEOUT", 2.0)
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [
        {"kind": "kinetic_caption", "segment": "V01.S005", "words": "asking about this trip", "text": "asking about this trip", "subtext": "", "seconds": 0, "direction": ""},
        {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2, "direction": ""},
    ]
    provider = DesignsBy({"G02": FREEZES}, plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    t0 = time.monotonic()
    result = pipeline.build(pipeline.Options(inputs=[footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True))
    assert time.monotonic() - t0 < 120

    frozen = [w for w in result.warnings if w.startswith("graphic G02 ")]
    assert frozen and re.search(r"froze at frame [34] \(window.seek never returned\)", frozen[0]) and "G02-title-card.html" in frozen[0] and "roughcut render" in frozen[0]
    assert [c.clip_id for c in result.timeline.connected if c.kind == "graphic"] == ["G01"]
    assert not list((out / "graphics").glob("G02-*.mov")) and not list((out / "graphics").glob("*.partial.mov"))
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    # The design is still saved (with a note of the timing it was made for), so it can be fixed and re-rendered.
    saved = (out / "graphics" / "G02-title-card.html").read_text()
    assert graphics.without_timing(saved) == FREEZES and graphics.design_timing(saved, result.timeline.frame_duration)["seconds"] == pytest.approx(2.0, abs=0.04)


@needs_renderer
def test_a_page_stuck_outside_seek_cant_hang_the_renderer(tmp_path, monkeypatch):
    """A timer that never returns blocks the page's main thread; every browser call is bounded on our side."""
    # Short enough to keep the test quick, long enough that loading the page on a busy machine never hits it.
    monkeypatch.setattr(graphics, "STEP_TIMEOUT", 8.0)
    monkeypatch.setattr(graphics, "FRAME_TIMEOUT", 2.0)
    t0 = time.monotonic()
    result = graphics._render_one(job_for(tmp_path, STALLS_IN_A_TIMER, frames=60))
    assert time.monotonic() - t0 < 40
    assert "froze at frame" in result["error"] and "left out" in result["error"]
    assert not list(tmp_path.glob("*.mov"))


@needs_renderer
def test_a_whole_graphic_has_a_time_budget(tmp_path):
    slow = GRAPHIC_HTML.replace("<script>window.seek=function(t){", "<script>window.seek=function(t){var e=Date.now()+150;while(Date.now()<e){}")
    result = graphics._render_one(job_for(tmp_path, slow, frames=60, budget=1.0))
    assert "was still rendering after 1s" in result["error"]
    assert not list(tmp_path.glob("*.mov"))


@needs_renderer
def test_ctrl_c_stops_renders_at_the_next_frame(tmp_path):
    graphics._STOP.set()
    try:
        result = graphics._render_one(job_for(tmp_path, GRAPHIC_HTML))
    finally:
        graphics._STOP.clear()
    assert result["error"] == "stopped" and not list(tmp_path.glob("*.mov"))


# ------------------------------------------------------------------ N2 (graphics part): designs saved before rendering


def test_designs_are_saved_before_rendering_starts(tmp_path, monkeypatch):
    a, plan, tl = small_cut()
    out = tmp_path / "out"

    def fail(*args, **kwargs):
        raise KeyboardInterrupt  # the run stops while rendering

    monkeypatch.setattr(graphics, "render", fail)
    with pytest.raises(KeyboardInterrupt):
        graphics.make(FakeProvider(), plan.graphics, tl, a, out, brief="", style="orange", workers=2)

    manifest = json.loads((out / "graphics.json").read_text())
    assert [g["id"] for g in manifest["graphics"]] == ["G01", "G02"] and manifest["style"] == "orange"
    assert all("file" not in g and "rendered" not in g for g in manifest["graphics"])
    for g in manifest["graphics"]:
        assert (out / g["html"]).read_text().startswith("<!doctype html>")
    specs, html, style = graphics.saved_designs(out)
    assert [s.text for s in specs] == ["Heading Out", "Weeks of asking"] and sorted(html) == [0, 1] and style == "orange"


@needs_renderer
def test_roughcut_render_finishes_designs_a_stopped_run_left(tmp_path, monkeypatch):
    a, plan, tl = small_cut()
    out = tmp_path / "out"

    def fail(*args, **kwargs):
        raise RuntimeError("the renderer crashed")

    with monkeypatch.context() as m:
        m.setattr(graphics, "render", fail)
        with pytest.raises(RuntimeError):
            graphics.make(FakeProvider(), plan.graphics, tl, a, out, brief="", style="", workers=2)

    specs, html, style = graphics.saved_designs(out)
    _, _, tl2 = small_cut()
    clips, placed, warnings = graphics.make(None, specs, tl2, a, out, brief="", style=style, workers=2, saved_html=html)  # no model
    assert [c.id for c in clips] == ["G01", "G02"] and not [w for w in warnings if "didn't render" in w]
    manifest = json.loads((out / "graphics.json").read_text())
    assert all(g["rendered"] == g["digest"] and (out / g["file"]).is_file() for g in manifest["graphics"])


def test_unchanged_renders_are_reused_and_an_older_folder_re_renders_once(tmp_path, monkeypatch):
    import hashlib

    from roughcut import versions

    rendered: list = []
    fake_renderer(monkeypatch, rendered)
    a, plan, tl = small_cut()
    out = tmp_path / "out"
    graphics.make(FakeProvider(), plan.graphics, tl, a, out, brief="", style="", workers=2)
    assert sorted(gid for gid, _ in rendered) == ["G01", "G02"]

    def rerender():
        rendered.clear()
        versions.adopt_old_graphics(out)  # as `roughcut render` does before re-timing the graphics
        specs, html, style = graphics.saved_designs(out)
        graphics.make(None, specs, small_cut()[2], a, out, brief="", style=style, workers=2, saved_html=html)
        return sorted(gid for gid, _ in rendered)

    assert rerender() == []  # nothing changed

    # A folder from before this renderer: graphics.json with "digest" (an older formula) + "file" and no
    # "rendered", and clips named before the digest was in the name (G01) or with that older digest (G02).
    manifest = json.loads((out / "graphics.json").read_text())
    old_names = []
    for g in manifest["graphics"]:
        g.pop("rendered")
        page = (out / g["html"]).read_text()
        g["digest"] = hashlib.sha256(f"{g['frames']}|30000/1001|640x360|{page}".encode()).hexdigest()[:16]
        slug = g["html"].removeprefix("graphics/").removesuffix(".html")
        old = f"graphics/{slug}.mov" if g["id"] == "G01" else f"graphics/{slug}-{g['digest'][:6]}.mov"
        (out / g["file"]).rename(out / old)
        g["file"] = old
        old_names.append(old)
    (out / "graphics.json").write_text(json.dumps(manifest))
    assert rerender() == ["G01", "G02"]  # rendered with the new encoder and seek once...
    assert rerender() == []  # ...then reused
    assert all((out / name).is_file() for name in old_names)  # the old clips stay for cuts already in Final Cut
    # A clip at another graphic's name is never taken for this one's, even made from the same design.
    manifest = json.loads((out / "graphics.json").read_text())
    g01 = manifest["graphics"][0]
    other = f"graphics/G01-quote-{g01['digest'][:6]}.mov"
    (out / g01["file"]).rename(out / other)
    g01["file"] = other
    (out / "graphics.json").write_text(json.dumps(manifest))
    assert rerender() == ["G01"]


# ------------------------------------------------------------------ T5 (graphics part): say what a long run is doing


@needs_renderer
def test_design_and_render_say_what_they_are_doing(tmp_path, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(graphics, "log", lines.append)
    monkeypatch.setattr(graphics, "PROGRESS_EVERY", 0.0)
    a, plan, tl = small_cut()
    graphics.make(FakeProvider(), plan.graphics, tl, a, tmp_path / "out", brief="", style="", workers=2)
    text = "\n".join(lines)
    assert re.search(r"designed G01-G02 in \d+s", text)
    assert re.search(r"designed 2 of 2 graphics in \d+s", text)
    assert re.search(r"rendered G01-lower-third \(\d+ frames\) in \d+s", text) and re.search(r"rendered G02-stat \(\d+ frames\)", text)
    assert re.search(r"G0\d-\w[\w-]*: frame \d+ of \d+", text)  # the periodic line on a long render
    assert "2 of 2 graphics on the timeline" in text


def test_a_failed_design_batch_is_logged(tmp_path, monkeypatch):
    from roughcut.providers import ProviderError

    class Fails(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            raise ProviderError("overloaded")

    lines: list[str] = []
    monkeypatch.setattr(graphics, "log", lines.append)
    a, plan, tl = small_cut()
    warnings: list[str] = []
    placed = graphics.place(plan.graphics, tl, a, warnings)
    assert graphics.design(Fails(), placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings) == {}
    assert any("designing G01-G02 failed" in ln and "overloaded" in ln for ln in lines)
    assert any("graphics design failed for G01, G02" in w for w in warnings)


def _probe(path):
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,pix_fmt,width,height", "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(probe.stdout)["streams"][0]


# ------------------------------------------------------------------ T12: render and design faster


@needs_renderer
def test_fast_frame_capture_matches_playwrights_screenshot():
    import asyncio

    from playwright.async_api import async_playwright

    def rgba(png: bytes) -> bytes:
        return subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgba", "-"], input=png, capture_output=True, check=True).stdout

    async def both(width, height):
        cw, ch, scale = graphics.canvas(width, height)
        async with async_playwright() as p:
            browser = await graphics._launch_browser_async(p)
            page = await browser.new_page(viewport={"width": cw, "height": ch}, device_scale_factor=scale)
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
            await page.set_content(GRAPHIC_HTML.replace("TEXT", "Match"), wait_until="load")
            await graphics.load_fonts(page)
            await graphics._evaluate(cdp, f"({graphics._SEEK_JS})(1.0)", 5, "seek")
            fast = await graphics._frame_png(page, cdp, (cw, ch, scale), True, "fast")
            slow = await graphics._frame_png(page, cdp, (cw, ch, scale), False, "slow")
            await browser.close()
            return fast, slow

    for size in ((640, 360), (360, 640)):
        fast, slow = asyncio.run(both(*size))
        assert graphics._png_size(fast) == size == graphics._png_size(slow)
        a, b = rgba(fast), rgba(slow)
        assert a == b and min(a[3::4]) == 0 and max(a[3::4]) == 255  # same pixels, transparent background, opaque plate


@needs_renderer
def test_renders_are_full_size_prores_4444_at_a_fixed_quality(tmp_path):
    result = graphics._render_one(job_for(tmp_path, GRAPHIC_HTML, size=(320, 180)))
    assert result["error"] is None and result["fast"] is True
    stream = _probe(tmp_path / "G01-test.mov")
    assert stream["codec_name"] == "prores" and stream["pix_fmt"].startswith("yuva") and (stream["width"], stream["height"]) == (320, 180)
    assert "-qscale:v" in graphics.ENCODER

    # If the quick capture ever comes out the wrong size, frames come from Playwright's screenshot instead.
    result = graphics._render_one(job_for(tmp_path, GRAPHIC_HTML, gid="G02", size=(999, 999)))
    assert result["error"] is None and result["fast"] is False
    stream = _probe(tmp_path / "G02-test.mov")
    assert (stream["width"], stream["height"]) == (320, 180)


def test_how_a_graphic_is_encoded_is_part_of_its_digest(monkeypatch):
    before = graphics._digest("<html>", 90, "30000/1001", 1920, 1080)
    monkeypatch.setattr(graphics, "ENCODER", [*graphics.ENCODER[:-2], "-vendor", "other"])
    assert graphics._digest("<html>", 90, "30000/1001", 1920, 1080) != before
    monkeypatch.undo()
    monkeypatch.setattr(graphics, "_SEEK_JS", graphics._SEEK_JS + " ")
    assert graphics._digest("<html>", 90, "30000/1001", 1920, 1080) != before


def test_first_design_batch_sets_the_look_and_the_rest_run_in_parallel():
    from roughcut.graphics import PlacedGraphic
    from roughcut.plan import GraphicSpec

    a, _, tl = small_cut([])
    kinds = ["title_card", "lower_third"] + ["stat"] * 8
    placed = [
        PlacedGraphic(id=f"G{n + 1:02d}", spec=GraphicSpec(kind=k, segment="V01.S003", words="", text=f"Graphic {n + 1}"), index=n, start=n * 40, frames=30)
        for n, k in enumerate(kinds)
    ]
    provider = DesignsBy(delay=0.4)
    warnings: list[str] = []
    html = graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings)
    assert sorted(html) == [g.id for g in placed] and not warnings

    requests = [c["parts"][-1].text for c in provider.calls if c["schema_name"] == "graphics"]
    assert len(requests) == 3
    assert "<design_system>" not in requests[0] and "<reference_design" not in requests[0]
    later = sorted(requests[1:], key=lambda r: "G05" not in r)
    assert "Design these 4 graphics: G05, G06, G07, G08." in later[0] and "Design these 2 graphics: G09, G10." in later[1]
    for r in later:
        # The house style from batch 1, and its first design that isn't a title card (G02) as the reference.
        assert "<design_system>\nAlready established for this video; match it exactly:\ntest plate" in r
        assert '<reference_design id="G02" kind="lower_third">' in r and "G02 <span" in r

    first, second, third = sorted(provider.spans)
    assert first[1] <= second[0] and first[1] <= third[0]  # batch 1 finished before the others started
    assert second[0] < third[1] and third[0] < second[1]  # the others overlapped


def test_ctrl_c_while_designing_stops_without_waiting_for_requests_in_flight():
    import _thread
    import threading

    from roughcut.graphics import PlacedGraphic
    from roughcut.plan import GraphicSpec

    a, _, tl = small_cut([])
    placed = [
        PlacedGraphic(id=f"G{n + 1:02d}", spec=GraphicSpec(kind="stat", segment="V01.S003", words="", text=f"Graphic {n + 1}"), index=n, start=n * 40, frames=30)
        for n in range(8)
    ]
    release = threading.Event()

    class SlowAfterTheFirstBatch(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            reply = super().complete_json(system, parts, schema, schema_name=schema_name, **kw)
            if "<design_system>" in parts[-1].text:  # batches 2+: a long, paid request
                release.wait(30)
            return reply

    timer = threading.Timer(1.0, _thread.interrupt_main)  # Ctrl-C a second in
    t0 = time.monotonic()
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            graphics.design(SlowAfterTheFirstBatch(), placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[])
        stopped = time.monotonic() - t0
    finally:
        timer.cancel()
        release.set()
    assert stopped < 3


def test_a_rejected_key_while_designing_later_batches_still_stops_the_run():
    from roughcut.graphics import PlacedGraphic
    from roughcut.plan import GraphicSpec
    from roughcut.providers import ProviderAuthError

    class KeyRevoked(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            if "<design_system>" in parts[-1].text:
                raise ProviderAuthError("invalid x-api-key")
            return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)

    a, _, tl = small_cut([])
    placed = [PlacedGraphic(id=f"G{n + 1:02d}", spec=GraphicSpec(kind="stat", segment="V01.S003", words="", text="x"), index=n, start=n * 40, frames=30) for n in range(6)]
    with pytest.raises(ProviderAuthError):
        graphics.design(KeyRevoked(), placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[])


# ------------------------------------------------------------------ T1: seek-started animations, script errors, fonts, QC

FADE = "@keyframes f{from{opacity:0}to{opacity:1}}"
SMIL_WIDTH = '<animate attributeName="width" from="0" to="100" dur="2s" fill="freeze"/>'  # 50 at 1 s
SEEK_CASES = {
    # A transition started by a class window.seek adds at 0.5 s is halfway through 0.5 s later.
    "class toggle": (
        "<style>#b{opacity:0;transition:opacity 1s linear}#b.on{opacity:1}</style><div id=b>Hi</div>"
        "<script>window.seek=t=>document.getElementById('b').classList.toggle('on',t>=0.5)</script>",
        1.0, "getComputedStyle(document.getElementById('b')).opacity", 0.5,
    ),
    "inserted element": (
        f"<style>{FADE}.w{{animation:f 1s linear both}}</style><div id=box></div>"
        "<script>window.seek=t=>{if(t>=0.5&&!document.querySelector('.w')){const s=document.createElement('span');"
        "s.className='w';s.textContent='word';document.getElementById('box').appendChild(s)}}</script>",
        1.0, "getComputedStyle(document.querySelector('.w')).opacity", 0.5,
    ),
    "element.animate": (
        "<div id=b>Hi</div><script>window.seek=t=>{if(t>=0.5&&!window.started){window.started=true;"
        "document.getElementById('b').animate([{opacity:0},{opacity:1}],{duration:1000,fill:'both'})}}</script>",
        1.0, "getComputedStyle(document.getElementById('b')).opacity", 0.5,
    ),
    # Plain keyframes present from the start behave exactly as before.
    "keyframes at load": (
        f"<style>{FADE}#b{{animation:f 2s linear both}}</style><div id=b>Hi</div>",
        1.0, "getComputedStyle(document.getElementById('b')).opacity", 0.5,
    ),
    # A design that rebuilds its words every frame must still show them (it would never progress otherwise).
    "rebuilt every frame": (
        f"<style>{FADE}.w{{animation:f .3s linear both}}</style><div id=box></div>"
        "<script>const words=[['one',0.2],['two',0.6]];window.seek=t=>{document.getElementById('box').innerHTML="
        "words.filter(w=>t>=w[1]).map(w=>'<span class=w>'+w[0]+'</span>').join(' ')}</script>",
        1.5, "Math.min(...[...document.querySelectorAll('.w')].map(e=>+getComputedStyle(e).opacity))", 1.0,
    ),
    "element.animate every frame": (
        "<div id=b>Hi</div><script>window.seek=t=>document.getElementById('b').animate([{opacity:0},{opacity:1}],{duration:300,fill:'both'})</script>",
        1.0, "getComputedStyle(document.getElementById('b')).opacity", 1.0,
    ),
    # SVG animations (SMIL) aren't Web Animations: they follow the frame clock too, not the wall clock.
    "svg animate at load": (
        f"<svg width=320 height=40><rect id=r height=10 width=0>{SMIL_WIDTH}</rect></svg>",
        1.0, "document.getElementById('r').width.animVal.value", 50.0,
    ),
    "svg inserted later": (
        "<div id=box></div><script>window.seek=t=>{if(t>=0.5&&!document.querySelector('svg')){document.getElementById('box').innerHTML="
        f"'<svg width=320 height=40><rect id=r height=10 width=0>{SMIL_WIDTH}</rect></svg>'}}}}</script>",
        1.5, "document.getElementById('r').width.animVal.value", 50.0,
    ),
    "svg rebuilt every frame": (
        "<div id=box></div><script>window.seek=t=>{document.getElementById('box').innerHTML="
        f"'<svg width=320 height=40><rect id=r height=10 width=0>{SMIL_WIDTH}</rect></svg>'}}</script>",
        1.0, "document.getElementById('r').width.animVal.value", 50.0,
    ),
}


@needs_renderer
@pytest.mark.parametrize("case", list(SEEK_CASES))
def test_animations_started_during_playback_play_from_when_they_start(case):
    from playwright.sync_api import sync_playwright

    html, until, probe, want = SEEK_CASES[case]
    with sync_playwright() as p:
        browser = graphics.launch_browser(p)
        page = browser.new_page(viewport={"width": 320, "height": 180})
        page.set_content(html, wait_until="load")
        for frame in range(round(until * 30) + 1):
            assert page.evaluate(f"({graphics._SEEK_JS})({frame / 30!r})") is None
        got = float(page.evaluate(probe))
        browser.close()
    assert got == pytest.approx(want, abs=0.05)


@needs_renderer
def test_svg_animations_render_on_the_frame_clock(tmp_path):
    """A bar drawn on with SVG <animate> is as long in each frame as that frame's time says,
    however slowly the frames are captured (SMIL would otherwise run on the wall clock)."""
    bar = (
        "<!doctype html><html><head><style>html,body{margin:0;background:transparent}</style></head><body>"
        '<svg width="1920" height="1080" style="position:absolute;left:0;top:0"><rect x="0" y="480" height="120" width="0" fill="#fff">'
        '<animate attributeName="width" from="0" to="1920" dur="2s" fill="freeze"/></rect></svg></body></html>'
    )  # the canvas is 1920x1080 CSS pixels, rendered at 320x180
    result = graphics._render_one(job_for(tmp_path, bar, frames=60, size=(320, 180)))
    assert result["error"] is None
    w, h, frames = graphics._alpha_frames(tmp_path / "G01-test.mov", (320, 180))
    row = 90 * w

    def width(n: int) -> int:
        return sum(1 for v in frames[n][row : row + w] if v > 128)

    for n in (0, 15, 30, 45):
        assert width(n) == pytest.approx(320 * (n / 30) / 2, abs=3), n  # 0, 80, 160, 240 px


def _plate(css="left:10%;top:40%", extra="", exits=True):
    out = "animation:out .2s linear .6s both;" if exits else ""
    return (f"<!doctype html><html><head><style>html,body{{margin:0;background:transparent}}@keyframes out{{to{{opacity:0}}}}"
            f".p{{position:absolute;{css};padding:8px;background:#c00;color:#fff;font:40px sans-serif;{out}}}</style></head>"
            f"<body><div class=p>Plate</div>{extra}</body></html>")


@needs_renderer
def test_script_errors_blocked_loads_and_missing_fonts_are_reported(tmp_path):
    html = _plate(extra=(
        "<div style=\"position:absolute;left:30%;top:30%;font:40px 'Gotham Test Font',sans-serif;color:#fff\">Brand</div>"
        "<img src=\"https://example.com/logo.png\" style=\"position:absolute;left:40%;top:40%\">"
        "<script>console.error('bad thing'); undefinedFunction();</script>"
        "<script>window.seek=function(t){if(t>0.2){null.x}}</script>"
    ))
    result = graphics._render_one(job_for(tmp_path, html, frames=24, kind="lower_third"))
    assert result["error"] is None  # rendered anyway
    notes = result["notes"]
    assert "console error: bad thing" in notes and "script error: undefinedFunction is not defined" in notes
    assert "tried to load https://example.com/logo.png (blocked: designs must be self-contained)" in notes
    assert any(n.startswith("its window.seek failed at 0.23s (Cannot read properties of null") and "rendered anyway" in n for n in notes)
    assert any("asks for the font Gotham Test Font, which isn't installed" in n for n in notes)
    assert not any("sans-serif" in n for n in notes)  # generic families are never "missing"


@needs_renderer
def test_renders_are_checked_for_blank_stuck_on_and_outside_the_safe_area(tmp_path):
    def notes(html, kind="lower_third", gid="G01"):
        result = graphics._render_one(job_for(tmp_path, html, frames=30, gid=gid, kind=kind, size=(320, 180)))
        assert result["error"] is None
        return result["notes"]

    assert notes(_plate()) == []  # in the safe area, gone by the end
    assert notes("<!doctype html><html><body style='margin:0;background:transparent'></body></html>", gid="G02") == [
        "came out blank: nothing is visible in any frame"
    ]
    assert notes(_plate(exits=False), gid="G03") == ["is still on screen in its last frame, so it cuts off instead of animating out"]
    # A fade timed to end exactly at the graphic's end leaves a trace on the last frame (drawn a frame early): fine.
    assert notes(_plate().replace("out .2s linear .6s", "out .3s linear .7s"), gid="G07") == []
    assert notes(_plate("left:0;top:40%"), gid="G04") == ["reaches outside the title-safe area (5% in from the edges)"]
    assert notes(_plate("left:0;top:40%"), kind="title_card", gid="G05") == []  # title cards may fill the frame
    # A plate that slides in from off-frame is judged on its hold, not its entrance.
    slide = _plate(extra="").replace(".p{", ".p{animation:in .3s ease-out both,out .2s linear .6s both;").replace(
        "@keyframes out", "@keyframes in{from{transform:translateX(-200%)}}@keyframes out")
    assert notes(slide, gid="G06") == []


def test_the_same_note_on_several_graphics_is_one_warning():
    from roughcut.graphics import PlacedGraphic
    from roughcut.plan import GraphicSpec

    placed = [PlacedGraphic(id=f"G0{n}", spec=GraphicSpec(kind="stat", segment="", words="", text=""), index=n, start=0, frames=30) for n in (1, 2, 3)]
    font = "asks for the font Gotham, which isn't installed on this computer, so it rendered in a fallback font"
    notes = {"G01": [font], "G02": [font, "came out blank: nothing is visible in any frame"], "G03": [f"console error: {n}" for n in range(5)]}
    assert graphics.note_warnings(placed, notes) == [
        f"graphics G01, G02: {font}",
        "graphic G02: came out blank: nothing is visible in any frame",
        "graphic G03: console error: 0", "graphic G03: console error: 1", "graphic G03: console error: 2",
        "graphic G03: 2 more messages from its scripts",
    ]


# ------------------------------------------------------------------ T11: one at a time, safe zones, the shot underneath


def spec(kind, seg, words, text, seconds):
    return {"kind": kind, "segment": seg, "words": words, "text": text, "subtext": "", "seconds": seconds, "direction": ""}


def placed_for(specs):
    a, plan, tl = small_cut(specs)
    warnings: list[str] = []
    return graphics.place(plan.graphics, tl, a, warnings), warnings, 1 / float(tl.frame_duration)


def test_an_overlapping_graphic_shortens_the_one_before_it():
    (lower, stat), warnings, fps = placed_for([spec("lower_third", "V01.S003", "Today", "Lower", 4), spec("stat", "V01.S004", "long day", "Stat", 2)])
    assert lower.start + lower.frames == stat.start - graphics.GAP_FRAMES and lower.frames >= graphics.MIN_SECONDS * fps
    assert warnings == [f"graphic 'Lower' shortened to {lower.frames / fps:.1f}s so it's gone before 'Stat'"]


def test_an_overlapping_graphic_is_left_out_when_the_one_before_cant_make_room():
    caption = spec("kinetic_caption", "V01.S003", "Today we're heading up the mountain", "Today we're heading up the mountain", 0)
    # Too little room left for the caption.
    placed, warnings, _ = placed_for([caption, spec("stat", "V01.S003", "heading", "Stat", 2)])
    assert [g.spec.text for g in placed] == [caption["text"]] and placed[0].frames == round(3.0 * 30000 / 1001)
    assert warnings == ['graphic \'Stat\' would cover "Today we\'re heading up the mountain"; left out (one graphic at a time)']
    # Room enough in seconds, but the caption would still be revealing its words.
    long_caption = spec("kinetic_caption", "V01.S005", "The kids have been asking about this trip for weeks", "The kids", 0)
    placed, warnings, fps = placed_for([long_caption, spec("stat", "V01.S005", "this trip", "Trip", 2)])
    (only,) = placed
    assert only.spec.kind == "kinetic_caption" and warnings == ["graphic 'Trip' would cover 'The kids'; left out (one graphic at a time)"]
    assert only.word_times[-1][1] > 2.5  # "weeks." comes well after the stat would have started


def test_names_read_back_to_back_each_get_their_graphic():
    # A rundown: one name ends a line, the next starts the line after. The second used to be left out.
    first = spec("lower_third", "V01.S003", "the mountain", "Mountain", 3)
    second = spec("lower_third", "V01.S004", "It's going to be a long day", "Long Day", 3)
    a, plan, tl = small_cut([first, second])
    (one, two), warnings, fps = placed_for([first, second])
    assert one.frames >= round(graphics.MIN_SECONDS * fps) and one.start + one.frames == two.start - graphics.GAP_FRAMES
    assert warnings == [f"graphic 'Mountain' shortened to {one.frames / fps:.1f}s so it's gone before 'Long Day'"]
    # It waited, but only while its words are still being said, and still opens on the clip its words are in.
    seg = a.segment("V01.S004")
    words_start = graphics._frame_at(tl, "V01", seg.words[0].start)
    words_end = graphics._frame_at(tl, "V01", seg.words[-1].end - 0.01)
    assert words_start < two.start <= min(words_end, words_start + round(graphics.LATE_SECONDS * fps))
    assert tl.spine_at(two.start) is tl.spine_at(words_start)
    assert two.frames >= round(graphics.MIN_SECONDS * fps) and two.start + two.frames == words_start - round(graphics.LEAD * fps) + round(3 * fps)  # it kept its end
    assert two.word_times and all(t >= 0 for _, t in two.word_times) and two.word_times[-1][0] == "day."
    # Kinetic captions reveal word by word, so they never wait: one over a caption is still left out.
    caption = spec("kinetic_caption", "V01.S004", "It's going to be a long day", "It's going to be a long day", 0)
    placed, warnings, _ = placed_for([first, caption])
    assert [g.spec.text for g in placed] == ["Mountain"] and "left out (one graphic at a time)" in warnings[0]


def test_graphics_are_numbered_in_the_order_they_appear():
    placed, warnings, _ = placed_for([spec("stat", "V01.S005", "asking", "Later", 2), spec("lower_third", "V01.S003", "Today", "Earlier", 2)])
    assert [(g.id, g.index, g.spec.text) for g in placed] == [("G01", 1, "Earlier"), ("G02", 0, "Later")] and not warnings


def test_vertical_designs_are_told_where_the_platform_buttons_are():
    a, plan, tl = small_cut(fmt="1080x1920")
    placed = graphics.place(plan.graphics, tl, a, [])
    vertical = graphics._design_request(placed, a, tl.frame_duration, brief="", style="", width=1080, height=1920)
    assert "1080x1920 CSS pixels, rendered at 1080x1920" in vertical
    for line in ("- top 269 px (y 0-269)", "- bottom 384 px (y 1536-1920)", "- right 130 px (x 950-1080)"):
        assert line in vertical
    assert "Reels, TikTok and Shorts" in vertical and "keep all text and plates out of them" in graphics.DESIGN_SYSTEM
    custom = graphics._design_request(placed, a, tl.frame_duration, brief="", style="", width=1080, height=1920, keep_out={"bottom": 0.25})
    assert "- bottom 480 px (y 1440-1920)" in custom and "- top" not in custom
    landscape = graphics._design_request(placed, a, tl.frame_duration, brief="", style="", width=1920, height=1080)
    assert "Reels" not in landscape and "- bottom" not in landscape
    assert graphics.platform_zones(1080, 1350) == {}  # 4:5 feed posts have no button strips


@needs_renderer
def test_a_graphic_under_the_platform_buttons_is_flagged(tmp_path):
    def notes(css, gid, kind="lower_third"):
        job = job_for(tmp_path, _plate(css), frames=30, gid=gid, kind=kind, size=(180, 320))
        job["canvas"] = graphics.canvas(180, 320)
        result = graphics._render_one(job)
        assert result["error"] is None
        return result["notes"]

    assert notes("left:8%;bottom:8%", "G01") == ["sits under the app's buttons and captions on a vertical video (bottom 20%)"]
    assert notes("left:8%;top:45%", "G02") == []
    # Title cards may fill the frame, but their words still shouldn't sit under the buttons.
    assert notes("left:8%;bottom:8%", "G03", kind="title_card") == ["sits under the app's buttons and captions on a vertical video (bottom 20%)"]
    assert notes("left:0;top:0;right:0;bottom:0", "G04", kind="title_card") == []  # a full-frame card: can't tell where its words are
    assert notes("left:8%;top:45%", "G05", kind="title_card") == []


def test_designers_are_told_what_each_graphic_sits_over(footage, tmp_path):
    from roughcut.analyze import Clip
    from roughcut.media import probe
    from roughcut.providers import ImagePart

    talk = make_clip("V01", seconds=20, sentences=SPEECH)
    broll = Clip(id="V02", media=probe(footage / "broll.mp4"), role="broll", visual={"description": "Test pattern bars", "shot": "wide"})
    a = Analysis(clips=[talk, broll])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S003", "V01.S004", "V01.S005"], "broll": [{"clip": "V02", "over": ["V01.S004"], "source_in": 1, "reason": "cover"}]}],
        "graphics": [spec("lower_third", "V01.S003", "Today", "On camera", 1.5), spec("stat", "V01.S004", "It's going", "Over B-roll", 1.5)],
    }
    plan = normalize(raw, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", format_override="640x360")
    placed = graphics.place(plan.graphics, tl, a, [])
    graphics.look_under(placed, tl, a)
    on_camera, over_broll = placed
    assert on_camera.over == "V01 V01.mov (A-Roll)" and on_camera.under[0] == "V01"
    assert over_broll.over == "V02 broll.mp4 (B-Roll): Test pattern bars; wide" and over_broll.under[0] == "V02"
    clip, at = graphics.picture_under(tl, a, over_broll.start + over_broll.frames // 2)
    assert clip is broll and 1.0 <= at < broll.duration

    provider = FakeProvider()
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[])
    parts = provider.calls[-1]["parts"]
    assert '"over": "V02 broll.mp4 (B-Roll): Test pattern bars; wide"' in parts[-1].text
    # V01's media path is made up, so only V02's frame could be grabbed.
    assert [p.text for p in parts[:-1] if hasattr(p, "text")] == ["G02 sits over V02 broll.mp4 (B-Roll): Test pattern bars; wide"]
    images = [p for p in parts if isinstance(p, ImagePart)]
    assert len(images) == 1 and images[0].data[:2] == b"\xff\xd8"

    provider.supports_vision = False  # a model that can't see gets the words only
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[])
    assert len(provider.calls[-1]["parts"]) == 1


# ------------------------------------------------------------------ T22: preview every graphic over its real footage


def _thumb(path, at=None) -> bytes:
    """A tiny RGB thumbnail (32x18) of an image or of a video frame, for comparing pictures."""
    seek = ["-ss", f"{at:.3f}"] if at else []
    cmd = ["ffmpeg", "-v", "error", *seek, "-i", str(path), "-frames:v", "1", "-vf", "scale=32:18:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def _differ(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _transparent_mov(path, frames, size="640x360"):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black@0.0:s={size}:r=30000/1001,format=rgba", "-frames:v", str(frames),
         *graphics.ENCODER, str(path)],
        check=True,
    )
    return path


def test_each_graphic_is_previewed_over_the_shot_it_sits_on(footage, tmp_path):
    from roughcut.analyze import Clip
    from roughcut.graphics_preview import write_previews
    from roughcut.media import probe

    talk = make_clip("V01", seconds=20, sentences=SPEECH)
    talk.media.path = str(footage / "talk.mov")  # the real pattern clip behind the made-up transcript
    broll = Clip(id="V02", media=probe(footage / "broll.mp4"), role="broll")
    a = Analysis(clips=[talk, broll])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S003", "V01.S004", "V01.S005"], "broll": [{"clip": "V02", "over": ["V01.S004"], "source_in": 1, "reason": "cover"}]}],
        "graphics": [spec("lower_third", "V01.S003", "Today", "On camera", 1.5), spec("stat", "V01.S004", "It's going", "Over B-roll", 1.5),
                     spec("quote", "V01.S005", "asking", "Never rendered", 2)],
    }
    plan = normalize(raw, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="Trip - Rough Cut", format_override="640x360")
    placed = graphics.place(plan.graphics, tl, a, [])
    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    files = {g.id: _transparent_mov(out / "graphics" / f"{g.slug}.mov", g.frames) for g in placed[:2]}  # G03 "didn't render"

    page = write_previews(placed, files, tl, a, out, notes={"G02": ["came out blank: nothing is visible in any frame"]})
    assert page == out / "graphics" / "preview.html"
    previews = out / "graphics" / "previews"
    assert sorted(f.name for f in previews.glob("*.mp4")) == ["G01.mp4", "G02.mp4"] and (previews / "G01.jpg").is_file()
    stream = _probe(previews / "G02.mp4")
    assert stream["codec_name"] == "h264" and (stream["width"], stream["height"]) == (960, 540)

    # The background is the picture under each graphic: the B-roll for G02, the talking clip for G01.
    poster = {g.id: _thumb(previews / f"{g.id}.jpg") for g in placed[:2]}
    under = {}
    for g in placed[:2]:
        clip, at = graphics.picture_under(tl, a, g.start + g.frames // 2)
        under[g.id] = _thumb(clip.media.path, at)
    assert graphics.picture_under(tl, a, placed[1].start + placed[1].frames // 2)[0] is broll
    assert _differ(poster["G02"], under["G02"]) < 12 and _differ(poster["G01"], under["G01"]) < 12
    assert _differ(poster["G02"], under["G01"]) > 30  # and the two shots really are different

    text = page.read_text()
    assert 'src="previews/G01.mp4"' in text and 'poster="previews/G02.jpg"' in text
    assert "Over V02 broll.mp4 (B-Roll)" in text and "Over V01 V01.mov (A-Roll)" in text
    assert "Never rendered" in text and "Not rendered" in text and "came out blank" in text
    assert 'class="zone"' not in text  # no platform strips on a landscape cut

    # Unchanged graphics keep their previews; a graphic that's gone loses its files.
    stamp = (previews / "G01.mp4").stat().st_mtime_ns
    write_previews(placed[:1], {"G01": files["G01"]}, tl, a, out)
    assert (previews / "G01.mp4").stat().st_mtime_ns == stamp and not (previews / "G02.mp4").exists()
    # Nothing rendered: no page left behind pointing at old clips.
    assert write_previews(placed, {}, tl, a, out) is None and not page.exists()


def test_previews_are_written_next_to_their_final_files(tmp_path, monkeypatch):
    """Output folders often live on an external drive, where a rename from a temp folder fails."""
    import errno
    from pathlib import Path

    from roughcut.graphics_preview import write_previews

    real_replace = Path.replace

    def same_folder_only(self, target):
        if Path(self).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", same_folder_only)
    a, plan, tl = small_cut()
    placed = graphics.place(plan.graphics, tl, a, [])[:1]
    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    files = {placed[0].id: _transparent_mov(out / "graphics" / "G01.mov", placed[0].frames)}
    warnings: list[str] = []
    assert write_previews(placed, files, tl, a, out, warnings=warnings) is not None and not warnings
    assert sorted(f.name for f in (out / "graphics" / "previews").iterdir()) == ["G01.jpg", "G01.key", "G01.mp4"]


def test_vertical_previews_can_show_the_platform_button_strips(tmp_path):
    from roughcut.graphics_preview import write_previews

    a, plan, tl = small_cut(fmt="1080x1920")
    placed = graphics.place(plan.graphics, tl, a, [])[:1]
    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    files = {placed[0].id: _transparent_mov(out / "graphics" / "G01.mov", placed[0].frames, "1080x1920")}
    text = write_previews(placed, files, tl, a, out).read_text()
    assert _probe(out / "graphics" / "previews" / "G01.mp4")["height"] == 960
    assert 'id="zones" checked' in text and "bottom:0;height:20.00%" in text and "right:0;width:12.00%" in text


@needs_renderer
def test_build_points_to_the_preview_page(footage, tmp_path, monkeypatch, capsys):
    from roughcut import pipeline
    from roughcut.cli import main

    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [spec("title_card", "V01.S003", "heading up the mountain", "Heading Out", 1.5)]
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(plan))
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics", "--format", "640x360", "--no-menu"]) == 0
    printed = capsys.readouterr().out
    assert f"Graphics:  {out / 'graphics' / 'preview.html'}" in printed
    assert "`graphics/preview.html`" in (out / "edit_report.md").read_text()
    assert (out / "graphics" / "previews" / "G01.mp4").is_file()


# ------------------------------------------------------------------ review fixes: saved designs, stopping, stale files


def fake_renderer(monkeypatch, rendered: list, *, slow: dict | None = None, jobs: list | None = None, fail: set | None = None):
    """Stand-in for the browser: records (id, html) and writes a blank ProRes clip of the right length.

    `slow` maps a graphic ID to a function run before that graphic "renders"; `jobs` collects
    every render job as given; graphics whose text is in `fail` freeze instead of rendering.
    """
    from pathlib import Path

    def render_one(job):
        if slow and job["id"] in slow:
            slow[job["id"]]()
        if jobs is not None:
            jobs.append(dict(job))
        if fail and any(text in job["html"] for text in fail):
            return {"id": job["id"], "error": "froze at frame 3 (window.seek never returned); left out", "notes": [], "seconds": 0.0}
        rendered.append((job["id"], job["html"]))
        w, h = job.get("size") or (640, 360)
        _transparent_mov(Path(job["out"]), job["frames"], f"{w}x{h}")
        return {"id": job["id"], "error": None, "notes": [], "seconds": 0.0}

    monkeypatch.setattr(graphics, "_render_one", render_one)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)


def _manifest(out) -> list[dict]:
    return json.loads((out / "graphics.json").read_text())["graphics"]


def _placed(out) -> dict[str, str]:
    return {g["id"]: g["text"] for g in _manifest(out)}


def _html_files(out) -> dict[str, str]:
    return {g["id"]: g["html"].removeprefix("graphics/") for g in _manifest(out)}


def test_roughcut_render_keeps_each_design_with_its_graphic_when_the_plan_lists_them_out_of_order(footage, tmp_path, monkeypatch):
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut import pipeline

    rendered: list[tuple[str, str]] = []
    fake_renderer(monkeypatch, rendered)
    plan = json.loads(json.dumps(CANNED_PLAN))
    # Listed in a different order from where they land on the timeline (the cold open plays S005 first).
    plan["graphics"] = [
        spec("quote", "V01.S006", "I needed it too", "C wrap", 2),
        spec("lower_third", "V01.S003", "heading up the mountain", "A heading", 2),
        spec("stat", "V01.S005", "asking about this trip", "B asking", 2),
    ]
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(plan))
    out = tmp_path / "out"
    opts = pipeline.Options(inputs=[footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True)
    pipeline.build(opts)
    texts = _placed(out)
    assert sorted(texts.values()) == ["A heading", "B asking", "C wrap"]
    assert [g["index"] for g in _manifest(out)] != [0, 1, 2]  # really out of order
    designs = {gid: (out / "graphics" / name).read_text() for gid, name in _html_files(out).items()}
    assert all(f"{gid} <span" in page for gid, page in designs.items())  # each designed for its own ID

    # Hand-edit one design, then re-render twice: every graphic stays, with its own (edited) design.
    b_id = next(gid for gid, text in texts.items() if text == "B asking")
    edited = designs[b_id].replace("<body>", "<body><!-- edited by hand -->")
    (out / "graphics" / _html_files(out)[b_id]).write_text(edited)
    for n in range(2):
        rendered.clear()
        result = pipeline.rerender(out, opts)
        assert sorted(c.clip_id for c in result.timeline.connected if c.kind == "graphic") == sorted(texts)
        assert not [w for w in result.warnings if w.startswith("graphic")]
        assert _placed(out) == texts  # same graphics, same IDs
        pages = {gid: (out / "graphics" / name).read_text() for gid, name in _html_files(out).items()}
        assert pages[b_id] == edited and all(pages[gid] == designs[gid] for gid in texts if gid != b_id)
        # Only the edited design re-renders, once, from its edited HTML.
        assert rendered == ([(b_id, edited)] if n == 0 else [])
        validate_dtd(result.fcpxml)
        check_invariants(result.fcpxml)


def test_a_stopped_render_keeps_the_graphics_that_finished(tmp_path, monkeypatch):
    out = tmp_path / "out"

    def stop_once_g01_is_saved():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(g["id"] == "G01" and g.get("rendered") for g in _manifest(out)):
                raise KeyboardInterrupt  # Ctrl-C while G02 is still rendering
            time.sleep(0.05)
        raise AssertionError("G01 was never recorded as rendered")

    rendered: list = []
    fake_renderer(monkeypatch, rendered, slow={"G02": stop_once_g01_is_saved})
    a, plan, tl = small_cut()
    with pytest.raises(KeyboardInterrupt):
        graphics.make(FakeProvider(), plan.graphics, tl, a, out, brief="", style="", workers=2)
    entries = {g["id"]: g for g in _manifest(out)}
    g01 = f"graphics/G01-lower-third-{entries['G01']['digest'][:6]}.mov"  # the digest is in a clip's name
    assert entries["G01"]["rendered"] == entries["G01"]["digest"] and entries["G01"]["file"] == g01 and (out / g01).is_file()
    assert "rendered" not in entries["G02"] and "file" not in entries["G02"]

    # `roughcut render` then renders only what's missing.
    rendered.clear()
    monkeypatch.setattr(graphics, "_render_one", graphics._render_one)
    fake_renderer(monkeypatch, rendered)
    specs, html, style = graphics.saved_designs(out)
    clips, _, _ = graphics.make(None, specs, small_cut()[2], a, out, brief="", style=style, workers=2, saved_html=html)
    assert [gid for gid, _ in rendered] == ["G02"] and [c.id for c in clips] == ["G01", "G02"]


def test_renumbered_graphics_keep_their_designs_and_old_names_are_put_aside(tmp_path, monkeypatch):
    rendered: list = []
    fake_renderer(monkeypatch, rendered)
    a, plan, tl = small_cut([spec("stat", "V01.S005", "asking", "Later", 2), spec("lower_third", "V01.S003", "Today", "Earlier", 2)])
    out = tmp_path / "out"
    folder = out / "graphics"
    folder.mkdir(parents=True)
    # A folder from before graphics were numbered in timeline order: G01 is the plan's first graphic.
    entries = []
    for n, g in enumerate(plan.graphics):
        slug = f"G{n + 1:02d}-{g.kind.replace('_', '-')}"
        (folder / f"{slug}.html").write_text(GRAPHIC_HTML.replace("TEXT", f"{g.text} (edited by hand)"))
        _transparent_mov(folder / f"{slug}.mov", 30)
        entries.append({"id": f"G{n + 1:02d}", "index": n, **g.__dict__, "html": f"graphics/{slug}.html", "file": f"graphics/{slug}.mov", "digest": "0" * 16})
    (out / "graphics.json").write_text(json.dumps({"style": "", "graphics": entries}))
    (folder / "preview.html").write_text("contact sheet")
    (folder / "old").mkdir()
    (folder / "old" / "G01-stat.html").write_text("put aside by an earlier run")

    specs, html, style = graphics.saved_designs(out)
    clips, placed, _ = graphics.make(None, specs, tl, a, out, brief="", style=style, workers=2, saved_html=html)
    assert [(g.slug, g.spec.text) for g in placed] == [("G01-lower-third", "Earlier"), ("G02-stat", "Later")]
    # Each graphic kept its own (hand-edited) design under its new name...
    assert "Earlier (edited by hand)" in (folder / "G01-lower-third.html").read_text()
    assert "Later (edited by hand)" in (folder / "G02-stat.html").read_text()
    # ...and the old names, which no graphic uses now, moved to graphics/old/ (nothing overwritten there).
    assert sorted(p.name for p in folder.glob("*.html")) == ["G01-lower-third.html", "G02-stat.html", "preview.html"]
    assert (folder / "old" / "G01-stat.html").read_text() == "put aside by an earlier run"
    assert "Later (edited by hand)" in (folder / "old" / "G01-stat (2).html").read_text()
    assert "Earlier (edited by hand)" in (folder / "old" / "G02-lower-third.html").read_text()
    # Rendered clips stay put: a project already imported into Final Cut may use them.
    assert (folder / "G01-stat.mov").is_file() and (folder / "G02-lower-third.mov").is_file()


def test_saved_designs_are_read_by_plan_position_in_any_order(tmp_path):
    out = tmp_path / "out"
    (out / "graphics").mkdir(parents=True)
    entries = []
    for gid, index, text in (("G01", 2, "two"), ("G02", 0, "zero"), ("G03", 3, "three")):  # index 1 was dropped by a re-cut
        (out / "graphics" / f"{gid}-quote.html").write_text(f"<p>{text}</p>")
        entries.append({"id": gid, "index": index, "kind": "quote", "segment": "V01.S003", "words": "", "text": text, "html": f"graphics/{gid}-quote.html"})
    (out / "graphics.json").write_text(json.dumps({"style": "s", "graphics": entries}))
    specs, html, style = graphics.saved_designs(out)
    assert [s.text for s in specs] == ["zero", "", "two", "three"]
    assert html == {0: "<p>zero</p>", 2: "<p>two</p>", 3: "<p>three</p>"} and style == "s"
    # A re-cut also gets back the planned graphics that cut left out (index 1, and 4 after the last saved one),
    # so it can place them and --design-missing can design them; saved ones (maybe hand-edited) win.
    planned = [GraphicSpec(kind="quote", segment="V01.S003", words="", text=t) for t in ("plan zero", "one", "plan two", "plan three", "four")]
    assert [s.text for s in graphics.with_planned(specs, planned)] == ["zero", "one", "two", "three", "four"]
    # Never a second copy of a graphic the saved cut has (positions can shift when a revision avoided one).
    shifted = [GraphicSpec(kind="quote", segment="V01.S003", words="", text=t) for t in ("avoided", "zero", "one", "two")]
    assert [s.text for s in graphics.with_planned(specs, shifted)] == ["zero", "", "two", "three"]


def test_a_graphic_without_a_saved_design_is_reported_not_dropped_silently(tmp_path, monkeypatch):
    a, plan, tl = small_cut()
    fake_renderer(monkeypatch, [])
    clips, placed, warnings = graphics.make(None, plan.graphics, tl, a, tmp_path / "out", brief="", style="", workers=2, saved_html={0: GRAPHIC_HTML})
    assert [c.id for c in clips] == ["G01"]
    assert "graphic G02 'Weeks of asking' has no saved design; left out" in warnings


# ------------------------------------------------------------------ re-cuts keep a saved design on its words

# Whisper-style words: contiguous, with the word before each pause stretched over it. The silence map
# cuts the pauses, by more at a tighter pacing, so the caption's words move against its start.
PAUSED_WORDS = [
    ("We", 1.00, 1.30), ("started", 1.30, 1.80), ("five", 1.80, 2.10), ("years", 2.10, 2.50), ("ago", 2.50, 4.40),
    ("with", 4.40, 4.70), ("twelve", 4.70, 5.10), ("vendors", 5.10, 5.70), ("and", 5.70, 7.40), ("now", 7.40, 7.70),
    ("it", 7.70, 7.85), ("is", 7.85, 8.00), ("the", 8.00, 8.15), ("biggest", 8.15, 8.60), ("festival", 8.60, 9.20), ("around.", 9.20, 9.70),
    ("See", 10.60, 10.90), ("you", 10.90, 11.10), ("there.", 11.10, 11.60),
]
SOUND = [(0.95, 3.05), (4.25, 6.05), (7.25, 9.8), (10.55, 11.65)]  # tone where the words are; true silence in the pauses
CAPTION = "five years ago with twelve vendors and now it is the biggest"


@pytest.fixture(scope="module")
def paused_footage(tmp_path_factory):
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("paused")
    gates = "+".join(f"between(t,{s},{e})" for s, e in SOUND)
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30000/1001:duration=12",
        "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*300*t)*({gates})':s=48000:d=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "talk.mov"),
    )
    (root / "talk.json").write_text(json.dumps({"words": [{"start": s, "end": e, "text": t, "prob": 0.95} for t, s, e in PAUSED_WORDS]}))
    return root


def _caption_plan(*extra):
    caption = spec("kinetic_caption", "V01.S001", CAPTION, CAPTION, 0)
    return {"title": "Fest", "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}], "graphics": [*extra, caption]}


def _placed_now(out, result):
    """The graphics as placed on a result's cut (its timeline, the saved analysis and plan graphics), by text."""
    analysis = Analysis.load(out / "analysis.json")
    specs, _, _ = graphics.saved_designs(out)
    return {g.spec.text: g for g in graphics.place(specs, result.timeline, analysis, [])}


def _check_on_its_words(job, now, made, fd):
    """Every word the design was made for lands on that word in this cut, and its exit plays at full speed at the end."""
    assert job["retime"], "a design made for other timing must play re-timed"
    made_words = dict(made["words"])
    for word, t in now.word_times:
        assert graphics.design_time(t, job["retime"]) == pytest.approx(made_words[word], abs=0.002), word
    end = float(now.frames * fd)
    assert graphics.design_time(end, job["retime"]) == pytest.approx(made["seconds"], abs=0.002)
    for back in (0.1, 0.4, 0.7):  # the exit
        assert graphics.design_time(end - back, job["retime"]) == pytest.approx(made["seconds"] - back, abs=0.002)


def test_timing_notes_round_trip_and_are_never_shown_to_the_designer():
    from fractions import Fraction

    fd = Fraction(1001, 30000)
    page = GRAPHIC_HTML
    timing = {"seconds": 4.2, "words": [["Over", 0.12], ["<b>-->", 0.5]]}
    noted = graphics.with_timing(page, timing)
    assert noted.startswith(page) and graphics.without_timing(noted) == page
    assert graphics.design_timing(noted, fd) == {"seconds": 4.2, "words": [("Over", 0.12), ("<b>-->", 0.5)]}
    assert noted.count("roughcut-timing") == 1 and "<b>" not in noted.split("roughcut-timing")[1]  # can't close the comment early
    renoted = graphics.with_timing(noted + "<!-- hand edit -->", {"seconds": 3.0, "words": []})
    assert renoted.count("roughcut-timing") == 1 and "<!-- hand edit -->" in renoted and graphics.design_timing(renoted, fd)["seconds"] == 3.0
    assert graphics.design_timing(graphics.with_timing(page, {"frames": 60}), fd) == {"seconds": float(60 * fd), "words": None}
    assert graphics.design_timing(page, fd) is None and graphics.design_timing(page + "<!-- roughcut-timing {nonsense} -->", fd) is None

    # Saved designs shown as examples (approved looks, a revision's reference) lose their notes.
    a, plan, tl = small_cut()
    placed = graphics.place(plan.graphics, tl, a, [])
    request = graphics._design_request(
        placed, a, tl.frame_duration, brief="", style="Match this:\n" + noted, width=640, height=360, reference=("G01", "stat", noted)
    )
    assert "roughcut-timing" not in request and request.count("TEXT <span") == 2


def test_time_map_puts_each_word_on_its_word_and_the_exit_at_the_end():
    from fractions import Fraction

    from roughcut.graphics import PlacedGraphic
    from roughcut.plan import GraphicSpec

    fd = Fraction(1, 30)
    g = PlacedGraphic(id="G01", spec=GraphicSpec(kind="kinetic_caption", segment="", words="", text="x"), index=0, start=0, frames=150,
                      word_times=[("Over", 0.1), ("forty", 0.4), ("chefs,", 0.8), ("are", 1.2), ("cooking.", 1.6)])
    same = graphics.design_timing(graphics.with_timing("<p>", g.timing(fd)), fd)
    assert graphics.time_map(g, same, fd) is None  # made for this cut: plays as it is
    made = {"seconds": 6.0, "words": [("Over", 0.1), ("forty", 0.4), ("chefs,", 0.8), ("are", 1.9), ("cooking.", 2.3)]}
    anchors = graphics.time_map(g, made, fd)
    for (word, t), (_, want) in zip(g.word_times, made["words"]):
        assert graphics.design_time(t, anchors) == pytest.approx(want), word
    assert graphics.design_time(5.0, anchors) == pytest.approx(6.0) and graphics.design_time(4.5, anchors) == pytest.approx(5.5)
    assert all(a1 > a0 and b1 > b0 for (a0, b0), (a1, b1) in zip(anchors, anchors[1:]))  # never runs backwards
    # A design with no word times (from before notes) is fitted by its length, its exit at full speed at the end.
    assert graphics.time_map(g, {"seconds": 4.0, "words": None}, fd) == [(0.0, 0.0), (4.2, 3.2), (5.0, 4.0)]


@needs_renderer
def test_a_retimed_design_is_rendered_on_its_new_clock(tmp_path):
    """What the design shows from 1.95 s (a plate appearing) shows from 1.0 s in a cut that re-times 1.0 s to 2.0 s."""
    page = (
        "<!doctype html><html><head><style>html,body{margin:0;background:transparent}@keyframes show{from{opacity:0}to{opacity:1}}"
        ".p{position:absolute;left:10%;top:40%;width:30%;height:20%;background:#c00;animation:show .001s linear 1.95s both}</style></head>"
        "<body><div class=p></div></body></html>"
    )
    retime = [(0.0, 0.0), (1.0, 2.0), (2.0, 3.0)]
    result = graphics._render_one(job_for(tmp_path, page, frames=60, size=(320, 180), retime=retime))
    assert result["error"] is None
    _, _, frames = graphics._alpha_frames(tmp_path / "G01-test.mov", (320, 180))
    shown = [n for n, f in enumerate(frames) if max(f) > 128]
    assert shown == list(range(30, 60))  # from 1.0 s (design time 2.0 s) on; without re-timing, only the last frame


def test_a_recut_with_other_pacing_keeps_a_saved_design_on_its_words(paused_footage, tmp_path, monkeypatch):
    from roughcut import pipeline

    rendered: list = []
    jobs: list[dict] = []
    fake_renderer(monkeypatch, rendered, jobs=jobs)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_caption_plan()))
    out = tmp_path / "out"
    opts = pipeline.Options(inputs=[paused_footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True, style="medium")
    built = pipeline.build(opts)
    fd = built.timeline.frame_duration
    (job,) = jobs
    medium = _placed_now(out, built)[CAPTION]
    assert job["retime"] is None and job["frames"] == medium.frames
    design = (out / "graphics" / "G01-kinetic-caption.html").read_text()
    made = graphics.design_timing(design, fd)  # the timing it was designed for: the medium cut's
    assert made == {"seconds": round(float(medium.frames * fd), 3), "words": medium.word_times}

    # `roughcut render --style tight`: the pauses shrink, so the words come sooner after the caption starts.
    jobs.clear()
    tight_cut = pipeline.rerender(out, pipeline.Options(inputs=[], style="tight"), overrides={"style"})
    (job,) = jobs
    tight = _placed_now(out, tight_cut)[CAPTION]
    assert tight.frames < medium.frames and dict(tight.word_times)["biggest"] < dict(medium.word_times)["biggest"] - 0.15
    _check_on_its_words(job, tight, made, fd)
    assert job["html"] == design  # the same design (and its note), played on another clock
    assert json.loads((out / "graphics.json").read_text())["graphics"][0]["retime"] == [list(a) for a in job["retime"]]
    assert not [w for w in tight_cut.warnings if "length only" in w]

    # Looser still, the other way; and back at medium the first clip is reused as it is.
    jobs.clear()
    loose_cut = pipeline.rerender(out, pipeline.Options(inputs=[], style="loose"), overrides={"style"})
    (job,) = jobs
    _check_on_its_words(job, _placed_now(out, loose_cut)[CAPTION], made, fd)
    jobs.clear()
    again = pipeline.rerender(out, pipeline.Options(inputs=[], style="medium"), overrides={"style"})
    assert jobs == [] and [c.clip_id for c in again.timeline.connected if c.kind == "graphic"] == ["G01"]
    assert (out / "graphics" / "G01-kinetic-caption.html").read_text() == design


def test_a_revision_keeps_carried_designs_on_their_words_and_notes_new_ones(paused_footage, tmp_path, monkeypatch):
    from roughcut import pipeline

    rendered: list = []
    jobs: list[dict] = []
    fake_renderer(monkeypatch, rendered, jobs=jobs)
    provider = FakeProvider(_caption_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    opts = pipeline.Options(inputs=[paused_footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True, style="medium")
    built = pipeline.build(opts)
    fd = built.timeline.frame_duration
    made = graphics.design_timing((out / "graphics" / "G01-kinetic-caption.html").read_text(), fd)

    # Tighter, and a title card on the last line: the caption's design is carried over, the title is new.
    provider.plan = _caption_plan(spec("title_card", "V01.S002", "See you there", "Fest", 1.5))
    jobs.clear()
    revised = pipeline.revise(out, "Tighter, with a title.", pipeline.Options(inputs=[], style="tight"), overrides={"style"})
    request = [c for c in provider.calls if c["schema_name"] == "graphics"][-1]["parts"][-1].text
    assert "Match the graphics already made for this video" in request and "roughcut-timing" not in request
    now = _placed_now(out, revised)
    by_id = {j["id"]: j for j in jobs}
    caption, title = now[CAPTION], now["Fest"]
    _check_on_its_words(by_id[caption.id], caption, made, fd)
    assert by_id[title.id]["retime"] is None  # designed for this cut
    title_design = (out / "graphics" / f"{title.slug}.html").read_text()
    assert graphics.design_timing(title_design, fd) == {"seconds": round(float(title.frames * fd), 3), "words": title.word_times}


def test_a_graphic_renumbered_by_a_revision_reuses_its_clip(paused_footage, tmp_path, monkeypatch):
    """Adding a graphic earlier in the cut renumbers the ones after it; their clips are reused, not rendered again."""
    import os

    from roughcut import pipeline, versions

    rendered: list = []
    fake_renderer(monkeypatch, rendered)
    lower = spec("lower_third", "V01.S002", "See you there", "See You There", 2)
    provider = FakeProvider({"title": "Fest", "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}], "graphics": [lower]})
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    pipeline.build(pipeline.Options(inputs=[paused_footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True))
    (first,) = (out / "graphics").glob("G01-lower-third-*.mov")

    provider.plan = json.loads(json.dumps(provider.plan))
    provider.plan["graphics"].insert(0, spec("stat", "V01.S001", "twelve vendors", "12 Vendors", 1.5))
    rendered.clear()
    revised = pipeline.revise(out, "Call out the twelve vendors.", pipeline.Options(inputs=[]))
    assert [gid for gid, _ in rendered] == ["G01"]  # only the new stat
    (again,) = (out / "graphics").glob("G02-lower-third-*.mov")
    assert again.name.removeprefix("G02") == first.name.removeprefix("G01") and os.path.samefile(again, first)
    assert again.name in revised.fcpxml.read_text()
    versions.prune_graphics(out)
    assert first.is_file() and again.is_file()  # v1 in Final Cut still uses its clip


def test_a_reused_clip_keeps_what_its_render_reported_whatever_its_number(tmp_path):
    a, plan, tl = small_cut()
    placed = graphics.place(plan.graphics, tl, a, [])
    html = {g.id: graphics.with_timing(GRAPHIC_HTML.replace("TEXT", g.spec.text), g.timing(tl.frame_duration)) for g in placed}
    digests = {g.id: graphics.graphic_digest(g, html[g.id], tl) for g in placed}
    # The earlier version numbered them the other way round.
    previous = {"graphics": [
        {"id": "G02", "rendered": digests["G01"], "notes": ["came out blank: nothing is visible in any frame"]},
        {"id": "G01", "rendered": "0" * 16, "notes": ["console error: from another design"]},
    ]}
    reused = {g.id: tmp_path / f"{g.slug}.mov" for g in placed}
    assert graphics._old_notes(previous, placed, html, tl, reused) == {"G01": ["came out blank: nothing is visible in any frame"]}


def test_a_graphic_shortened_for_one_that_never_arrives_gets_its_length_back(tmp_path, monkeypatch):
    class NoStatDesign(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            reply = super().complete_json(system, parts, schema, schema_name=schema_name, **kw)
            reply["graphics"] = [g for g in reply["graphics"] if g["id"] != "G02"]
            return reply

    graphics_raw = [spec("lower_third", "V01.S003", "Today", "Lower", 4), spec("stat", "V01.S004", "long day", "Stat", 2)]
    a, plan, tl = small_cut(graphics_raw)
    fps = 1 / float(tl.frame_duration)
    jobs: list[dict] = []
    fake_renderer(monkeypatch, [], jobs=jobs)
    clips, placed, warnings = graphics.make(NoStatDesign(), plan.graphics, tl, a, tmp_path / "out", brief="", style="", workers=2)
    lower = placed[0]
    assert [c.id for c in clips] == ["G01"] and lower.frames == round(4 * fps)
    assert "no design came back for G02; skipped" in warnings and not [w for w in warnings if "shortened" in w]
    # It was designed to make way, so its design plays with the hold stretched and the exit at the end.
    made = graphics.design_timing((tmp_path / "out" / "graphics" / "G01-lower-third.html").read_text(), tl.frame_duration)
    (job,) = jobs
    assert made["seconds"] < 3.5 and job["frames"] == lower.frames
    assert graphics.design_time(lower.frames / fps, job["retime"]) == pytest.approx(made["seconds"])
    assert graphics.design_time(lower.frames / fps - 0.5, job["retime"]) == pytest.approx(made["seconds"] - 0.5)
    assert next(c for c in tl.connected if c.clip_id == "G01").frames == lower.frames


def test_a_graphic_shortened_for_one_that_didnt_render_is_rendered_again_at_its_length(tmp_path, monkeypatch):
    graphics_raw = [spec("lower_third", "V01.S003", "Today", "Lower", 4), spec("stat", "V01.S004", "long day", "Stat", 2)]
    a, plan, tl = small_cut(graphics_raw)
    fps = 1 / float(tl.frame_duration)
    jobs: list[dict] = []
    fake_renderer(monkeypatch, [], jobs=jobs, fail={"G02 <span"})
    clips, placed, warnings = graphics.make(FakeProvider(), plan.graphics, tl, a, tmp_path / "out", brief="", style="", workers=2)
    lower = placed[0]
    short = next(j for j in jobs if j["id"] == "G01")["frames"]
    assert [(j["id"], j["frames"]) for j in jobs if j["id"] == "G01"] == [("G01", short), ("G01", round(4 * fps))]
    assert [c.id for c in clips] == ["G01"] and lower.frames == round(4 * fps) > short
    assert any(w.startswith("graphic G02 froze") for w in warnings) and not [w for w in warnings if "shortened" in w]
    entry = json.loads((tmp_path / "out" / "graphics.json").read_text())["graphics"][0]
    assert entry["frames"] == lower.frames and entry["rendered"] == entry["digest"] and entry["file"].endswith(f"-{entry['digest'][:6]}.mov")
    assert next(c for c in tl.connected if c.clip_id == "G01").frames == lower.frames


def test_a_graphic_that_waited_for_one_that_never_arrives_starts_on_its_words(tmp_path, monkeypatch):
    # Names read back to back: "Long Day" waits for "Mountain" to have its time on screen (see
    # test_names_read_back_to_back_each_get_their_graphic). When "Mountain" doesn't make it onto the
    # timeline after all, "Long Day" starts on its words again, as if it had been placed alone.
    first = spec("lower_third", "V01.S003", "the mountain", "Mountain", 3)
    second = spec("lower_third", "V01.S004", "It's going to be a long day", "Long Day", 3)
    a, plan, tl = small_cut([first, second])
    (alone,) = graphics.place(plan.graphics[1:], tl, a, [])
    (_, waited), _, _ = placed_for([first, second])
    assert waited.start > alone.start and len(waited.word_times) < len(alone.word_times)

    class NoMountainDesign(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, **kw):
            reply = super().complete_json(system, parts, schema, schema_name=schema_name, **kw)
            reply["graphics"] = [g for g in reply["graphics"] if g["id"] != "G01"]
            return reply

    jobs: list[dict] = []
    fake_renderer(monkeypatch, [], jobs=jobs)
    clips, placed, warnings = graphics.make(NoMountainDesign(), plan.graphics, tl, a, tmp_path / "out", brief="", style="", workers=2)
    long_day = next(g for g in placed if g.spec.text == "Long Day")
    assert (long_day.start, long_day.frames, long_day.word_times) == (alone.start, alone.frames, alone.word_times)
    assert [(j["id"], j["frames"]) for j in jobs] == [(long_day.id, alone.frames)]
    assert next(c for c in tl.connected if c.clip_id == long_day.id).start == alone.start

    # The same when the one it waited for doesn't render: it's rendered again, on its words.
    a, plan, tl = small_cut([first, second])
    jobs.clear()
    fake_renderer(monkeypatch, [], jobs=jobs, fail={"G01 <span"})
    clips, placed, warnings = graphics.make(FakeProvider(), plan.graphics, tl, a, tmp_path / "out2", brief="", style="", workers=2)
    long_day = next(g for g in placed if g.spec.text == "Long Day")
    assert [c.id for c in clips] == [long_day.id] and (long_day.start, long_day.frames, long_day.word_times) == (alone.start, alone.frames, alone.word_times)
    assert [j["frames"] for j in jobs if j["id"] == long_day.id] == [waited.frames, alone.frames]
    assert next(c for c in tl.connected if c.clip_id == long_day.id).start == alone.start
    entry = next(g for g in json.loads((tmp_path / "out2" / "graphics.json").read_text())["graphics"] if g["id"] == long_day.id)
    assert (entry["start_frame"], entry["frames"]) == (alone.start, alone.frames) and entry["rendered"] == entry["digest"]

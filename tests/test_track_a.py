"""Motion graphics robustness and quality: bounded renders, saved designs, logging, QC, placement, previews."""

from __future__ import annotations

import json
import re
import subprocess
import time

import pytest
from conftest import CANNED_PLAN, GRAPHIC_HTML, SPEECH, FakeProvider, make_clip

from roughcut import graphics
from roughcut.analyze import Analysis
from roughcut.plan import normalize
from roughcut.timeline import STYLES, build_timeline

needs_renderer = pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")

TWO_GRAPHICS = [
    {"kind": "lower_third", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2, "direction": ""},
    {"kind": "stat", "segment": "V01.S005", "words": "asking about this trip", "text": "Weeks of asking", "subtext": "", "seconds": 2, "direction": ""},
]

FREEZES = GRAPHIC_HTML.replace("<script>window.seek=function(t){", "<script>window.seek=function(t){if(t>0.1){while(true){}}")
STALLS_IN_A_TIMER = GRAPHIC_HTML.replace("</script>", ";setTimeout(function(){while(true){}},50)</script>")


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
    assert not (out / "graphics" / "G02-title-card.mov").exists() and not list((out / "graphics").glob("*.partial.mov"))
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    # The design is still saved, so it can be fixed and re-rendered.
    assert (out / "graphics" / "G02-title-card.html").read_text() == FREEZES


@needs_renderer
def test_a_page_stuck_outside_seek_cant_hang_the_renderer(tmp_path, monkeypatch):
    """A timer that never returns blocks the page's main thread; every browser call is bounded on our side."""
    monkeypatch.setattr(graphics, "STEP_TIMEOUT", 2.0)
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


@needs_renderer
def test_unchanged_renders_are_reused_and_an_old_manifest_still_counts(tmp_path):
    a, plan, tl = small_cut()
    out = tmp_path / "out"
    graphics.make(FakeProvider(), plan.graphics, tl, a, out, brief="", style="", workers=2)
    mov = out / "graphics" / "G01-lower-third.mov"
    stamp = mov.stat().st_mtime_ns
    # A manifest from before designs were saved ahead of rendering: "digest" + "file", no "rendered".
    manifest = json.loads((out / "graphics.json").read_text())
    for g in manifest["graphics"]:
        g.pop("rendered")
    (out / "graphics.json").write_text(json.dumps(manifest))
    specs, html, style = graphics.saved_designs(out)
    _, _, tl2 = small_cut()
    graphics.make(None, specs, tl2, a, out, brief="", style=style, workers=2, saved_html=html)
    assert mov.stat().st_mtime_ns == stamp


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


# ------------------------------------------------------------------ T1: seek-started animations, script errors, fonts, QC

FADE = "@keyframes f{from{opacity:0}to{opacity:1}}"
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
    def notes(css, gid):
        job = job_for(tmp_path, _plate(css), frames=30, gid=gid, kind="lower_third", size=(180, 320))
        job["canvas"] = graphics.canvas(180, 320)
        result = graphics._render_one(job)
        assert result["error"] is None
        return result["notes"]

    assert notes("left:8%;bottom:8%", "G01") == ["sits under the app's buttons and captions on a vertical video (bottom 20%)"]
    assert notes("left:8%;top:45%", "G02") == []


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

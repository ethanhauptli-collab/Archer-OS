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

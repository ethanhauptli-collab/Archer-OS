"""Motion graphics: planned on the transcript, designed as HTML, rendered to ProRes 4444 with alpha."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import CANNED_PLAN, SPEECH, FakeProvider, make_clip

from roughcut import graphics
from roughcut.analyze import Analysis
from roughcut.plan import PLAN_SCHEMA, GraphicSpec, normalize, plan_schema
from roughcut.prompts import render_brief
from roughcut.timeline import STYLES, build_timeline

GRAPHICS = [
    {"kind": "kinetic_caption", "segment": "V01.S005", "words": "asking about this trip for weeks", "text": "asking about this trip for weeks", "subtext": "", "seconds": 0, "direction": ""},
    {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""},
    {"kind": "stat", "segment": "V01.S999", "words": "x", "text": "Bad reference", "subtext": "", "seconds": 3, "direction": ""},
    {"kind": "lower_third", "segment": "V01.S002", "words": "wait", "text": "Not in the cut", "subtext": "", "seconds": 3, "direction": ""},
]


def analysis_and_cut(plan_raw):
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize(plan_raw, a)
    return a, plan, build_timeline(plan, a, style=STYLES["medium"], name="t")


def test_schema_and_brief_only_change_when_graphics_are_on():
    assert plan_schema(False) is PLAN_SCHEMA and "graphics" not in PLAN_SCHEMA["properties"]
    schema = plan_schema(True)
    assert "graphics" in schema["required"] and schema["properties"]["graphics"]["items"]["additionalProperties"] is False
    assert "<motion_graphics>" not in render_brief("b", None, None)
    brief = render_brief("b", None, None, graphics=True, graphics_style="orange, Gotham")
    assert "<motion_graphics>" in brief and "never invented" in brief and "orange, Gotham" in brief


def test_plan_keeps_only_graphics_on_words_in_the_cut():
    raw = {"sections": [{"name": "x", "items": ["V01.S003", "V01.S005"], "broll": []}], "graphics": GRAPHICS}
    a, plan, _ = analysis_and_cut(raw)
    assert [g.segment for g in plan.graphics] == ["V01.S005", "V01.S003"]
    assert any("unknown segment" in w for w in plan.warnings) and any("isn't in the cut" in w for w in plan.warnings)


def test_graphics_are_timed_from_the_transcript():
    raw = {"sections": [{"name": "x", "items": ["V01.S003", "V01.S005"], "broll": []}], "graphics": GRAPHICS[:2]}
    a, plan, tl = analysis_and_cut(raw)
    warnings = []
    title, kinetic = graphics.place(plan.graphics, tl, a, warnings)  # numbered in the order they appear
    assert (title.id, title.index, kinetic.id, kinetic.index) == ("G01", 1, "G02", 0)
    fps = 1 / float(tl.frame_duration)
    seg = a.segment("V01.S005")
    first = next(w for w in seg.words if w.text == "asking")
    # Starts just before "asking", wherever that landed in the cut.
    asking = graphics._frame_at(tl, "V01", first.start)
    assert kinetic.start == asking - round(graphics.LEAD * fps)
    assert [w for w, _ in kinetic.word_times] == ["asking", "about", "this", "trip", "for", "weeks."]
    times = [t for _, t in kinetic.word_times]
    assert times == sorted(times) and times[0] == pytest.approx(graphics.LEAD, abs=1.5 / fps)
    assert kinetic.frames / fps >= times[-1] + 0.5  # holds past the last word
    assert title.frames == round(2.5 * fps)
    assert not warnings


@pytest.mark.parametrize("style", ["tight", "medium", "loose"])
def test_a_graphic_on_the_words_that_open_a_clip_hangs_off_that_clip(style, tmp_path):
    """The lead before a graphic's first word never reaches back over the cut into the clip before.

    Final Cut connects a graphic to the clip under its first frame, and it moves and is deleted
    with that clip: a lower third on a speaker's first words belongs to the speaker's clip.
    """
    import xml.etree.ElementTree as ET
    from dataclasses import replace

    from test_end_to_end import check_invariants, validate_dtd

    from roughcut.fcpxml import build_fcpxml

    lower = {"kind": "lower_third", "segment": "V01.S005", "words": "The kids have", "text": "The Kids", "subtext": "", "seconds": 2, "direction": ""}
    a, plan, _ = analysis_and_cut({"sections": [{"name": "x", "items": ["V01.S003", "V01.S005"], "broll": []}], "graphics": [lower]})
    tl = build_timeline(plan, a, style=STYLES[style], name="t", format_override="640x360")
    fps = 1 / float(tl.frame_duration)
    (g,) = graphics.place(plan.graphics, tl, a, [])
    first = graphics._frame_at(tl, "V01", a.segment("V01.S005").words[0].start)
    piece = tl.spine_at(first)
    assert piece is tl.spine[1]  # "The kids" opens the second clip
    assert g.start == max(piece.offset, first - round(graphics.LEAD * fps))
    assert piece.offset <= g.start <= first
    assert g.word_times[0] == ("The", round((first - g.start) * float(tl.frame_duration), 3))  # still timed from its own start

    mov = tmp_path / f"{g.slug}.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black@0.0:s=640x360:r=30000/1001,format=rgba", "-frames:v", str(g.frames),
         *graphics.ENCODER, str(mov)],
        check=True,
    )
    clips = graphics.attach(tl, [g], {g.id: mov}, [])
    path = tmp_path / "cut.fcpxml"
    path.write_text(build_fcpxml([tl], replace(a, clips=a.clips + clips), event_name="e"))
    validate_dtd(path)
    check_invariants(path)
    spine = list(ET.parse(path).getroot().find(".//project/sequence/spine"))
    parents = [i for i, el in enumerate(spine) if any((c.get("name") or "").startswith(g.slug) for c in el)]
    assert parents == [1]


def test_find_phrase_tolerates_punctuation_and_partial_matches():
    words = make_clip("V01", seconds=20, sentences=SPEECH).segments[4].words  # "The kids have been asking about this trip for weeks."
    assert graphics._find_phrase(words, "TRIP, for weeks") == (7, 9)
    assert graphics._find_phrase(words, "asking about that trip") == (4, 5)  # the matching start of the phrase
    assert graphics._find_phrase(words, "nothing like it") == (0, len(words) - 1)


needs_renderer = pytest.mark.skipif(graphics.renderer_problem() is not None, reason="no Playwright browser to render with")


@needs_renderer
def test_build_renders_graphics_above_the_footage_and_recut_reuses_them(footage, tmp_path, monkeypatch):
    from test_end_to_end import validate_dtd

    from roughcut import pipeline

    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = GRAPHICS
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    opts = pipeline.Options(inputs=[footage], out=out, transcriber="none", cache_dir=tmp_path / "cache", format="640x360", graphics=True, graphics_style="orange")
    result = pipeline.build(opts)

    # Planned with the graphics brief and schema, then designed in one request.
    plan_call = next(c for c in provider.calls if c["schema_name"] == "edit_plan")
    assert "<motion_graphics>" in plan_call["parts"][-1].text and "orange" in plan_call["parts"][-1].text
    design = next(c for c in provider.calls if c["schema_name"] == "graphics")
    assert '"kind": "kinetic_caption"' in design["parts"][-1].text and "640x360" in design["parts"][-1].text

    movs = sorted((out / "graphics").glob("*.mov"))
    assert [m.name.rsplit("-", 1)[0] for m in movs] == ["G01-kinetic-caption", "G02-title-card"]  # + design digest
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,pix_fmt,width,height", "-of", "json", str(movs[0])], capture_output=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "prores" and stream["pix_fmt"].startswith("yuva") and stream["width"] == 640

    validate_dtd(result.fcpxml)
    xml = result.fcpxml.read_text()
    assert 'name="G01-kinetic-caption-' in xml and "Graphics" in xml
    broll_lanes = [c.lane for c in result.timeline.connected if c.kind == "broll"]
    graphic_lanes = [c.lane for c in result.timeline.connected if c.kind == "graphic"]
    assert len(graphic_lanes) == 2 and min(graphic_lanes) > max(broll_lanes, default=0)
    report = result.report.read_text()
    assert "## Motion graphics" in report and "Heading Out" in report

    # Hand-edit one design; a re-cut re-renders that one only, to a new file:
    # the first version (now in history/) keeps the clip it was imported with.
    untouched = movs[0].stat().st_mtime_ns
    html = out / "graphics" / "G02-title-card.html"
    html.write_text(html.read_text().replace("#F26B21", "#2155F2"))
    recut = pipeline.rerender(out, pipeline.Options(inputs=[], format="640x360"))
    after = sorted((out / "graphics").glob("*.mov"))
    assert movs[0].stat().st_mtime_ns == untouched and movs[1] in after
    new = [m for m in after if m not in movs]
    assert len(new) == 1 and new[0].name.startswith("G02-title-card-") and new[0].name in recut.fcpxml.read_text()
    validate_dtd(recut.fcpxml)


def test_graphics_need_a_provider(footage, tmp_path):
    from roughcut import pipeline

    with pytest.raises(pipeline.RoughcutError, match="need a provider"):
        pipeline.build(pipeline.Options(inputs=[footage], out=tmp_path / "o", provider="none", graphics=True, transcriber="none"))

"""A long shoot: two hours of talking head, B-roll, a dozen takes of the same lines, planned, cut and written in seconds.

In memory (no ffmpeg), so it is quick; the bounds are generous: they are here to catch a step that turns quadratic in the
number of lines, which a short test footage never shows. A real two-hour shoot built in about a minute when this was
written, nearly all of it in ffmpeg's silence and loudness passes.
"""

from __future__ import annotations

import time

from conftest import make_clip
from test_end_to_end import check_invariants, validate_dtd

from roughcut.analyze import Analysis
from roughcut.fcpxml import build_fcpxml
from roughcut.plan import normalize
from roughcut.timeline import STYLES, build_timeline

TALKING = 12
MINUTES = 10
BROLL = 30


def _gaps(script, length):
    gaps, cursor = [], 0.0
    for s, e, _ in script:
        if s - cursor > 0.35:
            gaps.append((cursor, s))
        cursor = e
    if length - cursor > 0.35:
        gaps.append((cursor, length))
    return gaps


def _shoot() -> Analysis:
    clips = []
    for i in range(1, TALKING + 1):
        seconds = MINUTES * 60
        # a line every 5 s, a long pause now and then, and every tenth line said again straight away (a retake)
        script, t, k = [], 1.0, 0
        while t < seconds - 10:
            text = " ".join(f"w{(i * 7919 + k * 104729 + n * 31) % 1009}" for n in range(11)) + "."  # a different line each time
            script.append((t, t + 3.6, text))
            t += 5.0 + (6.0 if k % 17 == 0 else 0.0)
            k += 1
            if k % 10 == 0 and t < seconds - 10:
                script.append((t, t + 3.6, text))
                t += 5.0
        clips.append(make_clip(f"V{i:02d}", seconds=seconds, sentences=script, silences=_gaps(script, seconds)))
    for j in range(TALKING + 1, TALKING + BROLL + 1):
        clips.append(make_clip(f"V{j:02d}", seconds=45))
    return Analysis(clips=clips)


def _plan(analysis: Analysis) -> dict:
    """What a model would send back for this much footage: a section per thirty lines, B-roll over every fifth."""
    aroll = [c for c in analysis.clips if c.role == "aroll"]
    broll = [c.id for c in analysis.clips if c.role == "broll"]
    ids = [s.id for c in aroll for s in c.segments][::2]
    sections = []
    for n, start in enumerate(range(0, len(ids), 30)):
        chunk = ids[start : start + 30]
        sections.append(
            {
                "name": f"Part {n + 1}",
                "purpose": "",
                "items": chunk,
                "broll": [{"clip": broll[(n + k) % len(broll)], "over": [chunk[k]], "source_in": -1, "reason": "cover"} for k in range(2, len(chunk), 5)],
            }
        )
    return {"title": "Long shoot", "sections": sections, "alternates": [], "flags": [], "music": []}


def test_two_hours_of_footage_is_cut_and_written_in_seconds(tmp_path):
    analysis = _shoot()
    lines = sum(len(c.segments) for c in analysis.clips)
    assert lines > 1200

    t0 = time.monotonic()
    plan = normalize(_plan(analysis), analysis, source="fake:fake-model")
    planned = time.monotonic() - t0

    t0 = time.monotonic()
    rough = build_timeline(plan, analysis, style=STYLES["medium"], name="Long shoot", dialogue_level=-16.0, fill=False)
    cut = time.monotonic() - t0

    t0 = time.monotonic()
    xml = build_fcpxml([rough], analysis, event_name="Long shoot", alternates=plan.alternates)
    written = time.monotonic() - t0

    assert len(rough.spine) > 250
    assert planned < 20 and cut < 60 and written < 60, f"plan {planned:.1f}s, cut {cut:.1f}s, write {written:.1f}s"

    path = tmp_path / "long.fcpxml"
    path.write_text(xml)
    validate_dtd(path)
    check_invariants(path)

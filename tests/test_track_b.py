"""Run lifecycle: saved plans, versions, revise, summaries, stopping cleanly."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd

from roughcut import graphics, pipeline, progress
from roughcut.cli import main
from roughcut.pipeline import Options, RoughcutError
from roughcut.providers import ProviderAuthError

GRAPHIC = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


def _opts(footage, out, cache, **kw) -> Options:
    return Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw)


def _rough(projects: dict) -> dict:
    return next(v for k, v in projects.items() if "Rough Cut" in k)


def _graphics_plan() -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [GRAPHIC]
    return plan


def _events(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------- N2: the paid plan is never lost


def test_plan_is_saved_before_graphics_and_render_finishes_without_the_model(footage, tmp_path, monkeypatch, capsys):
    provider = FakeProvider(_graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def crash(*a, **k):
        raise RuntimeError("the renderer crashed")

    monkeypatch.setattr(graphics, "render", crash)
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics", "--progress-json"])
    assert code == 1
    events = _events(capsys.readouterr().out)
    assert events[-1]["event"] == "error" and events[-1]["resume"] == f"roughcut render {out}"

    saved = json.loads((out / "plan.json").read_text())
    assert saved["plan"]["title"] == "Mountain Day" and saved["analysis_digest"]
    assert saved["settings"]["graphics"] is True and saved["settings"]["style"] == "medium"
    cut = next(out.glob("*.fcpxml"))  # the cut without graphics, written before rendering started
    validate_dtd(cut)
    check_invariants(cut)
    assert "The edit plan was saved" in (out / "roughcut.log").read_text()

    planner_calls = len(provider.calls)
    monkeypatch.setattr(graphics, "render", lambda *a, **k: {})
    again = pipeline.rerender(out, Options(inputs=[]))
    assert len(provider.calls) == planner_calls  # no model calls to finish it
    validate_dtd(again.fcpxml)
    assert again.plan.title == "Mountain Day"


def test_stop_after_the_plan_prints_how_to_finish(footage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "make_timelines", broken)
    out = tmp_path / "my out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 1
    err = capsys.readouterr().err
    assert f"roughcut render '{out}'" in err  # quoted: the path has a space


def test_a_failed_rebuild_leaves_the_previous_plan_working(footage, tmp_path, monkeypatch, fake):
    """IDs follow recording order: an earlier clip renumbers everything after it."""
    src = tmp_path / "footage"
    shutil.copytree(footage, src)
    out, cache = tmp_path / "out", tmp_path / "c"
    pipeline.build(_opts(src, out, cache))
    before = (out / "analysis.json").read_text()

    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-metadata", "creation_time=2026-08-12T07:00:00Z", str(src / "early.mp4"))

    class KeyRevoked(FakeProvider):
        def complete_json(self, *a, **k):
            if k.get("schema_name") == "edit_plan":
                raise ProviderAuthError("rejected")
            return super().complete_json(*a, **k)

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: KeyRevoked())
    with pytest.raises(ProviderAuthError):
        pipeline.build(_opts(src, out, cache))
    assert (out / "analysis.json").read_text() == before  # nothing touched before a new plan existed
    assert pipeline.rerender(out, Options(inputs=[])).plan.title == "Mountain Day"


def test_render_refuses_a_plan_made_for_other_clips(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    data = json.loads((out / "analysis.json").read_text())
    data["clips"][0]["media"]["fingerprint"] = "a-different-file"
    (out / "analysis.json").write_text(json.dumps(data))
    with pytest.raises(RoughcutError, match="different set of clips"):
        pipeline.rerender(out, Options(inputs=[]))


def test_render_keeps_the_builds_cut_settings(footage, tmp_path, fake):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"),
                 "--style", "tight", "--no-stringout"]) == 0
    tight = _rough(check_invariants(next(out.glob("*.fcpxml"))))["seconds"]

    assert main(["render", str(out)]) == 0  # no flags: still tight, still no stringout
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] == tight and not any("Stringout" in k for k in projects)

    assert main(["render", str(out), "--style", "medium", "--stringout"]) == 0
    projects = check_invariants(next(out.glob("*.fcpxml")))
    assert _rough(projects)["seconds"] > tight and any("Stringout" in k for k in projects)
    assert json.loads((out / "plan.json").read_text())["settings"]["style"] == "medium"  # the next render starts here


def test_a_new_build_without_graphics_drops_the_old_ones(footage, tmp_path, monkeypatch, fake):
    out = tmp_path / "out"
    out.mkdir()
    (out / "graphics.json").write_text(json.dumps({"style": "", "graphics": [{"id": "G01", "index": 0, **GRAPHIC, "html": "graphics/G01-title-card.html"}]}))
    pipeline.build(_opts(footage, out, tmp_path / "c"))
    assert not (out / "graphics.json").exists()

    def never(*a, **k):
        raise AssertionError("old graphics re-attached")

    monkeypatch.setattr(graphics, "make", never)
    pipeline.rerender(out, Options(inputs=[]))


def test_progress_stream_with_graphics_only_moves_forward(footage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(_graphics_plan()))
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def fake_make(provider, specs, tl, analysis, out_dir, **k):
        progress.stage("graphics", "Designing 1 graphics")
        return [], [], []

    monkeypatch.setattr(graphics, "make", fake_make)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--graphics", "--progress-json"]) == 0
    events = _events(capsys.readouterr().out)
    stages = [e["stage"] for e in events if e["event"] == "stage"]
    idx = [progress.STAGES.index(s) for s in stages]
    assert idx == sorted(idx) and stages[-1] == "write" and "graphics" in stages
    assert events[-1]["event"] == "done"

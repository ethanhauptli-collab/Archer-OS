"""Real ffmpeg-generated footage -> full pipeline -> FCPXML checked against Apple's DTD.

The DTD (tests/fixtures/FCPXMLv1_10.dtd) is Apple's, as shipped inside Final
Cut Pro and vendored by open-source FCPXML tools.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import SPEECH, FakeProvider

from roughcut import pipeline
from roughcut.cli import main
from roughcut.pipeline import Options
from roughcut.timecode import parse_fcpx_time as T

DTD = Path(__file__).parent / "fixtures" / "FCPXMLv1_10.dtd"


def validate_dtd(path: Path) -> None:
    lxml = pytest.importorskip("lxml.etree")
    dtd = lxml.DTD(open(DTD, "rb"))
    doc = lxml.parse(str(path))
    assert dtd.validate(doc), "\n".join(f"{e.line}: {e.message}" for e in dtd.error_log.filter_from_errors())


def check_invariants(path: Path) -> dict:
    """Timing rules Final Cut enforces, checked on our own output."""
    root = ET.parse(path).getroot()
    formats = {f.get("id"): f for f in root.iter("format")}
    assets = {a.get("id"): a for a in root.iter("asset")}
    report = {}
    for project in root.iter("project"):
        seq = project.find("sequence")
        fd = T(formats[seq.get("format")].get("frameDuration"))
        pos = Fraction(0)
        clips = list(seq.find("spine"))
        for el in clips:
            off, dur, start = T(el.get("offset")), T(el.get("duration")), T(el.get("start"))
            assert off == pos, f"gap/overlap at {el.get('name')}"
            assert (off / fd).denominator == 1 and (dur / fd).denominator == 1, "spine edit off the frame grid"
            _check_source_range(el, assets)
            for child in el:
                if child.tag in ("asset-clip", "video"):
                    c_off = T(child.get("offset"))
                    assert start <= c_off < start + dur, f"connected {child.get('name')} outside its parent"
                    assert child.get("lane") not in (None, "0")
                    _check_source_range(child, assets)
                elif child.tag in ("marker", "chapter-marker"):
                    assert start <= T(child.get("start")) < start + dur, "marker outside its parent"
            pos += dur
        assert T(seq.get("duration")) == pos
        report[project.get("name")] = {"seconds": float(pos), "spine": clips}
    return report


def _check_source_range(el, assets):
    asset = assets[el.get("ref")]
    if T(asset.get("duration")) == 0:  # stills
        assert T(el.get("start")) == 3600
        return
    a0, ad = T(asset.get("start")), T(asset.get("duration"))
    s, d = T(el.get("start")), T(el.get("duration"))
    assert a0 <= s and s + d <= a0 + ad + Fraction(1, 1000), f"{el.get('name')} reads outside its media"


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


def test_build_with_model_plan(footage, tmp_path, fake):
    opts = Options(inputs=[footage], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=tmp_path / "cache", context="Family trip", target_seconds=60)
    result = pipeline.build(opts)

    # The planner saw transcripts, visual notes and the brief.
    plan_call = next(c for c in fake.calls if c["purpose"] == "edit plan")
    media_text = plan_call["parts"][0].text
    assert "V01.S005" in media_text and "The kids have been asking" in media_text
    assert "Visual: Test pattern V02" in media_text
    assert plan_call["parts"][0].cache is True
    assert "Family trip" in plan_call["parts"][1].text and "1:00" in plan_call["parts"][1].text

    validate_dtd(result.fcpxml)
    projects = check_invariants(result.fcpxml)
    assert set(projects) == {"footage - Rough Cut", "footage - Stringout"}

    xml = result.fcpxml.read_text()
    root = ET.fromstring(xml)
    # Timecoded source: clip starts sit after the 01:00:00:00 origin.
    talk_asset = next(a for a in root.iter("asset") if a.get("name") == "talk")
    assert T(talk_asset.get("start")) == 108000 * Fraction(1001, 30000)
    assert [c.get("value") for c in root.iter("chapter-marker")][:3] == ["Cold open", "Heading out", "Wrap"]
    assert any(m.get("completed") == "0" for m in root.iter("marker"))
    assert any(e.get("lane") == "-1" and e.get("name") == "music" for e in root.iter("asset-clip"))
    assert root.find(".//event/clip/video") is not None  # browser still
    keywords = {k.get("value") for k in root.iter("keyword")}
    assert "A-Roll, test, pattern" in keywords and "Music & Audio" in keywords

    # The filler in "And honestly, uh, I needed it too." is cut: two pieces, and
    # neither covers the "uh".
    s, e, _ = SPEECH[5]
    uh_start = s + 2 * (e - s) / 7
    rough = projects["footage - Rough Cut"]["spine"]
    origin = T(talk_asset.get("start"))
    wrap = [el for el in rough if el.get("name") == "talk" and T(el.get("start")) - origin > 15]
    assert len(wrap) == 2
    first_end = T(wrap[0].get("start")) + T(wrap[0].get("duration")) - origin
    assert float(first_end) <= uh_start + 0.04

    report = result.report.read_text()
    assert "## Paper edit" in report and "Cold open" in report and "V09.S001" in report  # warning surfaced


def test_rerender_changes_style_without_model(footage, tmp_path, fake):
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=tmp_path / "cache"))
    n_calls = len(fake.calls)
    medium = check_invariants(next(out.glob("*.fcpxml")))["footage - Rough Cut"]["seconds"]
    assert main(["render", str(out), "--style", "tight", "--name", "footage"]) == 0
    tight = check_invariants(next(out.glob("*.fcpxml")))["footage - Rough Cut"]["seconds"]
    assert tight < medium
    assert len(fake.calls) == n_calls


def test_silence_only_mode_via_cli(footage, tmp_path):
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--format", "vertical", "--name", "footage"])
    assert code == 0
    fcpxml = next(out.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    projects = check_invariants(fcpxml)
    rough = projects["footage - Rough Cut"]
    # 20s talk clip with ~12s of sound: dead air is gone.
    assert 10 < rough["seconds"] < 17
    root = ET.parse(fcpxml).getroot()
    seq_fmt = root.find(".//sequence").get("format")
    fmt = next(f for f in root.iter("format") if f.get("id") == seq_fmt)
    assert (fmt.get("width"), fmt.get("height"), fmt.get("name")) == ("1080", "1920", None)


def test_model_failure_falls_back_to_stringout(footage, tmp_path, monkeypatch):
    from roughcut.providers import ProviderError

    class Broken(FakeProvider):
        def complete_json(self, *a, **k):
            if k.get("schema_name") == "edit_plan":
                raise ProviderError("boom")
            return super().complete_json(*a, **k)

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: Broken())
    result = pipeline.build(Options(inputs=[footage], out=tmp_path / "o", transcriber="none", cache_dir=tmp_path / "c"))
    assert result.fell_back
    assert "AI planning failed" in result.report.read_text()
    validate_dtd(result.fcpxml)


def test_scanner_skips_junk(footage):
    from roughcut.media import scan

    names = sorted(p.name for p in scan([footage]))
    assert names == ["broll.mp4", "broll_2398.mov", "music.m4a", "photo.png", "talk.mov"]


def test_probe_reads_timecode_and_rates(footage):
    from roughcut.media import probe

    talk = probe(footage / "talk.mov")
    assert talk.rate == "29.97" and talk.timecode == "01:00:00:00"
    assert talk.start == 108000 * Fraction(1001, 30000)
    assert abs(float(talk.duration) - 20) < 0.05
    film = probe(footage / "broll_2398.mov")
    assert film.rate == "23.98" and not film.has_audio
    still = probe(footage / "photo.png")
    assert still.kind == "image" and (still.width, still.height) == (1600, 900)
    music = probe(footage / "music" / "music.m4a")
    assert music.kind == "audio" and music.audio_rate == 48000

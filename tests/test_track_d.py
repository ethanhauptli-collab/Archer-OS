"""Audio and Final Cut details: frame-rate warnings, roles and notes, browser
keyword ranges for alternate reads, speech level matching, de-click fades and
the music bed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest
from conftest import NTSC, FakeProvider, make_clip
from test_end_to_end import check_invariants, validate_dtd

from roughcut import pipeline
from roughcut.analyze import Analysis
from roughcut.fcpxml import build_fcpxml
from roughcut.media import MediaInfo, frame_rate_warnings
from roughcut.pipeline import Options
from roughcut.plan import normalize
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import STYLES, build_timeline


@pytest.fixture(scope="module")
def built(footage, tmp_path_factory):
    """One planned build of the shared synthetic footage (fake model)."""
    tmp = tmp_path_factory.mktemp("track_d")
    provider = FakeProvider()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "make_provider", lambda *a, **k: provider)
        result = pipeline.build(Options(inputs=[footage], out=tmp / "out", name="footage", transcriber="none", cache_dir=tmp / "cache"))
    return result


def _write(tmp_path, timelines, analysis, **kw):
    path = tmp_path / "t.fcpxml"
    path.write_text(build_fcpxml(timelines, analysis, event_name="Test", **kw))
    return path


# ------------------------------------------------------------ T16 frame rates


def _video(name: str, fd: Fraction, vfr: bool = False) -> MediaInfo:
    return MediaInfo(path=f"/x/{name}", name=name, kind="video", duration=Fraction(10), start=Fraction(0), has_video=True, has_audio=True, frame_duration=fd, vfr=vfr)


def test_frame_rate_warnings_group_rates_and_list_vfr():
    project = NTSC
    used = [
        _video("a.mov", NTSC),
        _video("gopro.mp4", Fraction(1001, 60000)),
        _video("film.mov", Fraction(1001, 24000)),
        _video("phone.mov", NTSC, vfr=True),
        MediaInfo(path="/x/s.jpg", name="s.jpg", kind="image", duration=Fraction(0), start=Fraction(0), has_video=True, has_audio=False),
    ]
    lines = frame_rate_warnings(project, used)
    assert len(lines) == 2
    assert "2 clips" in lines[0] and "29.97 fps project" in lines[0]
    assert "59.94 fps: gopro.mp4" in lines[0] and "23.98 fps: film.mov" in lines[0]
    assert "judder" in lines[0]  # 23.98 in 29.97 isn't a whole multiple
    assert "Automatic Speed" in lines[0]  # the 59.94 clip could be slow motion
    assert "phone.mov" in lines[1] and "a.mov" not in lines[1]
    # A clean project says nothing; 59.94 in 29.97 alone doesn't judder.
    assert frame_rate_warnings(project, [_video("a.mov", NTSC)]) == []
    assert "judder" not in frame_rate_warnings(project, [_video("g.mp4", Fraction(1001, 60000))])[0]
    slower = frame_rate_warnings(project, [_video("film.mov", Fraction(1001, 24000))])[0]
    assert slower.startswith("1 clip in this cut runs") and "Automatic Speed" not in slower


def test_report_warns_about_the_2398_clip_in_a_2997_cut(built):
    report = built.report.read_text()
    warnings = report.split("## Warnings", 1)[1]
    assert "23.98 fps: broll_2398.mov" in warnings and "29.97 fps project" in warnings

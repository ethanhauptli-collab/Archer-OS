"""Setup and robustness: transcription failures, the visual log's cost, paths, updates, presets."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import ffmpeg

from roughcut.analyze import Cache, analyze
from roughcut.transcribe import Transcript, TranscriptionError, Word

# ------------------------------------------------------------ T17: one bad clip


@pytest.fixture(scope="module")
def four_talks(tmp_path_factory) -> list[Path]:
    """Four short clips with continuous sound, recorded in name order."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("talks")
    paths = []
    for i, name in enumerate("abcd"):
        p = root / f"{name}.mov"
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=3:sample_rate=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-metadata", f"creation_time=2026-08-12T09:0{i}:00Z",
            str(p),
        )
        paths.append(p)
    return paths


class StubTranscriber:
    """Transcribes everything except the named files, which fail like a corrupt file in mlx-whisper."""

    model = "stub"
    cache_key = "stub"

    def __init__(self, fail=(), error=None):
        self.fail = set(fail)
        self.error = error
        self.calls: list[str] = []

    def transcribe(self, path: Path) -> Transcript:
        self.calls.append(path.name)
        if path.name in self.fail:
            if self.error is not None:
                raise self.error
            raise RuntimeError(f"Failed to load audio: ffmpeg version 7.1 Copyright (c) the FFmpeg developers\n  built with clang\n{path}: Invalid data found when processing input")
        return Transcript(words=[Word(0.5, 1.0, "hello"), Word(1.0, 1.6, "there.")], source="stub")


def _analyze(paths, transcriber, tmp_path):
    return analyze(paths, transcriber=transcriber, cache=Cache(tmp_path / "cache"), workers=2)


def test_one_undecodable_clip_is_cut_on_its_pauses(four_talks, tmp_path):
    stub = StubTranscriber(fail={"b.mov"})
    analysis = _analyze(four_talks, stub, tmp_path)
    assert stub.calls == ["a.mov", "b.mov", "c.mov", "d.mov"]  # kept going after b
    by_name = {c.media.name: c for c in analysis.clips}
    assert by_name["b.mov"].transcript_source == "silence-map"
    assert by_name["b.mov"].segments, "still cut on its sound regions"
    assert by_name["a.mov"].transcript_source == "stub" and by_name["c.mov"].transcript_source == "stub"
    warning = next(w for w in analysis.warnings if w.startswith("b.mov"))
    assert "couldn't transcribe" in warning and "Invalid data found when processing input" in warning
    assert "ffmpeg version" not in warning, "the useful last line, not the banner"
    assert str(four_talks[1].parent) not in warning, "the file name, not its full path"
    # A failure isn't cached: the next run tries the clip again.
    stub2 = StubTranscriber()
    _analyze(four_talks, stub2, tmp_path)
    assert stub2.calls == ["b.mov"]


def test_a_failing_first_clip_alone_does_not_stop_the_run(four_talks, tmp_path):
    analysis = _analyze(four_talks, StubTranscriber(fail={"a.mov"}), tmp_path)
    assert [c.transcript_source for c in analysis.clips] == ["silence-map", "stub", "stub", "stub"]


def test_a_broken_transcriber_still_stops_the_run(four_talks, tmp_path):
    with pytest.raises(TranscriptionError) as e:
        _analyze(four_talks, StubTranscriber(fail={"a.mov", "b.mov"}), tmp_path)
    assert "a.mov" in str(e.value) and "b.mov" in str(e.value) and "--transcriber none" in str(e.value)


def test_three_failures_in_a_row_stop_the_run(four_talks, tmp_path):
    with pytest.raises(TranscriptionError, match="3 clips in a row"):
        _analyze(four_talks, StubTranscriber(fail={"b.mov", "c.mov", "d.mov"}), tmp_path)


def test_transcription_errors_are_never_swallowed(four_talks, tmp_path):
    stub = StubTranscriber(fail={"c.mov"}, error=TranscriptionError("mlx-whisper is not installed"))
    with pytest.raises(TranscriptionError, match="not installed"):
        _analyze(four_talks, stub, tmp_path)


# ------------------------------------------------------------ T18: cheaper visual log


def _dims(path) -> tuple[int, int]:
    import subprocess

    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def test_portrait_frames_are_scaled_by_the_long_edge(tmp_path):
    from roughcut import ffmpeg_ops

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    portrait, landscape = tmp_path / "portrait.mp4", tmp_path / "landscape.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(portrait))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1600x900:duration=1", "-frames:v", "1", str(landscape))
    assert _dims(ffmpeg_ops.extract_frame(portrait, 0.5, tmp_path / "p.jpg")) == (288, 512)
    assert _dims(ffmpeg_ops.extract_frame(landscape, 0, tmp_path / "l.jpg")) == (512, 288)  # unchanged


def test_hidden_duplicate_exports_are_not_described_again(footage, tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    from conftest import FakeProvider
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut import pipeline
    from roughcut.analyze import Analysis
    from roughcut.pipeline import Options

    folder = tmp_path / "shoot"
    folder.mkdir()
    for name in ("talk.mov", "talk.json", "broll.mp4", "broll.json"):
        shutil.copy2(footage / name, folder / name)
    shutil.copy2(footage / "broll.mp4", folder / "broll_copy.mp4")  # the same clip exported twice
    shutil.copy2(footage / "broll.json", folder / "broll_copy.json")
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=900x1600:duration=1", "-frames:v", "1", str(folder / "portrait.png"))

    fake = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: fake)
    opts = Options(inputs=[folder], out=tmp_path / "out", name="shoot", transcriber="none", cache_dir=tmp_path / "cache")
    result = pipeline.build(opts)

    analysis = Analysis.load(result.out_dir / "analysis.json")
    hidden = [c for c in analysis.clips if c.hidden]
    assert len(hidden) == 1, [c.media.name for c in analysis.clips]
    copy = hidden[0]
    kept = analysis.clip(copy.duplicate_of)
    sent = [p.text.split()[1] for call in fake.calls if call["schema_name"] == "clip_log" for p in call["parts"] if getattr(p, "text", "").startswith("Clip ")]
    assert kept.id in sent and copy.id not in sent, sent
    assert copy.visual == kept.visual and copy.visual["tags"] == ["test", "pattern"]

    # The hidden copy still arrives in Final Cut keyworded like the clip it duplicates.
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    root = ET.parse(result.fcpxml).getroot()
    browser = {el.get("name"): el for el in root.find(".//event") if el.tag == "asset-clip"}
    assert "test" in browser[copy.media.stem].find("keyword").get("value")

    still = next(c for c in analysis.clips if c.media.name == "portrait.png")
    assert still.frames and _dims(still.frames[0]) == (288, 512)

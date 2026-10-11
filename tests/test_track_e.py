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


# ------------------------------------------------------------ N4: zsh-safe commands, split paths, update

README = Path(__file__).parents[1] / "README.md"


def _bash_blocks(text: str) -> list[str]:
    import re

    return re.findall(r"```bash\n(.*?)```", text, flags=re.S)


def test_readme_commands_paste_cleanly_into_zsh():
    """zsh (macOS's shell) doesn't treat # as a comment when pasting, and globs unquoted [...]."""
    blocks = _bash_blocks(README.read_text(encoding="utf-8"))
    assert len(blocks) >= 8
    for block in blocks:
        for line in block.splitlines():
            assert not line.lstrip().startswith("#") and " #" not in line, f"comment in a shell block: {line!r}"
            assert "<" not in line and ">" not in line, f"placeholder or redirect in a shell block: {line!r}"
            assert ".[" not in line.replace("'.[", ""), f"unquoted pip extras: {line!r}"


def test_readme_check_catches_comments():
    assert " #" in _bash_blocks("```bash\npip install -e '.[mac]'   # fast\n```\n")[0]


@pytest.fixture
def spaced(tmp_path) -> Path:
    folder = tmp_path / "YouTube Current Project Staging" / "OCVIBE"
    folder.mkdir(parents=True)
    return folder


def test_split_path_is_rejoined(spaced):
    from roughcut.paths import resolve

    notes = []
    pieces = str(spaced).split(" ")
    assert len(pieces) == 4
    assert resolve(pieces, ["build", *pieces], notify=notes.append) == [str(spaced)]
    assert notes == [f"Read that as '{spaced}'. Next time put the path in quotes, or drag the folder into Terminal."]


def test_pieces_that_exist_on_their_own_are_not_joined(tmp_path):
    from roughcut.paths import rejoin

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a b").mkdir()  # exists too, but both pieces are real folders
    assert rejoin([str(tmp_path / "a"), str(tmp_path / "b")]) == [str(tmp_path / "a"), str(tmp_path / "b")]
    # An existing first piece is still joined when the rest only makes sense with it.
    (tmp_path / "Trip").mkdir()
    (tmp_path / "Trip Day").mkdir()
    assert rejoin([str(tmp_path / "Trip"), "Day"]) == [str(tmp_path / "Trip Day")]
    # Two folders, one with spaces.
    assert rejoin([str(tmp_path / "a"), str(tmp_path / "Trip"), "Day"]) == [str(tmp_path / "a"), str(tmp_path / "Trip Day")]


def test_missing_path_fails_with_the_quoted_command(tmp_path):
    from roughcut.paths import PathInputError, resolve

    pieces = [str(tmp_path / "My"), "Trip"]
    with pytest.raises(PathInputError) as e:
        resolve(pieces, ["build", *pieces, "-t", "8m"])
    message = str(e.value)
    assert f"can't find '{tmp_path}/My Trip'" in message
    assert f"roughcut build '{tmp_path}/My Trip' -t 8m" in message
    with pytest.raises(PathInputError) as e:  # a plain typo: no advice about quotes
        resolve([str(tmp_path / "Tirp")], ["build", str(tmp_path / "Tirp")])
    assert "quotes" not in str(e.value)


def test_build_and_render_accept_split_paths(footage, spaced, tmp_path, capsys):
    from roughcut.cli import main

    for name in ("talk.mov", "talk.json"):
        shutil.copy2(footage / name, spaced / name)
    out = tmp_path / "Rough Cuts" / "OCVIBE out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", *str(spaced).split(" "), "-o", str(out), *common]) == 0
    assert (out / "OCVIBE.fcpxml").is_file()
    assert "Read that as" in capsys.readouterr().err
    assert main(["render", *str(out).split(" "), "--style", "tight"]) == 0


def test_build_with_a_missing_path_stops_before_creating_anything(tmp_path, capsys):
    from roughcut.cli import main

    before = sorted(p.name for p in tmp_path.parent.iterdir())
    assert main(["build", str(tmp_path / "My"), "Trip", "--provider", "none", "--transcriber", "none"]) == 1
    err = capsys.readouterr().err
    assert f"roughcut build '{tmp_path}/My Trip' --provider none --transcriber none" in err
    assert sorted(p.name for p in tmp_path.parent.iterdir()) == before
    assert list(tmp_path.iterdir()) == []


def _git(cwd: Path, *args: str) -> str:
    import os
    import subprocess

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def clone(tmp_path) -> tuple[Path, Path]:
    """An 'upstream' repo and a clone of it, like the owner's install."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, {"pyproject.toml": "[project]\nname='roughcut'\n", "roughcut/__init__.py": '__version__ = "0.1.0"\n'}, "first")
    local = tmp_path / "Archer OS"
    _git(tmp_path, "clone", "-q", str(upstream), str(local))
    return upstream, local


class Runner:
    """subprocess.run that does git for real and records pip instead of running it."""

    def __init__(self, pip_code: int = 0):
        self.pip: list[list[str]] = []
        self.pip_code = pip_code

    def __call__(self, cmd, **kwargs):
        import subprocess

        if cmd[0] == "git":
            return subprocess.run(cmd, **kwargs)
        self.pip.append(cmd)
        return subprocess.CompletedProcess(cmd, self.pip_code, "", "")


def test_update_pulls_and_reinstalls_only_when_dependencies_change(clone):
    import sys

    from roughcut import update

    upstream, local = clone
    lines: list[str] = []
    runner = Runner()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert any("Already up to date: roughcut 0.1.0" in ln for ln in lines) and runner.pip == []

    _commit(upstream, {"roughcut/cut.py": "x = 1\n"}, "Cut tighter around breaths")
    lines.clear()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert runner.pip == [], "code-only change: an editable install needs no reinstall"
    assert any("Cut tighter around breaths" in ln for ln in lines)
    assert (local / "roughcut" / "cut.py").is_file()

    _commit(upstream, {"pyproject.toml": "[project]\nname='roughcut'\ndependencies=['x']\n", "roughcut/__init__.py": '__version__ = "0.2.0"\n'}, "New dependency")
    lines.clear()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert len(runner.pip) == 1
    cmd = runner.pip[0]
    assert cmd[:5] == [sys.executable, "-m", "pip", "install", "-e"] and cmd[5].startswith(str(local))
    assert any("roughcut 0.1.0 → 0.2.0" in ln for ln in lines)


def test_update_explains_a_failed_pull(clone):
    from roughcut import update

    upstream, local = clone
    _commit(upstream, {"roughcut/__init__.py": '__version__ = "0.2.0"\n'}, "bump")
    (local / "roughcut" / "__init__.py").write_text('__version__ = "mine"\n')  # edited by hand
    lines: list[str] = []
    assert update.update(local, runner=Runner(), out=lines.append) == 1
    assert "edited by hand" in lines[-1] and "git -C" in lines[-1]


def test_update_refuses_outside_a_git_checkout(tmp_path):
    from roughcut import update

    lines: list[str] = []
    assert update.update(tmp_path, runner=Runner(), out=lines.append) == 1
    assert "isn't a git clone" in lines[0]


def test_update_command_is_wired(monkeypatch):
    from roughcut import update
    from roughcut.cli import main

    seen = []
    monkeypatch.setattr(update, "update", lambda: seen.append(True) or 0)
    assert main(["update"]) == 0 and seen == [True]

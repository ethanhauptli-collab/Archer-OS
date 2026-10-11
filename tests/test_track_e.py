"""Setup and robustness: transcription failures, the visual log's cost, paths, updates, presets."""

from __future__ import annotations

import json
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


def test_one_clip_that_cant_be_transcribed_stops_the_run(four_talks, tmp_path):
    # A one-clip project on the first run, while the Whisper model can't be downloaded:
    # going on would plan (and bill) a cut with no words at all.
    error = OSError("Cannot find the requested files in the disk cache and outgoing traffic has been disabled")
    with pytest.raises(TranscriptionError) as e:
        _analyze(four_talks[:1], StubTranscriber(fail={"a.mov"}, error=error), tmp_path)
    message = str(e.value)
    assert "a.mov" in message and "disk cache" in message
    assert "roughcut doctor" in message and "--transcriber none" in message


def test_one_bad_clip_among_transcribed_ones_still_goes_on(four_talks, tmp_path):
    # The other clips were transcribed on an earlier run (cached), so the transcriber works.
    _analyze(four_talks[1:], StubTranscriber(), tmp_path)
    stub = StubTranscriber(fail={"a.mov"})
    analysis = _analyze(four_talks, stub, tmp_path)
    assert stub.calls == ["a.mov"]
    assert [c.transcript_source for c in analysis.clips] == ["silence-map", "stub", "stub", "stub"]
    assert any(w.startswith("a.mov: couldn't transcribe") for w in analysis.warnings)


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


def test_readme_inline_commands_paste_cleanly_into_zsh():
    import re

    text = README.read_text(encoding="utf-8")
    inline = [c for c in re.findall(r"`([^`\n]+)`", text) if c.startswith(("roughcut ", "pip ", "git ", "cd "))]
    assert len(inline) >= 10
    for command in inline:
        assert "<" not in command and ">" not in command, f"placeholder or redirect in {command!r}"


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


def test_a_quoted_path_with_a_typo_gets_no_quoting_advice(spaced):
    # It arrived whole (typed in quotes, or from the Mac app): the folder is what's wrong.
    from roughcut.paths import PathInputError, resolve

    typo = str(spaced.parent / "OCVIBX")
    with pytest.raises(PathInputError) as e:
        resolve([typo], ["build", typo, "--progress-json"])
    message = str(e.value)
    assert message == f"can't find '{typo}' (no such file or folder)."


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
    """subprocess.run that does git for real and records pip and doctor instead of running them."""

    def __init__(self, pip_code: int = 0, doctor_code: int = 0):
        self.ran: list[tuple[list[str], dict]] = []
        self.pip_code, self.doctor_code = pip_code, doctor_code

    @property
    def pip(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.ran if "pip" in cmd]

    @property
    def doctor(self) -> list[tuple[list[str], dict]]:
        return [(cmd, kw) for cmd, kw in self.ran if cmd[-1] == "doctor"]

    def __call__(self, cmd, **kwargs):
        import subprocess

        if cmd[0] == "git":
            return subprocess.run(cmd, **kwargs)
        self.ran.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, self.doctor_code if cmd[-1] == "doctor" else self.pip_code, "", "")


def test_update_pulls_and_reinstalls_only_when_dependencies_change(clone):
    import sys

    from roughcut import update

    upstream, local = clone
    lines: list[str] = []
    runner = Runner()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert any("Already up to date: roughcut 0.1.0" in ln for ln in lines) and runner.pip == []
    assert runner.doctor == [], "nothing new, nothing to check"

    _commit(upstream, {"roughcut/cut.py": "x = 1\n"}, "Cut tighter around breaths")
    lines.clear()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert runner.pip == [], "code-only change: an editable install needs no reinstall"
    assert any("Cut tighter around breaths" in ln for ln in lines)
    assert (local / "roughcut" / "cut.py").is_file()
    # Then doctor checks the setup, in a fresh process on the new code, run from the clone.
    assert runner.doctor == [([sys.executable, "-m", "roughcut", "doctor"], {"cwd": str(local)})]

    _commit(upstream, {"pyproject.toml": "[project]\nname='roughcut'\ndependencies=['x']\n", "roughcut/__init__.py": '__version__ = "0.2.0"\n'}, "New dependency")
    lines.clear()
    assert update.update(local, runner=runner, out=lines.append) == 0
    assert len(runner.pip) == 1
    cmd = runner.pip[0]
    assert cmd[:5] == [sys.executable, "-m", "pip", "install", "-e"] and cmd[5].startswith(str(local))
    assert any("roughcut 0.1.0 → 0.2.0" in ln for ln in lines)


def test_update_says_when_doctor_finds_a_problem(clone):
    from roughcut import update

    upstream, local = clone
    _commit(upstream, {"roughcut/cut.py": "x = 1\n"}, "Cut tighter")
    lines: list[str] = []
    runner = Runner(doctor_code=1)
    assert update.update(local, runner=runner, out=lines.append) == 0, "the update itself worked"
    assert len(runner.doctor) == 1 and "fix what doctor flagged" in lines[-1]


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


# ------------------------------------------------------------ T13: presets


class Captured(Exception):
    """Stops main() at the pipeline with the Options it built."""


@pytest.fixture
def config(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


@pytest.fixture
def capture(monkeypatch):
    from roughcut import graphics, pipeline

    seen = {}
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)  # don't start a browser for each check

    def grab(kind):
        def fn(*args):
            seen[kind] = args
            raise Captured

        return fn

    monkeypatch.setattr(pipeline, "build", grab("build"))
    monkeypatch.setattr(pipeline, "rerender", grab("render"))
    return seen


def _opts(capture, argv):
    from roughcut.cli import main

    with pytest.raises(Captured):
        main(argv)
    return capture["build"][0]


def test_built_in_presets(config):
    from roughcut import presets

    vo, stk = presets.load("venture-oc"), presets.load("Save-The-Kid")
    assert (vo.target, vo.style, vo.graphics) == ("90s", "tight", True)
    assert (stk.target, stk.style, stk.graphics) == ("10m", "loose", True)
    assert vo.graphics_style == presets.VENTURE_OC_LOOK and stk.graphics_style == presets.SAVE_THE_KID_LOOK
    assert "fatherhood" in stk.brief and "minimal" in stk.brief.lower() and "Venture" not in stk.brief
    assert vo.brief != stk.brief and vo.source == "built in"
    assert presets.check(vo) is None and presets.check(stk) is None


def test_no_invented_brand_colors_anywhere():
    import re

    from roughcut import presets
    from roughcut.cli import build_parser

    hex_code = re.compile(r"#[0-9A-Fa-f]{6}\b")
    help_text = build_parser()._subparsers._group_actions[0].choices["build"].format_help()
    for text in (presets.VENTURE_OC_LOOK, presets.SAVE_THE_KID_LOOK, presets.VENTURE_OC_BRIEF, help_text, README.read_text(encoding="utf-8")):
        assert not hex_code.search(text), hex_code.search(text)
    # But the brand's color family is pinned, so two runs don't pick two different accents.
    assert "navy" in presets.VENTURE_OC_LOOK and "orange accent" in presets.VENTURE_OC_LOOK


def test_preset_fills_in_what_you_didnt_type(footage, config, capture):
    from roughcut import presets

    opts = _opts(capture, ["build", str(footage), "--preset", "venture-oc", "-c", "OC Vibe launch night"])
    assert opts.target_seconds == 90 and opts.style == "tight" and opts.format is None
    assert opts.graphics is True and opts.graphics_style == presets.VENTURE_OC_LOOK
    # The preset's brief and yours are combined, the preset's first.
    assert opts.context.startswith(presets.VENTURE_OC_BRIEF) and opts.context.endswith("OC Vibe launch night")


def test_flags_you_type_win_over_the_preset(footage, config, capture, tmp_path):
    opts = _opts(
        capture,
        ["build", str(footage), "-t", "2m", "--preset=venture-oc", "--style", "medium", "--graphics-style", "plain", "--no-graphics", "--format", "vertical"],
    )
    assert opts.target_seconds == 120 and opts.style == "medium" and opts.graphics_style == "plain"
    assert opts.graphics is False and opts.format == "vertical"
    # Without a preset nothing changes.
    opts = _opts(capture, ["build", str(footage)])
    assert (opts.target_seconds, opts.style, opts.graphics, opts.graphics_style, opts.context) == (None, "medium", False, "", "")


def test_preset_graphics_are_skipped_without_a_model(footage, config, capture, capsys):
    opts = _opts(capture, ["build", str(footage), "--preset", "venture-oc", "--provider", "none"])
    assert opts.graphics is False and opts.style == "tight"
    assert "graphics are skipped" in capsys.readouterr().err


def test_preset_graphics_without_a_renderer_stop_before_anything_runs(footage, config, capture, monkeypatch, capsys):
    from roughcut import graphics
    from roughcut.cli import main

    monkeypatch.setattr(graphics, "renderer_problem", lambda: graphics.INSTALL_HELP)
    assert main(["build", str(footage), "--preset", "save-the-kid", "--no-menu"]) == 1
    err = capsys.readouterr().err
    assert "save-the-kid preset turns motion graphics on" in err and "pip install" in err
    assert "--no-graphics" in err and "preset save save-the-kid --no-graphics" in err
    assert not (footage.parent / f"{footage.name}_roughcut").exists(), "nothing created, no log left behind"
    # Typing --no-graphics gets the rest of the preset.
    opts = _opts(capture, ["build", str(footage), "--preset", "save-the-kid", "--no-graphics"])
    assert opts.graphics is False and opts.style == "loose"
    # Graphics you typed yourself are checked by the build, as before.
    opts = _opts(capture, ["build", str(footage), "--preset", "save-the-kid", "--graphics"])
    assert opts.graphics is True


def test_render_takes_the_presets_pacing(config, capture, tmp_path):
    from roughcut.cli import main

    with pytest.raises(Captured):
        main(["render", str(tmp_path), "--preset", "save-the-kid"])
    assert capture["render"][1].style == "loose"


def test_unknown_preset_stops_before_anything_runs(footage, config, capsys):
    from roughcut.cli import main

    assert main(["build", str(footage), "--preset", "vacation"]) == 1
    err = capsys.readouterr().err
    assert "no preset called 'vacation'" in err and "venture-oc" in err


def test_save_your_version_of_a_built_in_preset(footage, config, capture, tmp_path, capsys):
    from roughcut import presets
    from roughcut.cli import main

    logo = tmp_path / "logo.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(logo))
    assert main(["preset", "save", "venture-oc", "--graphics-style", "navy plates, orange accent, Gotham", "--logo", str(logo)]) == 0
    saved = presets.load("venture-oc")
    assert saved.source == "saved over built in" and saved.graphics_style == "navy plates, orange accent, Gotham"
    assert saved.brief == presets.VENTURE_OC_BRIEF and saved.target == "90s", "fields not given keep their values"
    assert saved.logo == str(logo)
    # Only your changes are stored, so later improvements to the built-in brief still reach you.
    stored = json.loads((config / "presets" / "venture-oc.json").read_text())
    assert set(stored) == {"graphics_style", "logo"}

    opts = _opts(capture, ["build", str(footage), "--preset", "venture-oc"])
    assert opts.graphics_style == "navy plates, orange accent, Gotham" and opts.graphics_logo == str(logo)

    # A new one, started from the other built-in.
    assert main(["preset", "save", "Trip-Vlog", "--from", "save-the-kid", "-t", "6m", "--no-graphics", "--description", "family trips"]) == 0
    capsys.readouterr()
    assert main(["preset", "list", "--json"]) == 0
    listed = {p["name"]: p for p in json.loads(capsys.readouterr().out)}
    assert set(listed) == {"venture-oc", "save-the-kid", "trip-vlog"}
    assert listed["trip-vlog"]["target"] == "6m" and listed["trip-vlog"]["graphics"] is False and listed["trip-vlog"]["style"] == "loose"
    assert listed["venture-oc"]["source"] == "saved over built in"

    assert main(["preset", "show", "trip-vlog"]) == 0
    assert "6m" in capsys.readouterr().out
    assert main(["preset", "remove", "venture-oc"]) == 0
    assert "built-in venture-oc preset is back" in capsys.readouterr().out
    assert presets.load("venture-oc").graphics_style == presets.VENTURE_OC_LOOK
    assert main(["preset", "remove", "save-the-kid"]) == 1  # built in, nothing saved


def test_bad_preset_values_are_refused(config, tmp_path, capsys):
    from roughcut.cli import main

    assert main(["preset", "save", "x", "-t", "soon"]) == 1
    assert main(["preset", "save", "x", "--format", "huge"]) == 1
    assert main(["preset", "save", "x", "--logo", str(tmp_path / "missing.png")]) == 1
    assert main(["preset", "save", "../evil"]) == 1
    assert not (config / "presets").exists() or not list((config / "presets").iterdir())
    err = capsys.readouterr().err
    assert "isn't a length" in err and "doesn't exist" in err and "can't be a preset name" in err


def test_saving_over_a_broken_preset_file_never_blanks_it(config, capsys):
    from roughcut import presets
    from roughcut.cli import main

    folder = config / "presets"
    folder.mkdir(parents=True)
    broken = folder / "save-the-kid.json"
    broken.write_text('{"target": "nonsense"}')
    assert main(["preset", "save", "save-the-kid", "--graphics-style", "navy and orange"]) == 1
    err = capsys.readouterr().err
    assert "isn't a length" in err and "roughcut preset remove save-the-kid" in err
    assert broken.read_text() == '{"target": "nonsense"}', "left as it was"
    (folder / "mine.json").write_text("{not json")
    assert main(["preset", "save", "mine", "-t", "5m"]) == 1
    assert (folder / "mine.json").read_text() == "{not json"
    # After removing it, saving starts from the built-in again.
    assert main(["preset", "remove", "save-the-kid"]) == 0
    assert main(["preset", "save", "save-the-kid", "--graphics-style", "navy and orange"]) == 0
    saved = presets.load("save-the-kid")
    assert saved.brief == presets.SAVE_THE_KID_BRIEF and (saved.target, saved.style, saved.graphics) == ("10m", "loose", True)
    assert json.loads(broken.read_text()) == {"graphics_style": "navy and orange"}


def test_missing_logo_stops_before_anything_runs(footage, config, tmp_path, capsys):
    from roughcut.cli import main

    assert main(["build", str(footage), "--graphics", "--graphics-logo", str(tmp_path / "nope.png")]) == 1
    assert "nope.png doesn't exist" in capsys.readouterr().err
    assert not (footage.parent / f"{footage.name}_roughcut").exists()


def test_pipeline_hands_the_logo_to_graphics(footage, tmp_path, monkeypatch):
    from conftest import CANNED_PLAN, FakeProvider

    from roughcut import graphics, pipeline

    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [{"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2, "direction": ""}]
    fake = FakeProvider(plan)
    seen = {}
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: fake)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "make", lambda *a, **k: seen.update(k) or ([], [], []))
    opts = pipeline.Options(inputs=[footage], out=tmp_path / "out", transcriber="none", cache_dir=tmp_path / "c", graphics=True, graphics_logo="/brand/logo.svg")
    pipeline.build(opts)
    assert seen["logo"] == "/brand/logo.svg"


def test_logo_is_inlined_for_the_offline_renderer(tmp_path):
    from roughcut import presets

    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    html = '<img src="brand:logo" alt="">'
    inlined = presets.inline_logo(html, str(svg))
    assert inlined.startswith('<img src="data:image/svg+xml;base64,') and "brand:logo" not in inlined
    assert presets.inline_logo(html, None) == html
    assert presets.with_logo("bold", None) == "bold"
    assert presets.with_logo("bold", str(svg)).startswith("bold\n\nBrand logo:") and 'src="brand:logo"' in presets.with_logo("", str(svg))


LOGO_HTML = """<!doctype html><html><head><style>
html,body{margin:0;background:transparent;overflow:hidden}
img{position:absolute;left:0;top:0;height:540px}
</style></head><body><img src="brand:logo" alt=""></body></html>"""


def _pixel(mov: Path, x: int, y: int, at: float) -> tuple[int, ...]:
    import subprocess

    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(mov), "-frames:v", "1", "-vf", f"crop=1:1:{x}:{y},format=rgba", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    return tuple(out[:4])


def test_logo_renders_in_a_graphic(tmp_path):
    from conftest import SPEECH, make_clip

    from roughcut import graphics
    from roughcut.analyze import Analysis
    from roughcut.plan import GraphicSpec, normalize
    from roughcut.timeline import STYLES, build_timeline

    if graphics.renderer_problem() is not None:
        pytest.skip("no Playwright browser to render with")
    logo = tmp_path / "logo.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(logo))

    class Designer:
        name, model = "fake", "fake"

        def __init__(self):
            self.requests = []

        def complete_json(self, system, parts, schema, **kw):
            self.requests.append(parts[-1].text)
            return {"style": "logo test", "graphics": [{"id": "G01", "html": LOGO_HTML}]}

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S003"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", format_override="320x180")
    spec = GraphicSpec(kind="title_card", segment="V01.S003", words="heading up the mountain", text="Heading Out", seconds=1.0)
    designer = Designer()
    clips, placed, warnings = graphics.make(designer, [spec], tl, a, tmp_path / "out", brief="", style="bold", workers=1, logo=str(logo))
    assert not warnings and len(clips) == 1
    assert 'src="brand:logo"' in designer.requests[0], "the designer is told about the logo"
    saved = (tmp_path / "out" / "graphics" / f"{placed[0].slug}.html").read_text()
    assert "data:image/png;base64," in saved and "brand:logo" not in saved, "re-renders don't need the logo file"
    mov = tmp_path / "out" / "graphics" / f"{placed[0].slug}.mov"
    r, g, b, alpha = _pixel(mov, 40, 40, 0.5)  # inside the logo (top-left, 90 px tall at this size)
    assert r > 200 and g < 60 and b < 60 and alpha > 200
    assert _pixel(mov, 300, 170, 0.5)[3] < 20  # elsewhere: transparent


# ------------------------------------------------------------ T13 step 2: an approved look


def _run_with_graphics(tmp_path: Path, name: str) -> Path:
    """A fake earlier run folder: graphics.json plus its saved designs, one with an inlined logo."""
    out = tmp_path / name
    (out / "graphics").mkdir(parents=True)
    pages = {
        "G01": ("lower_third", "G01-lower-third-maria.html", "<html><body><div class=plate>Maria</div></body></html>"),
        "G02": ("stat", "G02-stat-40.html", "<html><body><b>40</b></body></html>"),
        "G03": ("lower_third", "G03-lower-third-ana.html", "<html><body><div class=plate>Ana</div></body></html>"),
        "G04": ("title_card", "G04-title-card.html", '<html><body><img src="data:image/png;base64,iVBORw0KGgo+/=" alt=""><h1>Launch</h1></body></html>'),
    }
    entries = []
    for gid, (kind, file, html) in pages.items():
        (out / "graphics" / file).write_text(html)
        entries.append({"id": gid, "kind": kind, "text": gid, "html": f"graphics/{file}"})
    (out / "graphics.json").write_text(json.dumps({"style": "x", "graphics": entries}))
    return out


def test_approved_designs_are_picked_from_an_earlier_run(tmp_path):
    from roughcut import presets

    run = _run_with_graphics(tmp_path, "Promo_roughcut")
    picked = presets.approved_from_run(run, [])  # default: the first two of different kinds
    assert [Path(p).name for p in picked] == ["G01-lower-third-maria.html", "G02-stat-40.html"]
    picked = presets.approved_from_run(run, ["g4", "G01", "3"])
    assert [Path(p).name for p in picked] == ["G04-title-card.html", "G01-lower-third-maria.html", "G03-lower-third-ana.html"]
    # The inlined logo goes back to brand:logo, so the designer isn't sent the image bytes.
    assert presets.approved_html(Path(picked[0])) == '<html><body><img src="brand:logo" alt=""><h1>Launch</h1></body></html>'
    with pytest.raises(presets.PresetError, match="no design for G09; it has G01, G02, G03, G04"):
        presets.approved_from_run(run, ["G09"])
    with pytest.raises(presets.PresetError, match="up to 3"):
        presets.approved_from_run(run, ["G1", "G2", "G3", "G4"])
    with pytest.raises(presets.PresetError, match="no graphics to approve"):
        presets.approved_from_run(tmp_path / "nothing here", [])


def test_a_preset_with_a_logo_still_checks_its_approved_designs(tmp_path):
    from roughcut import presets

    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    preset = presets.Preset(name="x", logo=str(logo), look=(str(tmp_path / "gone.html"),))
    assert "gone.html doesn't exist" in presets.check(preset)
    assert presets.check(preset, need_files=False) is None
    big = tmp_path / "big.html"
    big.write_text("<p>" + "x" * presets.LOOK_MAX_CHARS + "</p>")
    assert "too big" in presets.look_problem(str(big))
    assert "should be an .html" in presets.look_problem(str(logo))


def test_design_request_shows_the_approved_designs(tmp_path):
    from roughcut import presets

    run = _run_with_graphics(tmp_path, "r")
    pages = presets.approved_from_run(run, ["G1", "G4"])
    assert presets.design_style("navy and orange") == "navy and orange"
    text = presets.design_style("navy and orange", None, pages)
    assert text.startswith("navy and orange\n\nApproved designs:")
    assert '<approved_design n="1">\n<html><body><div class=plate>Maria</div>' in text and "<h1>Launch</h1>" in text
    assert presets.NO_LOGO_NOTE in text, "the approved title card had a logo; this run has none"
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    with_logo = presets.design_style("navy", str(logo), pages)
    assert "Brand logo:" in with_logo and presets.NO_LOGO_NOTE not in with_logo
    assert "base64" not in with_logo


def test_save_an_approved_look_and_build_with_it(footage, config, capture, tmp_path, capsys):
    from roughcut import presets
    from roughcut.cli import main

    run = _run_with_graphics(tmp_path, "OCVIBE_roughcut")
    assert main(["preset", "save", "venture-oc", "--look-from", str(run), "G04", "G01"]) == 0
    assert "approved look (2 designs)" in capsys.readouterr().out
    saved = presets.load("venture-oc")
    folder = config / "presets" / "venture-oc.look"
    assert [Path(p).parent for p in saved.look] == [folder, folder], "copies kept with the preset"
    assert [Path(p).name for p in saved.look] == ["1-G04-title-card.html", "2-G01-lower-third-maria.html"]
    assert "brand:logo" in Path(saved.look[0]).read_text() and "base64" not in Path(saved.look[0]).read_text()
    assert set(json.loads((config / "presets" / "venture-oc.json").read_text())) == {"look"}
    shutil.rmtree(run)  # the run folder can go; the preset has its own copies

    opts = _opts(capture, ["build", str(footage), "--preset", "venture-oc"])
    assert opts.graphics and list(opts.graphics_look) == list(saved.look)
    # One you name replaces the preset's.
    mine = tmp_path / "mine.html"
    mine.write_text("<html><body>mine</body></html>")
    opts = _opts(capture, ["build", str(footage), "--preset", "venture-oc", "--graphics-look", str(mine)])
    assert list(opts.graphics_look) == [str(mine)]
    assert main(["build", str(footage), "--graphics", "--graphics-look", str(tmp_path / "gone.html")]) == 1
    assert "gone.html doesn't exist" in capsys.readouterr().err

    # Saving other changes keeps the look; --no-look drops it and its copies.
    assert main(["preset", "save", "venture-oc", "-t", "2m"]) == 0
    assert presets.load("venture-oc").look == saved.look
    assert main(["preset", "save", "venture-oc", "--no-look"]) == 0
    assert presets.load("venture-oc").look == () and not folder.exists()
    assert main(["preset", "save", "venture-oc", "--look-from", str(tmp_path / "nope")]) == 1
    run = _run_with_graphics(tmp_path, "again")
    assert main(["preset", "save", "venture-oc", "--look-from", str(run)]) == 0
    assert main(["preset", "remove", "venture-oc"]) == 0
    assert not folder.exists()


def test_an_approved_look_reaches_the_designer_end_to_end(footage, config, tmp_path, monkeypatch, capsys):
    """Run 1 makes graphics, the owner tweaks one and approves it, run 2's designer matches it."""
    from conftest import CANNED_PLAN, FakeProvider
    from test_end_to_end import check_invariants, validate_dtd
    from test_graphics import GRAPHICS

    from roughcut import graphics, pipeline, presets
    from roughcut.cli import main

    if graphics.renderer_problem() is not None:
        pytest.skip("no Playwright browser to render with")
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = [GRAPHICS[1]]  # one short title card keeps the two renders quick
    provider = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    common = ["--no-menu", "--no-vision", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--format", "320x180"]
    first = tmp_path / "Promo one"
    assert main(["build", str(footage), "--graphics", "-o", str(first), *common]) == 0
    title = first / "graphics" / "G01-title-card.html"
    title.write_text(title.read_text().replace("#F26B21", "#123ABC"))  # the owner's tweak

    assert main(["preset", "save", "promo", "--from", "venture-oc", "--look-from", str(first), "G01"]) == 0
    provider.calls.clear()
    second = tmp_path / "Promo two"
    assert main(["build", str(footage), "--preset", "promo", "-o", str(second), *common]) == 0
    capsys.readouterr()
    request = next(c for c in provider.calls if c["schema_name"] == "graphics")["parts"][-1].text
    assert presets.LOOK_DIRECTION in request and "#123ABC" in request, "the tweaked design, as approved"
    plan_request = next(c for c in provider.calls if c["schema_name"] == "edit_plan")["parts"][-1].text
    assert "<approved_design" not in plan_request, "the planner doesn't need the HTML"
    assert "<approved_design" not in json.loads((second / "graphics.json").read_text())["style"], "the manifest keeps the plain style"
    assert sorted(p.name for p in (second / "graphics").glob("*.mov")) == ["G01-title-card.mov"]
    fcpxml = next(second.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    check_invariants(fcpxml)

"""Footage that changed after it was read, and a drive that doesn't answer when a cut is made again.

`render` and `revise` reuse the saved reading of the footage (words, pauses, lengths), so a clip that was trimmed,
re-exported or replaced in the meantime would be cut on times that no longer fit it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import SPEECH, make_clip

from roughcut import pipeline
from roughcut.analyze import Analysis
from roughcut.cli import main
from roughcut.media import fingerprint
from roughcut.pipeline import Options
from roughcut.plan import normalize
from roughcut.timeline import STYLES, build_timeline


def _cut(tmp_path: Path, names=("talk.mov",)):
    """A cut of real files in `tmp_path`, each read as it is now."""
    clips = []
    for i, name in enumerate(names, 1):
        clip = make_clip(f"V0{i}", seconds=20, sentences=SPEECH)
        path = tmp_path / name
        path.write_bytes(b"footage " * 100)
        clip.media.path, clip.media.name = str(path), name
        clip.media.fingerprint = fingerprint(path)
        clips.append(clip)
    analysis = Analysis(clips=clips)
    sections = [{"name": f"Part {i}", "purpose": "", "items": [s.id for s in c.segments[:3]], "broll": []} for i, c in enumerate(clips, 1)]
    plan = normalize({"title": "t", "sections": sections, "music": []}, analysis, source="fake:fake-model")
    tl = build_timeline(plan, analysis, style=STYLES["medium"], name="t", dialogue_level=None, fill=False)
    return tl, analysis, [Path(c.media.path) for c in clips]


def test_footage_that_is_still_the_file_that_was_read_says_nothing(tmp_path):
    tl, analysis, _ = _cut(tmp_path)
    assert pipeline.changed_media(analysis, tl) == []


@pytest.mark.parametrize("how", ["grew", "touched"])
def test_a_file_with_another_size_or_edit_date_is_named(tmp_path, how):
    tl, analysis, (path,) = _cut(tmp_path)
    if how == "grew":
        with path.open("ab") as f:
            f.write(b"more")
    else:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    (warning,) = pipeline.changed_media(analysis, tl)
    assert "talk.mov" in warning and "has changed since Roughcut read it" in warning and "Build the footage again" in warning


def test_several_changed_files_are_one_warning_naming_three(tmp_path):
    tl, analysis, paths = _cut(tmp_path, names=("a.mov", "b.mov", "c.mov", "d.mov"))
    for path in paths:
        path.write_bytes(b"replaced")
    (warning,) = pipeline.changed_media(analysis, tl)
    assert warning.startswith("4 files this cut uses have changed") and "a.mov, b.mov, c.mov and 1 more" in warning


def test_a_file_the_cut_does_not_use_is_not_checked(tmp_path):
    tl, analysis, (path,) = _cut(tmp_path)
    spare = make_clip("V09", seconds=10)
    spare.media.path, spare.media.fingerprint = str(tmp_path / "spare.mov"), "not what the file is"
    analysis.clips.append(spare)
    assert pipeline.changed_media(analysis, tl) == []


def test_a_file_that_is_gone_is_missing_medias_to_say_not_changed_medias(tmp_path):
    tl, analysis, (path,) = _cut(tmp_path)
    path.unlink()
    assert pipeline.changed_media(analysis, tl) == []
    (warning,) = pipeline.missing_media(analysis, tl)
    assert "no longer where the footage was read" in warning and "talk.mov" in warning


def test_a_drive_that_does_not_answer_is_waited_on_once(tmp_path, monkeypatch):
    tl, analysis, paths = _cut(tmp_path, names=("a.mov", "b.mov", "c.mov"))
    for path in paths:
        path.write_bytes(b"replaced")  # changed, but the drive won't say so
    calls = []

    def stalled(fn, *args, seconds, what=""):
        calls.append(what)
        raise pipeline.Stalled(what)

    monkeypatch.setattr(pipeline, "bounded", stalled)
    assert pipeline.changed_media(analysis, tl) == []
    assert calls == ["a.mov"]


def test_footage_on_a_drive_that_does_not_answer_is_said_and_is_not_called_a_song(tmp_path, monkeypatch):
    tl, analysis, _ = _cut(tmp_path, names=("a.mov", "b.mov"))

    def stalled(fn, *args, seconds, what=""):
        raise pipeline.Stalled(what)

    monkeypatch.setattr(pipeline, "bounded", stalled)
    (warning,) = pipeline.missing_media(analysis, tl)
    assert "The drive or share holding 2 files this cut uses didn't answer" in warning
    assert "Venture Studio" not in warning and "a.mov" in warning


def test_a_recut_says_when_the_footage_changed_since_it_was_read(footage, tmp_path):
    shoot = tmp_path / "shoot"
    shutil.copytree(footage, shoot)
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    assert main(["build", str(shoot), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none", "--cache-dir", str(cache)]) == 0

    fresh = pipeline.rerender(out, Options(inputs=[], style="tight"))
    assert not [w for w in fresh.warnings if "has changed since Roughcut read" in w or "have changed since Roughcut read" in w]

    with (shoot / "talk.mov").open("ab") as f:  # trimmed, re-exported or replaced since
        f.write(b"\0" * 64)
    again = pipeline.rerender(out, Options(inputs=[], style="tight"))
    (warning,) = [w for w in again.warnings if "changed since Roughcut read" in w]
    assert "talk.mov" in warning and "Build the footage again" in warning
    assert "talk.mov" in (out / "edit_report.md").read_text() and "changed since Roughcut read" in (out / "edit_report.md").read_text()

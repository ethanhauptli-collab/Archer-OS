"""One run at a time in an output folder (roughcut/locking.py)."""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from roughcut import locking, pipeline
from roughcut.cli import main
from roughcut.locking import LOCK_NAME, FolderBusy, folder_lock
from roughcut.pipeline import Options, RoughcutError

HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
print("ready", flush=True)
time.sleep(120)
"""


@pytest.fixture
def other_run(tmp_path):
    """A second process that holds a folder's lock until the test lets go."""
    procs = []

    def hold(folder: Path):
        proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(folder / LOCK_NAME)], stdout=subprocess.PIPE, text=True)
        procs.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        return proc

    yield hold
    for proc in procs:
        proc.kill()
        proc.wait()


def _free(folder: Path) -> bool:
    """Could another process take the folder's lock right now?"""
    fd = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_the_lock_is_held_for_the_block_and_let_go_after_it(tmp_path):
    with folder_lock(tmp_path):
        assert not _free(tmp_path)
    assert _free(tmp_path)
    assert (tmp_path / LOCK_NAME).read_bytes() == b""  # never written to: a folder's contents don't change from run to run


def test_the_lock_is_let_go_when_the_run_fails(tmp_path):
    with pytest.raises(RuntimeError):
        with folder_lock(tmp_path):
            raise RuntimeError("the run broke")
    assert _free(tmp_path)


def test_a_run_inside_a_run_is_only_counted(tmp_path):
    with folder_lock(tmp_path):
        with folder_lock(tmp_path):
            assert not _free(tmp_path)
        assert not _free(tmp_path)  # the outer run still holds it
    assert _free(tmp_path)


def test_another_process_holding_the_lock_is_refused_with_a_message(tmp_path, other_run):
    other_run(tmp_path)
    with pytest.raises(FolderBusy, match="Another Roughcut run is already writing"):
        with folder_lock(tmp_path):
            raise AssertionError("must not run")


def test_a_folder_that_is_not_there_is_not_locked_or_made(tmp_path):
    missing = tmp_path / "nope"
    with folder_lock(missing):
        pass
    assert not missing.exists()


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.ENOTSUP, errno.EINVAL, errno.EIO])
def test_a_drive_that_cannot_lock_never_stops_a_run(tmp_path, monkeypatch, code):
    def no(fd, op):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(locking.fcntl, "flock", no)
    ran = []
    with folder_lock(tmp_path):
        ran.append(True)
    assert ran == [True]


def test_a_drive_that_does_not_answer_is_not_waited_on_and_leaves_no_lock_behind(tmp_path, monkeypatch):
    real = fcntl.flock
    answer = threading.Event()

    def slow(fd, op):
        answer.wait(10)
        return real(fd, op)

    monkeypatch.setattr(locking.fcntl, "flock", slow)
    monkeypatch.setattr(locking, "LOCK_SECONDS", 0.2)
    t0 = time.monotonic()
    ran = []
    with folder_lock(tmp_path):
        ran.append(True)
    assert ran == [True] and time.monotonic() - t0 < 5
    monkeypatch.setattr(locking.fcntl, "flock", real)
    answer.set()  # the drive answers at last: the run is long over, so the lock it grants must not stay held
    for _ in range(100):
        if _free(tmp_path):
            break
        time.sleep(0.05)
    assert _free(tmp_path)


def test_a_recut_in_a_folder_another_run_is_writing_is_refused_and_changes_nothing(footage, tmp_path, other_run):
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != LOCK_NAME}
    proc = other_run(out)
    with pytest.raises(RoughcutError, match="already writing"):
        pipeline.rerender(out, Options(inputs=[], style="tight"))
    assert {p.name: p.read_bytes() for p in out.iterdir() if p.is_file() and p.name != LOCK_NAME} == before  # not even the log was touched
    proc.kill()
    proc.wait()
    assert pipeline.rerender(out, Options(inputs=[], style="tight")).version == 2


def test_a_build_into_a_folder_another_run_is_writing_is_refused_before_anything_is_logged(footage, tmp_path, other_run, capsys):
    out = tmp_path / "out"
    out.mkdir()
    other_run(out)
    code = main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")])
    assert code == 1 and "already writing" in capsys.readouterr().err
    assert not (out / "roughcut.log").exists() and not list(out.glob("*.fcpxml"))

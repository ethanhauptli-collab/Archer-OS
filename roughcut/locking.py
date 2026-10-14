"""One run at a time in an output folder.

A build, re-cut, revision or graphics finish numbers its version from `versions.json`, moves earlier cuts into `history/` and
prunes: two of them at once (the app and Terminal, or a run the app was closed on that is still going) would pick the same
version and take files out from under each other. So each holds an advisory lock on `.roughcut.lock` in the folder for as long
as it writes there.

The lock is the operating system's `flock`, which goes away with the process that holds it: a crash or a cancelled run leaves
nothing behind to clear. Where a drive can't lock (some network shares and exFAT volumes answer "not supported"), or doesn't
answer in time, the run goes ahead without it: a lock must never be the reason a cut can't be made. The file stays empty and is
never rewritten, so a folder's contents don't change from one run to the next.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # not a Unix: nothing to lock with
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = ".roughcut.lock"
LOCK_SECONDS = 10.0  # a network drive gets this long to open and lock the file

_held: dict[str, list] = {}  # folder -> [file descriptor, how many times this process holds it]
_guard = threading.Lock()


class FolderBusy(Exception):
    """Another process is writing in the folder."""


def _acquire(folder: str, box: dict, guard: threading.Lock, done: threading.Event) -> None:
    """Open and lock the file, in a thread: the call can block in the kernel on a share that stops answering."""
    fd = None
    try:
        fd = os.open(os.path.join(folder, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)  # not inherited by the programs this run starts
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as e:  # noqa: BLE001 - handed to the caller
        if fd is not None:
            os.close(fd)
        box["error"] = e
        done.set()
        return
    with guard:
        if box.get("abandoned"):  # the caller gave up waiting: don't keep a lock nobody will release
            os.close(fd)
        else:
            box["fd"] = fd
    done.set()


def _take(folder: str) -> int | None:
    """The locked file's descriptor; None when the folder isn't there or can't be locked; FolderBusy when someone else holds it."""
    if not os.path.isdir(folder):
        return None
    box: dict = {}
    guard = threading.Lock()
    done = threading.Event()
    threading.Thread(target=_acquire, args=(folder, box, guard, done), daemon=True, name="folder-lock").start()
    if not done.wait(LOCK_SECONDS):
        with guard:
            box["abandoned"] = True
            return box.get("fd")  # None unless it finished in the instant between the wait and now
    err = box.get("error")
    if isinstance(err, BlockingIOError):
        raise FolderBusy(f"Another Roughcut run is already writing in {folder}. Wait for it to finish (or quit it), then try again.")
    return None if err else box.get("fd")  # any other error is a drive that can't lock: the run goes ahead, and reports its own problems


def _join(key: str) -> list | None:
    """The entry for a folder this process already holds, counted once more; None when it doesn't."""
    with _guard:
        entry = _held.get(key)
        if entry:
            entry[1] += 1
        return entry


def _release(fd: int) -> None:
    for step in (lambda: fcntl.flock(fd, fcntl.LOCK_UN), lambda: os.close(fd)):
        try:
            step()
        except OSError:
            pass


@contextmanager
def folder_lock(folder: str | Path):
    """Hold the folder's lock for the `with` block; raises FolderBusy when another process holds it.

    Held again by the same process (a run that calls another) it is only counted. A folder that doesn't exist is not
    locked and not created: the caller's own check says what is wrong with it.
    """
    if fcntl is None:
        yield
        return
    key = os.path.realpath(os.fspath(folder))
    entry = _join(key)
    if not entry:
        try:
            fd = _take(key)
        except FolderBusy:
            entry = _join(key)  # another thread of this process took it an instant ago: that is not another process
            if not entry:
                raise
        else:
            if fd is None:
                yield
                return
            with _guard:
                entry = _held.setdefault(key, [fd, 0])
                entry[1] += 1
    try:
        yield
    finally:
        with _guard:
            entry[1] -= 1
            last = entry[1] <= 0
            if last:
                _held.pop(key, None)
        if last:
            _release(entry[0])

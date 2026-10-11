"""Machine-readable progress for the Mac app (`--progress-json`).

One JSON object per line on stdout. Every line has an "event" key:

  {"event": "stage", "stage": "transcribe", "message": "Transcribing IMG_1.MOV", "current": 2, "total": 9}
  {"event": "log", "message": "probed 12 files in 0.4s"}
  {"event": "done", "result": {...}}          # see pipeline.result_summary
  {"event": "error", "message": "...", "resume": "roughcut render '...'", "kind": "usage_limit"}

Stages, in order: scan, probe, silence, transcribe, vision, plan, cut, graphics, write.
They only ever move forward; `error` (when there is one) is the last line.
Human-readable logging still goes to stderr. Off by default, so the CLI's
normal output is unchanged.
"""

from __future__ import annotations

import contextlib
import faulthandler
import json
import sys
import threading
import time

STAGES = ("scan", "probe", "silence", "transcribe", "vision", "plan", "cut", "graphics", "write")

_enabled = False
_lock = threading.Lock()


def enable(on: bool = True) -> None:
    global _enabled
    _enabled = on


def enabled() -> bool:
    return _enabled


def emit(event: str, **fields) -> None:
    _touch()
    if not _enabled:
        return
    line = json.dumps({"event": event, **fields}, default=str)
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def stage(name: str, message: str = "", current: int | None = None, total: int | None = None) -> None:
    global _stage
    what = message or name
    if _stage is None or _stage[0] != what:  # progress ticks of the same step keep its start time
        _stage = (what, time.monotonic())
    fields: dict = {"stage": name, "message": message}
    if current is not None:
        fields["current"] = current
    if total is not None:
        fields["total"] = total
    emit("stage", **fields)


# ------------------------------------------------------------------ heartbeat and stall dump
#
# A long run can sit in one step for many minutes (an Opus plan, a big render).
# While a run is going, a watcher thread logs "still working (6m10s): planning
# the edit with claude-opus-5-5" whenever nothing else was logged for a minute,
# so Terminal, the app and roughcut.log show it's alive and what it's waiting on.
# If nothing at all happens for STALL_SECONDS, every thread's stack is written to
# roughcut.log, so a freeze shows exactly where it is. Waiting on a model request
# or an outside program (marked `external`) is not a freeze: those have their own
# timeouts, and a long Opus plan can legitimately take more than ten minutes.

HEARTBEAT_SECONDS = 60.0
STALL_SECONDS = 600.0

_activities: dict[str, tuple[str, float, bool]] = {}  # key -> (what, started, external)
_stage: tuple[str, float] | None = None
_last_output = time.monotonic()  # last real output: the heartbeat speaks after a quiet minute
_last_moved = time.monotonic()  # last sign the run is moving: the stall clock
_last_beat = 0.0
_watch: dict = {}  # thread, stop event, stall file


def activity(key: str, text: str, *, external: bool = False) -> None:
    """Something that may take a while has started (a model request, a render...).

    `external`: waiting on a model request or another program that has its own
    timeout. While one is running, a long silence isn't treated as a stall.
    """
    with _lock:
        _activities[key] = (text, time.monotonic(), external)
    _touch()


def done(key: str) -> None:
    with _lock:
        _activities.pop(key, None)
    _touch()


@contextlib.contextmanager
def doing(text: str, key: str | None = None, *, external: bool = False):
    """`with progress.doing("planning the edit with opus"):` marks an activity for the heartbeat."""
    key = key or f"{text}#{threading.get_ident()}"
    activity(key, text, external=external)
    try:
        yield
    finally:
        done(key)


def current_activity() -> tuple[str, float] | None:
    """The most recently started activity still running, else the current stage."""
    with _lock:
        if _activities:
            what, since, _ = max(_activities.values(), key=lambda a: a[1])
            return what, since
    return _stage


def _waiting_outside() -> bool:
    with _lock:
        return any(a[2] for a in _activities.values())


def _touch() -> None:
    """Real output or a change of activity: the run is moving. (The heartbeat's own lines don't count.)"""
    global _last_output
    if _watch.get("thread") is threading.current_thread():
        return
    _last_output = time.monotonic()
    _moved()


def _moved() -> None:
    """The run is moving: restart the stall clock (not the heartbeat's)."""
    global _last_moved
    _last_moved = time.monotonic()
    _arm_stall_dump()


_arm_lock = threading.RLock()  # re-entrant: the SIGTERM handler may run while the main thread holds it


def _arm_stall_dump() -> None:
    with _arm_lock:
        out = _watch.get("file")
        if out is None:
            return
        try:
            faulthandler.dump_traceback_later(_watch["stall"], repeat=False, file=out)
        except (ValueError, OSError, RuntimeError):
            pass


def _elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _beat(stop: threading.Event, interval: float) -> None:
    global _last_beat
    from .analyze import log  # lazy: analyze imports this module

    told_stall = None
    while not stop.wait(min(5.0, max(0.05, interval / 4))):
        if _waiting_outside():
            _moved()  # a model request or outside program, bounded by its own timeout: not a freeze
        now = time.monotonic()
        if now - max(_last_output, _last_beat) < interval:
            continue
        _last_beat = now
        current = current_activity()
        what, since = current if current else ("working", now)
        stalled = _watch.get("file") is not None and now - _last_moved >= _watch.get("stall", STALL_SECONDS)
        try:
            log(f"still working ({_elapsed(now - since)}): {what}")
            if stalled and told_stall != _last_moved:
                told_stall = _last_moved
                log(f"nothing has moved for {_elapsed(now - _last_moved)}: roughcut.log now has a stack trace of every thread (include it if you report this)")
        except Exception:  # noqa: BLE001 - the heartbeat must never take the run down
            pass


def start_watch(log_path=None, *, interval: float | None = None, stall: float | None = None) -> None:
    """Start the heartbeat (and the stall dump into `log_path`) for a run. Restarts any earlier watch."""
    global _last_output, _last_moved, _stage
    stop_watch()
    _last_output = _last_moved = time.monotonic()
    _stage = None
    with _lock:
        _activities.clear()
    _watch["stall"] = stall if stall is not None else STALL_SECONDS
    if log_path is not None:
        try:
            _watch["file"] = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - held for faulthandler
        except OSError:
            _watch["file"] = None
    _arm_stall_dump()
    stop = threading.Event()
    thread = threading.Thread(target=_beat, args=(stop, interval if interval is not None else HEARTBEAT_SECONDS), name="roughcut-heartbeat", daemon=True)
    _watch.update(thread=thread, stop=stop)
    thread.start()


def stop_watch() -> None:
    """End of a run: stop the heartbeat and cancel the stall dump."""
    stop, thread = _watch.pop("stop", None), _watch.pop("thread", None)
    _watch.pop("stall", None)
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2)
    with _arm_lock:
        out = _watch.pop("file", None)
        try:
            faulthandler.cancel_dump_traceback_later()
        except (ValueError, RuntimeError):
            pass
    if out is not None:
        try:
            out.close()
        except OSError:
            pass

"""Machine-readable progress for the Mac app (`--progress-json`).

One JSON object per line on stdout. Every line has an "event" key:

  {"event": "stage", "stage": "transcribe", "message": "Transcribing IMG_1.MOV", "current": 2, "total": 9}
  {"event": "log", "message": "probed 12 files in 0.4s"}
  {"event": "done", "result": {...}}          # see pipeline.result_summary
  {"event": "error", "message": "..."}

Stages, in order: scan, probe, silence, transcribe, vision, plan, cut, write.
Human-readable logging still goes to stderr. Off by default, so the CLI's
normal output is unchanged.
"""

from __future__ import annotations

import json
import sys
import threading

STAGES = ("scan", "probe", "silence", "transcribe", "vision", "plan", "cut", "graphics", "write")

_enabled = False
_lock = threading.Lock()


def enable(on: bool = True) -> None:
    global _enabled
    _enabled = on


def enabled() -> bool:
    return _enabled


def emit(event: str, **fields) -> None:
    if not _enabled:
        return
    line = json.dumps({"event": event, **fields}, default=str)
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def stage(name: str, message: str = "", current: int | None = None, total: int | None = None) -> None:
    fields: dict = {"stage": name, "message": message}
    if current is not None:
        fields["current"] = current
    if total is not None:
        fields["total"] = total
    emit("stage", **fields)

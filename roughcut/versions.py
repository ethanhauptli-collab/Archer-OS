"""Output files that are never half-written."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file in the same folder and a rename.

    A crash, a full disk or a cancelled run leaves either the old file or the
    new one, never a truncated mix that Final Cut or `roughcut render` can't read.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:  # mkstemp makes the file private; keep the usual read-for-everyone mode
            os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

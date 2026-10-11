"""Text roughcut didn't write itself.

Tool output and outside files aren't always UTF-8: cameras store metadata in
older encodings (a "25°C" or a GPS "33°47'" tag is byte 0xB0 in Latin-1, and
ffmpeg and ffprobe print tags as stored), a brief saved from an old editor may
be Windows-1252 or Mac Roman, and on SD cards and other non-Mac drives macOS
writes a binary "._name" companion beside every file it creates. None of
that may stop a run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# macOS Finder/AppleDouble litter: "._clip.mov" resource-fork shadows and folder settings.
_JUNK_NAMES = {".DS_Store", ".localized", "Icon\r"}


def is_junk(path: str | Path) -> bool:
    """A file macOS leaves beside real ones: "._name" companions on non-Mac drives, .DS_Store."""
    name = Path(path).name
    return name.startswith("._") or name in _JUNK_NAMES


def real_files(paths) -> list[Path]:
    """`paths` without macOS's "._" companions and .DS_Store files."""
    return [Path(p) for p in paths if not is_junk(p)]


def run_text(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with output decoded as UTF-8, any other byte replaced instead of raising.

    For ffmpeg, ffprobe, git, `security` and `claude`: their own text is UTF-8,
    but they echo file metadata and names as stored.
    """
    kwargs.setdefault("capture_output", True)
    return subprocess.run(cmd, encoding="utf-8", errors="replace", **kwargs)


def decode_user_text(data: bytes) -> str:
    """Text a person saved: UTF-8 (with or without a BOM), UTF-16 with a BOM, else Windows-1252 or Mac Roman."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        # Windows-1252 leaves five bytes undefined; old Mac files use them, so fall back to Mac Roman.
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("mac_roman")


def read_user_text(path: str | Path) -> str:
    return decode_user_text(Path(path).expanduser().read_bytes())


def read_text_lenient(path: str | Path) -> str:
    """A file that should be UTF-8 (one roughcut wrote, or an FCPXML); stray bytes become U+FFFD."""
    return Path(path).read_bytes().decode("utf-8", errors="replace")

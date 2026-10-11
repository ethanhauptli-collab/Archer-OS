"""Numbered versions of a run's outputs, and files that are never half-written.

Every build, render and revise writes a new version into the same output
folder: `<name> v3.fcpxml` with the project "<name> - Rough Cut v3 (tight,
Opus)". The newest version's files sit at the top of the folder; when the
next one is written, the previous version's text files (FCPXML, plan, paper
edit, graphics designs) are copied into `history/v2 <date time>/`. The last
KEEP of those are kept, plus every graphics clip they use, so a cut already
imported into Final Cut never loses media underneath it.

Nothing is moved when a run starts. A run that stops early leaves the
previous version (and `roughcut render`) working.

`versions.json` lists the versions. A folder written before versions existed
has none; its cut counts as version 1.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

VERSIONS = "versions.json"
HISTORY = "history"
KEEP = 5  # earlier versions kept in history/
# Text outputs that belong to one version (the FCPXML is listed in its entry).
VERSION_FILES = ("plan.json", "prompt.md", "edit_report.md", "edit_report.html", "graphics.json", "result.json")


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


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read(out_dir: Path) -> list[dict]:
    try:
        data = json.loads((out_dir / VERSIONS).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [v for v in data.get("versions", []) if isinstance(v, dict) and isinstance(v.get("n"), int)]


def _save(out_dir: Path, entries: list[dict]) -> None:
    atomic_write(out_dir / VERSIONS, json.dumps({"versions": entries}, indent=1))


def entries(out_dir: Path) -> list[dict]:
    """Every version, oldest first. A folder from before versions has one: its existing cut."""
    found = _read(out_dir)
    if found:
        return found
    legacy = sorted(out_dir.glob("*.fcpxml"), key=lambda p: p.stat().st_mtime)
    if legacy and (out_dir / "plan.json").is_file():
        created = datetime.fromtimestamp(legacy[-1].stat().st_mtime).isoformat(timespec="seconds")
        return [{"n": 1, "created": created, "command": "build", "fcpxml": legacy[-1].name, "files": [p.name for p in legacy], "legacy": True}]
    return []


def current(out_dir: Path) -> dict | None:
    found = entries(out_dir)
    return found[-1] if found else None


def next_number(out_dir: Path) -> int:
    cur = current(out_dir)
    return (cur["n"] if cur else 0) + 1


def _stamp(created: str) -> str:
    try:
        return datetime.fromisoformat(created).strftime("%Y-%m-%d %H.%M")
    except (TypeError, ValueError):
        return "undated"


def snapshot(out_dir: Path, before: int) -> None:
    """Copy the current version's files into history/ before version `before` replaces them.

    Copies, never moves, and only once per version, so it is safe to call at
    every write (and again after a run that stopped half-way).
    """
    found = entries(out_dir)
    if not found or found[-1]["n"] >= before:
        return
    cur = found[-1]
    if cur.get("snapshot") and (out_dir / cur["snapshot"]).is_dir():
        return
    dest = out_dir / HISTORY / f"v{cur['n']} {_stamp(cur.get('created', ''))}"
    dest.mkdir(parents=True, exist_ok=True)
    names = list(VERSION_FILES) + list(cur.get("files") or [cur.get("fcpxml") or ""])
    for name in dict.fromkeys(n for n in names if n):
        src = out_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    # The graphics' HTML designs (the rendered .mov files stay where they are).
    try:
        manifest = json.loads((out_dir / "graphics.json").read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    for g in manifest.get("graphics", []):
        rel = str(g.get("html") or "")
        if rel and (out_dir / rel).is_file() and ".." not in Path(rel).parts:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_dir / rel, dest / rel)
    cur["snapshot"] = str(dest.relative_to(out_dir))
    _save(out_dir, found)


def record(out_dir: Path, entry: dict) -> None:
    """Add version entry["n"] (or update it, for a cut rewritten after its graphics finish)."""
    found = [v for v in entries(out_dir) if v["n"] != entry["n"]]
    found.append({"created": _now(), **entry})
    found.sort(key=lambda v: v["n"])
    _save(out_dir, found)


def finish(out_dir: Path, n: int, *, prune: bool = True) -> None:
    """After version n is written: take earlier cuts off the top level, then trim history."""
    found = entries(out_dir)
    keep_top = next((v.get("fcpxml") for v in found if v["n"] == n), None)
    for v in found:
        if v["n"] >= n or not v.get("snapshot"):
            continue  # only files already safe in history/ leave the top level
        for name in v.get("files") or [v.get("fcpxml") or ""]:
            path = out_dir / name
            if name and name != keep_top and path.suffix == ".fcpxml" and path.is_file():
                path.unlink()
    if prune:
        prune_history(out_dir)
        prune_graphics(out_dir)


def prune_history(out_dir: Path, keep: int = KEEP) -> list[str]:
    """Delete history beyond the last `keep` versions. Returns what was removed."""
    found = entries(out_dir)
    snaps = [v for v in found if v.get("snapshot")]
    removed = []
    for v in snaps[: max(0, len(snaps) - keep)]:
        path = out_dir / v["snapshot"]
        if path.is_dir() and path.resolve().parent == (out_dir / HISTORY).resolve():
            shutil.rmtree(path, ignore_errors=True)
        removed.append(v.pop("snapshot"))
        v["pruned"] = True
    if removed:
        _save(out_dir, found)
    return removed


_SRC = re.compile(r'src="file://[^"]*?/graphics/([^"/]+)"')


def referenced_graphics(out_dir: Path) -> set[str]:
    """Graphics clips used by the current cut or any version still in history."""
    files = list(out_dir.glob("*.fcpxml")) + list((out_dir / HISTORY).glob("*/*.fcpxml"))
    names: set[str] = set()
    for f in files:
        try:
            names.update(unquote(m) for m in _SRC.findall(f.read_text(encoding="utf-8")))
        except OSError:
            continue
    return names


def prune_graphics(out_dir: Path) -> list[str]:
    """Delete rendered graphics no kept version uses (and leftovers of interrupted renders).

    HTML designs the current version doesn't use go too: history/ has a copy
    of every earlier version's designs.
    """
    folder = out_dir / "graphics"
    if not folder.is_dir():
        return []
    keep = referenced_graphics(out_dir)
    try:
        manifest = json.loads((out_dir / "graphics.json").read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    keep |= {Path(str(g.get("html") or "")).name for g in manifest.get("graphics", [])}
    removed = []
    for f in list(folder.glob("*.mov")) + list(folder.glob("*.html")):
        if f.name not in keep:
            f.unlink(missing_ok=True)
            removed.append(f.name)
    return removed


# ------------------------------------------------------------------ names


def planner_label(source: str) -> str:
    """'anthropic:claude-opus-5-5' -> 'Opus'; the stringout or --provider none -> 'no AI'."""
    if not source or source == "heuristic":
        return "no AI"
    model = source.split(":", 1)[-1].strip()
    m = re.match(r"claude-(opus|sonnet|haiku|fable|mythos)\b", model)
    return m.group(1).title() if m else safe_name(model)


def safe_name(text: str) -> str:
    """Final Cut and Finder names: no ':' or '/'."""
    return re.sub(r"\s*[:/]\s*", " - ", text).strip() or "Roughcut"

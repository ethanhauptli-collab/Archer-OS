"""Numbered versions of a run's outputs, and files that are never half-written.

Every build, render and revise writes a new version into the same output
folder: `<name> v3.fcpxml` with the project "<name> - Rough Cut v3 (tight,
Opus)". The newest version's files sit at the top of the folder; when the
next one is written, the previous version's text files (FCPXML, plan, paper
edit, graphics designs) are copied into `history/v2 <date time>/`. The last
KEEP of those are kept. Rendered graphics clips are never deleted once any
version used them (versions.json remembers each version's clips even after its
history folder goes), so a cut already imported into Final Cut never loses
media underneath it.

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

from .textio import read_text_lenient, real_files

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


def slug(text: str) -> str:
    """A run's name as a file name: what `<name> v3.fcpxml` (and, before versions, `<name>.fcpxml`) starts with."""
    s = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"\s+", " ", s) or "Roughcut"


# 255 bytes (the longest file name, counted so it fits on any disk) less " v99999.fcpxml" and the
# ".<name>.<8 random>.tmp" atomic_write writes first.
SHORT_SLUG_BYTES = 255 - len(" v99999.fcpxml") - len("..12345678.tmp")


def short_slug(text: str) -> str:
    """slug(text) cut to SHORT_SLUG_BYTES of UTF-8, at a character boundary.

    Only for a name the disk refused as too long (a long folder name or --name), so every name
    that could be written keeps its file names from one version to the next.
    """
    s = slug(text)
    raw = s.encode("utf-8")
    if len(raw) <= SHORT_SLUG_BYTES:
        return s
    return raw[:SHORT_SLUG_BYTES].decode("utf-8", errors="ignore").rstrip() or "Roughcut"


def _legacy_cut(out_dir: Path) -> Path | None:
    """The cut an earlier release wrote into a folder from before versions: `<slug(name)>.fcpxml`.

    Only that file is roughcut's. Any other FCPXML beside it (an export from Final Cut,
    a hand-fixed copy) is the user's, and is never moved into history/ or deleted.
    """
    try:
        name = json.loads((out_dir / "plan.json").read_text()).get("name")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    cut = out_dir / f"{slug(name)}.fcpxml" if isinstance(name, str) and name.strip() else None
    try:
        return cut if cut is not None and cut.is_file() else None
    except OSError:  # a name too long for the disk: no release could have written it
        return None


def entries(out_dir: Path) -> list[dict]:
    """Every version, oldest first. A folder from before versions has one: its existing cut."""
    found = _read(out_dir)
    if found:
        return found
    if not (out_dir / "plan.json").is_file() or not real_files(out_dir.glob("*.fcpxml")):
        return []
    cut = _legacy_cut(out_dir)
    if cut is None:  # renamed or never written: still version 1, but no file of it to take off the top level
        created = datetime.fromtimestamp((out_dir / "plan.json").stat().st_mtime).isoformat(timespec="seconds")
        return [{"n": 1, "created": created, "command": "build", "fcpxml": None, "files": [], "legacy": True}]
    created = datetime.fromtimestamp(cut.stat().st_mtime).isoformat(timespec="seconds")
    return [{"n": 1, "created": created, "command": "build", "fcpxml": cut.name, "files": [cut.name], "legacy": True}]


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
    if entry.get("fcpxml") and "graphics" not in entry:
        entry = {**entry, "graphics": graphics_in([out_dir / entry["fcpxml"]])}
    found.append({"created": _now(), **entry})
    found.sort(key=lambda v: v["n"])
    _save(out_dir, found)


def finish(out_dir: Path, n: int, *, prune: bool = True) -> None:
    """After version n is written: take earlier cuts off the top level, then trim history."""
    found = entries(out_dir)
    keep_top = next((v.get("fcpxml") for v in found if v["n"] == n), None)
    keep_subs = next((v.get("captions") for v in found if v["n"] == n), None)
    for v in found:
        if v["n"] >= n or not v.get("snapshot"):
            continue  # only files already safe in history/ leave the top level
        name = str(v.get("fcpxml") or "")  # the version's own cut, never another file that happened to be there
        path = out_dir / name
        if name and name != keep_top and path.suffix == ".fcpxml" and path.parent == out_dir and path.is_file():
            path.unlink()
        subs = str(v.get("captions") or "")  # and its captions
        path = out_dir / subs
        if subs and subs != keep_subs and path.suffix == ".srt" and path.parent == out_dir and path.is_file():
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
        if "graphics" not in v:  # a folder from before versions: remember what its cut used before the copy goes
            v["graphics"] = graphics_in(path.glob("*.fcpxml"))
        if path.is_dir() and path.resolve().parent == (out_dir / HISTORY).resolve():
            shutil.rmtree(path, ignore_errors=True)
        removed.append(v.pop("snapshot"))
        v["pruned"] = True
    if removed:
        _save(out_dir, found)
    return removed


_SRC = re.compile(r'src="file://[^"]*?/graphics/([^"/]+)"')


def graphics_in(files) -> list[str]:
    """The graphics clips (file names in graphics/) these FCPXML files use."""
    names: set[str] = set()
    for f in real_files(files):
        try:
            names.update(unquote(m) for m in _SRC.findall(read_text_lenient(f)))
        except OSError:
            continue
    return sorted(names)


def referenced_graphics(out_dir: Path) -> set[str]:
    """Graphics clips any version ever used: the FCPXML files still here, plus what versions.json remembers."""
    names = set(graphics_in(list(out_dir.glob("*.fcpxml")) + list((out_dir / HISTORY).glob("*/*.fcpxml"))))
    for v in _read(out_dir):
        names.update(str(n) for n in v.get("graphics") or [])
    return names


def adopt_old_graphics(out_dir: Path) -> list[str]:
    """Clips rendered before their names carried the design digest (`G01-title-card.mov`).

    Gives each a second name with the digest graphics.json recorded for it (a
    hard link, else a copy), so a re-cut reuses the clip instead of rendering
    it again; the old name stays for the cut already in Final Cut.
    """
    try:
        manifest = json.loads((out_dir / "graphics.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    made = []
    for e in manifest.get("graphics", []) if isinstance(manifest, dict) else []:
        if not isinstance(e, dict):
            continue
        digest, name = str(e.get("digest") or ""), Path(str(e.get("file") or "")).name
        src = out_dir / "graphics" / name
        if len(digest) < 6 or not name.endswith(".mov") or src.stem.endswith(digest[:6]) or not src.is_file():
            continue
        dest = src.with_name(f"{src.stem}-{digest[:6]}.mov")
        if dest.exists():
            continue
        try:
            os.link(src, dest)
        except OSError:
            try:
                shutil.copy2(src, dest)
            except OSError:
                dest.unlink(missing_ok=True)
                continue
        made.append(dest.name)
    return made


def prune_graphics(out_dir: Path) -> list[str]:
    """Delete only what no version ever used: leftovers of interrupted or failed renders.

    A clip an earlier version used stays even after that version's history
    folder is gone: Final Cut reads imported media in place, so deleting it
    would take the graphics out of a project already in a library. HTML
    designs the current version doesn't use go: history/ keeps a copy of
    every kept version's designs. The preview page (graphics/preview.html)
    belongs to the current version: it stays while that version has rendered
    graphics, and goes (with its previews) when it has none.
    """
    from .graphics import DESIGN_FILE
    from .graphics_preview import FOLDER, PAGE

    folder = out_dir / "graphics"
    if not folder.is_dir():
        return []
    keep = referenced_graphics(out_dir)
    try:
        manifest = json.loads((out_dir / "graphics.json").read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {}
    entries = [g for g in manifest.get("graphics", []) if isinstance(g, dict)] if isinstance(manifest, dict) else []
    keep |= {Path(str(g.get("html") or "")).name for g in entries}
    removed = []
    designs = [f for f in folder.glob("*.html") if DESIGN_FILE.fullmatch(f.name)]
    for f in real_files(folder.glob("*.mov")) + designs:
        if f.name not in keep:
            f.unlink(missing_ok=True)
            removed.append(f.name)
    if not any(g.get("file") for g in entries):  # this version has no graphics: no page showing an earlier one's
        page = folder / PAGE
        if page.is_file():
            page.unlink(missing_ok=True)
            removed.append(page.name)
        shutil.rmtree(folder / FOLDER, ignore_errors=True)
    return removed


# ------------------------------------------------------------------ names


def planner_label(source: str) -> str:
    """'anthropic:claude-opus-5-5' -> 'Opus'; the stringout or --provider none -> 'no AI'."""
    if not source or source == "heuristic":
        return "no AI"
    model = source.split(":", 1)[-1].strip()
    m = re.match(r"claude-(opus|sonnet|haiku|fable|mythos)\b", model)
    return m.group(1).title() if m else safe_name(model)


_CONTROL = re.compile(r"\s*[\x00-\x1f\x7f-\x9f]+\s*")  # tabs, newlines, DEL and the other control characters
NAME_LIMIT = 200  # characters: the longest project name Venture Studio's Project history accepts (Final Cut takes more)


def _no_controls(text: str) -> str:
    """Control characters (and the spaces around them) become one space; text without any stays exactly as it is."""
    return _CONTROL.sub(" ", text).strip() if _CONTROL.search(text) else text


def safe_name(text: str) -> str:
    """Final Cut and Finder names: no ':' or '/', and no control characters (a tab or newline from a folder name or --name)."""
    return re.sub(r"\s*[:/]\s*", " - ", _no_controls(text)).strip() or "Roughcut"


def fit_name(base: str, suffix: str = "", limit: int = NAME_LIMIT) -> str:
    """`base + suffix` in at most `limit` characters. A longer base is shortened (ending in '…') so the suffix,
    which carries the version, stays whole; a name that fits is returned as it is."""
    if len(base) + len(suffix) <= limit:
        return base + suffix
    room = limit - len(suffix) - 1
    if room < 1:  # a suffix that long (an odd model name) is cut too
        return (base + suffix)[: limit - 1].rstrip() + "…"
    return base[:room].rstrip() + "…" + suffix


def final_cut_name(text: str, limit: int = NAME_LIMIT) -> str:
    """A project or event name as the FCPXML gets it: no control characters, at most `limit` characters.

    Venture Studio's Project history review rejects a whole file whose project name has a control character
    or runs past 200 characters. Names that already fit are written byte for byte as they are.
    """
    return fit_name(_no_controls(text), "", limit)

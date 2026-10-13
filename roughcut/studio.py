"""Venture Studio, read only: its catalog snapshots, settings and kit exports.

Venture Studio is the owner's Mac app for creative assets (sound effects,
transitions, overlays, titles, LUTs) on network folders it calls libraries. It
keeps its catalog in a live SQLite database that Roughcut never opens: any
SQLite open of a database in WAL mode, even read-only, can leave files beside
it or read a half-written state. Instead Roughcut reads one of two things:

- a plain byte copy of Studio's newest daily catalog snapshot
  (`backups/<id>.sqlite`, with a `<id>.json` manifest carrying its size and
  SHA-256). The copy lands in Roughcut's own cache, is checked against the
  manifest, and only that copy is opened, read-only and immutable;
- a kit file the owner exported from Studio (`load_kit`).

Nothing in Studio's data folder is ever opened with SQLite, written, renamed
or locked (`catalog.worker-lock` is never opened: Studio's restore needs it),
and this module creates nothing under a library root (a build whose output
folder is inside one still writes there, with the warning `output_guard`
gives, unless the Terminal offer moved it). Every problem comes back as a
`StudioUnavailable` reason, which callers report and never turn into a
failed run. Snapshots are made about daily, and only while Studio is open, so
the data can be a day old: `status` says how old, and flags a snapshot Studio
took just before restoring an older catalog.

This module is the only code that looks inside Studio's folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

STUDIO_ENV = "ROUGHCUT_STUDIO_DIR"  # Studio's data folder, for tests or a second Mac account
BUNDLE_ID = "com.hauptli.venturestudio"
CACHE_FOLDER = "venture-studio"  # inside Roughcut's cache: our copies of Studio's snapshots
SCHEMA_VERSION = 3  # the catalog format this release reads (PRAGMA user_version and the manifest's schema_version)
MANIFEST_MAX = 1 << 20  # bytes: a snapshot manifest, or maintenance.json
SNAPSHOT_MAX = 512 << 20  # bytes: the largest snapshot copied
KIT_MAX = 32 << 20  # bytes: Studio's own limit for a kit export
KIT_ENTRIES_MAX = 10_000
KIT_FORMAT = 1
KIT_KIND = "venture_studio_project_manifest"
ONLINE_SECONDS = 3.0  # one deadline for every library's online check together: a network mount can hang a stat
KEEP_COPIES = 2  # snapshot copies kept in the cache
STALE_HOURS = 48  # older than this, status says the snapshot is old (Studio makes one a day while it's open)
VOLUMES = "/Volumes"  # macOS mount points: an empty one left by a disconnected share is on the startup disk's device
RESTORE_NAME = "Before catalog restore"
IMPORTED_PREFIX = "Imported:"

# The columns Roughcut reads, checked by name (Studio adds columns over time, and their order varies).
REQUIRED_COLUMNS = {
    "libraries": {"id", "path", "name"},
    "assets": {
        "id", "library_id", "relative_path", "name", "display_name", "extension", "kind", "category", "provider", "license",
        "tags", "notes", "duration", "favorite", "missing", "browse_visible", "package_id", "template_resource",
    },
    "collections": {"id", "name"},
    "collection_assets": {"collection_id", "asset_id"},
    "project_kits": {"id", "name"},
    "project_kit_assets": {"kit_id", "asset_id"},
}
# What Studio lists as an item (its sidebar count): present, browse-visible, and a Motion template's leader file only.
BROWSE = "a.missing=0 AND a.browse_visible=1 AND (a.package_id IS NULL OR a.package_id=a.id)"
# Audio Studio hands off as a file on its own (drag, copy): present, browse-visible, and no part of a Motion template.
STANDALONE_AUDIO = "a.kind='audio' AND a.missing=0 AND a.browse_visible=1 AND a.package_id IS NULL AND a.template_resource=0"
# Studio's Project history tables: optional (Studio makes them the first time it records a project), checked by name when there.
HISTORY_COLUMNS = {"project_history_events": {"id", "kind"}, "project_history_items": {"event_id", "asset_id", "output_path"}}


class StudioUnavailable(Exception):
    """Studio's data can't be read right now. `reason` says why, in words for the editor.

    `silent`: Studio isn't on this Mac at all, which needs no message in a run.
    """

    def __init__(self, reason: str, *, silent: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.silent = silent


def studio_dir() -> Path:
    """Studio's data folder: $ROUGHCUT_STUDIO_DIR, else ~/Library/Application Support/com.hauptli.venturestudio."""
    override = os.environ.get(STUDIO_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / BUNDLE_ID


def installed(folder: Path | None = None) -> bool:
    try:
        return stat.S_ISDIR(os.stat(folder or studio_dir()).st_mode)
    except OSError:
        return False


def _require(folder: Path | None) -> Path:
    folder = folder or studio_dir()
    if not installed(folder):
        raise StudioUnavailable("not installed", silent=True)
    return folder


def copies_dir(cache_dir: Path | None = None) -> Path:
    """Where Roughcut keeps its copies: `<Roughcut's cache>/venture-studio` (the same cache root as the analysis)."""
    if cache_dir is None:
        from .analyze import default_cache_dir

        cache_dir = default_cache_dir()
    return Path(cache_dir).expanduser().absolute() / CACHE_FOLDER


def _os_reason(e: OSError, what: str) -> str:
    if isinstance(e, PermissionError):
        return f"macOS didn't let Roughcut read {what} (Privacy & Security may be asking; allow it, then try again)"
    return f"couldn't read {what}: {e.strerror or e}"


def _read_small(path: Path, limit: int) -> bytes:
    """A regular file's bytes, at most `limit`: never follows a symlink, never waits on a FIFO or device."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_size > limit:
            raise ValueError("too large")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------------ snapshots


@dataclass(frozen=True)
class Snapshot:
    """One of Studio's catalog snapshots, from its manifest (`backups/<id>.json`)."""

    id: str
    name: str
    created_at: int  # unix seconds
    schema_version: int
    bytes: int
    sha256: str
    records: int | None = None  # the manifest's asset_count: every catalog row, missing files and template parts included

    @property
    def order(self) -> tuple:
        """Newest first sorts on this, descending: the time, then the id's numbers (so -10 comes after -9)."""
        return (self.created_at, tuple(int(p) for p in self.id.split("-") if p.isdigit()))

    @property
    def before_restore(self) -> bool:
        return self.name.strip() == RESTORE_NAME

    @property
    def imported(self) -> bool:
        return self.name.strip().startswith(IMPORTED_PREFIX)

    def when(self) -> str:
        return _when(self.created_at)

    def age_hours(self, now: float | None = None) -> float:
        return max(0.0, ((now if now is not None else time.time()) - self.created_at) / 3600)


_ID = re.compile(r"[0-9-]+\Z")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _manifest(path: Path, stem: str) -> Snapshot | None:
    """A manifest, type-checked; None for anything else (Studio writes them in place, so one can be half-written)."""
    try:
        data = json.loads(_read_small(path, MANIFEST_MAX).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: nested deeper than json can read
        return None
    if not isinstance(data, dict):
        return None
    sid, name, created, schema, size, digest = (data.get(k) for k in ("id", "name", "created_at", "schema_version", "bytes", "sha256"))
    if sid != stem or not isinstance(name, str) or not _is_int(created) or not _is_int(schema) or not _is_int(size) or size < 0:
        return None
    if not isinstance(digest, str) or not _HEX64.match(digest):
        return None
    records = data.get("asset_count")
    return Snapshot(sid, name, created, schema, size, digest.lower(), records if _is_int(records) else None)


def snapshots(folder: Path | None = None) -> list[Snapshot]:
    """Studio's snapshots, newest first. Only regular `<digits-and-dashes>.json` manifests whose id matches the name count."""
    backups = _require(folder) / "backups"
    found = []
    try:
        with os.scandir(backups) as it:
            entries = list(it)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise StudioUnavailable(_os_reason(e, "Venture Studio's snapshot folder")) from e
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        stem = entry.name[: -len(".json")]
        if not stem or not _ID.match(stem):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > MANIFEST_MAX:
            continue
        snap = _manifest(Path(entry.path), stem)
        if snap is not None:
            found.append(snap)
    return sorted(found, key=lambda s: s.order, reverse=True)


class _Refused(Exception):
    pass


@dataclass
class Copy:
    """Roughcut's verified copy of a Studio snapshot, and what was skipped to get it."""

    path: Path
    snapshot: Snapshot
    newest: Snapshot  # the newest snapshot listed (the same one, unless it couldn't be used)
    skipped: list[tuple[Snapshot, str]] = field(default_factory=list)  # newer snapshots refused, and why
    reused: bool = False  # an earlier copy matched the manifest, so nothing was read from Studio's folder

    def notes(self, now: float | None = None) -> list[str]:
        """What the editor should know about this copy: never hidden behind an older snapshot that worked."""
        out = []
        if self.skipped:
            snap, why = self.skipped[0]
            out.append(f"Venture Studio's newest catalog snapshot ({snap.when()}) couldn't be used: {why}. Roughcut read the one from {self.snapshot.when()}.")
        if self.snapshot.before_restore:
            out.append(
                f"The newest catalog snapshot is the copy Studio saved just before a catalog restore ({self.snapshot.when()}): it holds the catalog "
                "from before the restore, not the one Studio shows now. In Studio, Back up catalog now refreshes it."
            )
        elif self.snapshot.imported:
            out.append(
                f"The catalog snapshot Roughcut read was imported into Studio ({self.snapshot.name.strip()!r}, dated {self.snapshot.when()}); "
                "it may not be the catalog Studio shows now. In Studio, Back up catalog now refreshes it."
            )
        age = self.snapshot.age_hours(now)
        if age > STALE_HOURS:
            out.append(
                f"The catalog snapshot is {age / 24:.0f} days old: Studio makes one a day, only while it's open. "
                "Open Studio, or click Back up catalog now in it, for a fresh one."
            )
        return out


def _sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _header_ok(head: bytes) -> bool:
    """A self-contained SQLite file: the format string, and read/write versions 1 (rollback journal, not WAL)."""
    return head[:16] == b"SQLite format 3\x00" and head[18:20] == b"\x01\x01"


def _cached(path: Path, snap: Snapshot) -> bool:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size != snap.bytes:
            return False
        with open(path, "rb") as f:
            head = f.read(100)
    except OSError:
        return False
    return _header_ok(head) and _sha256(path) == snap.sha256


def _copy_one(backups: Path, snap: Snapshot, cache: Path) -> tuple[Path, bool]:
    """Roughcut's copy of `snap`: an earlier one that still matches, or a fresh byte copy checked on the way in."""
    if snap.schema_version > SCHEMA_VERSION:
        raise _Refused(f"it's in catalog format {snap.schema_version}, newer than this Roughcut reads (run `roughcut update`)")
    if snap.schema_version != SCHEMA_VERSION:
        raise _Refused(f"it's in catalog format {snap.schema_version}, which this Roughcut doesn't read")
    if not 0 < snap.bytes <= SNAPSHOT_MAX:
        raise _Refused("its manifest gives a size Roughcut doesn't copy")
    src = backups / f"{snap.id}.sqlite"
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        raise _Refused("its file is gone (Studio archived or removed it)") from None
    except OSError as e:
        raise _Refused(_os_reason(e, "it")) from e
    if not stat.S_ISREG(st.st_mode):
        raise _Refused("it isn't a regular file")
    if st.st_size != snap.bytes:
        raise _Refused("its size doesn't match its manifest")
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(f"{src}{suffix}"):
            raise _Refused("SQLite files sit beside it, so it may be changing (Studio refuses it too)")
    mine = cache / f"{snap.id}.sqlite"
    if _cached(mine, snap):
        return mine, True
    cache.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise _Refused("its file is gone (Studio archived or removed it)") from None
    except OSError as e:
        raise _Refused(_os_reason(e, "it")) from e
    partial = None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise _Refused("it changed while Roughcut opened it")
        out_fd, partial = tempfile.mkstemp(dir=cache, prefix=f".{snap.id}.", suffix=".partial")
        h, n, head = hashlib.sha256(), 0, b""
        with os.fdopen(out_fd, "wb") as out:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > snap.bytes:
                    raise _Refused("it's larger than its manifest says")
                if len(head) < 100:
                    head += chunk[: 100 - len(head)]
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if n != snap.bytes or h.hexdigest() != snap.sha256:
            raise _Refused("its checksum doesn't match its manifest (changed or damaged since Studio made it)")
        if not _header_ok(head):
            raise _Refused("it isn't a self-contained SQLite snapshot")
        os.replace(partial, mine)
        partial = None
        return mine, False
    finally:
        os.close(fd)
        if partial is not None:
            try:
                os.unlink(partial)
            except OSError:
                pass


def _prune(cache: Path, keep: Path) -> None:
    """Keep `keep` and the newest other copy; delete older copies. Only Roughcut's own files in its own cache folder."""
    found = []
    try:
        with os.scandir(cache) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        found.append((e.name, e.path, e.stat(follow_symlinks=False).st_mtime))
                except OSError:
                    continue
    except OSError:
        return
    copies = sorted(
        ((name, path, mtime) for name, path, mtime in found if name.endswith(".sqlite") and _ID.match(name[: -len(".sqlite")]) and name != keep.name),
        key=lambda f: f[2],
        reverse=True,
    )
    stale = [path for _, path, _ in copies[KEEP_COPIES - 1 :]]
    # A partial copy another run is still writing is young; one an interrupted run left behind is not.
    stale += [path for name, path, mtime in found if name.startswith(".") and name.endswith(".partial") and time.time() - mtime > 3600]
    for path in stale:
        try:
            os.unlink(path)
        except OSError:
            pass


def newest_copy(folder: Path | None = None, cache_dir: Path | None = None) -> Copy:
    """A verified copy of the newest usable snapshot, in Roughcut's cache. Newer ones it couldn't use are in `skipped`."""
    folder = _require(folder)
    listed = snapshots(folder)
    if not listed:
        raise StudioUnavailable("Venture Studio has no catalog snapshot yet (in Studio, click Back up catalog now)")
    cache = copies_dir(cache_dir)
    skipped: list[tuple[Snapshot, str]] = []
    for snap in listed:
        try:
            path, reused = _copy_one(folder / "backups", snap, cache)
        except _Refused as e:
            skipped.append((snap, str(e)))
            continue
        except OSError as e:
            raise StudioUnavailable(_os_reason(e, "Roughcut's cache folder")) from e
        _prune(cache, path)
        return Copy(path, snap, listed[0], skipped, reused)
    snap, why = skipped[0]
    raise StudioUnavailable(f"no catalog snapshot Roughcut can read; the newest ({snap.when()}) was refused: {why}")


def open_copy(path: Path) -> sqlite3.Connection:
    """Open Roughcut's own copy, read-only and immutable (no journal or lock file, even in the cache), and check its format."""
    uri = Path(path).absolute().as_uri() + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise StudioUnavailable(f"couldn't open Roughcut's copy of the catalog: {e}") from e
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            newer = isinstance(version, int) and version > SCHEMA_VERSION
            raise StudioUnavailable(
                f"the catalog is in format {version}, " + ("newer than this Roughcut reads (run `roughcut update`)" if newer else "which this Roughcut doesn't read")
            )
        for table, columns in REQUIRED_COLUMNS.items():
            have = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
            missing = sorted(columns - have)
            if missing:
                raise StudioUnavailable(
                    f"the catalog's {table} table has no {', '.join(missing)}: Studio's catalog changed shape (run `roughcut update`)"
                    if have else f"the catalog has no {table} table: Studio's catalog changed shape (run `roughcut update`)"
                )
    except StudioUnavailable:
        con.close()
        raise
    except sqlite3.Error as e:
        con.close()
        raise StudioUnavailable(f"couldn't read Roughcut's copy of the catalog: {e}") from e
    return con


@contextmanager
def catalog(copy: Copy):
    """`with catalog(copy) as con:` the opened copy, closed after."""
    con = open_copy(copy.path)
    try:
        yield con
    finally:
        con.close()


# ------------------------------------------------------------------ what the catalog holds


@dataclass(frozen=True)
class Library:
    """A folder Studio catalogs (its "library"): stored as Studio found it, canonical."""

    id: int
    path: str
    name: str


@dataclass(frozen=True)
class Asset:
    """A catalog item, with its original file's absolute path (library root + "/" + its path below the root)."""

    id: int
    path: str
    library: str  # the library's name
    library_root: str
    relative_path: str
    name: str
    display_name: str
    extension: str
    kind: str  # from the extension: video, audio, image, lut, template, font, archive, file
    category: str  # transition, title, effect, generator, overlay, lut, video, audio, image, font, template, other
    provider: str
    license: str
    tags: tuple[str, ...]
    notes: str
    duration: float | None  # seconds; Studio fills it only for files it has previewed
    favorite: bool
    missing: bool

    @property
    def title(self) -> str:
        """The name Studio shows."""
        return self.display_name or self.name


def _query(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        raise StudioUnavailable(f"couldn't read Roughcut's copy of the catalog: {e}") from e


def libraries(con: sqlite3.Connection) -> list[Library]:
    return [Library(int(i), str(p), str(n)) for i, p, n in _query(con, "SELECT id, path, name FROM libraries ORDER BY id")]


def browse_count(con: sqlite3.Connection) -> int:
    """How many items Studio lists (its sidebar count), not every catalog row."""
    return int(_query(con, f"SELECT count(*) FROM assets a WHERE {BROWSE}")[0][0])


def collections(con: sqlite3.Connection) -> list[str]:
    return [str(n) for (n,) in _query(con, "SELECT name FROM collections ORDER BY name COLLATE NOCASE")]


def kits(con: sqlite3.Connection) -> list[str]:
    return [str(n) for (n,) in _query(con, "SELECT name FROM project_kits ORDER BY name COLLATE NOCASE")]


def original_path(root: str, relative: str) -> str | None:
    """Library root + "/" + the path below it, or None when either isn't a plain path (Studio's own rule)."""
    if not isinstance(root, str) or not isinstance(relative, str) or not root.startswith("/") or not relative:
        return None
    if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    if any(part in (".", "..") for part in root.split("/")[1:]):
        return None
    return root.rstrip("/") + "/" + relative if root != "/" else "/" + relative


def assets(
    con: sqlite3.Connection,
    *,
    collection: str | None = None,
    kit: str | None = None,
    kinds: tuple[str, ...] | None = None,
    categories: tuple[str, ...] | None = None,
    favorites: bool = False,
    browse: bool = True,
) -> list[Asset]:
    """Catalog items, by Studio's collection or kit name (any case), kind, category or favorite.

    `browse` (the default) applies Studio's own list filter: present files, browse-visible, a Motion
    template's leader file only. Items whose path isn't a plain path below their library are left out.
    """
    where, args, joins = [], [], []
    if browse:
        where.append(BROWSE)
    _in_group(con, where, args, joins, collection=collection, kit=kit)
    for column, values in (("kind", kinds), ("category", categories)):
        if values:
            where.append(f"a.{column} IN ({', '.join('?' * len(values))})")
            args += list(values)
    if favorites:
        where.append("a.favorite = 1")
    return _select(con, where, args, joins)


def _in_group(con: sqlite3.Connection, where: list, args: list, joins: list, *, collection: str | None, kit: str | None) -> None:
    """Only the members of Studio's collection or kit with this name (any case)."""
    if collection is not None:
        found = _query(con, "SELECT id FROM collections WHERE name = ? COLLATE NOCASE", (collection,))
        if not found:
            raise StudioUnavailable(f"Venture Studio has no collection named {collection!r}")
        joins.append("JOIN collection_assets ca ON ca.asset_id = a.id")
        where.append("ca.collection_id = ?")
        args.append(found[0][0])
    if kit is not None:
        found = _query(con, "SELECT id FROM project_kits WHERE name = ? COLLATE NOCASE", (kit,))
        if not found:
            raise StudioUnavailable(f"Venture Studio has no kit named {kit!r}")
        joins.append("JOIN project_kit_assets ka ON ka.asset_id = a.id")
        where.append("ka.kit_id = ?")
        args.append(found[0][0])


def _select(con: sqlite3.Connection, where: list, args: list, joins: list) -> list[Asset]:
    sql = (
        "SELECT a.id, l.path, l.name, a.relative_path, a.name, a.display_name, a.extension, a.kind, a.category, a.provider, a.license, "
        "a.tags, a.notes, a.duration, a.favorite, a.missing FROM assets a JOIN libraries l ON l.id = a.library_id "
        + " ".join(joins)
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY a.id"
    )
    out = []
    for row in _query(con, sql, tuple(args)):
        aid, root, lib, rel, name, display, ext, kind, category, provider, lic, tags, notes, duration, fav, missing = row
        path = original_path(root, rel)
        if path is None:
            continue
        out.append(Asset(
            int(aid), path, str(lib), str(root), str(rel), str(name or ""), str(display or ""), str(ext or ""), str(kind or ""),
            str(category or ""), str(provider or ""), str(lic or ""), tuple(t.strip() for t in str(tags or "").split(",") if t.strip()),
            str(notes or ""), float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
            bool(fav), bool(missing),
        ))
    return out


# ------------------------------------------------------------------ audio for the music bed

MUSIC_SECONDS = 45.0  # a known length this long or longer: a song
SFX_SECONDS = 8.0  # shorter: a sound effect
FILE_SECONDS = 5.0  # one file's check on a library folder (a network share can stop answering)
SEARCH_TOKENS_MAX = 64
_SFX_WORDS = {
    "sfx", "fx", "foley", "whoosh", "swoosh", "swish", "woosh", "impact", "impacts", "riser", "risers", "hit", "hits", "boom", "booms",
    "stinger", "sting", "click", "clicks", "pop", "pops", "transition", "transitions", "glitch", "braam", "ding", "chime", "swipe",
}
_MUSIC_WORDS = {"music", "song", "songs", "track", "tracks", "score", "soundtrack", "theme", "instrumental", "bed", "ballad", "anthem", "melody"}


def music_guess(name: str, seconds: float | None = None, words=()) -> str:
    """'music', 'sfx' or 'unknown'. A known length decides (45 s or longer: a song; under 8 s: a sound effect);
    otherwise the name, its folders and tags do, when they point one way only ("Big Impact Anthem" points both)."""
    if seconds is not None:
        if seconds >= MUSIC_SECONDS:
            return "music"
        if seconds < SFX_SECONDS:
            return "sfx"
    text = " ".join([name, *words]).lower()
    tokens = set(re.findall(r"[^\W_]+", text))
    sfx = bool(tokens & _SFX_WORDS) or "sound effect" in text
    music = bool(tokens & _MUSIC_WORDS)
    if sfx != music:
        return "sfx" if sfx else "music"
    return "unknown"


def search_tokens(words) -> list[str]:
    """Studio's search words: the letters and digits of what was typed, split at everything else."""
    text = " ".join(str(w) for w in words)
    return [t for t in re.split(r"[^\w]|_", text) if t][:SEARCH_TOKENS_MAX]


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def standalone_audio(
    con: sqlite3.Connection, *, words=(), collection: str | None = None, kit: str | None = None, favorites: bool = False,
) -> list[Asset]:
    """Audio Studio hands off as a file of its own: present, browse-visible, never part of a Motion template.

    By Studio's collection or kit (any case), favorites, and search words: every word, as Studio searches (its
    full-text index of names, paths, tags, notes, providers, licenses and collections, each word a word start),
    or, when the copy has no usable index, anywhere in those (wildcards escaped). Words with no letters or digits find nothing.
    """
    where, args, joins = [STANDALONE_AUDIO], [], []
    _in_group(con, where, args, joins, collection=collection, kit=kit)
    if favorites:
        where.append("a.favorite = 1")
    tokens = search_tokens(words)
    if any(str(w).strip() for w in words) and not tokens:
        return []
    if tokens:
        found = _fts_ids(con, tokens)
        if found is not None:
            if not found:
                return []
            where.append(f"a.id IN ({', '.join('?' * len(found))})")
            args += sorted(found)
        else:
            fields = ("a.name", "a.display_name", "a.relative_path", "a.tags", "a.notes", "a.provider", "a.license")
            for token in tokens:
                pattern = _like(token)
                where.append(
                    "(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields)
                    + " OR a.id IN (SELECT xa.asset_id FROM collection_assets xa JOIN collections xc ON xc.id = xa.collection_id"
                    " WHERE xc.name LIKE ? ESCAPE '\\'))"
                )
                args += [pattern] * (len(fields) + 1)
    return _select(con, where, args, joins)


def _fts_ids(con: sqlite3.Connection, tokens: list[str]) -> set[int] | None:
    """The ids Studio's own full-text index finds for these words, or None when the copy has none this Python can read."""
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets_fts'").fetchone():
            return None
        query = " AND ".join(f'"{t}"*' for t in tokens)
        return {int(r[0]) for r in con.execute("SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?", (query,)).fetchall()}
    except sqlite3.Error:  # no FTS5 in this Python's SQLite, or an index it can't read
        return None


def audio_row(asset: Asset) -> dict:
    """One line of `roughcut studio music`: what a music bed needs to know, from the snapshot (no file is touched)."""
    from .media import AUDIO_EXTS

    ext = ("." + asset.extension.lower().lstrip(".")) if asset.extension else PurePosixPath(asset.path).suffix.lower()
    usable = ext in AUDIO_EXTS
    folders = PurePosixPath(asset.relative_path).parts[:-1]
    return {
        "id": asset.id, "name": asset.title, "file": PurePosixPath(asset.path).name, "path": asset.path, "library": asset.library,
        "seconds": asset.duration, "length": clock(asset.duration), "provider": asset.provider, "license": asset.license,
        "favorite": asset.favorite, "tags": list(asset.tags), "notes": asset.notes,
        "guess": music_guess(asset.title + " " + PurePosixPath(asset.path).stem, asset.duration, [*folders, *asset.tags]),
        "usable": usable, "why_not": None if usable else f"{ext or 'no extension'}: not an audio type Roughcut reads",
    }


def kit_audio_row(item: KitItem) -> dict:
    """A kit item as a listing row: a kit carries no length, tags or favorite."""
    from .media import AUDIO_EXTS

    ext = ("." + item.extension.lower().lstrip(".")) if item.extension else PurePosixPath(item.path).suffix.lower()
    usable = ext in AUDIO_EXTS
    return {
        "id": item.asset_id, "name": item.name, "file": PurePosixPath(item.path).name, "path": item.path, "library": "",
        "seconds": None, "length": "?", "provider": item.provider, "license": item.license, "favorite": False, "tags": [], "notes": "",
        "guess": music_guess(item.name + " " + PurePosixPath(item.path).stem, None, PurePosixPath(item.path).parts[1:-1]),
        "usable": usable, "why_not": None if usable else f"{ext or 'no extension'}: not an audio type Roughcut reads",
    }


def clock(seconds: float | None) -> str:
    """3:04 for a known length, '?' for none (Studio measures a file only once it has previewed it)."""
    if seconds is None or seconds < 0:
        return "?"
    whole = int(round(seconds))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}" if whole >= 3600 else f"{whole // 60}:{whole % 60:02d}"


def is_kit_file(name: str) -> bool:
    return name.lower().endswith(".json") and Path(name).expanduser().is_file()


def song(con: sqlite3.Connection, asset_id: int) -> Asset:
    """The catalog's audio item with this id, as Studio would hand it off; a StudioUnavailable naming what's wrong otherwise."""
    row = _query(
        con,
        "SELECT coalesce(nullif(a.display_name,''), a.name), a.kind, a.missing, a.browse_visible, a.package_id, a.template_resource "
        "FROM assets a WHERE a.id = ?",
        (asset_id,),
    )
    if not row:
        raise StudioUnavailable(f"Venture Studio's catalog has no item {asset_id} (`roughcut studio music` lists its audio)")
    name, kind, missing, visible, package, template = row[0]
    if kind != "audio":
        raise StudioUnavailable(f"item {asset_id} ({name}) isn't audio")
    if package is not None or template or not visible:
        raise StudioUnavailable(f"item {asset_id} ({name}) is part of a Motion template or its setup files, which Studio doesn't hand off on its own")
    if missing:
        raise StudioUnavailable(f"Studio's catalog marks item {asset_id} ({name}) missing")
    found = _select(con, ["a.id = ?"], [asset_id], [])
    if not found:
        raise StudioUnavailable(f"item {asset_id} ({name}) has a path Studio refuses (not a plain path below its library)")
    return found[0]


@dataclass
class Listing:
    """Studio audio for a music bed (`roughcut studio music`, --music-from): rows, and what they came from."""

    rows: list[dict]
    source: str  # "the catalog snapshot from 12 Oct 21:04 (14 h old)", or "the kit 'Promo kit'"
    kit_file: Path | None = None
    notes: list[str] = field(default_factory=list)


def _kit_rows(kit: Kit, words=()) -> list[dict]:
    tokens = [t.lower() for t in search_tokens(words)]
    rows = []
    for item in kit.items:
        if item.kind and item.kind != "audio":
            continue
        row = kit_audio_row(item)
        text = f"{item.name} {item.path} {item.provider} {item.license}".lower()
        if all(t in text for t in tokens):
            rows.append(row)
    return rows


def music_listing(
    words=(), *, collection: str | None = None, kit: str | None = None, favorites: bool = False,
    folder: Path | None = None, cache_dir: Path | None = None, now: float | None = None,
) -> Listing:
    """Studio's standalone audio, by collection, kit (its name in the catalog, or a kit file the owner exported),
    favorites and search words, from the snapshot copy: no file on a library folder is touched."""
    if kit is not None and is_kit_file(kit):
        if collection is not None or favorites:
            raise StudioUnavailable("a kit file has no collections or favorites: give the kit file alone")
        loaded = load_kit(kit)
        exported = f", exported {_when(loaded.exported_at)}" if loaded.exported_at else ""
        held = [f"{name}: {why}" for name, why in loaded.held_back]
        return Listing(_kit_rows(loaded, words), f"the kit {loaded.name!r}{exported}", loaded.file, held)
    copy = newest_copy(folder, cache_dir)
    with catalog(copy) as con:
        found = standalone_audio(con, words=words, collection=collection, kit=kit, favorites=favorites)
    rows = sorted((audio_row(a) for a in found), key=lambda r: (r["name"].casefold(), r["id"]))
    snap = copy.snapshot
    return Listing(rows, f"the catalog snapshot from {snap.when()} ({_age(snap.age_hours(now))})", None, copy.notes(now))


def music_source(name: str, *, folder: Path | None = None, cache_dir: Path | None = None) -> tuple[str | None, str | None]:
    """(collection, kit) for --music-from NAME: "collection:NAME" or "kit:NAME", a kit file, or a name that is only
    one of the two in the catalog (a name that's both must say which)."""
    text = name.strip()
    for prefix in ("collection:", "kit:"):
        if text.lower().startswith(prefix):
            value = text[len(prefix):].strip()
            if not value:
                raise StudioUnavailable(f"{text!r} names no {prefix[:-1]}")
            return (value, None) if prefix == "collection:" else (None, value)
    if is_kit_file(text):
        return None, text
    copy = newest_copy(folder, cache_dir)
    with catalog(copy) as con:
        is_collection = any(c.casefold() == text.casefold() for c in collections(con))
        is_kit = any(k.casefold() == text.casefold() for k in kits(con))
    if is_collection and is_kit:
        raise StudioUnavailable(f"both a collection and a kit are named {text!r} in Venture Studio: say collection:{text} or kit:{text}")
    if not (is_collection or is_kit):
        raise StudioUnavailable(f"Venture Studio has no collection or kit named {text!r}")
    return (text, None) if is_collection else (None, text)


_STOP = {
    "the", "and", "for", "with", "this", "that", "from", "are", "our", "you", "your", "its", "it's", "about", "into", "who", "what",
    "when", "where", "will", "was", "were", "has", "have", "had", "not", "but", "all", "any", "can", "out", "use", "one", "more",
    "keep", "make", "video", "promo", "cut", "edit", "clip", "clips", "line", "lines", "music", "song", "track", "audio",
}


def rank_songs(rows: list[dict], brief: str = "") -> list[dict]:
    """Favorites first, then the most tags and name words the brief also uses, then by name (deterministic)."""
    wanted = {w for w in re.findall(r"[^\W_]+", (brief or "").lower()) if len(w) >= 3} - _STOP

    def hits(row: dict) -> int:
        words = set(re.findall(r"[^\W_]+", " ".join([row.get("name") or "", row.get("file") or "", *(row.get("tags") or [])]).lower()))
        return len(words & wanted)

    return sorted(rows, key=lambda r: (not r.get("favorite"), -hits(r), (r.get("name") or "").casefold(), r.get("id") or 0))


def listing_lines(listing: Listing) -> list[str]:
    """`roughcut studio music`: a header with where the list is from, one song a line, and how to use one."""
    lines = [f"Venture Studio audio from {listing.source}." + (" In Studio, Back up catalog now refreshes the snapshot." if listing.kit_file is None else "")]
    lines += [f"! {n}" for n in listing.notes]
    if not listing.rows:
        return lines + ["No audio found."]
    width = max(len(str(r["id"])) for r in listing.rows)
    lines.append(f"{'ID':>{width}}  {'Length':>7}  {'Guess':<7}  Name (provider, license)  [tags]")
    for r in listing.rows:
        who = ", ".join(x for x in (r["provider"], r["license"]) if x)
        tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
        star = "* " if r["favorite"] else ""
        usable = "" if r["usable"] else f"  (not usable: {r['why_not']})"
        lines.append(f"{str(r['id']):>{width}}  {r['length']:>7}  {r['guess']:<7}  {star}{r['name']}" + (f" ({who})" if who else "") + tags + usable)
    n = len(listing.rows)
    first = next((r for r in listing.rows if r["usable"] and r["id"] is not None), None)
    lines.append(f"{n} file{'s' if n != 1 else ''}; * = favorite; length ? = Studio hasn't measured it yet.")
    if first is not None:
        lines.append(f"Use one under a cut: roughcut build FOLDER --music studio:{first['id']}")
    return lines


def reachable(path: str, seconds: float | None = None) -> str | None:
    """None when `path` is a regular file reached with no link anywhere along it (Studio's own rule for handing
    a file off); else why not. Given at most `seconds` (FILE_SECONDS): a network share that stops answering can't hold it."""
    from .textio import Stalled, bounded

    seconds = FILE_SECONDS if seconds is None else seconds

    def check() -> str | None:
        try:
            if os.path.realpath(path, strict=True) != path:
                return "a link or alias is in its path (Studio refuses those too)"
            st = os.stat(path)
        except FileNotFoundError:
            return "it isn't there (moved or renamed since the snapshot, or its network folder isn't connected)"
        except (OSError, ValueError) as e:
            return _os_reason(e, "it") if isinstance(e, OSError) else str(e)
        return None if stat.S_ISREG(st.st_mode) else "it isn't a regular file"

    try:
        return bounded(check, seconds=seconds, what="its folder")
    except Stalled:
        return f"its network folder didn't answer in {seconds:g} s"


def settings(folder: Path | None = None) -> dict:
    """Studio's maintenance settings, read once (absent or unreadable: Studio's defaults, both on)."""
    out = {"automatic_updates": True, "daily_backups": True}
    try:
        data = json.loads(_read_small(_require(folder) / "maintenance.json", MANIFEST_MAX).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, StudioUnavailable):
        return out
    found = data.get("settings") if isinstance(data, dict) else None
    if isinstance(found, dict):
        for key in out:
            if isinstance(found.get(key), bool):
                out[key] = found[key]
    return out


# ------------------------------------------------------------------ kits the owner exports


@dataclass(frozen=True)
class KitItem:
    asset_id: int | None
    name: str
    kind: str
    extension: str
    provider: str
    license: str
    path: str  # the original file's absolute path
    readiness: str  # ready / unknown / attention, as Studio last saw it


@dataclass
class Kit:
    """A kit Studio exported (a venture-project-*.json): its usable items, and the ones held back with why."""

    file: Path
    name: str
    description: str
    exported_at: int | None
    items: list[KitItem]
    held_back: list[tuple[str, str]]  # (name, why): missing in Studio, needing attention, or unreadable


def _plain_absolute(path) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/")[1:])


def load_kit(file: Path | str) -> Kit:
    """Read a kit export: format 1 of Studio's project manifest, items under `project.assets`."""
    path = Path(file).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))  # the file you chose: a link to it is fine
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StudioUnavailable(f"{path.name} isn't a file")
            if st.st_size > KIT_MAX:
                raise StudioUnavailable(f"{path.name} is larger than a Venture Studio kit can be (32 MB)")
            raw = b"".join(iter(lambda: os.read(fd, 1 << 20), b""))
        finally:
            os.close(fd)
    except OSError as e:
        raise StudioUnavailable(_os_reason(e, path.name)) from e
    if len(raw) > KIT_MAX:
        raise StudioUnavailable(f"{path.name} is larger than a Venture Studio kit can be (32 MB)")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        raise StudioUnavailable(f"{path.name} isn't a Venture Studio kit (not readable JSON)") from e
    if not isinstance(data, dict) or data.get("kind") != KIT_KIND:
        raise StudioUnavailable(f"{path.name} isn't a Venture Studio kit export")
    version = data.get("format_version")
    if not _is_int(version) or version != KIT_FORMAT:
        raise StudioUnavailable(
            f"{path.name} is a kit in format {version}, " + ("newer than this Roughcut reads (run `roughcut update`)" if _is_int(version) and version > KIT_FORMAT else "which this Roughcut doesn't read")
        )
    project = data.get("project")
    entries = project.get("assets") if isinstance(project, dict) else None
    if not isinstance(entries, list):
        raise StudioUnavailable(f"{path.name} has no kit items (no project.assets)")
    if len(entries) > KIT_ENTRIES_MAX:
        raise StudioUnavailable(f"{path.name} lists more items than Studio exports ({KIT_ENTRIES_MAX:,})")
    items, held = [], []
    for entry in entries:
        if not isinstance(entry, dict):
            held.append(("(unreadable entry)", "not an item"))
            continue
        name = entry.get("name") if isinstance(entry.get("name"), str) else "(unnamed)"
        ref = entry.get("original_reference")
        if not _plain_absolute(ref):
            held.append((name, "its file path isn't a plain absolute path"))
        elif entry.get("catalog_missing") is True:
            held.append((name, "Studio's catalog marks the file missing"))
        elif entry.get("readiness") == "attention":
            reason = entry.get("reason")
            held.append((name, f"Studio says it needs attention{': ' + reason if isinstance(reason, str) and reason else ''}"))
        else:
            text = lambda k: entry.get(k) if isinstance(entry.get(k), str) else ""  # noqa: E731
            items.append(KitItem(
                entry.get("asset_id") if _is_int(entry.get("asset_id")) else None, name, text("kind"), text("extension"),
                text("provider"), text("license"), ref, text("readiness") or "unknown",
            ))
    exported = data.get("exported_at")
    return Kit(
        path, str(project.get("name") or path.stem), str(project.get("description") or ""), exported if _is_int(exported) else None, items, held,
    )


# ------------------------------------------------------------------ folders: online, and what's inside them


def library_online(root: str) -> bool:
    """Studio's own test: the stored path is still itself (no symlink or other mount in its place), a folder,
    and not an empty mount point a disconnected share left behind under /Volumes."""
    if not isinstance(root, str) or not root.startswith("/"):
        return False
    try:
        if os.path.realpath(root, strict=True) != root:
            return False
        st = os.stat(root)
    except (OSError, ValueError):
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    volumes = VOLUMES.rstrip("/")
    if root != volumes and root.startswith(volumes + "/"):
        try:
            if os.stat(volumes).st_dev == st.st_dev:
                return False
        except OSError:
            pass
    return True


def online(roots: list[str], deadline: float = ONLINE_SECONDS) -> dict[str, bool | None]:
    """Which library folders are reachable: True, False, or None when the check didn't answer in time.

    Every folder is checked at once, each in its own daemon thread, under one shared deadline:
    a hung network mount can't hold a run (or `doctor`) longer than that.
    """
    results: dict[str, bool | None] = {r: None for r in roots}

    def check(root: str) -> None:
        try:
            results[root] = library_online(root)
        except Exception:  # noqa: BLE001 - a check that breaks reads as offline
            results[root] = False

    threads = [threading.Thread(target=check, args=(r,), daemon=True, name="studio-online") for r in dict.fromkeys(roots)]
    for t in threads:
        t.start()
    end = time.monotonic() + max(0.0, deadline)
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    return dict(results)


def _parts(path) -> tuple[str, ...]:
    return PurePosixPath(os.path.normpath(str(path))).parts


def _fold(part: str) -> str:
    return unicodedata.normalize("NFC", part).casefold()


_DUPLICATE_MOUNT = re.compile(r"(.+)-[0-9]+\Z")  # macOS mounts a share at /Volumes/<name>-1 when /Volumes/<name> is taken


def _usual_mount(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """('/', 'Volumes', 'Media-1', ...) as ('/', 'Volumes', 'Media', ...); None for a path not under such a mount point."""
    volumes = _parts(VOLUMES)
    if len(parts) <= len(volumes) or tuple(_fold(p) for p in parts[: len(volumes)]) != tuple(_fold(p) for p in volumes):
        return None
    m = _DUPLICATE_MOUNT.match(parts[len(volumes)])
    return parts[: len(volumes)] + (m.group(1),) + parts[len(volumes) + 1 :] if m else None


def _library_of(path, roots) -> tuple[str | None, str | None]:
    """(root, mount): the library root `path` is inside, and the numbered mount point it was found through, if it was."""
    roots = [r for r in roots if isinstance(r, str) and r.startswith("/")]  # an empty or relative row holds nothing
    target = _parts(path)
    for root in roots:
        parts = _parts(root)
        if len(parts) <= len(target) and target[: len(parts)] == parts:
            return root, None
    folded = tuple(_fold(p) for p in target)
    for root in roots:
        parts = _parts(root)
        if len(parts) <= len(target) and folded[: len(parts)] == tuple(_fold(p) for p in parts):
            return root, None
    usual = _usual_mount(target)
    if usual is not None:
        folded = tuple(_fold(p) for p in usual)
        for root in roots:
            parts = _parts(root)
            if len(parts) <= len(usual) and folded[: len(parts)] == tuple(_fold(p) for p in parts):
                return root, str(PurePosixPath(*target[: len(_parts(VOLUMES)) + 1]))
    return None, None


def inside_library(path, roots) -> str | None:
    """The library root `path` is inside (or is), else None. Folder names are compared whole, exactly first,
    then ignoring Unicode form and letter case (Mac and network volumes usually do): /Studio2 isn't inside /Studio.
    A share macOS mounted under a numbered name (/Volumes/Media-1, when /Volumes/Media was taken) counts as
    the library on /Volumes/Media. Only absolute roots count: a catalog row with an empty path holds nothing."""
    return _library_of(path, roots)[0]


# ------------------------------------------------------------------ the output guard


@dataclass
class Guard:
    """An output folder inside a Studio library, and a folder outside every library to use instead (or None)."""

    out_dir: Path
    library: Library
    message: str
    elsewhere: Path | None


def elsewhere_for(out_dir: Path, library: Library, roots: list[str], home: Path | None = None) -> Path | None:
    """~/Movies/Roughcut/<library name>/<the path below the library root>: unique per footage folder, so two
    shoots with the same folder name never share versions. The folder as it really is (links followed), and None
    when that's inside a library too (~/Movies linked into one) or `out_dir` isn't inside `library` at all."""
    if inside_library(out_dir, [library.path]) is None:
        return None
    below = _parts(out_dir)[len(_parts(library.path)) :]
    if not below:
        return None
    label = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", library.name).replace("/", "-").replace(":", "-").strip()
    if label in ("", ".", ".."):
        label = PurePosixPath(library.path).name or "Venture Studio"
    try:
        alt = ((home or Path.home()) / "Movies" / "Roughcut" / label / Path(*below)).resolve()
    except (OSError, RuntimeError):  # a link loop: no folder to offer
        return None
    return None if inside_library(alt, roots) is not None else alt


def output_guard(out_dir: Path, *, folder: Path | None = None, cache_dir: Path | None = None) -> tuple[Guard | None, str | None]:
    """(guard, note): `guard` when `out_dir` is inside a Studio library; `note` when Studio is there but couldn't be
    checked. Both None when Studio isn't installed. Never raises, never writes anywhere but Roughcut's cache."""
    try:
        copy = newest_copy(folder, cache_dir)
        with catalog(copy) as con:
            libs = libraries(con)
        conf = settings(folder)
    except StudioUnavailable as e:
        return None, None if e.silent else f"Couldn't check whether the output folder is inside a Venture Studio library: {e.reason}"
    except Exception as e:  # noqa: BLE001 - Studio never stops a run
        return None, f"Couldn't check whether the output folder is inside a Venture Studio library: {e}"
    roots = [lib.path for lib in libs]
    root, mount = _library_of(out_dir, roots)
    if root is None:
        return None, None
    lib = next(lib for lib in libs if lib.path == root)
    alt = elsewhere_for(out_dir, lib, roots)
    if mount:  # Studio sees the library as offline while the share is there, and catalogs the folder once it's back
        where = f"{lib.path}; probably the same share, mounted now as {mount} because its usual folder was taken"
        when = "once the share is back where Studio expects it" + ("" if conf["automatic_updates"] else " and Studio next scans it")
    else:
        where = lib.path
        when = "within about a minute while Studio is open" if conf["automatic_updates"] else "on its next scan"
    message = (
        f"The output folder is inside the Venture Studio library {lib.name!r} ({where}): Studio catalogs what Roughcut writes there {when} "
        "(this cut's FCPXML, graphics and history copies, which show up as missing after Roughcut prunes its history). "
        + (f"Keep Roughcut's output outside the library: -o {shlex.quote(str(alt))}" if alt else "Keep Roughcut's output outside the library (-o).")
    )
    return Guard(out_dir, lib, message, alt), None


# ------------------------------------------------------------------ status


def _when(stamp: int) -> str:
    try:
        moment = datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return "an unknown date"
    return moment.strftime("%d %b %H:%M" if moment.year == datetime.now().year else "%d %b %Y %H:%M").lstrip("0")


def _age(hours: float) -> str:
    if hours < 1:
        return "under an hour old"
    if hours < 48:
        return f"{hours:.0f} h old"
    return f"{hours / 24:.0f} days old"


def status(folder: Path | None = None, *, cache_dir: Path | None = None, offline: bool = False, now: float | None = None) -> dict:
    """What `roughcut studio status` and `doctor` show. Keys may be added, never removed (the app reads it)."""
    folder = folder or studio_dir()
    report: dict = {
        "installed": installed(folder), "available": False, "reason": None, "folder": str(folder), "snapshot": None,
        "items": None, "records": None, "libraries": [], "notes": [], "settings": None, "summary": "", "offline": offline,
    }
    if not report["installed"]:
        report["reason"] = "not installed"
        report["summary"] = "not installed"
        return report
    try:
        copy = newest_copy(folder, cache_dir)
        with catalog(copy) as con:
            libs = libraries(con)
            items = browse_count(con)
        conf = settings(folder)
    except StudioUnavailable as e:
        report["reason"] = e.reason
        report["summary"] = e.reason
        return report
    except Exception as e:  # noqa: BLE001 - never a crash in doctor or a run
        report["reason"] = f"couldn't read Venture Studio: {e}"
        report["summary"] = report["reason"]
        return report
    snap = copy.snapshot
    up = {} if offline else online([lib.path for lib in libs])
    report.update(
        available=True,
        snapshot={
            "id": snap.id, "name": snap.name, "created_at": snap.created_at, "when": snap.when(), "age_hours": round(snap.age_hours(now), 1),
            "before_restore": snap.before_restore, "imported": snap.imported, "copy": str(copy.path),
        },
        items=items,
        records=snap.records,
        libraries=[{"name": lib.name, "path": lib.path, "online": up.get(lib.path) if not offline else None} for lib in libs],
        notes=copy.notes(now) + ([] if conf["daily_backups"] else ["Daily catalog snapshots are off in Studio, so Roughcut only sees the catalog as of the last one."]),
        settings=conf,
    )
    report["summary"] = summary(report)
    return report


def _folders(report: dict) -> str:
    libs = report["libraries"]
    n = len(libs)
    text = f"{n} folder{'s' if n != 1 else ''}"
    if report.get("offline"):
        return text + (" (not checked)" if n else "")
    down = sum(1 for lib in libs if lib["online"] is False)
    slow = sum(1 for lib in libs if lib["online"] is None)
    if not n or not (down or slow):
        return text + ("" if not n else ", all online" if n > 1 else ", online")
    bits = ([f"{down} offline" if n > 1 else "offline"] if down else []) + ([f"{slow} not answering" if n > 1 else "not answering"] if slow else [])
    return text + ", " + ", ".join(bits)


def summary(report: dict) -> str:
    """One line: 'catalog snapshot from 12 Oct 21:04 (14 h old), 5,123 items, 6 folders, all online'."""
    if not report.get("available"):
        return report.get("reason") or "not available"
    snap = report["snapshot"]
    n = report["items"]
    text = f"catalog snapshot from {snap['when']} ({_age(snap['age_hours'])}), {n:,} item{'s' if n != 1 else ''}, {_folders(report)}"
    if snap["before_restore"]:
        text += "; it's the copy from before a catalog restore"
    return text


def status_lines(report: dict) -> list[str]:
    """`roughcut studio status`: one fact per line."""
    if not report["installed"]:
        return [f"Venture Studio: not installed (looked in {report['folder']})"]
    lines = [f"Venture Studio: {report['folder']}"]
    if not report["available"]:
        return lines + [f"Not available: {report['reason']}"]
    snap = report["snapshot"]
    lines.append(f"Catalog snapshot: {snap['when']} ({_age(snap['age_hours'])}), {snap['name']!r}")
    records = f" ({report['records']:,} catalog records in all)" if isinstance(report.get("records"), int) else ""
    lines.append(f"Items: {report['items']:,}{records}")
    lines.append(f"Folders: {len(report['libraries'])}")
    for lib in report["libraries"]:
        state = "not checked" if report.get("offline") else {True: "online", False: "offline", None: "not answering"}[lib["online"]]
        lines.append(f"  {lib['name']}: {lib['path']} ({state})")
    conf = report["settings"] or {}
    lines.append(f"Automatic updates: {'on' if conf.get('automatic_updates') else 'off'}; daily snapshots: {'on' if conf.get('daily_backups') else 'off'}")
    lines.append(f"Roughcut's copy: {snap['copy']}")
    lines += [f"! {note}" for note in report["notes"]]
    return lines


# ------------------------------------------------------------------ what Studio's Project history review will match
#
# Studio's Project history reads an FCPXML (Final Cut's export of a finished project) and records the catalog items
# each project in it uses. It compares paths as plain strings, exactly, and never looks at the files: the path a
# media-rep's file URL names, decoded, against each catalog item's library root joined with its path below the root
# (missing items too), and against the output paths of the packs Studio prepared. A path two items share is
# ambiguous and counts for neither; items are counted once each, however many files point at them. Roughcut predicts
# that for its own FCPXML from the snapshot copy, the same way, without touching any media file.

REVIEW_MAX_BYTES = 16 << 20
REVIEW_MAX_VISITS = 100_000
REVIEW_MAX_DEPTH = 64
REVIEW_MAX_PROJECTS = 100
NAME_MAX = 200
_PICKS = {"audio", "video", "title", "mc-clip", "ref-clip", "clip", "asset-clip", "sync-clip"}  # an audition's active pick
_REFERS = {"asset-clip", "video", "audio", "ref-clip", "mc-clip"}
_NOT_WALKED = {"metadata", "filter-video", "filter-audio", "adjust-transform", "timeMap"}
_SAME_WINDOW = {"sequence", "spine", "mc-angle"}
# Rust's str::trim: Unicode White_Space.
_WHITE = "\t\n\x0b\x0c\r \x85\xa0                　"
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|inf|infinity|nan)\Z", re.I)


class ReviewRefused(Exception):
    """Studio's review would refuse the whole file (or, for a catalog problem, every file): `reason` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def catalog_path(root, relative) -> str | None:
    """The path Studio's review keys a catalog item by: its library root joined with its path below the root,
    as Rust's Path::join does it (neither is tidied: "a//b" stays), or None for a row Studio refuses (a root
    that isn't absolute or has "..", a path below it that is empty, absolute, or has "./" first or ".." anywhere)."""
    if not isinstance(root, str) or not isinstance(relative, str) or not root.startswith("/") or not relative:
        return None
    if ".." in root.split("/"):
        return None
    parts = relative.split("/")
    if relative.startswith("/") or parts[0] == "." or ".." in parts:
        return None
    return root + relative if root.endswith("/") else root + "/" + relative


def _dot(segment: str) -> int:
    """1 for a "." URL path segment, 2 for "..", 0 otherwise (percent-encoded dots count, as URL parsers read them)."""
    s = segment.lower().replace("%2e", ".")
    return {".": 1, "..": 2}.get(s, 0)


def file_url_path(url: str) -> str | None:
    """The local path Studio's review reads from a media-rep src: a file URL with no host (or localhost), its dot
    segments resolved and each segment percent-decoded, as a URL parser and Rust's `Url::to_file_path` do; None for
    anything else, which Studio reports as "Only exact local file URLs can match the catalog"."""
    from urllib.parse import unquote_to_bytes

    url = url.strip("".join(chr(c) for c in range(0x21))).replace("\t", "").replace("\n", "").replace("\r", "")
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):(.*)\Z", url, re.S)
    if not m or m.group(1).lower() != "file":
        return None
    rest = re.split(r"[?#]", m.group(2).replace("\\", "/"), maxsplit=1)[0]
    if rest.startswith("//"):
        host, slash, rest = rest[2:].partition("/")
        if host.lower() not in ("", "localhost"):
            return None
        rest = slash + rest
    raw = rest.lstrip("/").split("/") if rest else [""]
    segments: list[str] = []
    for i, seg in enumerate(raw):
        last = i == len(raw) - 1
        kind = _dot(seg)
        if kind == 2:
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif kind == 1:
            if last:
                segments.append("")
        else:
            segments.append(seg)
    data = b"".join(b"/" + unquote_to_bytes(seg) for seg in (segments or [""]))
    if len(data) > 2 and chr(data[-2]).isascii() and chr(data[-2]).isalpha() and data[-1:] in (b":", b"|"):
        data += b"/"  # the url crate's Windows drive-letter rule, applied on a Mac too
    path = data.decode("utf-8", errors="replace")
    if not path.startswith("/") or ".." in path.split("/"):
        return None
    return path


def _seconds(value) -> float | None:
    """An FCPXML time ("1001/30000s", "5s") in seconds, as Studio reads it; None when it can't."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    a, sep, b = value[:-1].partition("/")
    try:
        if sep:
            if not (_NUMBER.match(a) and _NUMBER.match(b)):
                return None
            x = float(a) / float(b)
        else:
            if not _NUMBER.match(a):
                return None
            x = float(a)
    except (ZeroDivisionError, ValueError, OverflowError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _window(start: float, duration: float, window, offset: float | None) -> tuple[float, float]:
    if window is not None and offset is not None:
        lo, hi = window
        return start + max(lo - offset, 0.0), start + min(hi - offset, duration)
    return start, start + duration


def _intersects(node, window) -> bool:
    if window is None:
        return True
    start, d = _seconds(node.get("offset")), _seconds(node.get("duration"))
    if start is None or d is None:
        return True
    return start < window[1] and start + d > window[0]


class _Walk:
    """Studio's walk of one project's timeline: the media every clip that plays refers to."""

    def __init__(self, resources: dict):
        self.resources = resources
        self.active: set[str] = set()
        self.references: dict[tuple[str, str, str], None] = {}  # (resource id, src URL or "", why it can't match or ""), as met
        self.visits = 0

    def walk(self, node, window, depth: int) -> None:
        self.visits += 1
        if depth > REVIEW_MAX_DEPTH or self.visits > REVIEW_MAX_VISITS:
            raise ReviewRefused("its timelines nest deeper, or refer to more clips, than Studio's review reads")
        if not _intersects(node, window) or node.get("enabled") == "0":
            return
        tag = node.tag
        if tag == "audition":  # only the active pick (the first) counts
            pick = next((c for c in node if c.tag in _PICKS), None)
            if pick is not None:
                self.walk(pick, None, depth + 1)
            return
        rid = node.get("ref")
        if rid is not None and tag in _REFERS:
            resource = self.resources.get(rid)
            if resource is None:
                self.references[(rid, "", "Timeline reference has no resource.")] = None
                return
            if resource.tag == "asset":
                srcs = [c.get("src") for c in resource if c.tag == "media-rep" and c.get("kind") == "original-media" and c.get("src") is not None]
                if resource.get("src") is not None:
                    srcs.append(resource.get("src"))
                if not srcs:
                    self.references[(rid, "", "Referenced asset has no original media URL.")] = None
                for src in srcs:
                    self.references[(rid, src, "")] = None
            elif resource.tag == "media":
                if rid in self.active:
                    raise ReviewRefused("a compound clip in it contains itself")
                self.active.add(rid)
                sequence = next((c for c in resource if c.tag == "sequence"), None)
                if any(c.tag == "timeMap" for c in node):
                    self.references[(rid, "", "Retimed nested media needs manual usage confirmation.")] = None
                elif sequence is not None:
                    start = next((s for s in (_seconds(node.get("start")), _seconds(sequence.get("start"))) if s is not None), 0.0)
                    d = _seconds(node.get("duration"))
                    d = _seconds(sequence.get("duration")) if d is None else d
                    self.walk(sequence, None if d is None else _window(start, d, window, _seconds(node.get("offset"))), depth + 1)
                elif any(c.tag == "multicam" for c in resource):
                    self.references[(rid, "", "Multicam timing and component selection need manual usage confirmation.")] = None
                else:
                    self.references[(rid, "", "Referenced media has no supported sequence or multicam timeline.")] = None
                self.active.discard(rid)
        for child in node:
            if child.tag in _NOT_WALKED:
                continue
            if tag in _SAME_WINDOW:
                inner = window
            else:
                start = _seconds(node.get("start"))
                d = _seconds(node.get("duration"))
                inner = None if d is None else _window(0.0 if start is None else start, d, window, _seconds(node.get("offset")))
            self.walk(child, inner, depth + 1)


def project_name_problem(name: str) -> str | None:
    """Why Studio's review would refuse a project named this (and with it the whole file), or None."""
    trimmed = name.strip(_WHITE)
    if not trimmed:
        return "a project has no name"
    if len(trimmed) > NAME_MAX:
        return f"a project's name is longer than {NAME_MAX} characters"
    if any(unicodedata.category(ch) == "Cc" for ch in trimmed):
        return "a project's name has a control character in it"
    return None


@dataclass
class ReviewedProject:
    """One project Studio's review would read: its name, and each media file its timeline uses."""

    name: str
    references: list[tuple[str, str, str]]  # (resource id, src URL or "", why Studio can't match it or "")

    def paths(self) -> list[tuple[str, str]]:
        """(path, why): the decoded local path ("" when there is none) and why it can't match ("" when it can)."""
        out = []
        for _, src, why in self.references:
            if why:
                out.append((src, why))
                continue
            path = file_url_path(src)
            out.append((path, "") if path is not None else (src, "Only exact local file URLs can match the catalog."))
        return list(dict.fromkeys(out))


def review_references(xml) -> list[ReviewedProject]:
    """What Studio's Project history review would read in this FCPXML: each project and the media it uses.
    Raises ReviewRefused where Studio would refuse the whole file. Reads only the text given, never a media file."""
    import xml.etree.ElementTree as ET

    data = xml.encode("utf-8") if isinstance(xml, str) else bytes(xml)
    if len(data) > REVIEW_MAX_BYTES:
        raise ReviewRefused("the file is larger than the 16 MiB Studio's review reads")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReviewRefused("the file isn't UTF-8") from None
    safe = text.replace("<!DOCTYPE fcpxml>", "")
    if "<!DOCTYPE" in safe or "<!ENTITY" in safe:
        raise ReviewRefused("the file declares a DTD or entities, which Studio's review doesn't read")
    try:
        root = ET.fromstring(safe)
    except ET.ParseError as e:
        raise ReviewRefused(f"the file isn't valid XML ({e})") from None
    if root.tag != "fcpxml":
        raise ReviewRefused("the file isn't FCPXML")
    resources: dict = {}
    for group in root:
        if group.tag != "resources":
            continue
        for resource in group:
            rid = resource.get("id")
            if rid is None:
                continue
            if rid in resources:
                raise ReviewRefused("two of its resources share an ID")
            resources[rid] = resource
    projects: list[ReviewedProject] = []
    for project in root.iter("project"):
        sequence = next((c for c in project if c.tag == "sequence"), None)
        if sequence is None:
            continue
        if len(projects) >= REVIEW_MAX_PROJECTS:
            raise ReviewRefused(f"it has more than {REVIEW_MAX_PROJECTS} projects")
        name = project.get("name", "Untitled Final Cut project")
        problem = project_name_problem(name)
        if problem:
            raise ReviewRefused(problem)
        walk = _Walk(resources)
        walk.walk(sequence, None, 0)
        projects.append(ReviewedProject(name.strip(_WHITE), list(walk.references)))  # in timeline order (Studio sorts them; the set is the same)
    if not projects:
        raise ReviewRefused("it has no project timeline")
    return projects


def cut_projects(xml) -> tuple[list[ReviewedProject], str | None]:
    """(projects, refused): the projects Studio's review would read, or none and the reason it would refuse the file."""
    try:
        return review_references(xml), None
    except ReviewRefused as e:
        return [], e.reason


def _all_media(xml) -> list[str]:
    """Every original-media path the file's resources name: for credits when Studio's review would refuse the file."""
    found = re.findall(r'<media-rep kind="original-media" src="([^"]*)"', xml if isinstance(xml, str) else xml.decode("utf-8", "replace"))
    from html import unescape

    return list(dict.fromkeys(p for p in (file_url_path(unescape(u)) for u in found) if p))


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return bool(_query(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def mappings(con: sqlite3.Connection) -> tuple[dict[str, dict[int, str]], str | None]:
    """(path -> {item id: name}, refused) as Studio's review builds it: every catalog row, missing or not, at its
    catalog_path, and the output paths of the packs Studio prepared (when the copy has its Project history).
    `refused`: a row whose path Studio refuses, which makes Studio's review stop on any file."""
    paths: dict[str, dict[int, str]] = {}
    refused = None
    rows = _query(con, "SELECT a.id, coalesce(nullif(a.display_name,''), a.name), l.path, a.relative_path FROM assets a JOIN libraries l ON l.id = a.library_id")
    for aid, name, root, relative in rows:
        path = catalog_path(root, relative)
        if path is None:
            refused = refused or (
                f"Studio's catalog has an item ({name}) whose path it refuses, so its Project history review stops on any file "
                "(\"Unsafe source path in catalog history\") until that item is fixed or removed in Studio"
            )
            continue
        paths.setdefault(path, {})[int(aid)] = str(name)
    if _has_table(con, "project_history_items"):
        for table, columns in HISTORY_COLUMNS.items():
            have = {row[1] for row in _query(con, f'PRAGMA table_info("{table}")')}
            if not columns <= have:
                raise StudioUnavailable(f"Studio's Project history in the catalog changed shape (run `roughcut update`)")
        prepared = _query(
            con,
            "SELECT i.asset_id, coalesce(nullif(a.display_name,''), a.name), i.output_path FROM project_history_items i "
            "JOIN project_history_events e ON e.id = i.event_id JOIN assets a ON a.id = i.asset_id "
            "WHERE e.kind = 'prepared' AND i.output_path <> ''",
        )
        for aid, name, output in prepared:
            paths.setdefault(str(output), {})[int(aid)] = str(name)
    return paths, refused


def _fold_path(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _root_match(path: str, roots: list[str]) -> tuple[str | None, str]:
    """(root, how): the library root `path` is under: "exact", "folded" (only ignoring case or Unicode form) or "mount" (through /Volumes/<name>-N)."""
    roots = [r for r in roots if isinstance(r, str) and r.startswith("/")]
    target = _parts(path)
    for root in roots:
        parts = _parts(root)
        if len(parts) <= len(target) and target[: len(parts)] == parts:
            return root, "exact"
    root, mount = _library_of(path, roots)
    if root is None:
        return None, ""
    return root, "mount" if mount else "folded"


class _Index:
    """The review's paths, and the same folded for near misses (built only when a path doesn't match)."""

    def __init__(self, paths: dict[str, dict[int, str]]):
        self.paths = paths
        self._folded: dict[str, list[str]] | None = None

    def near(self, path: str) -> list[str]:
        if self._folded is None:
            self._folded = {}
            for p in self.paths:
                self._folded.setdefault(_fold_path(p), []).append(p)
        return [p for p in self._folded.get(_fold_path(path), []) if p != path]


def _near_miss(path: str, index: _Index) -> tuple[str, str] | None:
    """(Studio's path, why) for a path that matches a catalog path only ignoring case or Unicode form, or through
    a share macOS mounted under a numbered name; None otherwise."""
    for other in index.near(path):
        nfc = unicodedata.normalize("NFC", other) == unicodedata.normalize("NFC", path)
        why = "the same name in another Unicode form (how accented letters are stored)" if nfc else (
            "only the letter case differs" if other.casefold() == path.casefold() else "the letter case and Unicode form differ"
        )
        return other, why
    usual = _usual_mount(_parts(path))
    if usual is not None:
        moved = str(PurePosixPath(*usual))
        mount = str(PurePosixPath(*_parts(path)[: len(_parts(VOLUMES)) + 1]))
        for other in ([moved] if moved in index.paths else []) + index.near(moved):
            return other, f"it's on {mount}, a second mount of the share Studio has at {PurePosixPath(*usual[: len(_parts(VOLUMES)) + 1])}"
    return None


def match_media(xml, *, out_dir: Path | None = None, folder: Path | None = None, cache_dir: Path | None = None, kit: Kit | None = None) -> dict:
    """What Studio's Project history review would match for each media file this FCPXML's projects use, from the
    snapshot copy (or, without one, a kit the owner exported). Pure string comparison: no media file is touched.

    Returns the record saved in plan.json as `studio_sources`: {"snapshot", "kit", "refused", "files": {path: {"status",
    "assets", "why", "studio_path"}}}. A status is matched / ambiguous (Studio counts neither item) / near_miss /
    not_in_snapshot (under a library root; probably matched after Studio's next scan) / not_in_kit / outside /
    render (Roughcut's own output) / unmatchable. Raises StudioUnavailable when there's nothing to check against.
    """
    projects, refused = cut_projects(xml)
    wanted: dict[str, str] = {}
    for project in projects:
        for path, why in project.paths():
            wanted.setdefault(path, why)
    if refused:
        for path in _all_media(xml):
            wanted.setdefault(path, "")
    record: dict = {"checked_at": int(time.time()), "snapshot": None, "kit": None, "refused": refused, "files": {}}
    details: dict[int, dict] = {}
    roots: list[str] = []
    try:
        copy = newest_copy(folder, cache_dir)
    except StudioUnavailable:
        if kit is None:
            raise
        copy = None
    if copy is not None:
        with catalog(copy) as con:
            paths, catalog_refused = mappings(con)
            roots = [lib.path for lib in libraries(con)]
            index = _Index(paths)
            ids = sorted({i for p in wanted if p in paths for i in paths[p]})
            for n in range(0, len(ids), 500):
                chunk = ids[n : n + 500]
                for aid, name, provider, lic in _query(
                    con, f"SELECT id, coalesce(nullif(display_name,''), name), provider, license FROM assets WHERE id IN ({', '.join('?' * len(chunk))})",
                    tuple(chunk),
                ):
                    details[int(aid)] = {"id": int(aid), "name": str(name or ""), "provider": str(provider or ""), "license": str(lic or "")}
        snap = copy.snapshot
        record["snapshot"] = {"id": snap.id, "name": snap.name, "created_at": snap.created_at}
        record["refused"] = refused or catalog_refused
        missing = "not_in_snapshot"
    else:
        paths = {}
        for n, item in enumerate(kit.items):
            aid = item.asset_id if item.asset_id is not None else -(n + 1)
            paths.setdefault(item.path, {})[aid] = item.name
            details[aid] = {"id": aid, "name": item.name, "provider": item.provider, "license": item.license}
        index = _Index(paths)
        record["kit"] = {"name": kit.name, "file": str(kit.file), "exported_at": kit.exported_at}
        missing = "not_in_kit"
    own = str(out_dir).rstrip("/") + "/" if out_dir is not None else None
    for path, why in wanted.items():
        entry: dict = {"status": "", "assets": [], "why": "", "studio_path": None}
        if why:
            entry.update(status="unmatchable", why=why)
        elif path in paths:
            found = paths[path]
            entry["status"] = "matched" if len(found) == 1 else "ambiguous"
            entry["assets"] = [details.get(i) or {"id": i, "name": n, "provider": "", "license": ""} for i, n in sorted(found.items())]
        else:
            near = _near_miss(path, index)
            root, how = _root_match(path, roots)
            if near is not None:
                entry.update(status="near_miss", studio_path=near[0], why=near[1])
            elif own is not None and path.startswith(own):
                entry["status"] = "render"
            elif root is not None and how == "exact":
                entry["status"] = missing
            elif root is not None:
                entry.update(
                    status="near_miss", studio_path=None,
                    why=f"it's on {_library_of(path, [root])[1]}, a second mount of the share Studio's library {root} is on" if how == "mount"
                    else f"the library folder is spelled {root} in Studio (Studio compares letter case and Unicode form exactly)",
                )
            else:
                entry["status"] = "outside" if copy is not None else missing
        record["files"][path] = entry
    return record

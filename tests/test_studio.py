"""Venture Studio, read only: snapshots, the catalog copy, kits, the online check, the output guard.

Every test builds a fake Studio folder from the minimal SQL below (only the tables and columns
Roughcut reads) and checks that reading it changed nothing there: bytes, modification times and
the listing are the same after as before, and SQLite never opens anything outside Roughcut's cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import types
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg

from roughcut import analyze, cli, pipeline, progress, sfx, studio
from roughcut.cli import main
from roughcut.pipeline import Options

# The catalog as Roughcut reads it: nothing more.
CATALOG_SQL = """
CREATE TABLE libraries (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'file',
    category TEXT NOT NULL DEFAULT 'other', provider TEXT NOT NULL DEFAULT '', license TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', duration REAL, favorite INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0, browse_visible INTEGER NOT NULL DEFAULT 1, package_id INTEGER
);
CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE collection_assets (collection_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, PRIMARY KEY (collection_id, asset_id));
CREATE TABLE project_kits (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
CREATE TABLE project_kit_assets (kit_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, PRIMARY KEY (kit_id, asset_id));
"""

NOW = int(time.time())


class FakeStudio:
    """A Studio data folder: a live catalog nobody may open, and snapshots built from CATALOG_SQL."""

    def __init__(self, root: Path):
        self.dir = root / "Studio data"
        (self.dir / "backups").mkdir(parents=True)
        (self.dir / "catalog.sqlite3").write_bytes(b"the live catalog: Roughcut never opens this")
        self.staging = root / "staging"
        self.staging.mkdir()
        self.libraries: list[tuple[str, str]] = []

    def snapshot(self, sid: str, created: int, *, name: str = "Catalog snapshot", assets=(), collections=None, kits=None,
                 schema: int = 3, user_version: int = 3, drop_column: str | None = None, libraries=None) -> Path:
        db = self.staging / f"{sid}.sqlite"
        con = sqlite3.connect(db)
        sql = re.sub(rf"\b{drop_column} [^,\n]*,\s*", "", CATALOG_SQL) if drop_column else CATALOG_SQL
        con.executescript(sql)
        for i, (path, lname) in enumerate(libraries if libraries is not None else self.libraries, 1):
            con.execute("INSERT INTO libraries (id, path, name) VALUES (?, ?, ?)", (i, path, lname))
        for a in assets:
            cols = ", ".join(a)
            con.execute(f"INSERT INTO assets ({cols}) VALUES ({', '.join('?' * len(a))})", tuple(a.values()))
        for cid, (cname, ids) in enumerate((collections or {}).items(), 1):
            con.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (cid, cname))
            con.executemany("INSERT INTO collection_assets VALUES (?, ?)", [(cid, i) for i in ids])
        for kid, (kname, ids) in enumerate((kits or {}).items(), 1):
            con.execute("INSERT INTO project_kits (id, name) VALUES (?, ?)", (kid, kname))
            con.executemany("INSERT INTO project_kit_assets VALUES (?, ?)", [(kid, i) for i in ids])
        con.execute(f"PRAGMA user_version = {user_version}")
        con.commit()
        con.close()
        data = db.read_bytes()
        target = self.dir / "backups" / f"{sid}.sqlite"
        target.write_bytes(data)
        manifest = {
            "id": sid, "name": name, "created_at": created, "schema_version": schema, "asset_count": len(assets),
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "a_later_field": 0,  # keys Roughcut doesn't read are fine
        }
        (self.dir / "backups" / f"{sid}.json").write_text(json.dumps(manifest))
        return target


def _state(folder: Path) -> dict:
    """Everything a reader could change: each entry's type, size, modification time and bytes."""
    out = {".": os.lstat(folder).st_mtime_ns}
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for n in dirnames + filenames:
            p = Path(dirpath, n)
            st = os.lstat(p)
            entry: tuple = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns)
            if stat.S_ISREG(st.st_mode):
                entry += (hashlib.sha256(p.read_bytes()).hexdigest(),)
            elif stat.S_ISLNK(st.st_mode):
                entry += (os.readlink(p),)
            out[str(p.relative_to(folder))] = entry
    return out


@contextmanager
def untouched(*folders: Path):
    before = [_state(f) for f in folders]
    yield
    assert [_state(f) for f in folders] == before


@pytest.fixture(autouse=True)
def only_the_cache_is_opened(monkeypatch):
    """SQLite may open Roughcut's own copy, read-only and immutable, and nothing else."""
    opened: list[Path] = []
    real = sqlite3.connect

    def connect(target, *args, **kwargs):
        assert kwargs.get("uri") is True and str(target).startswith("file:"), target
        parsed = urlparse(str(target))
        path = Path(unquote(parsed.path))
        assert parse_qs(parsed.query) == {"mode": ["ro"], "immutable": ["1"]}, target
        assert path.parent.name == studio.CACHE_FOLDER and "Studio data" not in path.parts, f"SQLite asked to open {path}"
        assert path.is_file(), f"{target} would make a new file"
        opened.append(path)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(studio, "sqlite3", types.SimpleNamespace(connect=connect, Error=sqlite3.Error))
    return opened


@pytest.fixture(autouse=True)
def _reset_progress():
    yield
    progress.enable(False)


@pytest.fixture
def fake(tmp_path, monkeypatch) -> FakeStudio:
    s = FakeStudio(tmp_path)
    monkeypatch.setenv(studio.STUDIO_ENV, str(s.dir))
    monkeypatch.setattr(analyze, "default_cache_dir", lambda: tmp_path / "cache")
    return s


@pytest.fixture
def library(tmp_path, fake) -> Path:
    root = tmp_path.resolve() / "Shared" / "Studio Library"
    root.mkdir(parents=True)
    (root / "Sound Effects").mkdir()
    (root / "Sound Effects" / "whoosh.wav").write_bytes(b"RIFF")
    fake.libraries = [(str(root), "Studio Library")]
    return root


def _os_with(**overrides):
    """`os` as roughcut.studio sees it, with some calls replaced."""
    proxy = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    for k, v in overrides.items():
        setattr(proxy, k, v)
    return proxy


def _asset(i, rel, **kw):
    return {"id": i, "library_id": 1, "relative_path": rel, "name": Path(rel).name, "extension": Path(rel).suffix[1:], **kw}


# ---------------------------------------------------------------- snapshots


def test_the_newest_snapshot_is_copied_and_minus_10_beats_minus_9(fake, tmp_path, only_the_cache_is_opened):
    fake.snapshot("1700000000-42-9", NOW - 3600, libraries=[("/Libraries/nine", "nine")])
    fake.snapshot("1700000000-42-10", NOW - 3600, libraries=[("/Libraries/ten", "ten")])
    fake.snapshot("1690000000-42-99", NOW - 86400 * 3, libraries=[("/Libraries/old", "old")])
    with untouched(fake.dir):
        listed = studio.snapshots()
        copy = studio.newest_copy()
        with studio.catalog(copy) as con:
            libs = studio.libraries(con)
    assert [s.id for s in listed] == ["1700000000-42-10", "1700000000-42-9", "1690000000-42-99"]
    assert copy.snapshot.id == "1700000000-42-10" and not copy.skipped and not copy.reused
    assert [lib.name for lib in libs] == ["ten"]
    assert copy.path.parent == tmp_path / "cache" / "venture-studio"
    assert only_the_cache_is_opened == [copy.path]
    # Immutable: no journal, WAL or lock file appears, even beside our own copy.
    assert sorted(p.name for p in copy.path.parent.iterdir()) == [copy.path.name]
    assert not (fake.dir / "catalog.worker-lock").exists()


def test_a_half_written_manifest_is_skipped_without_a_word(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    (fake.dir / "backups" / "1700000100-1-1.json").write_text('{"id": "1700000100-1-1", "name": "Catalog snap')
    (fake.dir / "backups" / "1700000200-1-1.json").write_text(json.dumps({"id": "1700000200-1-1", "created_at": "yesterday"}))
    with untouched(fake.dir):
        copy = studio.newest_copy()
    assert [s.id for s in studio.snapshots()] == ["1700000000-1-1"]
    assert copy.snapshot.id == "1700000000-1-1" and copy.skipped == [] and copy.notes() == []


def test_a_tampered_snapshot_falls_back_and_says_so(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    newest = fake.snapshot("1700000500-1-1", NOW - 3600)
    data = bytearray(newest.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size, different bytes
    newest.write_bytes(bytes(data))
    with untouched(fake.dir):
        copy = studio.newest_copy()
    assert copy.snapshot.id == "1700000000-1-1"
    assert copy.skipped[0][0].id == "1700000500-1-1" and "checksum" in copy.skipped[0][1]
    assert "newest catalog snapshot" in copy.notes()[0] and "checksum" in copy.notes()[0]
    assert not (studio.copies_dir() / "1700000500-1-1.sqlite").exists()
    assert not list(studio.copies_dir().glob(".*.partial"))


def test_a_snapshot_archived_while_reading_falls_back_and_says_so(fake, monkeypatch):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    fake.snapshot("1700000500-1-1", NOW - 3600)
    gone = fake.snapshot("1700000900-1-1", NOW - 60)
    gone.unlink()  # Studio archives the .sqlite first, then its manifest
    real_open = os.open

    def archived(path, *args, **kwargs):  # the next one goes between the check and the open
        if str(path).endswith("1700000500-1-1.sqlite"):
            raise FileNotFoundError(2, "No such file or directory")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(studio, "os", _os_with(open=archived))
    with untouched(fake.dir):
        copy = studio.newest_copy()
    assert copy.snapshot.id == "1700000000-1-1"
    assert [(s.id, "gone" in why) for s, why in copy.skipped] == [("1700000900-1-1", True), ("1700000500-1-1", True)]


def test_a_snapshot_with_sqlite_files_beside_it_is_refused(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    newest = fake.snapshot("1700000500-1-1", NOW - 3600)
    Path(f"{newest}-journal").write_bytes(b"")
    with untouched(fake.dir):
        copy = studio.newest_copy()
    assert copy.snapshot.id == "1700000000-1-1" and "SQLite files sit beside it" in copy.skipped[0][1]


def test_symlinked_manifests_and_snapshots_are_refused(fake, tmp_path):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    # A manifest that's a link: not listed at all.
    elsewhere = fake.snapshot("1700000500-1-1", NOW - 3600)
    manifest = elsewhere.with_suffix(".json")
    moved = tmp_path / "moved.json"
    manifest.rename(moved)
    manifest.symlink_to(moved)
    # A snapshot that's a link to a perfectly good file elsewhere: refused, like Studio does.
    linked = fake.snapshot("1700000900-1-1", NOW - 60)
    real = tmp_path / "real.sqlite"
    linked.rename(real)
    linked.symlink_to(real)
    with untouched(fake.dir):
        listed = [s.id for s in studio.snapshots()]
        copy = studio.newest_copy()
    assert listed == ["1700000900-1-1", "1700000000-1-1"]
    assert copy.snapshot.id == "1700000000-1-1" and "regular file" in copy.skipped[0][1]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_where_the_snapshot_should_be_does_not_hang(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    fifo = fake.snapshot("1700000500-1-1", NOW - 3600)
    fifo.unlink()
    os.mkfifo(fifo)
    done: dict = {}
    with untouched(fake.dir):
        t = threading.Thread(target=lambda: done.update(copy=studio.newest_copy()), daemon=True)
        t.start()
        t.join(10)
    assert "copy" in done, "newest_copy hung on a FIFO"
    assert done["copy"].snapshot.id == "1700000000-1-1" and "regular file" in done["copy"].skipped[0][1]


def test_a_newer_catalog_format_is_refused_and_reported(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    fake.snapshot("1700000500-1-1", NOW - 3600, schema=4, user_version=4)
    with untouched(fake.dir):
        copy = studio.newest_copy()
        report = studio.status(offline=True)
    assert copy.snapshot.id == "1700000000-1-1" and "format 4" in copy.skipped[0][1] and "roughcut update" in copy.skipped[0][1]
    assert report["available"] and any("format 4" in n for n in report["notes"])


def test_a_snapshot_whose_catalog_says_format_4_is_refused(fake):
    fake.snapshot("1700000500-1-1", NOW - 3600, user_version=4)  # the manifest says 3, the file says 4
    with untouched(fake.dir):
        copy = studio.newest_copy()
        with pytest.raises(studio.StudioUnavailable, match="format 4"):
            studio.open_copy(copy.path)
        report = studio.status(offline=True)
    assert not report["available"] and "format 4" in report["reason"]


def test_a_catalog_missing_a_column_roughcut_reads_is_refused(fake):
    fake.snapshot("1700000500-1-1", NOW - 3600, drop_column="browse_visible")
    copy = studio.newest_copy()
    with pytest.raises(studio.StudioUnavailable, match="browse_visible"):
        studio.open_copy(copy.path)


def test_extra_tables_and_columns_are_fine(fake):
    path = fake.snapshot("1700000500-1-1", NOW - 3600)
    con = sqlite3.connect(path)
    con.executescript("ALTER TABLE assets ADD COLUMN metadata_revision INTEGER; CREATE TABLE later_feature (x);")
    con.close()
    data = path.read_bytes()
    manifest = path.with_suffix(".json")
    m = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**m, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}))
    studio.open_copy(studio.newest_copy().path).close()


def test_the_copy_from_before_a_catalog_restore_is_flagged(fake):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    fake.snapshot("1700000500-1-1", NOW - 60, name="Before catalog restore")
    with untouched(fake.dir):
        report = studio.status(offline=True)
    assert report["snapshot"]["before_restore"] and "before a catalog restore" in report["summary"]
    assert any("before a catalog restore" in n and "Back up catalog now" in n for n in report["notes"])


def test_an_imported_or_old_snapshot_is_flagged(fake):
    fake.snapshot("1700000000-1-1", NOW - 86400 * 9, name="Imported: catalog from the old Mac")
    notes = studio.newest_copy().notes()
    assert any("imported" in n for n in notes) and any("9 days old" in n for n in notes)


def test_not_installed_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    report = studio.status()
    assert report["installed"] is False and report["reason"] == "not installed" and report["libraries"] == []
    assert studio.output_guard(tmp_path / "out") == (None, None)
    with pytest.raises(studio.StudioUnavailable) as e:
        studio.newest_copy()
    assert e.value.silent
    assert not (tmp_path / "nowhere").exists()


def test_no_snapshot_yet_is_a_reason_not_an_error(fake):
    report = studio.status(offline=True)
    assert report["installed"] and not report["available"] and "Back up catalog now" in report["reason"]
    guard, note = studio.output_guard(fake.dir.parent / "out")
    assert guard is None and "Back up catalog now" in note


def test_the_copy_is_reused_and_old_copies_pruned(fake, monkeypatch, tmp_path):
    fake.snapshot("1700000000-1-1", NOW - 7200)
    first = studio.newest_copy()
    assert not first.reused
    real_open = os.open

    def no_studio_reads(path, *args, **kwargs):  # the manifests are read, the snapshot isn't copied again
        assert not ("Studio data" in str(path) and str(path).endswith(".sqlite")), f"read {path} again"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(studio, "os", _os_with(open=no_studio_reads))
    again = studio.newest_copy()
    assert again.reused and again.path == first.path
    monkeypatch.undo()
    monkeypatch.setenv(studio.STUDIO_ENV, str(fake.dir))
    monkeypatch.setattr(analyze, "default_cache_dir", lambda: tmp_path / "cache")

    cache = studio.copies_dir()
    (cache / "keep-me.txt").write_text("not Roughcut's copy")
    old_partial = cache / ".1699999999-1-1.abc.partial"
    old_partial.write_bytes(b"x")
    os.utime(old_partial, (time.time() - 7200, time.time() - 7200))
    young_partial = cache / ".1700000900-1-1.def.partial"
    young_partial.write_bytes(b"x")
    for i, sid in enumerate(("1700000500-1-1", "1700000900-1-1")):
        time.sleep(0.02)
        fake.snapshot(sid, NOW - 3000 + i)
        studio.newest_copy()
    names = sorted(p.name for p in cache.iterdir())
    assert names == [".1700000900-1-1.def.partial", "1700000500-1-1.sqlite", "1700000900-1-1.sqlite", "keep-me.txt"]


def test_a_cache_path_with_url_characters_and_nfd_letters_works(fake, tmp_path, monkeypatch, only_the_cache_is_opened):
    fake.libraries = [("/Libraries/one", "one")]
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[_asset(1, "a.wav", kind="audio", category="audio")])
    odd = tmp_path / "odd" / "ca#c?he%20" / unicodedata.normalize("NFD", "Café")
    with untouched(fake.dir):
        copy = studio.newest_copy(cache_dir=odd)
        with studio.catalog(copy) as con:
            assert studio.browse_count(con) == 1
    assert copy.path.parent == odd / "venture-studio"
    assert only_the_cache_is_opened == [copy.path]
    # A URI built by hand would have opened (and created) a file named after the part before '#' or '?'.
    found = sorted(str(p.relative_to(tmp_path / "odd")) for p in (tmp_path / "odd").rglob("*"))
    assert found == sorted(str(p.relative_to(tmp_path / "odd")) for p in [odd.parent, odd, odd / "venture-studio", copy.path])


# ---------------------------------------------------------------- what the catalog holds


def test_items_paths_collections_and_kits(fake, library):
    root = str(library)
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[
        _asset(1, "Sound Effects/whoosh.wav", kind="audio", category="audio", provider="Vendor", license="Standard", tags="whoosh, fast ,, transition",
               duration=1.5, favorite=1),
        _asset(2, "Sound Effects/gone.wav", kind="audio", category="audio", missing=1),
        _asset(3, "Titles/Lower/Lower.moti", kind="template", category="title", display_name="Lower", package_id=3),
        _asset(4, "Titles/Lower/media/bg.mov", kind="video", category="video", package_id=3),
        _asset(5, "Sound Effects/hidden.txt", browse_visible=0),
        _asset(6, "../outside.wav", kind="audio", category="audio"),
        _asset(7, "Sound Effects/pop.wav", kind="audio", category="audio"),
    ], collections={"Promo Sounds": [1, 2, 7], "Other": [3]}, kits={"Promo kit": [1, 3]})
    copy = studio.newest_copy()
    with studio.catalog(copy) as con:
        assert studio.browse_count(con) == 4  # 1, 3, 6 and 7: Studio's own count has no path rule
        everything = studio.assets(con)
        sounds = studio.assets(con, collection="promo sounds")
        audio = studio.assets(con, kinds=("audio",))
        kit = studio.assets(con, kit="PROMO KIT")
        fav = studio.assets(con, favorites=True)
        with_missing = studio.assets(con, collection="Promo Sounds", browse=False)
        assert studio.collections(con) == ["Other", "Promo Sounds"] and studio.kits(con) == ["Promo kit"]
        with pytest.raises(studio.StudioUnavailable, match="no collection named"):
            studio.assets(con, collection="Nope")
    assert [a.id for a in everything] == [1, 3, 7]  # the member file, the hidden one and the unsafe path stay out
    whoosh = everything[0]
    assert whoosh.path == f"{root}/Sound Effects/whoosh.wav" and whoosh.library == "Studio Library" and whoosh.library_root == root
    assert whoosh.tags == ("whoosh", "fast", "transition") and whoosh.duration == 1.5 and whoosh.favorite and not whoosh.missing
    assert (whoosh.provider, whoosh.license, whoosh.kind, whoosh.category) == ("Vendor", "Standard", "audio", "audio")
    assert everything[1].title == "Lower" and everything[2].title == "pop.wav"
    assert [a.id for a in sounds] == [1, 7] and [a.id for a in audio] == [1, 7] and [a.id for a in kit] == [1, 3] and [a.id for a in fav] == [1]
    assert [(a.id, a.missing) for a in with_missing] == [(1, False), (2, True), (7, False)]


def test_original_paths_follow_studios_rule():
    assert studio.original_path("/Lib", "a/b.wav") == "/Lib/a/b.wav"
    for root, rel in (("Lib", "a.wav"), ("/Lib", ""), ("/Lib", "/a.wav"), ("/Lib", "a/../b.wav"), ("/Lib", "a/./b.wav"), ("/L/../x", "a.wav")):
        assert studio.original_path(root, rel) is None


def test_settings_default_to_studios_own(fake):
    assert studio.settings() == {"automatic_updates": True, "daily_backups": True}
    (fake.dir / "maintenance.json").write_text(json.dumps({"settings": {"automatic_updates": False, "daily_backups": True}, "status": {}}))
    with untouched(fake.dir):
        assert studio.settings() == {"automatic_updates": False, "daily_backups": True}
    (fake.dir / "maintenance.json").write_text("{half")
    assert studio.settings() == {"automatic_updates": True, "daily_backups": True}


# ---------------------------------------------------------------- kits


def _kit(tmp_path: Path, **over) -> Path:
    def item(name, ref, **kw):
        return {"asset_id": 1, "name": name, "kind": "audio", "extension": "wav", "provider": "Vendor", "license": "Standard",
                "library_path": "/Lib", "relative_path": name, "original_reference": ref, "catalog_missing": False,
                "readiness": "unknown", "reason": "", **kw}

    doc = {
        "format_version": 1, "kind": studio.KIT_KIND, "exported_at": NOW, "notice": "References only.",
        "project": {"kit_id": 1, "name": "Promo kit", "description": "for promos", "assets": [
            item("whoosh.wav", "/Lib/Sound Effects/whoosh.wav"),
            item("gone.wav", "/Lib/gone.wav", catalog_missing=True, readiness="attention"),
            item("check.wav", "/Lib/check.wav", readiness="attention", reason="The original is offline."),
            item("sneaky.wav", "/Lib/../etc/sneaky.wav"),
            item("relative.wav", "Lib/relative.wav"),
        ]},
        "assets": [item("decoy.wav", "/Lib/decoy.wav")],  # not where Studio puts them: ignored
        **over,
    }
    path = tmp_path / "venture-project-promo-kit.json"
    path.write_text(json.dumps(doc))
    return path


def test_a_kit_export_loads_and_holds_back_what_it_must(tmp_path):
    kit = studio.load_kit(_kit(tmp_path))
    assert kit.name == "Promo kit" and kit.exported_at == NOW
    assert [(i.name, i.path, i.provider, i.license) for i in kit.items] == [("whoosh.wav", "/Lib/Sound Effects/whoosh.wav", "Vendor", "Standard")]
    held = dict(kit.held_back)
    assert set(held) == {"gone.wav", "check.wav", "sneaky.wav", "relative.wav"}
    assert "missing" in held["gone.wav"] and "offline" in held["check.wav"] and "plain absolute" in held["sneaky.wav"]


def test_kits_in_another_format_or_kind_are_refused(tmp_path):
    with pytest.raises(studio.StudioUnavailable, match="format 2"):
        studio.load_kit(_kit(tmp_path, format_version=2))
    with pytest.raises(studio.StudioUnavailable, match="isn't a Venture Studio kit"):
        studio.load_kit(_kit(tmp_path, kind="something_else"))
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"format_version": 1, "kind": studio.KIT_KIND, "assets": [{"name": "a", "original_reference": "/a"}]}))
    with pytest.raises(studio.StudioUnavailable, match="project.assets"):
        studio.load_kit(flat)


def test_deeply_nested_json_is_skipped_or_a_reason_never_a_crash(fake, tmp_path):
    """A manifest, maintenance.json or kit nested past Python's recursion limit (well under 1 MiB) reads as unreadable."""
    fake.snapshot("1700000000-1-1", NOW - 7200)
    deep = "[" * 200_000 + "]" * 200_000
    (fake.dir / "backups" / "1700000500-1-1.json").write_text(deep)
    (fake.dir / "maintenance.json").write_text("{" + '"a":' * 100_000 + "1" + "}" * 100_000)
    kit = tmp_path / "venture-project-deep.json"
    kit.write_text(deep)
    with untouched(fake.dir):
        assert [s.id for s in studio.snapshots()] == ["1700000000-1-1"]
        assert studio.settings() == {"automatic_updates": True, "daily_backups": True}
        report = studio.status(offline=True)
        guard, note = studio.output_guard(tmp_path / "out")
    assert report["available"] and report["snapshot"]["id"] == "1700000000-1-1"
    assert guard is None and note is None
    with pytest.raises(studio.StudioUnavailable, match="not readable JSON"):
        studio.load_kit(kit)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_kit_that_is_a_fifo_does_not_hang(tmp_path):
    fifo = tmp_path / "kit.json"
    os.mkfifo(fifo)
    with pytest.raises(studio.StudioUnavailable, match="isn't a file"):
        studio.load_kit(fifo)


# ---------------------------------------------------------------- online, and inside a library


def test_online_follows_studios_rule(tmp_path, monkeypatch):
    real = (tmp_path / "lib").resolve()
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    (tmp_path / "file").write_text("x")
    assert studio.library_online(str(real))
    assert not studio.library_online(str(tmp_path / "missing"))
    assert not studio.library_online(str(link))  # a link where the folder was: not the folder Studio registered
    assert not studio.library_online(str((tmp_path / "file").resolve()))
    # An empty mount point left by a disconnected share is on the same device as /Volumes.
    volumes = (tmp_path / "Volumes").resolve()
    (volumes / "Share").mkdir(parents=True)
    monkeypatch.setattr(studio, "VOLUMES", str(volumes))
    assert not studio.library_online(str(volumes / "Share"))


def test_online_answers_within_its_deadline_when_a_stat_hangs(tmp_path, monkeypatch):
    good = (tmp_path / "good").resolve()
    good.mkdir()
    hung = (tmp_path / "hung").resolve()
    hung.mkdir()
    release = threading.Event()
    real_stat = os.stat

    def stat_(path, *args, **kwargs):
        if str(path) == str(hung):
            release.wait(30)  # a network mount that never answers
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(studio, "os", _os_with(stat=stat_))
    t0 = time.monotonic()
    found = studio.online([str(good), str(hung)], deadline=0.5)
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 2.0 and found == {str(good): True, str(hung): None}


def test_inside_library_compares_whole_folder_names_then_case_and_unicode():
    roots = ["/Shared/Studio", unicodedata.normalize("NFD", "/Shared/Café Library")]
    assert studio.inside_library("/Shared/Studio/Projects/Shoot_roughcut", roots) == "/Shared/Studio"
    assert studio.inside_library("/Shared/Studio", roots) == "/Shared/Studio"
    assert studio.inside_library("/Shared/Studio2/Shoot_roughcut", roots) is None  # a lookalike prefix
    assert studio.inside_library("/Shared/Studi", roots) is None
    assert studio.inside_library("/shared/STUDIO/x", roots) == "/Shared/Studio"
    assert studio.inside_library(unicodedata.normalize("NFC", "/Shared/Café Library/x"), roots) == roots[1]
    assert studio.inside_library("/Shared/Studio/../Elsewhere", roots) is None


def test_the_suggested_folder_mirrors_the_path_below_the_library(tmp_path):
    lib = studio.Library(1, "/Shared/Studio", "Studio: Main")
    home = tmp_path / "home"
    alt = studio.elsewhere_for(Path("/Shared/Studio/Projects/Promo/Shoot_roughcut"), lib, [lib.path], home=home)
    assert alt == home / "Movies" / "Roughcut" / "Studio- Main" / "Projects" / "Promo" / "Shoot_roughcut"
    # Two shoots with the same folder name in different places get different folders.
    other = studio.elsewhere_for(Path("/Shared/Studio/Archive/Shoot_roughcut"), lib, [lib.path], home=home)
    assert other != alt
    # Never a folder inside a library either.
    movies = studio.Library(2, str(home / "Movies"), "Movies")
    assert studio.elsewhere_for(Path("/Shared/Studio/Projects/Shoot_roughcut"), lib, [lib.path, movies.path], home=home) is None


def test_a_library_row_without_an_absolute_path_contains_nothing(tmp_path):
    """A catalog row whose path is empty, '.' or relative isn't a folder anything can be inside."""
    roots = ["", ".", "Shared/Studio", "/Shared/Studio"]
    assert studio.inside_library(Path("/tmp/anything_roughcut"), roots) is None
    assert studio.inside_library(Path("Shared/Studio/x"), roots) is None
    assert studio.inside_library(Path("/Shared/Studio/x"), roots) == "/Shared/Studio"
    out = Path("/Shared/Studio/Projects/Shoot_roughcut")
    home = tmp_path / "home"
    for bad in ("", ".", "Shared/Studio"):
        assert studio.elsewhere_for(out, studio.Library(1, bad, "Old row"), roots, home=home) is None
    # A folder the library doesn't hold gets no suggestion from it (never the output folder itself).
    assert studio.elsewhere_for(Path("/Elsewhere/Shoot_roughcut"), studio.Library(2, "/Shared/Studio", "Studio"), roots, home=home) is None
    assert studio.elsewhere_for(out, studio.Library(2, "/Shared/Studio", "Studio"), roots, home=home) == (
        home / "Movies" / "Roughcut" / "Studio" / "Projects" / "Shoot_roughcut"
    )


def test_a_share_mounted_under_its_duplicate_name_is_the_same_library(fake, tmp_path, monkeypatch):
    """macOS mounts a share at /Volumes/<name>-1 when /Volumes/<name> is taken (a stale mount point): still the library."""
    roots = ["/Volumes/Media/Studio"]
    assert studio.inside_library("/Volumes/Media-1/Studio/Projects/Shoot_roughcut", roots) == roots[0]
    assert studio.inside_library("/volumes/media-12/STUDIO/x", roots) == roots[0]
    assert studio.inside_library("/Volumes/Media-1/Other/x", roots) is None
    assert studio.inside_library("/Volumes/Media-1x/Studio/x", roots) is None
    assert studio.inside_library("/Volumes/Media-/Studio/x", roots) is None
    assert studio.inside_library("/Elsewhere/Media-1/Studio/x", ["/Elsewhere/Media/Studio"]) is None  # only mount points
    fake.libraries = [("/Volumes/Media/Studio", "Studio")]
    fake.snapshot("1700000500-1-1", NOW - 3600)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    guard, note = studio.output_guard(Path("/Volumes/Media-1/Studio/Projects/Shoot_roughcut"))
    assert note is None and guard is not None and guard.message.startswith(GUARD)
    assert "/Volumes/Media-1" in guard.message and "same share" in guard.message
    assert guard.elsewhere == home / "Movies" / "Roughcut" / "Studio" / "Projects" / "Shoot_roughcut"
    assert studio.output_guard(Path("/Volumes/Media/Studio/Shoot_roughcut"))[0].message.count("Media-1") == 0


def test_the_suggested_folder_is_checked_where_it_really_points(tmp_path):
    """~/Movies (or ~/Movies/Roughcut) linked into a library: no suggestion, since it would write inside the library."""
    library = (tmp_path / "Shared" / "Studio Library").resolve()
    (library / "Movies").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / "Movies").symlink_to(library / "Movies")
    lib = studio.Library(1, str(library), "Studio Library")
    out = library / "Projects" / "Shoot_roughcut"
    assert studio.elsewhere_for(out, lib, [lib.path], home=home) is None
    # A link to somewhere outside every library is fine, and the suggestion is where it really points.
    elsewhere = (tmp_path / "Other disk" / "Movies").resolve()
    elsewhere.mkdir(parents=True)
    (home / "Movies").unlink()
    (home / "Movies").symlink_to(elsewhere)
    assert studio.elsewhere_for(out, lib, [lib.path], home=home) == elsewhere / "Roughcut" / "Studio Library" / "Projects" / "Shoot_roughcut"


def test_the_warnings_folder_can_be_pasted_into_terminal(fake, library, tmp_path, monkeypatch):
    import shlex

    monkeypatch.setenv("HOME", str(tmp_path / "my home"))
    fake.snapshot("1700000500-1-1", NOW - 3600)
    guard, _ = studio.output_guard(library / "Projects" / "Promo shoot_roughcut")
    hint = guard.message.split("outside the library: ", 1)[1]
    args = cli.build_parser().parse_args(["build", "/tmp/shoot", *shlex.split(hint)])
    assert args.out == str(guard.elsewhere) and " " in args.out


# ---------------------------------------------------------------- the output guard in runs


@pytest.fixture
def shoot(footage, library, fake) -> Path:
    """Footage inside a Studio library, and a snapshot that lists the library."""
    folder = library / "Projects" / "Promo shoot"
    folder.mkdir(parents=True)
    for name in ("talk.mov", "talk.json", "broll.mp4", "broll.json"):
        shutil.copy2(footage / name, folder / name)
    fake.snapshot("1700000500-1-1", NOW - 3600)
    return folder


def _no_prompt(*args, **kwargs):
    raise AssertionError("asked a question it mustn't")


def _events(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip().startswith("{")]


GUARD = "The output folder is inside the Venture Studio library"


def test_the_app_never_gets_a_prompt_and_render_and_revise_warn_too(shoot, library, fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    cache = tmp_path / "cache"
    out = shoot.parent / "Promo shoot_roughcut"
    with untouched(fake.dir):
        code = main(["build", str(shoot), "--provider", "none", "--transcriber", "none", "--cache-dir", str(cache), "--progress-json"])
    assert code == 0
    events = _events(capsys.readouterr().out)
    done = events[-1]
    assert done["event"] == "done" and any(w.startswith(GUARD) for w in done["result"]["warnings"])
    assert any(e["event"] == "log" and e["message"].startswith(GUARD) for e in events)
    assert out.is_dir() and GUARD in (out / "edit_report.md").read_text() and GUARD in (out / "roughcut.log").read_text()

    # An existing project in the default folder: a Terminal build warns, and doesn't offer to move it.
    assert main(["build", str(shoot), "--provider", "none", "--transcriber", "none", "--cache-dir", str(cache)]) == 0
    printed = capsys.readouterr()
    assert GUARD in printed.out + printed.err and "Save this cut in" not in printed.out

    result = pipeline.rerender(out, Options(inputs=[], cache_dir=cache))
    assert any(w.startswith(GUARD) for w in result.warnings)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    result = pipeline.revise(out, "Tighter.", Options(inputs=[], cache_dir=cache))
    assert any(w.startswith(GUARD) for w in result.warnings)
    assert GUARD in (out / "edit_report.md").read_text()


def test_an_explicit_output_folder_inside_a_library_only_warns(shoot, library, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    out = library / "Projects" / "Cuts"
    assert main(["build", str(shoot), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    printed = capsys.readouterr()
    assert "Save this cut in" not in printed.out and GUARD in printed.out + printed.err
    assert GUARD in (out / "edit_report.md").read_text()


def test_the_terminal_offer_writes_nothing_under_the_library(shoot, library, fake, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")  # Return: yes
    with untouched(library, fake.dir):
        assert main(["build", str(shoot), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    elsewhere = home / "Movies" / "Roughcut" / "Studio Library" / "Projects" / "Promo shoot_roughcut"
    assert len(asked) == 1 and str(elsewhere) in asked[0]
    assert elsewhere.is_dir() and list(elsewhere.glob("*.fcpxml")) and GUARD not in (elsewhere / "edit_report.md").read_text()
    assert not (shoot.parent / "Promo shoot_roughcut").exists()


def test_a_library_row_with_no_path_never_turns_the_offer_back_into_the_library(footage, library, fake, tmp_path, monkeypatch, capsys):
    folder = library / "Projects" / "Promo shoot"
    folder.mkdir(parents=True)
    for name in ("talk.mov", "talk.json", "broll.mp4", "broll.json"):
        shutil.copy2(footage / name, folder / name)
    fake.libraries = [("", "Old row"), (str(library), "Studio Library")]
    fake.snapshot("1700000500-1-1", NOW - 3600)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    cache = str(tmp_path / "cache")
    with untouched(library, fake.dir):
        assert main(["build", str(folder), "--provider", "none", "--transcriber", "none", "--cache-dir", cache]) == 0
    elsewhere = home / "Movies" / "Roughcut" / "Studio Library" / "Projects" / "Promo shoot_roughcut"
    assert len(asked) == 1 and str(elsewhere) in asked[0] and list(elsewhere.glob("*.fcpxml"))
    # A build outside every library hears nothing about the row with no path.
    capsys.readouterr()
    out = tmp_path / "outside"
    assert main(["build", str(folder), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", cache]) == 0
    printed = capsys.readouterr()
    assert "Venture Studio" not in printed.out + printed.err + (out / "edit_report.md").read_text()


def test_movies_linked_into_the_library_gets_no_offer_and_nothing_written_through_it(shoot, library, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (library / "Movies").mkdir()
    (home / "Movies").symlink_to(library / "Movies")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    with untouched(library / "Movies"):
        assert main(["build", str(shoot), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    out = shoot.parent / "Promo shoot_roughcut"
    report = (out / "edit_report.md").read_text()
    assert GUARD in report and "Movies/Roughcut" not in report


def test_no_menu_asks_nothing_and_its_help_says_so(shoot, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert main(["build", str(shoot), "--no-menu", "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    assert GUARD in (shoot.parent / "Promo shoot_roughcut" / "edit_report.md").read_text()
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["build", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "default output folder" in help_text.rsplit("--no-menu", 1)[1][:200]  # its entry, after the usage line


def test_declining_the_offer_keeps_the_default_folder(shoot, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["build", str(shoot), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]) == 0
    out = shoot.parent / "Promo shoot_roughcut"
    assert GUARD in (out / "edit_report.md").read_text()
    assert not (tmp_path / "home" / "Movies").exists()


def test_a_build_outside_every_library_says_nothing_about_studio(footage, library, fake, tmp_path):
    fake.snapshot("1700000500-1-1", NOW - 3600)
    out = tmp_path / "out"
    result = pipeline.build(Options(inputs=[footage], out=out, provider="none", transcriber="none", cache_dir=tmp_path / "cache"))
    assert not any("Venture Studio" in w for w in result.warnings)
    assert "Venture Studio" not in (out / "roughcut.log").read_text()


def test_a_build_without_studio_says_nothing_about_it(footage, tmp_path):
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, provider="none", transcriber="none", cache_dir=tmp_path / "cache"))
    assert "Venture Studio" not in (out / "roughcut.log").read_text() + (out / "edit_report.md").read_text()


def test_the_wording_follows_studios_automatic_updates(fake, library):
    fake.snapshot("1700000500-1-1", NOW - 3600)
    out = library / "Projects" / "Shoot_roughcut"
    guard, _ = studio.output_guard(out)
    assert "within about a minute" in guard.message and guard.library.name == "Studio Library"
    (fake.dir / "maintenance.json").write_text(json.dumps({"settings": {"automatic_updates": False, "daily_backups": True}}))
    guard, _ = studio.output_guard(out)
    assert "on its next scan" in guard.message
    assert not out.exists()


# ---------------------------------------------------------------- status and doctor


def test_status_and_doctor_report_studio(fake, library, tmp_path, capsys, monkeypatch):
    fake.snapshot("1700000500-1-1", NOW - 3 * 3600, assets=[_asset(1, "Sound Effects/whoosh.wav", kind="audio", category="audio")])
    (library / "Projects" / "Shoot").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with untouched(fake.dir, library):
        assert main(["studio", "status", "--json", "--where", str(library / "Projects" / "Shoot")]) == 0
        report = json.loads(capsys.readouterr().out)
        assert main(["studio", "status"]) == 0
        text = capsys.readouterr().out
        assert main(["doctor", "--json", "--offline"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        main(["doctor", "--offline"])
        doctor_text = capsys.readouterr().out
    assert report["available"] and report["items"] == 1 and report["snapshot"]["id"] == "1700000500-1-1"
    assert report["libraries"] == [{"name": "Studio Library", "path": str(library), "online": True}]
    assert report["summary"].startswith("catalog snapshot from ") and "3 h old" in report["summary"] and "1 items" not in report["summary"]
    where = report["where"]
    assert where["inside"] and where["output_inside"] and where["root"] == str(library)
    assert where["elsewhere"] == str(tmp_path / "home" / "Movies" / "Roughcut" / "Studio Library" / "Projects" / "Shoot_roughcut")
    assert not (tmp_path / "home").exists()
    assert "Catalog snapshot:" in text and "Items: 1" in text and f"Studio Library: {library} (online)" in text
    vs = doctor["venture_studio"]
    assert vs["available"] and vs["libraries"][0]["online"] is None and "(not checked)" in vs["summary"]
    assert "Venture Studio" in doctor_text and "catalog snapshot from" in doctor_text


def test_doctor_without_studio_and_its_exit_code(fake, capsys, monkeypatch):
    # Installed with no snapshot: a reason, and doctor's exit code is what it would be without Studio.
    main(["doctor", "--json", "--offline"])
    vs = json.loads(capsys.readouterr().out)["venture_studio"]
    assert vs["installed"] and not vs["available"] and vs["reason"]
    with_studio = main(["doctor", "--offline"])
    capsys.readouterr()
    monkeypatch.setenv(studio.STUDIO_ENV, str(fake.dir.parent / "nowhere"))
    without = main(["doctor", "--offline"])
    printed = capsys.readouterr().out
    assert with_studio == without
    assert "Venture Studio  optional: not installed" in printed


def test_status_text_when_studio_is_not_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    assert main(["studio", "status", "--where", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Venture Studio: not installed" in out and "isn't inside a Venture Studio library" in out


# ---------------------------------------------------------------- sound effects from Studio (--sfx-from)


def _sounds(library: Path) -> None:
    """Real audio in the library, where Studio's catalog says it is."""
    folder = library / "Sound Effects" / "Accents"
    folder.mkdir(parents=True, exist_ok=True)
    swell = "volume='exp(-((t-0.5)*(t-0.5))/0.02)':eval=frame"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=1:r=48000", "-af", swell, str(folder / "whoosh_soft_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=0.8:r=48000", "-af", swell, str(folder / "take_02.wav"))  # classed by its Studio tags
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.9:d=1.2:r=48000", "-af", "volume='exp(-4*t)':eval=frame", str(folder / "impact_hit_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=0.5:sample_rate=48000", str(folder / "whoosh_ogg.flac"))
    (library / "Sound Effects" / "Template").mkdir(exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=0.5:sample_rate=48000", str(library / "Sound Effects" / "Template" / "whoosh_inside.wav"))


def _sfx_catalog(fake: FakeStudio, *, kits=None) -> None:
    rel = "Sound Effects/Accents/"
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[
        _asset(1, rel + "whoosh_soft_01.wav", kind="audio", category="audio", provider="Vendor A", license="Standard", duration=1.0),
        _asset(2, rel + "take_02.wav", kind="audio", category="audio", tags="transition, fast", provider="Vendor A", license="Standard"),
        _asset(3, rel + "impact_hit_01.wav", kind="audio", category="audio", provider="Vendor B", license="Extended"),
        _asset(4, rel + "whoosh_ogg.flac", kind="audio", category="audio"),  # Studio lists it; Roughcut doesn't read it
        _asset(5, "Sound Effects/Template/whoosh_inside.wav", kind="audio", category="audio", browse_visible=0),  # inside a template
        _asset(6, rel + "whoosh_gone.wav", kind="audio", category="audio", missing=1),
        _asset(7, rel + "whoosh_moved.wav", kind="audio", category="audio"),  # the snapshot is a day old: moved since
        _asset(8, rel + "long_whoosh.wav", kind="audio", category="audio", duration=30.0),  # Studio's length says it's no accent
        _asset(9, "Footage/clip.mov", kind="video", category="video"),
    ], collections={"Promo Sounds": [1, 2, 3, 4, 5, 6, 7, 8, 9]}, kits=kits)


def test_sound_effects_from_a_studio_collection(footage, fake, library, tmp_path, monkeypatch, only_the_cache_is_opened):
    _sounds(library)
    _sfx_catalog(fake)
    provider = FakeProvider(dict(CANNED_PLAN, sfx="soft"))
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    with untouched(fake.dir, library):
        assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache"),
                     "--sfx-from", "promo sounds"]) == 0
    assert all(p.parent.name == studio.CACHE_FOLDER for p in only_the_cache_is_opened)
    saved = json.loads((out / "plan.json").read_text())
    pool = sfx.Pool.from_json(saved["settings"]["sfx_pool"])
    assert sorted(s.name for s in pool.sounds) == ["impact_hit_01.wav", "take_02.wav", "whoosh_soft_01.wav"]
    take = next(s for s in pool.sounds if s.name == "take_02.wav")
    assert take.kind == "whoosh" and take.asset_id == 2 and (take.provider, take.license) == ("Vendor A", "Standard")
    assert take.origin == "collection promo sounds" and take.path == f"{library}/Sound Effects/Accents/take_02.wav"
    report = (out / "edit_report.md").read_text()
    section = report.split("## Sound effects")[1].split("\n## ")[0]
    assert 'the Venture Studio collection "promo sounds"' in section and "Provider and license" in section and "Vendor A, Standard" in section
    assert "whoosh_ogg.flac" in section and "long_whoosh.wav" in section
    assert "whoosh_inside" not in report and "whoosh_gone" not in report.split("## Warnings")[0]
    assert any("no longer there (whoosh_moved.wav)" in w for w in json.loads((out / "result.json").read_text())["warnings"])
    xml = next(out.glob("*.fcpxml")).read_text()
    assert "effects.SFX" in xml and (library / "Sound Effects" / "Accents").as_uri() + "/" in xml, "referenced in place, in the library"


def test_a_collection_typed_for_a_build_must_exist_and_a_presets_need_not(footage, fake, library, tmp_path, monkeypatch, capsys):
    _sfx_catalog(fake, kits={"Promo Sounds": [3], "Promo Kit": [3]})
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    out = tmp_path / "out"
    base = ["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]
    assert main(base + ["--sfx-from", "Whooshs"]) == 1
    assert "Venture Studio has no collection or kit named 'Whooshs'" in capsys.readouterr().err
    assert not out.exists(), "stopped before the output folder was made"
    assert main(base + ["--sfx-from", "kit:Promo Sounds 2"]) == 1 and not out.exists()
    # The same name from a preset (or a re-cut) is a warning: the run goes on without it.
    assert main(base + ["--preset-sfx-from", "Whooshs"]) == 0
    assert any("Whooshs" in w for w in json.loads((out / "result.json").read_text())["warnings"])
    # A name that's both a collection and a kit: the collection, said so; "kit:" picks the kit.
    src = sfx.sources([], ["Promo Sounds"], strict=["Promo Sounds"])
    assert len(src.items) == 6 and "both a collection and a kit" in src.notes[-1]  # its audio, by Studio's browse filter
    src = sfx.sources([], ["kit:promo sounds"], strict=["kit:promo sounds"])
    assert [c.name for c in src.items] == ["impact_hit_01.wav"] and src.items[0].origin == "kit promo sounds"


def test_studio_problems_never_stop_a_build(footage, tmp_path, monkeypatch):
    """Not installed, no snapshot, a library that's offline: warnings, and the cut goes on without those sounds."""
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    result = pipeline.build(Options(inputs=[footage], out=tmp_path / "a", transcriber="none", cache_dir=tmp_path / "cache",
                                    sfx_from=("Promo Sounds",), sfx_named=("Promo Sounds",)))
    assert result.timeline.accents == [] and any("Venture Studio is not installed" in w for w in result.warnings)


def test_an_offline_library_drops_its_sounds(fake, library, tmp_path):
    _sounds(library)
    _sfx_catalog(fake)
    src = sfx.sources([], ["Promo Sounds"])
    shutil.move(str(library), str(library.parent / "unplugged"))  # the share isn't mounted any more
    try:
        pool = sfx.build_pool(src, analyze.Cache(tmp_path / "c"))
    finally:
        shutil.move(str(library.parent / "unplugged"), str(library))
    assert pool.sounds == [] and "is offline" in pool.warnings[0] and "Studio Library" in pool.warnings[0]


def test_sound_effects_from_a_kit_studio_exported(footage, fake, library, tmp_path, monkeypatch):
    _sounds(library)
    rel = library / "Sound Effects" / "Accents"
    kit = _kit(tmp_path, project={"kit_id": 1, "name": "Promo kit", "description": "", "assets": [
        {"asset_id": 1, "name": "Soft whoosh", "kind": "audio", "extension": "wav", "provider": "Vendor A", "license": "Standard",
         "original_reference": str(rel / "whoosh_soft_01.wav"), "catalog_missing": False, "readiness": "ready"},
        {"asset_id": 3, "name": "impact_hit_01.wav", "kind": "audio", "extension": "wav", "original_reference": str(rel / "impact_hit_01.wav")},
        {"asset_id": 9, "name": "clip.mov", "kind": "video", "extension": "mov", "original_reference": str(library / "clip.mov")},
    ]})
    pool = sfx.build_pool(sfx.sources([], [str(kit)], strict=[str(kit)]), analyze.Cache(tmp_path / "c"))
    assert sorted((s.name, s.kind) for s in pool.sounds) == [("impact_hit_01.wav", "hit"), ("whoosh_soft_01.wav", "whoosh")]
    assert pool.asked == ['the Venture Studio kit file "venture-project-promo-kit.json"']
    with pytest.raises(sfx.SourceError, match="isn't a Venture Studio kit"):
        bad = tmp_path / "notes.json"
        bad.write_text("{}")
        sfx.sources([], [str(bad)], strict=[str(bad)])

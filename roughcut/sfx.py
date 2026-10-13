"""Sound-effect accents, part one: the pool of sounds to place (whooshes, risers, pops and hits).

Sounds come from `--sfx` (an audio file, or a folder searched through its subfolders) and from
`--sfx-from` (a Venture Studio collection or kit by name, read through studio.py's verified copy of
Studio's catalog, or a kit file Studio exported). Each file is classed by the words in its name,
its Studio tags and its folders. Only audio-only files in the formats Roughcut reads, at most
MAX_SECONDS long, are kept; the rest are listed as skipped, never placed. Each kept sound is probed
and measured once (cached by its fingerprint in Roughcut's cache): its length, its hit (the start of
its loudest 50 ms), and its loudness with silence around it, so a 150 ms pop gets a reading too.

Every touch of these files (a folder listing, a stat, ffprobe, ffmpeg) runs under textio.bounded:
a network share that is offline or stops answering drops its sounds with a warning and never
hangs or fails the run. Files are only read: nothing is written beside them, and Studio's own
folder is read only through studio.py. The pool is saved in plan.json (`Pool.to_json`), so
`render` and `revise` place the same sounds again without reading the share or Studio.

accents.py places them on the cut's own frames.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PurePosixPath

from . import ffmpeg_ops, progress
from .analyze import Cache, Clip, log
from .media import AUDIO_EXTS, SKIP_DIR_SUFFIXES, MediaError, MediaInfo, fingerprint, probe
from .textio import Stalled, bounded, is_junk

CLASSES = ("whoosh", "riser", "pop", "hit")
# The words that class a sound, in one place: its file name first, then its Studio tags, then its folders.
CLASS_WORDS: dict[str, tuple[str, ...]] = {
    "whoosh": ("whoosh", "swoosh", "swish", "woosh", "swipe", "sweep", "pass by", "passby", "fly by", "flyby", "whip", "transition"),
    "riser": ("riser", "rise", "rising", "uplifter", "build", "buildup", "build up", "tension", "swell"),
    "pop": ("pop", "click", "tick", "blip", "bubble", "tap", "ui", "notification"),
    "hit": ("hit", "impact", "boom", "punch", "thud", "slam", "stinger", "braam"),
}
MAX_SECONDS = 8.0  # longer isn't an accent (music, ambience), whatever its name says
MAX_FILES = 400  # sounds probed per run; the classes take turns, so a big folder of one kind can't crowd out the rest
PAD = 0.4  # seconds of silence around a sound when measuring its loudness (ebur128's window is 400 ms)
FILE_SECONDS = 20.0  # one file's stat, probe and measurements together
SCAN_SECONDS = 30.0  # listing one --sfx folder
POOL_SECONDS = 240.0  # the whole pool; the rest waits for the next run (each sound is cached once read)
SCAN_LIMIT = 50_000  # entries looked at in one folder tree
CACHE_NAME = "sfx-v1.json"
IMPORT_MARKER = ".venture-studio-import-incomplete.json"  # a folder Studio is still copying files into
# Audio Roughcut doesn't read (Final Cut's import of them is untested): listed as skipped, never placed.
OTHER_AUDIO = {".flac", ".ogg", ".oga", ".opus", ".wma", ".mka", ".ape", ".aifc", ".m4b", ".mp2", ".ac3", ".dsf", ".wv"}
ID_PREFIX = "X"  # clip IDs X01, X02...: V, A, I and G are taken
VOLUMES = "/Volumes"


class SourceError(RuntimeError):
    """A sound source named on the command line that doesn't exist: the build stops before its folder is made."""


# ------------------------------------------------------------------ classing


def _tokens(text: str) -> list[str]:
    """'WhooshSoft_01', 'UIClick' -> ['whoosh', 'soft', '01'], ['ui', 'click']."""
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _singular(token: str, known: set[str]) -> str:
    for cut in ("es", "s"):
        if token.endswith(cut) and token[: -len(cut)] in known:
            return token[: -len(cut)]
    return token


_KNOWN = {w for words in CLASS_WORDS.values() for phrase in words for w in phrase.split()}


def class_of_text(text: str) -> tuple[str, int] | None:
    """(class, position) of the first class word in `text`, or None."""
    tokens = [_singular(t, _KNOWN) for t in _tokens(text)]
    best: tuple[int, int, str] | None = None  # (position, -length, class)
    for kind, words in CLASS_WORDS.items():
        for phrase in words:
            want = phrase.split()
            for i in range(len(tokens) - len(want) + 1):
                if tokens[i : i + len(want)] == want:
                    found = (i, -len(want), kind)
                    if best is None or found < best:
                        best = found
                    break
    return (best[2], best[0]) if best else None


def classify(name: str, tags: tuple[str, ...] = (), folders: tuple[str, ...] = ()) -> str | None:
    """whoosh, riser, pop, hit or None: by the file's name, then its Studio tags, then its folders (nearest first)."""
    for text in (Path(name).stem if Path(name).suffix.lower() in AUDIO_EXTS | OTHER_AUDIO else name, *tags, *folders):
        found = class_of_text(text)
        if found:
            return found[0]
    return None


# ------------------------------------------------------------------ what was asked for


@dataclass
class Candidate:
    """A file that may become a sound, before anything on its disk is touched."""

    path: str  # absolute
    name: str  # the file's name
    tags: tuple[str, ...] = ()  # Studio's name for it (when it differs) and its tags
    folders: tuple[str, ...] = ()  # folder names above it, nearest first (inside what was given)
    origin: str = ""  # "folder", "collection NAME", "kit NAME"
    key: str = ""  # what stops answering together: the share or folder it's on
    asset_id: int | None = None
    provider: str = ""
    license: str = ""
    known_seconds: float | None = None  # Studio's length, when it has one
    root: str = ""  # its library root (Studio items): no link may lead out of it
    from_studio: bool = False

    @property
    def kind(self) -> str | None:
        return classify(self.name, self.tags, self.folders)


@dataclass
class Sources:
    """Where the sounds come from: folders and files to list, and Studio items (not yet checked on disk)."""

    paths: list[str] = field(default_factory=list)
    items: list[Candidate] = field(default_factory=list)
    roots: dict[str, str] = field(default_factory=dict)  # Studio library root -> its name, checked online before use
    asked: list[str] = field(default_factory=list)  # how to name the sources in the paper edit
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _share_key(path: str, fallback: str) -> str:
    parts = PurePosixPath(path).parts
    if len(parts) > 2 and "/" + parts[1] == VOLUMES:
        return f"{VOLUMES}/{parts[2]}"
    return fallback


def _which(name: str) -> tuple[str | None, str]:
    """'kit:Promo SFX' -> ('kit', 'Promo SFX'); 'collection:X' -> ('collection', 'X'); 'X' -> (None, 'X')."""
    head, sep, rest = name.partition(":")
    if sep and head.strip().lower() in ("kit", "collection") and rest.strip():
        return head.strip().lower(), rest.strip()
    return None, name.strip()


def is_kit_file(name: str) -> bool:
    """A --sfx-from value that names a kit file Studio exported, not a collection or kit in its catalog
    ("collection:Hits/Impacts" is a collection, slash and all)."""
    if _which(name)[0]:
        return False
    return name.strip().lower().endswith(".json") or "/" in name


def sources(
    paths, names, *, strict=(), cache_dir: Path | None = None, studio_folder: Path | None = None,
) -> Sources:
    """What --sfx and --sfx-from name. Reads Studio's catalog copy (and kit files), never the sound files.

    `strict`: the names typed for this build. One that Studio's catalog doesn't have (or a kit file that
    can't be read) raises SourceError, before the build makes its folder; any other problem, and every
    Studio problem (not installed, no snapshot), is a warning and the run goes on without those sounds.
    """
    from . import studio

    out = Sources(paths=[str(p) for p in paths or ()])
    out.asked += [str(p) for p in out.paths]
    strict = set(strict or ())
    names = list(dict.fromkeys(str(n).strip() for n in names or () if str(n).strip()))

    def problem(name: str, text: str) -> None:
        if name in strict:
            raise SourceError(text)
        out.warnings.append(text + " This cut has no sound effects from it.")

    for name in [n for n in names if is_kit_file(n)]:
        try:
            kit = studio.load_kit(Path(name).expanduser())
        except studio.StudioUnavailable as e:
            problem(name, f"--sfx-from {name}: {e.reason}.")
            continue
        out.asked.append(f'the Venture Studio kit file "{Path(name).name}"')
        for item in kit.items:
            if item.kind and item.kind != "audio":
                continue
            out.items.append(Candidate(
                path=item.path, name=Path(item.path).name, tags=_also(item.name, Path(item.path).name), origin=f"kit {kit.name}", key=_share_key(item.path, str(Path(item.path).parent)),
                asset_id=item.asset_id, provider=item.provider, license=item.license, folders=_folders_of(item.path, ""), from_studio=True,
            ))
        if kit.held_back:
            out.notes.append(f"{len(kit.held_back)} item{'s' if len(kit.held_back) != 1 else ''} of the kit {kit.name!r} held back by Studio (missing or needing attention).")
    catalog_names = [n for n in names if not is_kit_file(n)]
    if not catalog_names:
        return out
    try:
        copy = studio.newest_copy(studio_folder, cache_dir)
        out.notes += copy.notes()
        with studio.catalog(copy) as con:
            for name in catalog_names:
                want, bare = _which(name)
                is_collection, is_kit = studio.named(con, bare)
                if want == "kit" and not is_kit or want == "collection" and not is_collection or not (is_collection or is_kit):
                    what = want or "collection or kit"
                    problem(name, (
                        f"--sfx-from {name}: Venture Studio has no {what} named {bare!r} in its catalog snapshot from {copy.snapshot.when()} "
                        "(one made since then shows up after Studio's next snapshot: in Studio, click Back up catalog now)."
                    ))
                    continue
                use_kit = want == "kit" or (want is None and is_kit and not is_collection)
                if want is None and is_collection and is_kit:
                    out.notes.append(f"{bare!r} is both a collection and a kit in Venture Studio: the collection's sounds are used (--sfx-from \"kit:{bare}\" for the kit's).")
                found = studio.assets(con, **({"kit": bare} if use_kit else {"collection": bare}))
                label = f"{'kit' if use_kit else 'collection'} {bare}"
                out.asked.append(f'the Venture Studio {label.split(" ", 1)[0]} "{bare}"')
                for a in found:
                    if a.kind != "audio" and a.category != "audio":
                        continue
                    out.roots[a.library_root] = a.library
                    out.items.append(Candidate(
                        path=a.path, name=Path(a.relative_path).name, tags=_also(a.title, Path(a.relative_path).name) + a.tags, folders=_folders_of(a.relative_path, a.library), origin=label,
                        key=_share_key(a.path, a.library_root), asset_id=a.id, provider=a.provider, license=a.license,
                        known_seconds=a.duration, root=a.library_root, from_studio=True,
                    ))
    except studio.StudioUnavailable as e:
        out.warnings.append(
            f"Sound effects from Venture Studio ({', '.join(catalog_names)}): {e.reason if not e.silent else 'Venture Studio is not installed on this Mac'}. "
            "This cut has none from there."
        )
    return out


def _also(title: str, name: str) -> tuple[str, ...]:
    """Studio's name for an item, when it isn't just the file's name."""
    title = (title or "").strip()
    return (title,) if title and title not in (name, Path(name).stem) else ()


def _folders_of(relative: str, top: str) -> tuple[str, ...]:
    """Folder names above a file, nearest first, then `top` (the folder or library it's in)."""
    parents = [p for p in PurePosixPath(relative).parent.parts if p not in ("/", "")]
    return tuple(reversed(parents)) + ((top,) if top else ())


# ------------------------------------------------------------------ listing folders


def _scan(path: str) -> tuple[list[Candidate], list[str], list[str], bool]:
    """(audio files, names of audio Roughcut doesn't read, folders skipped mid-import, listing cut short). Runs bounded."""
    p = Path(path)
    st = os.stat(p)
    key = _share_key(str(p), str(p))
    if stat.S_ISREG(st.st_mode):
        ext = p.suffix.lower()
        if ext in AUDIO_EXTS:
            return [Candidate(str(p), p.name, folders=(p.parent.name,), origin="folder", key=key)], [], [], False
        return [], [p.name] if ext in OTHER_AUDIO else [], [], False
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{p} isn't a file or folder")
    found: list[Candidate] = []
    other: list[str] = []
    importing: list[str] = []
    seen = 0
    for root, dirs, files in os.walk(p):
        if IMPORT_MARKER in files:  # Studio is still copying into this folder: its files may be partial
            importing.append(PurePosixPath(root).name + "/")
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.endswith(SKIP_DIR_SUFFIXES))
        rel = PurePosixPath(root).relative_to(PurePosixPath(str(p)))
        folders = tuple(reversed([x for x in rel.parts if x not in (".", "")])) + (p.name,)
        for f in sorted(files):
            seen += 1
            if seen > SCAN_LIMIT:
                return found, other, importing, True
            if f.startswith(".") or is_junk(f):
                continue
            ext = Path(f).suffix.lower()
            if ext in AUDIO_EXTS:
                found.append(Candidate(str(PurePosixPath(root) / f), f, folders=folders, origin="folder", key=key))
            elif ext in OTHER_AUDIO:
                other.append(f)
    return found, other, importing, False


# ------------------------------------------------------------------ the sounds


@dataclass
class Sound:
    """One sound in the pool: what it is, where it hits and how loud it is, measured once."""

    id: str
    kind: str  # whoosh / riser / pop / hit
    media: MediaInfo
    hit: float  # seconds into the file where it lands on its event (a riser: its end)
    lufs: float | None  # loudness, measured with silence around it
    peak: float | None  # sample peak, dBFS
    origin: str = ""
    asset_id: int | None = None
    provider: str = ""
    license: str = ""

    @property
    def path(self) -> str:
        return self.media.path

    @property
    def name(self) -> str:
        return self.media.name

    def clip(self) -> Clip:
        """The clip Final Cut gets: on the effects.SFX role, its class as a keyword."""
        return Clip(id=self.id, media=self.media, role="sfx", transcript_source="sfx", visual={"tags": [self.kind]})

    def to_json(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "media": self.media.to_json(), "hit": self.hit, "lufs": self.lufs, "peak": self.peak,
            "origin": self.origin, "asset_id": self.asset_id, "provider": self.provider, "license": self.license,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Sound":
        return cls(
            id=str(d["id"]), kind=str(d["kind"]), media=MediaInfo.from_json(d["media"]), hit=float(d.get("hit") or 0.0),
            lufs=_num(d.get("lufs")), peak=_num(d.get("peak")), origin=str(d.get("origin") or ""),
            asset_id=d.get("asset_id") if isinstance(d.get("asset_id"), int) else None,
            provider=str(d.get("provider") or ""), license=str(d.get("license") or ""),
        )


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@dataclass
class Pool:
    """The sounds a cut may use, and what was left out of it and why."""

    sounds: list[Sound] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    skipped: dict[str, dict] = field(default_factory=dict)  # reason -> {"count": n, "names": [a few]}
    warnings: list[str] = field(default_factory=list)  # for this run's paper edit (not saved)
    notes: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Sound]:
        """This class's sounds in the order they take turns: by path."""
        return sorted((s for s in self.sounds if s.kind == kind), key=lambda s: s.path)

    def sound(self, sound_id: str) -> Sound | None:
        return next((s for s in self.sounds if s.id == sound_id), None)

    def counts(self) -> dict[str, int]:
        return {k: sum(1 for s in self.sounds if s.kind == k) for k in CLASSES}

    def skip(self, reason: str, name: str) -> None:
        entry = self.skipped.setdefault(reason, {"count": 0, "names": []})
        entry["count"] += 1
        if len(entry["names"]) < 5:
            entry["names"].append(name)

    def to_json(self) -> dict:
        return {"sounds": [s.to_json() for s in self.sounds], "asked": self.asked, "skipped": self.skipped, "notes": self.notes}

    @classmethod
    def from_json(cls, d) -> "Pool":
        if not isinstance(d, dict):
            return cls()
        sounds = []
        for s in d.get("sounds") or []:
            try:
                sounds.append(Sound.from_json(s))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue  # a hand-edited plan.json: only whole entries count
        skipped = {str(k): v for k, v in (d.get("skipped") or {}).items() if isinstance(v, dict)}
        return cls(sounds=sounds, asked=[str(a) for a in d.get("asked") or []], skipped=skipped, notes=[str(n) for n in d.get("notes") or []])


SKIP_REASONS = {
    "unclassified": "no whoosh, riser, pop or hit in their names, tags or folders",
    "unsupported": "in a format Roughcut doesn't read",
    "long": f"over {MAX_SECONDS:g} s (not an accent)",
    "pictures": "with pictures (sound effects are audio files)",
    "unreadable": "unreadable",
    "silent": "silent",
    "links": "links or not plain files",
    "importing": "in a folder Venture Studio is still copying files into",
    "offline": "on a folder or share that's offline or didn't answer",
    "over_cap": f"over the {MAX_FILES} Roughcut reads in one run",
    "late": "not read in the time a run gives them (the next run reads more)",
}


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_dirs_lock = threading.Lock()


def _importing(path: str, root: str, seen: dict[str, bool]) -> bool:
    """Whether a Studio item sits in a folder Studio is still importing into (the marker in it or a folder above, up to its library)."""
    folder = os.path.dirname(path)
    top = root.rstrip("/") or "/"
    while True:
        with _dirs_lock:
            known = seen.get(folder)
        if known is None:
            known = os.path.lexists(os.path.join(folder, IMPORT_MARKER))
            with _dirs_lock:
                seen[folder] = known
        if known:
            return True
        if not top or folder == top or len(folder) <= len(top) or folder in ("/", ""):
            return False
        folder = os.path.dirname(folder)


def _measure(c: Candidate, cache: Cache, seen: dict[str, bool]) -> dict:
    """Probe and measure one file (or take what the cache has for this version of it). Runs bounded."""
    st = os.lstat(c.path) if c.from_studio else os.stat(c.path)  # Studio's rule: no links; your own folders may hold them
    if not stat.S_ISREG(st.st_mode):
        raise _Skip("links")
    if c.from_studio and c.root and _importing(c.path, c.root, seen):
        raise _Skip("importing")
    fp = fingerprint(Path(c.path))
    cached = cache.get(fp, CACHE_NAME)
    if isinstance(cached, dict) and isinstance(cached.get("media"), dict):
        return cached
    info = probe(Path(c.path))
    out: dict = {"media": info.to_json()}
    if info.kind != "audio":
        out["error"] = "pictures"
    elif float(info.duration) > MAX_SECONDS:
        out["error"] = "long"
    else:
        peaks = ffmpeg_ops.window_peaks(Path(c.path))
        blocks = ffmpeg_ops.loudness_blocks(Path(c.path), pad=PAD) or {}
        summary = ffmpeg_ops.level_summary(blocks) if blocks else {"lufs": None, "peak": None}
        loudest = ffmpeg_ops.loudest_window(peaks)
        peak = summary.get("peak")
        if peak is not None and peak <= -119:
            peak = None
        if peaks and peak is None:
            peak = round(max(peaks), 1) if max(peaks) > -119 else None
        out.update(loudest=loudest, lufs=summary.get("lufs"), peak=peak)
    cache.put(fp, CACHE_NAME, out)
    return out


def _take_turns(found: list[Candidate], limit: int) -> tuple[list[Candidate], list[Candidate]]:
    """At most `limit` files, the classes taking turns (each in path order): (kept, left over)."""
    by_kind = {k: sorted((c for c in found if c.kind == k), key=lambda c: c.path) for k in CLASSES}
    kept: list[Candidate] = []
    i = 0
    while len(kept) < limit and any(i < len(v) for v in by_kind.values()):
        for k in CLASSES:
            if i < len(by_kind[k]) and len(kept) < limit:
                kept.append(by_kind[k][i])
        i += 1
    chosen = {id(c) for c in kept}
    return kept, [c for c in found if id(c) not in chosen and c.kind]


def build_pool(src: Sources, cache: Cache, *, workers: int = 4) -> Pool:
    """List, class, probe and measure the sounds `src` names, each under a deadline."""
    from . import studio

    pool = Pool(asked=list(src.asked), warnings=list(src.warnings), notes=list(src.notes))
    started = time.monotonic()
    found: list[Candidate] = []
    with progress.doing("looking for sound effects", external=True):
        for path in src.paths:
            try:
                files, other, importing, cut_short = bounded(_scan, path, seconds=SCAN_SECONDS, what=f"listing {path}")
            except Stalled:
                pool.warnings.append(f"--sfx {path}: the folder didn't answer in {SCAN_SECONDS:g} s (a network share that stopped responding?); this cut has no sound effects from it.")
                continue
            except FileNotFoundError:
                pool.warnings.append(f"--sfx {path}: can't find it (moved, renamed, or a share that isn't connected?); this cut has no sound effects from it.")
                continue
            except OSError as e:
                pool.warnings.append(f"--sfx {path}: couldn't read it ({e.strerror or e}); this cut has no sound effects from it.")
                continue
            found += files
            for name in other:
                pool.skip("unsupported", name)
            for folder in importing:
                pool.skip("importing", folder)
            if cut_short:
                pool.notes.append(f"--sfx {path} holds more than {SCAN_LIMIT:,} files; only the first ones were looked at.")
        items = list(src.items)
        if src.roots:
            up = studio.online(list(src.roots))
            for root, ok in up.items():
                if ok:
                    continue
                gone = [c for c in items if c.root == root]
                items = [c for c in items if c.root != root]
                if gone:
                    pool.warnings.append(
                        f"The Venture Studio library {src.roots[root]!r} ({root}) is {'offline' if ok is False else 'not answering'}, so its "
                        f"{len(gone)} sound effect{'s' if len(gone) != 1 else ''} aren't in this cut."
                    )
                    for c in gone:
                        pool.skip("offline", c.name)
        found += items
    # One file given twice (a folder and a collection holding it) counts once.
    unique: dict[str, Candidate] = {}
    for c in found:
        unique.setdefault(c.path, c)
    found = list(unique.values())
    classed = []
    for c in found:
        ext = Path(c.name).suffix.lower()
        if ext not in AUDIO_EXTS:
            if ext in OTHER_AUDIO:
                pool.skip("unsupported", c.name)
            continue
        if c.kind is None:
            pool.skip("unclassified", c.name)
        elif c.known_seconds is not None and c.known_seconds > MAX_SECONDS:
            pool.skip("long", c.name)
        else:
            classed.append(c)
    todo, over = _take_turns(classed, MAX_FILES)
    for c in over:
        pool.skip("over_cap", c.name)
    if not todo:
        if found or src.paths or src.items:
            log("sound effects: none usable " + _skipped_text(pool))
        return pool

    dead: set[str] = set()
    lock = threading.Lock()
    seen_dirs: dict[str, bool] = {}
    deadline = started + POOL_SECONDS

    def one(c: Candidate):
        with lock:
            if c.key in dead:
                return c, "offline", None
        if time.monotonic() > deadline:
            return c, "late", None
        try:
            return c, None, bounded(_measure, c, cache, seen_dirs, seconds=FILE_SECONDS, what=c.name)
        except Stalled:
            with lock:
                first = c.key not in dead
                dead.add(c.key)
            if first:
                pool.warnings.append(
                    f"{c.key} stopped answering while Roughcut read {c.name} (a network share that went to sleep or dropped?): "
                    "its sound effects aren't in this cut."
                )
            return c, "offline", None
        except _Skip as e:
            return c, e.reason, None
        except FileNotFoundError:
            return c, "gone", None
        except (OSError, MediaError, ValueError) as e:
            return c, "unreadable", str(e)

    log(f"reading {len(todo)} sound effects (each is measured once, then cached)")
    with progress.doing(f"reading {len(todo)} sound effects", external=True), ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(one, todo))
    gone = []
    for c, reason, rec in results:
        if reason == "gone":
            gone.append(c)
            continue
        if reason:
            pool.skip(reason, c.name)
            continue
        if rec.get("error"):
            pool.skip(rec["error"], c.name)
            continue
        try:
            info = MediaInfo.from_json(rec["media"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pool.skip("unreadable", c.name)
            continue
        lufs, peak = _num(rec.get("lufs")), _num(rec.get("peak"))
        if lufs is None and peak is None:
            pool.skip("silent", c.name)
            continue
        kind = c.kind
        hit = float(info.duration) if kind == "riser" else float(rec.get("loudest") or 0.0)
        info.path, info.name = c.path, Path(c.path).name  # as listed: never resolved again (the share may be asleep next time)
        pool.sounds.append(Sound(
            id="", kind=kind, media=info, hit=min(hit, float(info.duration)), lufs=lufs, peak=peak, origin=c.origin,
            asset_id=c.asset_id, provider=c.provider, license=c.license,
        ))
    if gone:
        named = ", ".join(c.name for c in gone[:3]) + (f" and {len(gone) - 3} more" if len(gone) > 3 else "")
        pool.warnings.append(
            f"{len(gone)} sound effect{'s' if len(gone) != 1 else ''} named in Venture Studio or a folder listing "
            f"{'are' if len(gone) != 1 else 'is'} no longer there ({named}); left out."
        )
    pool.sounds.sort(key=lambda s: s.path)
    width = max(2, len(str(len(pool.sounds))))
    for n, s in enumerate(pool.sounds, 1):
        s.id = f"{ID_PREFIX}{n:0{width}d}"
    counts = pool.counts()
    log(
        f"sound effects: {len(pool.sounds)} usable ("
        + ", ".join(f"{counts[k]} {k}{'es' if k == 'whoosh' and counts[k] != 1 else 's' if counts[k] != 1 else ''}" for k in CLASSES if counts[k])
        + ")" + (f" {_skipped_text(pool)}" if pool.skipped else "")
    )
    if pool.skipped.get("late"):
        pool.notes.append(f"{pool.skipped['late']['count']} sound effects weren't read in the {POOL_SECONDS:g} s a run gives them; the next run reads more (each one is cached once read).")
    return pool


def _skipped_text(pool: Pool) -> str:
    parts = [f"{v['count']} {SKIP_REASONS.get(k, k)}" for k, v in pool.skipped.items()]
    return f"(skipped: {'; '.join(parts)})" if parts else ""


def skipped_lines(pool: Pool) -> list[str]:
    """The paper edit's lines about files that aren't in the pool, one per reason."""
    out = []
    for reason, entry in pool.skipped.items():
        names = [n for n in entry.get("names") or [] if isinstance(n, str)]
        count = entry.get("count") if isinstance(entry.get("count"), int) else len(names)
        shown = ", ".join(names) + (f" and {count - len(names)} more" if count > len(names) else "")
        out.append(f"{count} {SKIP_REASONS.get(reason, reason)}" + (f": {shown}" if shown and reason not in ("over_cap", "late") else ""))
    return out


def clip_ids_used(connected) -> set[str]:
    return {c.clip_id for c in connected if c.kind == "sfx"}


def frames_of(sound: Sound, fd: Fraction) -> int:
    """Whole frames of the file: an accent never reads past its end."""
    from .timecode import floor_frames

    return floor_frames(sound.media.duration, fd)

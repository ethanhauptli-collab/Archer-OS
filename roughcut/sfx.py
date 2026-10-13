"""Sound-effect accents, part one: the pool of sounds to place (whooshes, risers, pops and hits).

Sounds come from `--sfx` (an audio file, or a folder searched through its subfolders) and from
`--sfx-from` (a Venture Studio collection or kit by name, read through studio.py's verified copy of
Studio's catalog, or a kit file Studio exported). Each file is classed by the words in its name,
its Studio tags and its folders. Only audio-only files in the formats Roughcut reads, at most
MAX_SECONDS long, are kept; the rest are listed as skipped, never placed. Each kept sound is probed
and measured once (cached by its fingerprint in Roughcut's cache): its length, its hit (the start of
its loudest 50 ms), and its loudness with silence around it, so a 150 ms pop gets a reading too.

Every touch of these files (a folder listing, a stat, ffprobe, ffmpeg, a kit file) runs under a
deadline (textio.bounded; a folder listing is watched for silence instead, so a slow share that keeps
answering is read to the end): a network share that is offline or stops answering drops its sounds
with a warning and never hangs or fails the run. Files are only read: nothing is written beside
them, and Studio's own folder is read only through studio.py. The pool is saved in plan.json (`Pool.to_json`), so
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
from functools import cached_property
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
KIT_SECONDS = 20.0  # reading a kit file Studio exported (a small JSON file, maybe on a share)
SCAN_IDLE_SECONDS = 30.0  # a folder listing has stopped answering when no entry has come for this long
SCAN_TOTAL_SECONDS = 180.0  # ... and is cut short, keeping what it listed, after this long in all (a slow share, still answering)
POOL_SECONDS = 240.0  # the whole pool; the rest waits for a new build or new --sfx/--sfx-from (each sound is cached once read)
SCAN_LIMIT = 50_000  # entries looked at in one folder tree
CACHE_NAME = "sfx-v1.json"
IMPORT_MARKER = ".venture-studio-import-incomplete.json"  # a folder Studio is still copying files into
# Audio Roughcut doesn't read (Final Cut's import of them is untested): listed as skipped, never placed.
OTHER_AUDIO = {".flac", ".ogg", ".oga", ".opus", ".wma", ".mka", ".ape", ".aifc", ".m4b", ".mp2", ".ac3", ".dsf", ".wv"}
ID_PREFIX = "X"  # clip IDs X01, X02...: V, A, I and G are taken
VOLUMES = "/Volumes"
PRIVACY_HINT = "Privacy & Security may be asking for access to that folder or network volume. Allow it, then build again."


class SourceError(RuntimeError):
    """A sound source named on the command line that doesn't exist: the build stops before its folder is made."""


# ------------------------------------------------------------------ classing


_CAMEL_RUN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL = re.compile(r"([a-z])([A-Z])")
_LETTER_DIGIT = re.compile(r"([A-Za-z])(\d)")
_DIGIT_LETTER = re.compile(r"(\d)([A-Za-z])")
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """'WhooshSoft_01', 'UIClick' -> ['whoosh', 'soft', '01'], ['ui', 'click']."""
    text = _CAMEL_RUN.sub(r"\1 \2", text)
    text = _CAMEL.sub(r"\1 \2", text)
    text = _LETTER_DIGIT.sub(r"\1 \2", text)
    text = _DIGIT_LETTER.sub(r"\1 \2", text)
    return [t for t in _NOT_WORD.split(text.lower()) if t]


def _singular(token: str, known: set[str]) -> str:
    for cut in ("es", "s"):
        if token.endswith(cut) and token[: -len(cut)] in known:
            return token[: -len(cut)]
    return token


_KNOWN = {w for words in CLASS_WORDS.values() for phrase in words for w in phrase.split()}
# The class words by their first word, so a name's words are looked up, not matched against every phrase.
_BY_FIRST: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
for _kind, _words in CLASS_WORDS.items():
    for _phrase in _words:
        _want = tuple(_phrase.split())
        _BY_FIRST.setdefault(_want[0], []).append((_kind, _want))


def class_of_text(text: str) -> tuple[str, int] | None:
    """(class, position) of the first class word in `text` (the longest phrase there, then the class's name), or None."""
    tokens = [_singular(t, _KNOWN) for t in _tokens(text)]
    for i, token in enumerate(tokens):
        best: tuple[int, str] | None = None  # (-length, class)
        for kind, want in _BY_FIRST.get(token, ()):
            if tuple(tokens[i : i + len(want)]) == want:
                found = (-len(want), kind)
                if best is None or found < best:
                    best = found
        if best:
            return best[1], i
    return None


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

    @cached_property
    def kind(self) -> str | None:
        """Its class, worked out once (a big folder asks many times)."""
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
    """A --sfx-from value that names a kit file Studio exported: it ends in .json (the rule --music-from uses too).
    Anything else is a collection or kit in Studio's catalog, whatever characters its name has: a slash is part of a
    name ("Hits/Impacts"), not a sign of a path."""
    if _which(name)[0]:
        return False
    return name.strip().lower().endswith(".json")


def _path_like(name: str) -> bool:
    """A name nobody prefixed with kit: or collection: that has a slash in it, so it may have been meant as a path."""
    return _which(name)[0] is None and "/" in name


def _is_folder(name: str) -> bool:
    """Whether `name` is a folder on this Mac (looked at under a deadline: it may be on a share that stopped answering)."""
    path = os.path.expanduser(name.strip())
    try:
        return bool(bounded(os.path.isdir, path, seconds=KIT_SECONDS, what=f"{Path(path).name}'s folder"))
    except (Stalled, OSError, ValueError):
        return False


FOLDER_TEXT = "that's a folder. Sound effects in a folder go with --sfx."
KIT_FILE_HINT = " If you meant a kit file Studio exported, give its path ending in .json."


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
            kit = bounded(_read_kit, Path(name).expanduser(), seconds=KIT_SECONDS, what=f"reading {Path(name).name}")
        except Stalled:
            problem(name, f"--sfx-from {name}: the file didn't answer in {KIT_SECONDS:g} s (a network share that stopped responding?).")
            continue
        except IsADirectoryError:
            problem(name, f"--sfx-from {name}: {FOLDER_TEXT}")
            continue
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
                    if _path_like(name) and _is_folder(name):
                        problem(name, f"--sfx-from {name}: {FOLDER_TEXT}")
                        continue
                    problem(name, (
                        f"--sfx-from {name}: Venture Studio has no {what} named {bare!r} in its catalog snapshot from {copy.snapshot.when()} "
                        "(one made since then shows up after Studio's next snapshot: in Studio, click Back up catalog now)."
                        + (KIT_FILE_HINT if _path_like(name) else "")
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
        # Studio can't say what it has. A folder typed here is still a folder; the rest is Studio's problem, a warning.
        rest = []
        for name in catalog_names:
            if _path_like(name) and _is_folder(name):
                problem(name, f"--sfx-from {name}: {FOLDER_TEXT}")
            else:
                rest.append(name)
        if rest:
            out.warnings.append(
                f"Sound effects from Venture Studio ({', '.join(rest)}): {e.reason if not e.silent else 'Venture Studio is not installed on this Mac'}. "
                "This cut has none from there."
            )
    return out


def _read_kit(path: Path):
    """studio.load_kit, which opens the file with plain calls (run it under textio.bounded). A folder is told apart from a bad kit."""
    from . import studio

    try:
        return studio.load_kit(path)
    except studio.StudioUnavailable:
        if os.path.isdir(path):
            raise IsADirectoryError(str(path)) from None
        raise


def _also(title: str, name: str) -> tuple[str, ...]:
    """Studio's name for an item, when it isn't just the file's name."""
    title = (title or "").strip()
    return (title,) if title and title not in (name, Path(name).stem) else ()


def _folders_of(relative: str, top: str) -> tuple[str, ...]:
    """Folder names above a file, nearest first, then `top` (the folder or library it's in)."""
    parents = [p for p in PurePosixPath(relative).parent.parts if p not in ("/", "")]
    return tuple(reversed(parents)) + ((top,) if top else ())


# ------------------------------------------------------------------ listing folders

_scandir = os.scandir  # the one way folders are opened (tests stand in for a slow or sleeping share here)


@dataclass
class Listing:
    """What a folder listing found: the audio files, and what was left out of it and why."""

    found: list[Candidate] = field(default_factory=list)
    other: list[str] = field(default_factory=list)  # names of audio Roughcut doesn't read
    importing: list[str] = field(default_factory=list)  # folders skipped mid-import
    denied: list[tuple[str, bool]] = field(default_factory=list)  # (folder inside it that wouldn't open, macOS refused it)
    seen: int = 0  # files looked at
    cut_short: bool = False  # past SCAN_LIMIT
    status: str = "done"  # done / slow (cut short after SCAN_TOTAL_SECONDS, still answering) / stalled (no answer for SCAN_IDLE_SECONDS)


class _Walk:
    """A listing in progress, shared by the thread doing it and the one waiting: every entry it reads is a sign of life."""

    def __init__(self) -> None:
        self.listing = Listing()
        self.lock = threading.Lock()
        self.last = time.monotonic()  # when the share last answered
        self.finished = threading.Event()
        self.error: BaseException | None = None  # the folder itself wouldn't open
        self.abandoned = False  # the waiting thread gave up: stop adding

    def beat(self) -> None:
        self.last = time.monotonic()


def _walk(path: str, w: _Walk, scandir) -> None:
    """List `path` (an audio file, or a folder searched through its subfolders) into `w.listing`. Runs in its own thread."""
    try:
        p = Path(path)
        st = os.stat(p)
        w.beat()
        key = _share_key(str(p), str(p))
        out = w.listing
        if stat.S_ISREG(st.st_mode):
            ext = p.suffix.lower()
            with w.lock:
                if ext in AUDIO_EXTS:
                    out.found.append(Candidate(str(p), p.name, folders=(p.parent.name,), origin="folder", key=key))
                elif ext in OTHER_AUDIO:
                    out.other.append(p.name)
            return
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{p} isn't a file or folder")
        stack: list[tuple[str, tuple[str, ...]]] = [(str(p), ())]  # (folder, its names below the one given)
        while stack:
            directory, below = stack.pop()
            try:
                entries = []
                with scandir(directory) as it:
                    for entry in it:
                        w.beat()
                        entries.append(entry)
            except OSError as e:
                if not below:
                    raise
                with w.lock:
                    out.denied.append(("/".join(below), isinstance(e, PermissionError)))
                continue
            if any(e.name == IMPORT_MARKER for e in entries):  # Studio is still copying into this folder: its files may be partial
                with w.lock:
                    out.importing.append(os.path.basename(directory) + "/")
                continue
            folders = tuple(reversed(below)) + (p.name,)
            dirs: list[str] = []
            for entry in sorted(entries, key=lambda e: e.name):
                w.beat()
                name = entry.name
                try:
                    is_dir = entry.is_dir()
                    followed = is_dir and entry.is_symlink()
                except OSError:
                    continue
                if w.abandoned:
                    return
                if is_dir:
                    if not name.startswith(".") and not name.endswith(SKIP_DIR_SUFFIXES) and not followed:
                        dirs.append(name)
                    continue
                with w.lock:
                    out.seen += 1
                    if out.seen > SCAN_LIMIT:
                        out.cut_short = True
                        return
                    if name.startswith(".") or is_junk(name):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext in AUDIO_EXTS:
                        out.found.append(Candidate(os.path.join(directory, name), name, folders=folders, origin="folder", key=key))
                    elif ext in OTHER_AUDIO:
                        out.other.append(name)
            stack += [(os.path.join(directory, d), below + (d,)) for d in reversed(dirs)]
    except BaseException as e:  # noqa: BLE001 - handed to the thread that waits
        w.error = e
    finally:
        w.finished.set()


def list_folder(path: str, *, idle: float | None = None, total: float | None = None, scandir=None) -> Listing:
    """The audio files at `path`, listed in a thread this one watches for silence.

    A folder on a share that answers slowly but steadily is listed to the end (up to `total` seconds, then
    cut short with what it has: status "slow"); one that stops answering for `idle` seconds is left behind,
    also keeping what it had (status "stalled"). Raises what the folder itself raised (not found, refused).
    """
    idle = SCAN_IDLE_SECONDS if idle is None else idle
    total = SCAN_TOTAL_SECONDS if total is None else total
    w = _Walk()
    started = w.last
    threading.Thread(target=_walk, args=(path, w, scandir or _scandir), daemon=True, name="sfx-listing").start()
    status = "done"
    while not w.finished.wait(min(0.25, max(0.01, idle / 4))):
        now = time.monotonic()
        if now - w.last > idle:
            status = "stalled"
            break
        if now - started > total:
            status = "slow"
            break
    with w.lock:
        w.abandoned = status != "done"
        out = Listing(
            list(w.listing.found), list(w.listing.other), list(w.listing.importing), list(w.listing.denied), w.listing.seen, w.listing.cut_short, status,
        )
    if status == "done" and w.error is not None:
        raise w.error
    return out


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
    "late": "not read yet (a run has only so long; a new build, or render with --sfx or --sfx-from again, reads the rest)",
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
                listing = list_folder(path)
            except FileNotFoundError:
                pool.warnings.append(f"--sfx {path}: can't find it (moved, renamed, or a share that isn't connected?); this cut has no sound effects from it.")
                continue
            except PermissionError:
                pool.warnings.append(f"--sfx {path}: macOS didn't let Roughcut read it. {PRIVACY_HINT} This cut has no sound effects from it.")
                continue
            except OSError as e:
                pool.warnings.append(f"--sfx {path}: couldn't read it ({e.strerror or e}); this cut has no sound effects from it.")
                continue
            found += listing.found
            for name in listing.other:
                pool.skip("unsupported", name)
            for folder in listing.importing:
                pool.skip("importing", folder)
            n = listing.seen
            if listing.status == "stalled":
                pool.warnings.append(
                    f"--sfx {path}: the folder stopped answering after {n} file{'s' if n != 1 else ''} {'was' if n == 1 else 'were'} listed (a network share that went "
                    f"to sleep or dropped?); " + ("this cut uses the sounds listed before that." if listing.found else "this cut has no sound effects from it.")
                )
            elif listing.status == "slow":
                pool.warnings.append(
                    f"--sfx {path}: listing the folder is slow (a busy network share?) and was cut short after {SCAN_TOTAL_SECONDS:g} s with "
                    f"{n} file{'s' if n != 1 else ''} listed; this cut uses those."
                )
            if listing.cut_short:
                pool.notes.append(f"--sfx {path} holds more than {SCAN_LIMIT:,} files; only the first ones were looked at.")
            refused = [d for d, perm in listing.denied if perm]
            other = [d for d, perm in listing.denied if not perm]
            if refused:
                pool.warnings.append(
                    f"macOS didn't let Roughcut open {len(refused)} folder{'s' if len(refused) != 1 else ''} in --sfx {path} ({_some(refused)}). {PRIVACY_HINT}"
                )
            if other:
                pool.warnings.append(f"{len(other)} folder{'s' if len(other) != 1 else ''} in --sfx {path} couldn't be read ({_some(other)}); left out.")
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
        except PermissionError:
            return c, "denied", None
        except (OSError, MediaError, ValueError) as e:
            return c, "unreadable", str(e)

    log(f"reading {len(todo)} sound effects (each is measured once, then cached)")
    with progress.doing(f"reading {len(todo)} sound effects", external=True), ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(one, todo))
    gone, denied = [], []
    for c, reason, rec in results:
        if reason == "gone":
            gone.append(c)
            continue
        if reason == "denied":
            denied.append(c)
            pool.skip("unreadable", c.name)
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
    if denied:
        pool.warnings.append(
            f"macOS didn't let Roughcut read {len(denied)} sound effect{'s' if len(denied) != 1 else ''} (in {os.path.dirname(denied[0].path)}). {PRIVACY_HINT}"
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
        n = pool.skipped["late"]["count"]
        pool.notes.append(
            f"{n} sound effect{'s' if n != 1 else ''} {'were' if n != 1 else 'was'}n't read in the {POOL_SECONDS:g} s a run gives them. Re-cuts and revisions "
            "place the sounds read so far, again; build again, or render with --sfx FOLDER (or --sfx-from NAME) again, to read the rest "
            "(each one is cached once read, so reading again is quick)."
        )
    return pool


def _some(names: list[str]) -> str:
    """A few names for a warning: 'UI, Whooshes and 3 more'."""
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


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

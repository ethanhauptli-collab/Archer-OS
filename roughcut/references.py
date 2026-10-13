"""Reference images for the motion graphics: pictures that show the look the editor wants.

`--graphics-ref IMAGE` (repeatable) names pictures: a screenshot of a title style, a mood board, a frame
from a video whose graphics they like. They are private to the editor and may show other people's brands,
so they go one place only: the graphics design requests (like an approved design or the logo). Never the
planner, graphics.json's style, the report beyond their file names, or a log.

Each picture is converted once, when a run is made: its first frame, flattened onto white, as a JPEG with
the long edge at most 1600 px, stored in the output folder as `graphics/references/R01-<sha6>.jpg`
(`sha6`: the first six hex digits of the stored bytes, so identical content has the same name). Settings
keep those stored names (relative to the output folder), never the picture's own path, so a moved or
deleted original changes nothing for `render --design-missing` or a later revision. The designer still
gets the look from the house style saved with the graphics, even where it can't see the pictures.

Everything done to a picture the editor named is bounded: stats and reads in `textio.bounded`, ffmpeg and
sips with a timeout, so a share that stopped answering is a warning, never a hang. macOS junk ("._name"
companions, .DS_Store) is skipped without a word; a picture that can't be read is a warning naming its
file (never its folder) and never fails a run.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .textio import Stalled, bounded, is_junk, run_text

FOLDER = "graphics/references"  # inside the output folder
MAX_REFERENCES = 8  # more than this are dropped with a warning
MAX_SOURCE_BYTES = 40 * 1024 * 1024  # a bigger picture is left out
LONG_EDGE = 1600  # px: larger pictures are scaled down, never up
STAT_SECONDS = 15.0  # a share that doesn't answer a stat or a read in this long
CONVERT_SECONDS = 60.0  # one picture's ffmpeg (or sips) run
KILL_GRACE = 10.0  # how much longer a run that ignores being killed (a hung read on a share) is waited for

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
KINDS = "PNG, JPEG, WebP, GIF, TIFF, BMP or HEIC"
STORED = re.compile(r"graphics/references/R\d{2}-[0-9a-f]{6}\.jpg")

# Flatten onto white (JPEG has no alpha), scale down to the long edge, 4:4:4 so a screenshot's coloured text stays sharp.
_FILTER = (
    f"scale='if(gt(iw,ih),min({LONG_EDGE},iw),-1)':'if(gt(iw,ih),-1,min({LONG_EDGE},ih))':flags=lanczos,format=rgba,split[a][b];"
    "[a]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill[bg];[bg][b]overlay=format=auto,format=yuvj444p"
)


def _sniff(head: bytes) -> str | None:
    """What a file's first bytes say it is ("heic" for the HEIF family), or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"BM"):
        return "bmp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"):
        return "heic"
    return None


def stored_names(raw) -> list[str]:
    """The stored reference names in a saved settings value; anything else (a hand-edited plan.json) is ignored."""
    found = [r for r in raw if isinstance(r, str) and STORED.fullmatch(r)] if isinstance(raw, list) else []
    return list(dict.fromkeys(found))[:MAX_REFERENCES]


@dataclass
class ReferenceSet:
    """The stored pictures a design request carries, read once for the run."""

    names: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    sent: bool = False  # a design request carried them
    warned: bool = False  # the designer can't look at pictures: said once
    dropped: bool = False  # a request that failed with them (turned down, or failed again when sent twice) was sent again without

    def __bool__(self) -> bool:
        return bool(self.images)

    def __len__(self) -> int:
        return len(self.images)

    def intro(self) -> str:
        """The one text part that comes before the pictures in a design request."""
        n = len(self.images)
        return (
            f"The editor attached {n} reference image{'s' if n != 1 else ''} showing the look they want for the graphics. "
            "Match their typography, colour, layering, spacing, shape language and the energy of motion they suggest. "
            "Do not copy their words, logos, brand marks or photographs. This video's own words, name spellings and brand "
            "(the preset's palette, fonts and logo) win where they conflict. They are guidance only: never put them in a design."
        )


def load(out_dir: Path, names, warnings: list[str]) -> ReferenceSet:
    """The stored pictures `names` (settings' graphics_refs) from the output folder. One that has gone is a warning."""
    refs = ReferenceSet()
    folder = Path(out_dir) / FOLDER
    for name in stored_names(list(names or [])):
        path = Path(out_dir) / name
        if path.parent != folder:
            continue
        try:
            data = bounded(path.read_bytes, seconds=STAT_SECONDS, what=f"reading {path.name}")
        except (OSError, Stalled):
            warnings.append(f"The reference image {path.name} is missing from {FOLDER}, so the designer didn't see it.")
            continue
        if data:
            refs.names.append(name)
            refs.images.append(data)
    return refs


def ingest(sources, out_dir: Path, warnings: list[str]) -> list[str]:
    """Convert and store the pictures `sources` (paths); returns their stored names, relative to `out_dir`.

    At most MAX_REFERENCES. Anything unreadable is a warning naming its file. Identical content is stored once.
    """
    names: list[str] = []
    seen: set[str] = set()
    over: list[str] = []
    folder = Path(out_dir) / FOLDER
    for raw in sources:
        path = Path(os.path.abspath(os.path.expanduser(str(raw))))
        if is_junk(path) or str(path) in seen:
            continue
        seen.add(str(path))
        if len(names) >= MAX_REFERENCES:
            over.append(path.name)
            continue
        stored, problem = _store(path, folder, len(names) + 1, {n[-10:-4] for n in names})
        if problem:
            warnings.append(f"The reference image {path.name} {problem}, so it was left out.")
        elif stored:
            names.append(stored)
    if over:
        shown = ", ".join(over[:3]) + (f" and {len(over) - 3} more" if len(over) > 3 else "")
        warnings.append(f"Only {MAX_REFERENCES} reference images are used; the rest were left out ({shown}).")
    return names


def _store(path: Path, folder: Path, number: int, taken: set[str]) -> tuple[str | None, str | None]:
    """(stored name, None) for a picture stored, (None, what is wrong with it), or (None, None) for one whose content
    (`taken`: the six-digit hashes already stored in this set) is stored already."""
    try:
        st = bounded(path.stat, seconds=STAT_SECONDS, what=f"looking at {path.name}")
        if not stat.S_ISREG(st.st_mode):
            return None, "isn't a file"
        if st.st_size == 0:
            return None, "is empty"
        if st.st_size > MAX_SOURCE_BYTES:
            return None, f"is {st.st_size / 1024 / 1024:.0f} MB (the limit is {MAX_SOURCE_BYTES // 1024 // 1024} MB)"
        head = bounded(_head, path, seconds=STAT_SECONDS, what=f"reading {path.name}")
    except Stalled:
        return None, f"didn't open in {STAT_SECONDS:g} s (is its disk or share still connected?)"
    except OSError as e:
        return None, f"can't be opened ({e.strerror or type(e).__name__})"
    kind = _sniff(head)
    if kind is None and path.suffix.lower() not in EXTENSIONS:
        return None, f"isn't a {KINDS} picture"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".R{number:02d}-{uuid.uuid4().hex[:8]}.tmp.jpg"
    except OSError as e:
        return None, f"couldn't be stored ({e.strerror or type(e).__name__})"
    try:
        problem = _convert(path, tmp, heic=kind == "heic" or path.suffix.lower() in (".heic", ".heif"))
        if problem:
            return None, problem
        try:
            data = bounded(tmp.read_bytes, seconds=STAT_SECONDS, what="reading the converted picture")
        except (OSError, Stalled):
            return None, "couldn't be converted"
        if not data:
            return None, "couldn't be converted"
        digest = hashlib.sha256(data).hexdigest()[:6]
        if digest in taken:
            return None, None
        final = folder / f"R{number:02d}-{digest}.jpg"
        try:
            if final.is_file() and final.stat().st_size == len(data):  # identical content: its file is reused
                tmp.unlink(missing_ok=True)
            else:
                os.replace(tmp, final)
        except OSError as e:
            return None, f"couldn't be stored ({e.strerror or type(e).__name__})"
        return f"{FOLDER}/{final.name}", None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _head(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(32)


def _convert(src: Path, dest: Path, *, heic: bool) -> str | None:
    """Write `src` to `dest` as a JPEG: None if it worked, else what is wrong with it. ffmpeg first, then macOS's sips."""
    with tempfile.TemporaryDirectory(prefix="roughcut-ref-") as tmp:
        try:
            source = src
            if "%" in src.name:  # ffmpeg would read the name as a numbered sequence
                source = Path(tmp) / f"picture{src.suffix.lower() or '.img'}"
                bounded(shutil.copyfile, src, source, seconds=CONVERT_SECONDS, what=f"copying {src.name}")
            error = _ffmpeg_jpeg(source, dest)
            if error is None:
                return None
            if error == "timeout":
                return f"took longer than {CONVERT_SECONDS:g} s to convert"
            if shutil.which("sips"):
                # macOS reads more than ffmpeg does (HEIC, odd TIFFs): through a full-size JPEG, then the same scaling.
                middle = Path(tmp) / "sips.jpg"
                if _sips_jpeg(source, middle) is None and _ffmpeg_jpeg(middle, dest) is None:
                    return None
            return "is a HEIC picture this computer can't read (save it as a JPEG or PNG)" if heic else "couldn't be read as a picture"
        except Stalled:
            return f"didn't open in {CONVERT_SECONDS:g} s (is its disk or share still connected?)"
        except OSError as e:
            return f"can't be opened ({e.strerror or type(e).__name__})"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """A tool run on the editor's picture, given CONVERT_SECONDS: killed after that, and (since a read that hangs in the
    kernel on a stalled share can outlive the kill) left behind in its thread a little later."""
    return bounded(run_text, cmd, seconds=CONVERT_SECONDS + KILL_GRACE, what=f"{cmd[0]} on a reference image", timeout=CONVERT_SECONDS)


def _ffmpeg_jpeg(src: Path, dest: Path) -> str | None:
    """None if ffmpeg wrote the stored JPEG, "timeout", or "failed". Metadata (EXIF, GPS) is not copied."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1", "-an", "-sn", "-dn",
        "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-vf", _FILTER, "-q:v", "3", "-update", "1", str(dest),
    ]
    try:
        proc = _run(cmd)
    except (subprocess.TimeoutExpired, Stalled):
        return "timeout"
    except OSError:  # no ffmpeg
        return "failed"
    return None if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0 else "failed"


def _sips_jpeg(src: Path, dest: Path) -> str | None:
    try:
        proc = _run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dest)])
    except (subprocess.TimeoutExpired, Stalled, OSError) as e:
        return str(e)
    return None if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0 else "sips failed"

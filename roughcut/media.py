"""Find media files and read their technical metadata with ffprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

from . import timecode as tc

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mts", ".m2ts", ".mxf", ".avi", ".3gp", ".insv"}
AUDIO_EXTS = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".caf", ".bwf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".psd", ".bmp", ".gif"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

# Bundles and folders that are never source media.
SKIP_DIR_SUFFIXES = (".fcpbundle", ".photoslibrary", ".imovielibrary", ".app", ".fcpxmld")


class MediaError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    path: str
    name: str
    kind: str  # "video" | "audio" | "image"
    duration: Fraction  # exact seconds; 0 for stills
    start: Fraction  # source timecode start in seconds (FCP asset "start")
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    frame_duration: Fraction | None = None
    rate: str = ""
    vfr: bool = False
    audio_channels: int = 0
    audio_rate: int = 0
    timecode: str | None = None
    creation_time: str | None = None
    codec: str = ""
    fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    def to_json(self) -> dict:
        d = asdict(self)
        for key in ("duration", "start", "frame_duration"):
            v = d[key]
            d[key] = None if v is None else f"{v.numerator}/{v.denominator}"
        return d

    @classmethod
    def from_json(cls, d: dict) -> "MediaInfo":
        d = dict(d)
        for key in ("duration", "start", "frame_duration"):
            v = d.get(key)
            d[key] = None if v is None else Fraction(v)
        return cls(**d)


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise MediaError(
            f"{' and '.join(missing)} not found on PATH. On a Mac: `brew install ffmpeg`."
        )


def scan(inputs: list[str | Path], exclude: list[Path] | None = None) -> list[Path]:
    """Expand files/folders into a sorted list of media files."""
    exclude_resolved = [p.resolve() for p in (exclude or [])]
    found: list[Path] = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_file():
            if p.suffix.lower() in MEDIA_EXTS:
                found.append(p.resolve())
            continue
        if not p.is_dir():
            raise MediaError(f"No such file or folder: {p}")
        for root, dirs, files in os.walk(p):
            root_path = Path(root).resolve()
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and not d.endswith(SKIP_DIR_SUFFIXES)
                and (root_path / d).resolve() not in exclude_resolved
            )
            for f in sorted(files):
                # "._foo.mov" are macOS resource-fork shadows on non-HFS drives.
                if f.startswith("."):
                    continue
                if Path(f).suffix.lower() in MEDIA_EXTS:
                    found.append(root_path / f)
    # De-duplicate while keeping order.
    seen: set[Path] = set()
    unique = []
    for f in found:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def fingerprint(path: Path) -> str:
    """Cheap identity for caching: path + size + mtime."""
    import hashlib

    st = path.stat()
    key = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    rot = (stream.get("tags") or {}).get("rotate")
    try:
        return int(float(rot)) if rot is not None else 0
    except ValueError:
        return 0


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    ext = path.suffix.lower()
    data = _ffprobe(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {}) or {}
    fmt_tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    video_streams = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
    ]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    warnings: list[str] = []

    if ext in IMAGE_EXTS:
        kind = "image"
    elif video_streams:
        kind = "video"
    elif audio_streams:
        kind = "audio"
    else:
        raise MediaError(f"{path.name}: no audio or video streams")

    info = MediaInfo(
        path=str(path),
        name=path.name,
        kind=kind,
        duration=Fraction(0),
        start=Fraction(0),
        has_video=bool(video_streams) or kind == "image",
        has_audio=bool(audio_streams) and kind != "image",
        creation_time=fmt_tags.get("creation_time") or fmt_tags.get("com.apple.quicktime.creationdate"),
        fingerprint=fingerprint(path),
    )

    if video_streams:
        v = video_streams[0]
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if abs(_rotation(v)) in (90, 270):
            w, h = h, w
        info.width, info.height = w, h
        info.codec = v.get("codec_name", "")

    if kind == "video":
        v = video_streams[0]
        snapped = snap_rate(v)
        if snapped is None:
            warnings.append("unknown frame rate; assuming 29.97")
            snapped = (Fraction(1001, 30000), "29.97")
        info.frame_duration, info.rate = snapped
        r = tc.parse_rate(v.get("r_frame_rate"))
        a = tc.parse_rate(v.get("avg_frame_rate"))
        if r and a and abs(float(r) - float(a)) / float(r) > 0.01:
            info.vfr = True
            warnings.append(f"variable frame rate (avg {float(a):.2f} fps); treated as {info.rate}")

        tc_str = (v.get("tags") or {}).get("timecode") or fmt_tags.get("timecode")
        if not tc_str:
            for s in streams:
                if s.get("codec_type") == "data" and (s.get("tags") or {}).get("timecode"):
                    tc_str = s["tags"]["timecode"]
                    break
        if tc_str:
            start = tc.timecode_to_seconds(tc_str, info.frame_duration)
            if start is not None:
                info.timecode, info.start = tc_str, start
            else:
                warnings.append(f"unparseable timecode {tc_str!r}")

        fd = info.frame_duration
        nb = v.get("nb_frames")
        seconds = _duration_seconds(v, fmt)
        if nb and str(nb).isdigit() and int(nb) > 0 and not info.vfr:
            frames = int(nb)
        else:
            frames = max(1, tc.round_frames(seconds, fd))
        info.duration = frames * fd

    if audio_streams:
        a0 = audio_streams[0]
        info.audio_channels = int(a0.get("channels") or 2)
        info.audio_rate = int(a0.get("sample_rate") or 48000)
        if kind == "audio":
            seconds = _duration_seconds(a0, fmt)
            samples = round(seconds * info.audio_rate)
            info.duration = Fraction(samples, info.audio_rate)
            info.codec = a0.get("codec_name", "")

    if kind == "image" and (info.width == 0 or info.height == 0):
        warnings.append("could not read image dimensions")

    info.warnings = warnings
    return info


def snap_rate(stream: dict) -> tuple[Fraction, str] | None:
    return tc.snap_frame_duration(stream.get("r_frame_rate")) or tc.snap_frame_duration(
        stream.get("avg_frame_rate")
    )


def _duration_seconds(stream: dict, fmt: dict) -> Fraction:
    for src in (stream.get("duration"), fmt.get("duration")):
        if src not in (None, "N/A"):
            try:
                return Fraction(str(src))
            except ValueError:
                continue
    return Fraction(0)

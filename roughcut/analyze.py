"""Stage 1: probe, silence-map, and transcribe every clip (cached)."""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ffmpeg_ops, progress
from .media import MediaError, MediaInfo, probe
from .segments import Segment, build_segments, drop_words_in_silence, segments_from_regions
from .transcribe import Transcriber, Transcript, find_sidecar, load_sidecar

ROLE_LABELS = {
    "aroll": "A-Roll",
    "broll": "B-Roll",
    "voiceover": "Voiceover",
    "music": "Music & Audio",
    "still": "Stills",
}


def log(msg: str) -> None:
    print(f"[roughcut] {msg}", file=sys.stderr, flush=True)
    progress.emit("log", message=msg)


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "roughcut"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "roughcut"


class Cache:
    def __init__(self, root: Path):
        self.root = root

    def path(self, fingerprint: str, name: str) -> Path:
        return self.root / fingerprint / name

    def get(self, fingerprint: str, name: str):
        p = self.path(fingerprint, name)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def put(self, fingerprint: str, name: str, value) -> None:
        p = self.path(fingerprint, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(p)


@dataclass
class Clip:
    id: str
    media: MediaInfo
    role: str = "broll"
    silences: list[tuple[float, float]] = field(default_factory=list)
    transcript_source: str = ""
    segments: list[Segment] = field(default_factory=list)
    visual: dict | None = None
    frames: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return float(self.media.duration)

    @property
    def speech_seconds(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def has_transcript(self) -> bool:
        return any(s.text for s in self.segments)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "media": self.media.to_json(),
            "silences": self.silences,
            "transcript_source": self.transcript_source,
            "segments": [s.to_json() for s in self.segments],
            "visual": self.visual,
            "frames": self.frames,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Clip":
        return cls(
            id=d["id"],
            role=d["role"],
            media=MediaInfo.from_json(d["media"]),
            silences=[tuple(s) for s in d.get("silences", [])],
            transcript_source=d.get("transcript_source", ""),
            segments=[Segment.from_json(s) for s in d.get("segments", [])],
            visual=d.get("visual"),
            frames=d.get("frames", []),
        )


@dataclass
class Analysis:
    clips: list[Clip]
    created: str = ""
    warnings: list[str] = field(default_factory=list)

    def clip(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)

    def segment(self, seg_id: str) -> Segment | None:
        clip = self.clip(seg_id.split(".", 1)[0])
        if clip is None:
            return None
        return next((s for s in clip.segments if s.id == seg_id), None)

    def to_json(self) -> dict:
        return {"created": self.created, "warnings": self.warnings, "clips": [c.to_json() for c in self.clips]}

    @classmethod
    def from_json(cls, d: dict) -> "Analysis":
        return cls(clips=[Clip.from_json(c) for c in d["clips"]], created=d.get("created", ""), warnings=d.get("warnings", []))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=1))

    @classmethod
    def load(cls, path: Path) -> "Analysis":
        return cls.from_json(json.loads(path.read_text()))


def _sort_key(info: MediaInfo) -> tuple[str, str]:
    stamp = info.creation_time
    if not stamp:
        try:
            stamp = datetime.fromtimestamp(Path(info.path).stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            stamp = ""
    return (stamp, info.path)


def assign_ids(infos: list[MediaInfo]) -> list[tuple[str, MediaInfo]]:
    prefix = {"video": "V", "audio": "A", "image": "I"}
    ordered = sorted(infos, key=_sort_key)
    counts = {k: sum(1 for i in infos if i.kind == k) for k in prefix}
    seen = {k: 0 for k in prefix}
    out = []
    for info in ordered:
        seen[info.kind] += 1
        width = max(2, len(str(counts[info.kind])))
        out.append((f"{prefix[info.kind]}{seen[info.kind]:0{width}d}", info))
    return out


def classify(clip: Clip) -> str:
    m = clip.media
    if m.kind == "image":
        return "still"
    words = sum(len(s.words) for s in clip.segments)
    speech = clip.speech_seconds
    dur = max(clip.duration, 0.001)
    if clip.transcript_source == "silence-map":
        # Silence-only mode: anything mostly audible counts as talking footage.
        audible = speech / dur
        if m.kind == "audio":
            return "music"
        return "aroll" if m.has_audio and audible > 0.5 else "broll"
    if m.kind == "audio":
        # Songs with lyrics transcribe as speech; trust a music-ish folder or name.
        hint = " ".join(Path(m.path).parts[-3:]).lower()
        if any(k in hint for k in ("music", "song", "soundtrack")):
            return "music"
        return "voiceover" if words >= 20 and speech / dur > 0.25 else "music"
    return "aroll" if words >= 12 and speech >= max(4.0, 0.2 * dur) else "broll"


def analyze(
    paths: list[Path],
    *,
    transcriber: Transcriber | None,
    cache: Cache,
    transcript_dirs: list[Path] | None = None,
    workers: int = 4,
    noise_db: float = -35.0,
) -> Analysis:
    t0 = time.monotonic()
    warnings: list[str] = []
    progress.stage("probe", f"Reading {len(paths)} files")

    def _probe(p: Path) -> MediaInfo | None:
        from .media import fingerprint

        try:
            fp = fingerprint(p)
        except OSError as e:
            warnings.append(f"{p.name}: {e}")
            return None
        cached = cache.get(fp, "probe.json")
        if cached:
            return MediaInfo.from_json(cached)
        try:
            info = probe(p)
        except MediaError as e:
            warnings.append(str(e))
            return None
        cache.put(fp, "probe.json", info.to_json())
        return info

    with ThreadPoolExecutor(max_workers=workers) as pool:
        infos = [i for i in pool.map(_probe, paths) if i is not None]
    for info in infos:
        for w in info.warnings:
            warnings.append(f"{info.name}: {w}")
    log(f"probed {len(infos)} files in {time.monotonic() - t0:.1f}s")

    clips = [Clip(id=cid, media=info) for cid, info in assign_ids(infos)]

    def _silence(clip: Clip) -> None:
        m = clip.media
        if not m.has_audio:
            return
        key = f"silence{noise_db:g}.json"
        cached = cache.get(m.fingerprint, key)
        if cached is None:
            cached = ffmpeg_ops.detect_silences(Path(m.path), float(m.duration), noise_db=noise_db)
            cache.put(m.fingerprint, key, cached)
        clip.silences = [tuple(s) for s in cached]

    t1 = time.monotonic()
    progress.stage("silence", "Finding the pauses")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_silence, clips))
    log(f"mapped silence in {time.monotonic() - t1:.1f}s")

    t2 = time.monotonic()
    audible = [c for c in clips if c.media.has_audio]
    for n, clip in enumerate(audible, 1):
        m = clip.media
        progress.stage("transcribe", f"Transcribing {m.name}", current=n, total=len(audible))
        transcript: Transcript | None = None
        sidecar = find_sidecar(Path(m.path), transcript_dirs)
        if sidecar:
            try:
                transcript = load_sidecar(sidecar)
            except Exception as e:  # a bad sidecar should not kill the run
                warnings.append(f"{sidecar.name}: could not read transcript ({e})")
        if transcript is None and transcriber is not None:
            key = f"transcript-{transcriber.cache_key}.json"
            cached = cache.get(m.fingerprint, key)
            if cached is not None:
                transcript = Transcript.from_json(cached)
            else:
                ts = time.monotonic()
                log(f"transcribing {n}/{len(audible)} {m.name} ({float(m.duration):.0f}s)")
                transcript = transcriber.transcribe(Path(m.path))
                cache.put(m.fingerprint, key, transcript.to_json())
                log(f"  done in {time.monotonic() - ts:.1f}s, {len(transcript.words)} words")
        if transcript is not None:
            words = drop_words_in_silence(transcript.words, clip.silences)
            clip.segments = build_segments(clip.id, words) if words else []
            clip.transcript_source = transcript.source
        else:
            regions = ffmpeg_ops.sound_regions(clip.silences, float(m.duration))
            clip.segments = segments_from_regions(clip.id, regions)
            clip.transcript_source = "silence-map"
    if audible:
        log(f"transcripts ready in {time.monotonic() - t2:.1f}s")

    for clip in clips:
        clip.role = classify(clip)

    return Analysis(clips=clips, created=datetime.now(timezone.utc).isoformat(timespec="seconds"), warnings=warnings)

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
    "graphic": "Graphics",
}


_log_file: Path | None = None


def log_to(path: Path | None) -> None:
    """Also append every log line to `path` (the run's roughcut.log), or stop with None."""
    global _log_file
    _log_file = path


def log(msg: str) -> None:
    print(f"[roughcut] {msg}", file=sys.stderr, flush=True)
    progress.emit("log", message=msg)
    log_file_only(msg)


def log_file_only(msg: str) -> None:
    """A line for roughcut.log alone (the app and Terminal already got it another way)."""
    if _log_file is not None:
        try:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except OSError:
            pass


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
    duplicate_of: str | None = None  # same footage exported again: hidden
    take_of: str | None = None  # another read of the same narration script
    same_words: float = 0.0  # word overlap with take_of
    silence_db: float | None = None  # level the silence map was detected at
    background_db: float | None = None  # this clip's measured background (peak dBFS)

    @property
    def hidden(self) -> bool:
        return self.duplicate_of is not None

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
            "duplicate_of": self.duplicate_of,
            "take_of": self.take_of,
            "same_words": self.same_words,
            "silence_db": self.silence_db,
            "background_db": self.background_db,
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
            duplicate_of=d.get("duplicate_of"),
            take_of=d.get("take_of"),
            same_words=d.get("same_words", 0.0),
            silence_db=d.get("silence_db"),
            background_db=d.get("background_db"),
        )


@dataclass
class Analysis:
    clips: list[Clip]
    created: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def clip(self, clip_id: str) -> Clip | None:
        return next((c for c in self.clips if c.id == clip_id), None)

    def segment(self, seg_id: str) -> Segment | None:
        clip = self.clip(seg_id.split(".", 1)[0])
        if clip is None:
            return None
        return next((s for s in clip.segments if s.id == seg_id), None)

    def to_json(self) -> dict:
        return {"created": self.created, "warnings": self.warnings, "notes": self.notes, "clips": [c.to_json() for c in self.clips]}

    @classmethod
    def from_json(cls, d: dict) -> "Analysis":
        return cls(
            clips=[Clip.from_json(c) for c in d["clips"]],
            created=d.get("created", ""),
            warnings=d.get("warnings", []),
            notes=d.get("notes", []),
        )

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


def pause_warning(clip: Clip) -> str | None:
    """Talking footage with no pauses found means no dead air can be cut: say so, and why."""
    if clip.role not in ("aroll", "voiceover") or clip.duration < 10:
        return None
    quiet = sum(e - s for s, e in clip.silences)
    if quiet >= 0.03 * clip.duration:
        return None
    why = (
        f" The background is about {clip.background_db:.0f} dB, close to the speech level, so pauses can't be told apart from it."
        if clip.background_db is not None and clip.background_db > -40
        else ""
    )
    return f"{clip.media.name}: found almost no pauses, so little dead air could be cut.{why}"


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
    noise_db: float | None = None,
    prefer_vertical: bool = False,
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

    default_maps: dict[str, list[tuple[float, float]]] = {}

    def _silence_at(m: MediaInfo, db: float) -> list[tuple[float, float]]:
        key = f"silence{db:g}.json"
        cached = cache.get(m.fingerprint, key)
        if cached is None:
            cached = ffmpeg_ops.detect_silences(Path(m.path), float(m.duration), noise_db=db)
            cache.put(m.fingerprint, key, cached)
        return [tuple(s) for s in cached]

    def _silence(clip: Clip) -> None:
        m = clip.media
        if not m.has_audio:
            return
        if noise_db is not None:  # fixed level asked for
            clip.silence_db = noise_db
            clip.silences = _silence_at(m, noise_db)
            return
        # Measure this clip's background so noisy rooms and quiet recordings both get pauses.
        level = cache.get(m.fingerprint, "noise-floor-v1.json")
        if level is None:
            db, floor = ffmpeg_ops.noise_threshold(ffmpeg_ops.window_peaks(Path(m.path)))
            level = {"db": db, "floor": floor}
            cache.put(m.fingerprint, "noise-floor-v1.json", level)
        clip.silence_db, clip.background_db = level["db"], level["floor"]
        clip.silences = _silence_at(m, clip.silence_db)
        if clip.silence_db != ffmpeg_ops.DEFAULT_NOISE_DB:
            default_maps[clip.id] = _silence_at(m, ffmpeg_ops.DEFAULT_NOISE_DB)

    t1 = time.monotonic()
    progress.stage("silence", "Finding the pauses")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_silence, clips))
    log(f"mapped silence in {time.monotonic() - t1:.1f}s")

    t2 = time.monotonic()
    audible = [c for c in clips if c.media.has_audio]
    for n, clip in enumerate(audible, 1):
        m = clip.media
        # current = files finished, so the bar never reads full while work remains.
        progress.stage("transcribe", f"Transcribing {m.name}", current=n - 1, total=len(audible))
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
            # Only true silence proves a word was invented. A raised level (music bed, loud
            # room) can sit above a quiet real word, so it never deletes words.
            words = drop_words_in_silence(transcript.words, default_maps.get(clip.id, clip.silences))
            clip.segments = build_segments(clip.id, words) if words else []
            clip.transcript_source = transcript.source
        else:
            regions = ffmpeg_ops.sound_regions(clip.silences, float(m.duration))
            clip.segments = segments_from_regions(clip.id, regions)
            clip.transcript_source = "silence-map"
    if audible:
        progress.stage("transcribe", "Transcripts ready", current=len(audible), total=len(audible))
        log(f"transcripts ready in {time.monotonic() - t2:.1f}s")

    for clip in clips:
        clip.role = classify(clip)

    from .duplicates import mark_duplicates

    notes = mark_duplicates(clips, cache, prefer_vertical=prefer_vertical)
    for note in notes:
        log(note)
    for clip in clips:
        if clip.silence_db is not None and clip.silence_db != ffmpeg_ops.DEFAULT_NOISE_DB and clip.role in ("aroll", "voiceover"):
            louder = clip.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB
            log(f"{clip.media.name}: {'noisy background' if louder else 'quiet recording'}, pauses detected below {clip.silence_db:g} dB")
        warning = None if clip.hidden else pause_warning(clip)
        if warning:
            warnings.append(warning)
    return Analysis(clips=clips, created=datetime.now(timezone.utc).isoformat(timespec="seconds"), warnings=warnings, notes=notes)

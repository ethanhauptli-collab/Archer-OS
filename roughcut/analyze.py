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
from .textio import Stalled, bounded
from .segments import Segment, build_segments, drop_words_in_silence, segments_from_regions
from .transcribe import Transcriber, TranscriptionError, Transcript, find_sidecar, load_sidecar

ROLE_LABELS = {
    "aroll": "A-Roll",
    "broll": "B-Roll",
    "voiceover": "Voiceover",
    "music": "Music & Audio",
    "still": "Stills",
    "graphic": "Graphics",
    "sfx": "Sound Effects",
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
    bed: str | None = None  # what plays in the pauses when the silence level is raised: "music" / "noise"
    cuts_per_min: float | None = None  # picture cuts a minute, for a clip over a music bed (finished edit?)
    breaths: list[tuple[float, float]] = field(default_factory=list)  # quiet sounds splitting a pause in two
    silence_db: float | None = None  # level the silence map was detected at
    background_db: float | None = None  # this clip's measured background (peak dBFS)
    # Loudness (EBU R128). The whole clip is what its nat sound or music plays
    # at; speech is measured over the transcribed words only.
    loudness: float | None = None  # integrated LUFS of the whole clip
    loudness_floor: float | None = None  # background, momentary LUFS
    peak_db: float | None = None  # sample peak dBFS of the whole clip
    speech_loudness: float | None = None  # integrated LUFS of the words (None: no words, or under 0.5 s)
    speech_peak: float | None = None  # sample peak dBFS inside the words
    sound_end: float | None = None  # seconds: where the last sound fades out
    music_bed: bool = False  # given with --music: not transcribed, always the cut's music
    music_order: int | None = None  # its place among the --music files (0: the first given), which plays when the plan cues none
    # A Venture Studio song: what Studio says about it (id, title, provider, license, tags, notes, favorite). Its file may be
    # on a network share, so every touch of it is given a deadline. Not part of analysis_digest.
    library: dict | None = None

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
            "bed": self.bed,
            "cuts_per_min": self.cuts_per_min,
            "breaths": self.breaths,
            "silence_db": self.silence_db,
            "background_db": self.background_db,
            "loudness": self.loudness,
            "loudness_floor": self.loudness_floor,
            "peak_db": self.peak_db,
            "speech_loudness": self.speech_loudness,
            "speech_peak": self.speech_peak,
            "sound_end": self.sound_end,
            "music_bed": self.music_bed,
            "music_order": self.music_order,
            **({"library": self.library} if self.library else {}),
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
            bed=d.get("bed"),
            cuts_per_min=d.get("cuts_per_min"),
            breaths=[tuple(b) for b in d.get("breaths", [])],
            silence_db=d.get("silence_db"),
            background_db=d.get("background_db"),
            loudness=d.get("loudness"),
            loudness_floor=d.get("loudness_floor"),
            peak_db=d.get("peak_db"),
            speech_loudness=d.get("speech_loudness"),
            speech_peak=d.get("speech_peak"),
            sound_end=d.get("sound_end"),
            music_bed=bool(d.get("music_bed", False)),
            music_order=d.get("music_order") if isinstance(d.get("music_order"), int) else None,
            library=d.get("library") if isinstance(d.get("library"), dict) else None,
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


# A Venture Studio song sits on a library folder, often a network share that can stop answering mid-run: each
# step that reads it gets a deadline, and a song whose share stalls is left out with a warning.
SHARE_STAT_SECONDS = 5.0
SHARE_PROBE_SECONDS = 30.0
SHARE_MEASURE_SECONDS = 180.0  # its silence map and loudness: three reads of the whole file


def _sort_key(info: MediaInfo, remote: frozenset[str] = frozenset()) -> tuple[str, str]:
    stamp = info.creation_time
    if not stamp:
        try:
            st = bounded(Path(info.path).stat, seconds=SHARE_STAT_SECONDS) if info.path in remote else Path(info.path).stat()
            stamp = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        except (OSError, Stalled):
            stamp = ""
    return (stamp, info.path)


def assign_ids(infos: list[MediaInfo], remote: frozenset[str] = frozenset()) -> list[tuple[str, MediaInfo]]:
    prefix = {"video": "V", "audio": "A", "image": "I"}
    ordered = sorted(infos, key=lambda i: _sort_key(i, remote))
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


LOUDNESS_CACHE = "loudness-v1.json"


def probe_cached(path: Path, cache: Cache) -> MediaInfo:
    """ffprobe's reading of a file, cached by its fingerprint (path, size, modification time)."""
    from .media import fingerprint

    fp = fingerprint(path)
    cached = cache.get(fp, "probe.json")
    if cached:
        return MediaInfo.from_json(cached)
    info = probe(path)
    cache.put(fp, "probe.json", info.to_json())
    return info


def _loudness_blocks(m: MediaInfo, cache: Cache) -> dict | None:
    """The clip's 100 ms loudness blocks, measured once per file version."""
    cached = cache.get(m.fingerprint, LOUDNESS_CACHE)
    if cached is None:
        if not Path(m.path).is_file():
            return None
        cached = ffmpeg_ops.loudness_blocks(Path(m.path)) or {}
        cache.put(m.fingerprint, LOUDNESS_CACHE, cached)
    return cached or None


def speech_spans(clip: Clip) -> list[tuple[float, float]] | None:
    """Where a clip has words: any clip with a transcript, since a B-roll clip's
    one spoken line can be a story item too. Talking footage without word
    times falls back to its sentences (or sound regions); None otherwise."""
    if clip.music_bed:
        return None
    spans = [(w.start, w.end) for s in clip.segments for w in s.words]
    if spans:
        return spans
    if clip.role in ("aroll", "voiceover"):
        return [(s.start, s.end) for s in clip.segments] or None
    return None


def measure_levels(clips: list[Clip], cache: Cache, *, workers: int = 4, only_missing: bool = False) -> None:
    """Set each audible clip's loudness, background, peak and last sound.

    Speech is measured over the words as well as the whole clip, so pauses
    and room tone don't drag the dialogue number down.
    """
    todo = [
        c
        for c in clips
        if c.media.has_audio and c.role != "graphic" and not (only_missing and c.loudness is not None and (c.speech_loudness is not None or not c.segments))
    ]

    def one(clip: Clip) -> None:
        blocks = _loudness_blocks(clip.media, cache)
        if not blocks:
            return
        s = ffmpeg_ops.level_summary(blocks, speech_spans(clip), clip.duration)
        clip.loudness, clip.loudness_floor, clip.peak_db, clip.sound_end = s["lufs"], s["floor"], s["peak"], s["end"]
        clip.speech_loudness, clip.speech_peak = s["speech"], s["speech_peak"]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))


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


def _short_error(e: BaseException, path: str = "", name: str = "") -> str:
    """One readable line from an exception (ffmpeg errors put the reason on the last line), with `path` shortened to `name`."""
    lines = [ln.strip() for ln in (str(e).replace(path, name) if path else str(e)).splitlines() if ln.strip()]
    if not lines:
        return type(e).__name__
    text = lines[0] if len(lines) == 1 else f"{lines[0].split(':', 1)[0]}: {lines[-1]}"
    return text[:200]


class _TranscribeFailures:
    """A corrupt or half-copied clip is cut on its pauses; a broken transcriber stops the run.

    The transcriber looks broken when the first two clips it tries both fail, or when
    three fail in a row. A run where no clip ends up with a transcript stops too (`finish`):
    with one clip to transcribe, a failed model download looks just like a bad file.
    """

    def __init__(self) -> None:
        self.ok = 0
        self.streak: list[str] = []
        self.error: BaseException | None = None

    def succeeded(self) -> None:
        self.ok += 1
        self.streak = []

    def failed(self, what: str, error: BaseException) -> None:
        self.streak.append(what)
        self.error = error
        if (self.ok == 0 and len(self.streak) >= 2) or len(self.streak) >= 3:
            raise TranscriptionError(
                f"Transcription failed on {len(self.streak)} clips in a row ({'; '.join(self.streak)}), so the "
                "transcriber itself looks broken. Run `roughcut doctor`, or use --transcriber none to cut on pauses only."
            ) from error

    def finish(self, transcribed: int) -> None:
        """Stop when clips failed and none has a transcript: there'd be nothing for the plan to read."""
        if self.streak and transcribed == 0:
            raise TranscriptionError(
                f"Couldn't transcribe {'; '.join(self.streak)}, and no clip has a transcript to plan from. If the file "
                "plays, the transcriber is the problem: run `roughcut doctor`. Or use --transcriber none to cut on pauses only."
            ) from self.error


def analyze(
    paths: list[Path],
    *,
    transcriber: Transcriber | None,
    cache: Cache,
    transcript_dirs: list[Path] | None = None,
    workers: int = 4,
    noise_db: float | None = None,
    prefer_vertical: bool = False,
    music: list[Path] | None = None,
    library: dict[str, dict] | None = None,
) -> Analysis:
    """`music`: files given with --music. They aren't transcribed and become the cut's music bed.

    `library`: the Venture Studio songs among them (their path as Studio has it -> what Studio says about each, kept as
    Clip.library). Their files may be on a network share: every read of one has a deadline (SHARE_*_SECONDS), and a
    song whose share stops answering is left out with a warning instead of holding up the run.
    """
    t0 = time.monotonic()
    warnings: list[str] = []
    library = {str(Path(k)): v for k, v in (library or {}).items()}
    remote = frozenset(library)

    def key(p) -> str:  # a Studio path is used as Studio has it: resolving it would touch the share
        return str(p) if str(p) in remote else str(Path(p).expanduser().resolve())

    music_paths: dict[str, int] = {}  # each --music file and its place in the order given (the first time it's given)
    for i, p in enumerate(music or []):
        music_paths.setdefault(key(p), i)
    progress.stage("probe", f"Reading {len(paths)} files")

    def _probe(p: Path) -> MediaInfo | None:
        if str(p) in remote:
            try:
                return bounded(probe_cached, p, cache, seconds=SHARE_PROBE_SECONDS, what=f"reading {p.name}")
            except Stalled:
                warnings.append(f"{p.name}: left out: its network folder didn't answer in {SHARE_PROBE_SECONDS:g} s (a Venture Studio song)")
                return None
            except OSError as e:
                warnings.append(f"{p.name}: {e}")
                return None
            except MediaError as e:
                warnings.append(str(e))
                return None
        try:
            return probe_cached(p, cache)
        except OSError as e:
            warnings.append(f"{p.name}: {e}")
            return None
        except MediaError as e:
            warnings.append(str(e))
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        infos = [i for i in pool.map(_probe, paths) if i is not None]
    for info in infos:
        for w in info.warnings:
            warnings.append(f"{info.name}: {w}")
    log(f"probed {len(infos)} files in {time.monotonic() - t0:.1f}s")

    clips = [Clip(id=cid, media=info) for cid, info in assign_ids(infos, remote)]
    for clip in clips:
        order = music_paths.get(key(clip.media.path))
        if order is not None:
            if clip.media.kind == "audio":
                clip.music_bed = True
                clip.music_order = order
                clip.library = library.get(clip.media.path)
            else:
                warnings.append(f"{clip.media.name}: --music takes an audio file; this one has pictures, so it's used as an ordinary clip")

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

    def _measure(clip: Clip) -> None:
        _silence(clip)
        if clip.media.has_audio:
            _loudness_blocks(clip.media, cache)  # measured now; summarized once roles are known

    stalled: set[str] = set()

    def _sound(clip: Clip) -> None:
        if clip.media.path not in remote:
            _measure(clip)
            return
        try:
            bounded(_measure, clip, seconds=SHARE_MEASURE_SECONDS, what=f"reading {clip.media.name}")
        except Stalled:
            stalled.add(clip.id)
            warnings.append(f"{clip.media.name}: left out: its network folder stopped answering while it was read (a Venture Studio song)")

    t1 = time.monotonic()
    progress.stage("silence", "Finding the pauses")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_sound, clips))
    clips = [c for c in clips if c.id not in stalled]
    log(f"mapped silence and loudness in {time.monotonic() - t1:.1f}s")

    t2 = time.monotonic()
    for clip in clips:
        if clip.music_bed:  # a song's lyrics aren't lines to cut
            clip.transcript_source = "music"
    audible = [c for c in clips if c.media.has_audio and not c.music_bed]
    failures = _TranscribeFailures()
    transcribed = 0
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
                try:
                    transcript = transcriber.transcribe(Path(m.path))
                except TranscriptionError:
                    raise  # the backend itself is missing or misconfigured
                except Exception as e:  # noqa: BLE001 - one undecodable clip must not stop the run
                    reason = _short_error(e, m.path, m.name)
                    failures.failed(f"{m.name}: {reason}", e)  # raises when the transcriber looks broken
                    warning = f"{m.name}: couldn't transcribe ({reason}); cut on its pauses only"
                    warnings.append(warning)
                    log(warning)
                else:
                    failures.succeeded()
                    cache.put(m.fingerprint, key, transcript.to_json())
                    log(f"  done in {time.monotonic() - ts:.1f}s, {len(transcript.words)} words")
        if transcript is not None:
            transcribed += 1
            # Only true silence proves a word was invented. A raised level (music bed, loud
            # room) can sit above a quiet real word, so it never deletes words.
            words = drop_words_in_silence(transcript.words, default_maps.get(clip.id, clip.silences))
            clip.segments = build_segments(clip.id, words) if words else []
            clip.transcript_source = transcript.source
        else:
            regions = ffmpeg_ops.sound_regions(clip.silences, float(m.duration))
            clip.segments = segments_from_regions(clip.id, regions)
            clip.transcript_source = "silence-map"
    failures.finish(transcribed)
    if audible:
        progress.stage("transcribe", "Transcripts ready", current=len(audible), total=len(audible))
        log(f"transcripts ready in {time.monotonic() - t2:.1f}s")

    for clip in clips:
        clip.role = "music" if clip.music_bed else classify(clip)
    measure_levels(clips, cache, workers=workers)

    from .duplicates import mark_duplicates

    notes = mark_duplicates(clips, cache, prefer_vertical=prefer_vertical)
    for note in notes:
        log(note)
    from .pauses import mark_beds, mark_breaths

    notes += mark_beds(clips, cache, workers=workers)
    mark_breaths(clips, cache, workers=workers)
    for clip in clips:
        if clip.silence_db is not None and clip.silence_db != ffmpeg_ops.DEFAULT_NOISE_DB and clip.role in ("aroll", "voiceover"):
            louder = clip.silence_db > ffmpeg_ops.DEFAULT_NOISE_DB
            log(f"{clip.media.name}: {'noisy background' if louder else 'quiet recording'}, pauses detected below {clip.silence_db:g} dB")
        warning = None if clip.hidden else pause_warning(clip)
        if warning:
            warnings.append(warning)
    return Analysis(clips=clips, created=datetime.now(timezone.utc).isoformat(timespec="seconds"), warnings=warnings, notes=notes)

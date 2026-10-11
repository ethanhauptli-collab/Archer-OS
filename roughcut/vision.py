"""Stage 2 (optional): sample frames and have a vision model log each clip."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpeg_ops, progress
from .analyze import Analysis, Cache, Clip, log
from .prompts import MOMENTS_SCHEMA, MOMENTS_SYSTEM, VISION_SCHEMA, VISION_SYSTEM
from .providers import ImagePart, Provider, ProviderAuthError, ProviderError, TextPart
from .timecode import seconds_to_clock


def sample_times(clip: Clip) -> list[float]:
    dur = clip.duration
    if clip.media.kind == "image":
        return [0.0]
    if clip.role == "aroll" or dur < 3:
        return [dur * 0.5]
    return [dur * 0.15, dur * 0.5, dur * 0.85]


def extract_frames(clip: Clip, cache: Cache) -> list[str]:
    m = clip.media
    frames = []
    for i, t in enumerate(sample_times(clip)):
        dest = cache.path(m.fingerprint, f"frames/{i}.jpg")
        if not dest.exists():
            ffmpeg_ops.extract_frame(Path(m.path), t, dest)
        if dest.exists():
            frames.append(str(dest))
    return frames


def describe_clips(
    analysis: Analysis,
    provider: Provider,
    cache: Cache,
    *,
    batch_size: int = 6,
    workers: int = 4,
    effort: str | None = "low",
) -> list[str]:
    """Fill clip.visual for every clip with pictures, and timed moments for long B-roll. Returns warnings."""
    warnings = _describe(analysis, provider, cache, batch_size=batch_size, workers=workers, effort=effort)
    return warnings + describe_moments(analysis, provider, cache, workers=workers, effort=effort)


def _cache_key(provider: Provider, what: str) -> str:
    # Claude Code runs the same Claude models, so it shares descriptions with the API.
    family = "anthropic" if provider.name == "claude-code" else provider.name
    return f"{what}-{family}-{provider.model}.json".replace("/", "_")


def _describe(analysis: Analysis, provider: Provider, cache: Cache, *, batch_size: int, workers: int, effort: str | None) -> list[str]:
    warnings: list[str] = []
    key = _cache_key(provider, "vision")
    visual_clips = [c for c in analysis.clips if c.media.has_video]
    todo: list[Clip] = []
    for clip in visual_clips:
        cached = cache.get(clip.media.fingerprint, key)
        if cached:
            clip.visual = cached
        else:
            todo.append(clip)
    if not todo:
        return warnings

    t0 = time.monotonic()
    progress.stage("vision", f"Looking at {len(todo)} clips")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for clip, frames in zip(todo, pool.map(lambda c: extract_frames(c, cache), todo)):
            clip.frames = frames
    todo = [c for c in todo if c.frames]
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    log(f"describing {len(todo)} clips visually in {len(batches)} request(s)")

    stop = threading.Event()  # a rejected key fails every batch; stop after the first

    def run(batch: list[Clip]) -> list[str]:
        if stop.is_set():
            return []
        parts = []
        for clip in batch:
            parts.append(TextPart(f'Clip {clip.id} "{clip.media.name}" ({clip.media.kind}, {clip.duration:.0f}s):'))
            for f in clip.frames:
                parts.append(ImagePart(Path(f).read_bytes(), "image/jpeg"))
        parts.append(TextPart(f"Log these {len(batch)} clips: {', '.join(c.id for c in batch)}."))
        try:
            result = provider.complete_json(
                VISION_SYSTEM, parts, VISION_SCHEMA, schema_name="clip_log", purpose="visual log", effort=effort, max_tokens=16000
            )
        except ProviderAuthError:
            stop.set()
            raise
        except ProviderError as e:
            return [f"visual log failed for {', '.join(c.id for c in batch)}: {e}"]
        by_id = {str(r.get("id", "")).upper(): r for r in result.get("clips", []) if isinstance(r, dict)}
        missing = []
        for clip in batch:
            r = by_id.get(clip.id)
            if r is None:
                missing.append(clip.id)
                continue
            clip.visual = {
                "description": str(r.get("description", "")),
                "tags": [str(t).lower() for t in r.get("tags", [])][:8],
                "shot": str(r.get("shot", "")),
                "quality": str(r.get("quality", "")),
            }
            cache.put(clip.media.fingerprint, key, clip.visual)
        return [f"visual log skipped {', '.join(missing)}"] if missing else []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, w in enumerate(pool.map(run, batches), 1):
            warnings.extend(w)
            progress.stage("vision", f"Looking at {len(todo)} clips", current=done, total=len(batches))
    log(f"visual log done in {time.monotonic() - t0:.1f}s")
    return warnings


# ------------------------------------------------------------------ moments in long B-roll

MOMENTS_MIN_SECONDS = 15.0  # shorter B-roll: the three frames of its description are enough
MAX_MOMENT_FRAMES = 12


def moment_times(clip: Clip) -> list[float]:
    """Where to look inside a long B-roll clip: every 6-10 s, at most 12 frames."""
    dur = clip.duration
    if clip.role != "broll" or clip.media.kind != "video" or clip.hidden or dur <= MOMENTS_MIN_SECONDS:
        return []
    n = min(MAX_MOMENT_FRAMES, max(2, int(dur // 8)))
    return [round((i + 0.5) * dur / n, 2) for i in range(n)]


def extract_moment_frames(clip: Clip, cache: Cache) -> list[tuple[float, str]]:
    m = clip.media
    frames = []
    for t in moment_times(clip):
        dest = cache.path(m.fingerprint, f"frames/{t:.2f}.jpg")
        if not dest.exists():
            ffmpeg_ops.extract_frame(Path(m.path), t, dest)
        if dest.exists():
            frames.append((t, str(dest)))
    return frames


def _moments_from(result: dict, clip: Clip, sampled: list[float]) -> list[dict] | None:
    """The model's moments for one clip, snapped to the frames it was shown. None if missing."""
    entry = next((r for r in result.get("clips", []) if isinstance(r, dict) and str(r.get("id", "")).upper() == clip.id), None)
    if entry is None or not isinstance(entry.get("moments"), list):
        return None
    by_time: dict[float, dict] = {}
    for mo in entry["moments"]:
        if not isinstance(mo, dict):
            continue
        try:
            at = float(mo.get("at"))
        except (TypeError, ValueError):
            continue
        near = min(sampled, key=lambda t: abs(t - at))
        if abs(near - at) > 2.0:  # not a frame it was shown
            continue
        by_time[near] = {"at": near, "note": str(mo.get("note", "")).strip()[:120], "usable": mo.get("usable") is not False}
    return [by_time[t] for t in sorted(by_time)]


def describe_moments(
    analysis: Analysis,
    provider: Provider,
    cache: Cache,
    *,
    batch_size: int = 3,
    workers: int = 4,
    effort: str | None = "low",
) -> list[str]:
    """Log timed moments in long B-roll (clip.visual["moments"]), so source_in can point at the good part."""
    key = _cache_key(provider, "vision-moments-v1")
    todo: list[Clip] = []
    for clip in analysis.clips:
        if not moment_times(clip):
            continue
        cached = cache.get(clip.media.fingerprint, key)
        if cached is not None:
            clip.visual = {**(clip.visual or {}), "moments": cached}
        else:
            todo.append(clip)
    if not todo:
        return []
    frames: dict[str, list[tuple[float, str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for clip, got in zip(todo, pool.map(lambda c: extract_moment_frames(c, cache), todo)):
            frames[clip.id] = got
    todo = [c for c in todo if frames[c.id]]
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    log(f"logging moments in {len(todo)} long B-roll clips in {len(batches)} request(s)")
    stop = threading.Event()

    def run(batch: list[Clip]) -> list[str]:
        if stop.is_set():
            return []
        parts = []
        for clip in batch:
            parts.append(TextPart(f'Clip {clip.id} "{clip.media.name}" ({clip.duration:.0f}s):'))
            for t, path in frames[clip.id]:
                parts.append(TextPart(f"{clip.id} @{seconds_to_clock(t)} ({t:g}s)"))
                parts.append(ImagePart(Path(path).read_bytes(), "image/jpeg"))
        parts.append(TextPart(f"Log the moments in these {len(batch)} clips: {', '.join(c.id for c in batch)}."))
        try:
            result = provider.complete_json(
                MOMENTS_SYSTEM, parts, MOMENTS_SCHEMA, schema_name="clip_moments", purpose="visual log", effort=effort, max_tokens=16000
            )
        except ProviderAuthError:
            stop.set()
            raise
        except ProviderError as e:
            return [f"moment log failed for {', '.join(c.id for c in batch)}: {e}"]
        missing = []
        for clip in batch:
            moments = _moments_from(result if isinstance(result, dict) else {}, clip, [t for t, _ in frames[clip.id]])
            if moments is None:
                missing.append(clip.id)
                continue
            clip.visual = {**(clip.visual or {}), "moments": moments}
            cache.put(clip.media.fingerprint, key, moments)
        return [f"moment log skipped {', '.join(missing)}"] if missing else []

    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for w in pool.map(run, batches):
            warnings.extend(w)
    return warnings

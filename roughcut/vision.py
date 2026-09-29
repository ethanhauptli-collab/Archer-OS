"""Stage 2 (optional): sample frames and have a vision model log each clip."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpeg_ops
from .analyze import Analysis, Cache, Clip, log
from .prompts import VISION_SCHEMA, VISION_SYSTEM
from .providers import ImagePart, Provider, ProviderError, TextPart


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
    """Fill clip.visual for every clip with pictures. Returns warnings."""
    warnings: list[str] = []
    key = f"vision-{provider.name}-{provider.model}.json".replace("/", "_")
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
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for clip, frames in zip(todo, pool.map(lambda c: extract_frames(c, cache), todo)):
            clip.frames = frames
    todo = [c for c in todo if c.frames]
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    log(f"describing {len(todo)} clips visually in {len(batches)} request(s)")

    def run(batch: list[Clip]) -> list[str]:
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
        for w in pool.map(run, batches):
            warnings.extend(w)
    log(f"visual log done in {time.monotonic() - t0:.1f}s")
    return warnings

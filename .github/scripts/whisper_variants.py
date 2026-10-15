"""Does the way roughcut asks Whisper for words lose speech?

Whisper can leave a hole in a transcript: a stretch of a few seconds with speech in it and no words. The cut then removes it as
dead air. This transcribes the clips kept by whisper_timing.py (the macOS `say` speech and the readings) in several ways and
counts, for each, the sound that no word is anywhere near, and the holes: runs of such sound.

    python whisper_variants.py --shoots DIR [--clips reading,reading-twain,shoot6]

A clip is `talk.mov` in a folder of DIR (shoot1..7 and reading*). Sound is where the clip's 10 ms loudness is 12 dB over its room
(ffmpeg_ops.level_threshold); a word covers the sound from 0.25 s before it to 0.25 s after.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import numpy as np  # noqa: E402

import whisper_timing as wt  # noqa: E402
from roughcut import ffmpeg_ops  # noqa: E402
from roughcut.transcribe import VERBATIM_PROMPT, _from_whisper_dict  # noqa: E402

VARIANTS = [  # (name, condition on the previous text, hallucination_silence_threshold)
    ("as shipped: conditioned on the previous text, skips 2 s of silence it suspects of an invention", True, 2.0),
    ("without the silence skipping", True, None),
    ("not conditioned on the previous text", False, 2.0),
    ("neither", False, None),
]


def transcribe(mov: Path, model: str, condition: bool, threshold: float | None):
    import mlx_whisper

    kwargs = dict(path_or_hf_repo=model, word_timestamps=True, condition_on_previous_text=condition, initial_prompt=VERBATIM_PROMPT)
    if threshold is not None:
        kwargs["hallucination_silence_threshold"] = threshold
    return _from_whisper_dict(mlx_whisper.transcribe(str(mov), **kwargs), source="variant").words


def sound_of(mov: Path) -> np.ndarray | None:
    levels = ffmpeg_ops.window_rms(mov, 0.01)
    found = ffmpeg_ops.level_threshold(levels)
    if found is None:
        return None
    return np.array(levels) > found[1] + 12.0


def uncovered(words, sound: np.ndarray, reach: float = 0.25) -> tuple[float, list[tuple[float, float, float]]]:
    """(seconds of sound no word is within `reach` of, the runs of it: (start, end, seconds of sound) for those of 0.3 s or more)"""
    n = len(sound)
    covered = np.zeros(n, dtype=bool)
    for w in words:
        covered[max(0, int((w.start - reach) / 0.01)) : min(n, int((w.end + reach) / 0.01) + 1)] = True
    free = sound & ~covered
    runs: list[tuple[float, float, float]] = []
    start = last = None
    count = 0
    for i in np.flatnonzero(free):
        if start is not None and i - last <= 15:  # up to 150 ms of quiet inside a run
            last = i
            count += 1
            continue
        if start is not None and count >= 30:
            runs.append((start * 0.01, (last + 1) * 0.01, count * 0.01))
        start, last, count = i, i, 1
    if start is not None and count >= 30:
        runs.append((start * 0.01, (last + 1) * 0.01, count * 0.01))
    return float(free.sum()) * 0.01, runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shoots", type=Path, required=True)
    ap.add_argument("--clips", default="reading,reading-austen,reading-doyle,reading-twain,shoot2,shoot6,shoot7")
    a = ap.parse_args()
    model = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    totals: dict[str, list[float]] = {name: [0.0, 0.0, 0.0] for name, _, _ in VARIANTS}  # sound uncovered, all sound, words
    for clip in a.clips.split(","):
        mov = a.shoots / clip / "talk.mov"
        if not mov.exists():
            print(f"\n#### {clip}: not here; skipped")
            continue
        sound = sound_of(mov)
        if sound is None:
            print(f"\n#### {clip}: speech and background within 12 dB of each other; skipped")
            continue
        truth_file = a.shoots / clip / "truth.json"
        truth = json.loads(truth_file.read_text()) if truth_file.exists() else None
        print(f"\n#### {clip}: {len(sound) * 0.01:.0f} s, {float(sound.sum()) * 0.01:.0f} s of it sound")
        for name, condition, threshold in VARIANTS:
            began = time.monotonic()
            words = transcribe(mov, model, condition, threshold)
            took = time.monotonic() - began
            free, runs = uncovered(words, sound)
            totals[name][0] += free
            totals[name][1] += float(sound.sum()) * 0.01
            totals[name][2] += len(words)
            holes = ", ".join(f"{s:.1f}-{e:.1f} ({t:.1f} s)" for s, e, t in runs[:8]) or "none"
            print(f"  {name}: {len(words)} words, {free:.1f} s of sound with no word near it ({100 * free / (float(sound.sum()) * 0.01):.1f}%), {len(runs)} runs of 0.3 s or more: {holes}   [{took:.0f} s]", flush=True)
            if truth is not None:
                heard = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
                with contextlib.redirect_stdout(io.StringIO()) as held:
                    wt.compare(truth["words"], heard)
                    wt.invented(truth, heard)
                lines = held.getvalue().splitlines()
                print("    " + " | ".join(line.strip() for line in lines if line.startswith("Whisper")))
    print("\n#### all the clips")
    for name, (free, sound_s, words) in totals.items():
        print(f"  {name}: {int(words)} words, {free:.1f} s of sound with no word near it of {sound_s:.0f} s ({100 * free / max(sound_s, 1e-9):.2f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

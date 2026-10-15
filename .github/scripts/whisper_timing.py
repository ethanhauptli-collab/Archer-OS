"""How far off are Whisper's word times on speech, and what does the cut do with them?

Every word is said on its own by a speech synthesizer (macOS `say`; `espeak-ng` for a dry run elsewhere), so each word's true start
and end are known to the sample. The words become sentences, the sentences a few minutes of talk over a noise bed (room tone, a
60 Hz hum, wind gusts), as AAC in a MOV, like a phone's. Then:

  1. Whisper (the model the app uses by default, or $WHISPER_MODEL) transcribes it and its word times are compared with the truth:
     how many words, and how far off their starts and ends are;
  2. `roughcut build --provider none` cuts it from that transcript (handed over as a sidecar, so every cut shares it), at tight and
     medium pacing, and the cut is measured against the audio itself: words clipped, cut edges inside speech, pauses left.

It prints the numbers and exits 0 unless the cut is plainly bad. Synthetic speech is cleaner than a person talking outdoors, so these
are the best case for Whisper, not the typical one.

    python .github/scripts/whisper_timing.py [--minutes 3] [--dry] [--keep DIR]
`--dry` takes the transcript from the truth with timing errors added, so the generator, the comparison and the cut can be tried
without a model.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import difflib
import gzip
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import numpy as np

SR = 48000
VOCAB = (
    "we climbed the ridge before rain and kids led way up to lake this is a long day honestly I think that it was better than expected "
    "because the trail steep cold warm quiet river bridge morning coffee camera battery tent fire stars sleep wind storm clouds "
    "water fresh bread map packed every thing happy tired proud simple first last again always never maybe really actually "
    "sunrise sunset breakfast dinner family friends dog car road village market harbor boat sound shot frame light shade stone"
).split()


# ------------------------------------------------------------------ the speech


def say(word: str, work: Path, voice: str | None, rate: int) -> np.ndarray:
    """One word as 48 kHz mono floats."""
    raw = work / f"{voice or 'default'}_{rate}_{word}.aiff"
    out = work / f"{voice or 'default'}_{rate}_{word}.wav"
    if not out.exists():
        if shutil.which("say"):
            cmd = ["say", "-r", str(rate), "-o", str(raw), "--", word] if not voice else ["say", "-v", voice, "-r", str(rate), "-o", str(raw), "--", word]
            subprocess.run(cmd, check=True, capture_output=True)
        else:  # a dry run on Linux
            raw = raw.with_name(raw.stem + "_raw.wav")
            subprocess.run(["espeak-ng", "-s", str(rate), "-w", str(raw), word], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(out)], check=True)
    with wave.open(str(out)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def extent(x: np.ndarray, floor: float = 0.004) -> tuple[int, int]:
    idx = np.flatnonzero(np.abs(x) > floor)
    return (int(idx[0]), int(idx[-1]) + 1) if len(idx) else (0, len(x))


def make_talk(out: Path, minutes: float, seed: int, voice: str | None, rate: int, noise_db: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    work.mkdir(exist_ok=True)
    rng = random.Random(seed)
    seconds = minutes * 60
    n = int(seconds * SR)
    audio = np.zeros(n, dtype=np.float32)
    words, pauses = [], []
    t = 1.2
    while t < seconds - 8:
        gain = 10 ** (rng.uniform(-3, 2) / 20)
        for _ in range(rng.randint(6, 13)):
            text = rng.choice(VOCAB)
            x = say(text, work, voice, rate)
            a, b = extent(x)
            if a >= b:
                continue
            x = x[a:b] * gain
            i = int(t * SR)
            if i + len(x) >= n:
                break
            audio[i : i + len(x)] += x
            words.append({"text": text, "start": round(i / SR, 4), "end": round((i + len(x)) / SR, 4)})
            t += len(x) / SR + rng.uniform(0.03, 0.11)
        gap = rng.uniform(0.35, 1.5) + (rng.uniform(3, 8) if rng.random() < 0.07 else 0)
        pauses.append({"after": words[-1]["end"], "length": round(gap, 3)})
        t += gap
    audio *= 0.5 / max(np.max(np.abs(audio)), 1e-9)
    nr = np.random.default_rng(seed)
    white = nr.standard_normal(n).astype(np.float32)
    bed = np.convolve(white, np.ones(8, dtype=np.float32) / 8, mode="same") * 0.7 + white * 0.3
    bed *= 10 ** (noise_db / 20) / (np.sqrt(np.mean(bed**2)) + 1e-9)
    tt = np.arange(n) / SR
    bed += 0.5 * 10 ** ((noise_db - 10) / 20) * (np.sin(2 * np.pi * 60 * tt) + 0.5 * np.sin(2 * np.pi * 120 * tt))
    k = 0
    while True:
        k += int(nr.uniform(8, 25) * SR)
        length = int(nr.uniform(1.0, 2.5) * SR)
        if k + length >= n:
            break
        low = np.convolve(nr.standard_normal(length), np.ones(40) / 40, mode="same").astype(np.float32)
        bed[k : k + length] += np.hanning(length).astype(np.float32) * low * 10 ** ((noise_db + 7) / 20) / (np.sqrt(np.mean(low**2)) + 1e-9)
    mix = np.clip(audio + bed, -1, 1)
    wav = out / ".mix.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x203040:size=480x270:rate=30000/1001:duration={seconds}",
         "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(out / "talk.mov")],
        check=True,
    )
    wav.unlink()
    truth = {"words": words, "pauses": pauses, "seconds": seconds, "noise_db": noise_db}
    (out / "truth.json").write_text(json.dumps(truth))
    return truth


# ------------------------------------------------------------------ whisper against the truth


def norm(text: str) -> str:
    return re.sub(r"[^a-z']", "", text.lower())


def compare(truth_words: list[dict], heard: list[dict]) -> dict[int, int]:
    """Prints how Whisper's word times differ from the truth; returns {index of a word said: index of the word heard for it}."""
    a = [norm(w["text"]) for w in truth_words]
    b = [norm(w["text"]) for w in heard]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pairs = [(i + k, j + k) for i, j, size in matcher.get_matching_blocks() for k in range(size)]
    print(f"Whisper heard {len(heard)} words for {len(truth_words)} said; {len(pairs)} line up with a word that was said ({100 * len(pairs) / len(truth_words):.1f}%)")
    start_err, end_err, end_err_pause = [], [], []
    for i, j in pairs:
        t, h = truth_words[i], heard[j]
        start_err.append((h["start"] - t["start"]) * 1000)
        followed_by_pause = i + 1 < len(truth_words) and truth_words[i + 1]["start"] - t["end"] > 0.3
        (end_err_pause if followed_by_pause else end_err).append((h["end"] - t["end"]) * 1000)

    def line(name: str, errs: list[float]) -> None:
        if not errs:
            return
        e = np.array(errs)
        a_ = np.abs(e)
        print(
            f"  {name:<34} n={len(e):<4} bias {np.median(e):+6.0f} ms (median signed)  |err| median {np.median(a_):4.0f}  90th {np.percentile(a_, 90):4.0f}  "
            f"99th {np.percentile(a_, 99):4.0f}  >60 ms {100 * np.mean(a_ > 60):4.1f}%  >100 ms {100 * np.mean(a_ > 100):4.1f}%  >150 ms {100 * np.mean(a_ > 150):4.1f}%"
        )
        late = e[e > 60]
        early = e[e < -60]
        print(f"  {'':<34}        later than the truth by >60 ms: {100 * len(late) / len(e):4.1f}%   earlier by >60 ms: {100 * len(early) / len(e):4.1f}%")

    line("word starts", start_err)
    line("word ends (next word follows)", end_err)
    line("word ends (a pause follows)", end_err_pause)
    return dict(pairs)


def fake_heard(truth_words: list[dict], seed: int = 5) -> list[dict]:
    """--dry: the truth with the kinds of error Whisper makes."""
    rng = random.Random(seed)
    out = []
    for i, w in enumerate(truth_words):
        nxt = truth_words[i + 1]["start"] if i + 1 < len(truth_words) else w["end"] + 1
        s = w["start"] + rng.gauss(0, 0.04) - (rng.uniform(0.1, 0.3) if rng.random() < 0.1 else 0)
        e = w["end"] + rng.gauss(0, 0.03)
        if rng.random() < 0.35:
            e = nxt - 0.005 if nxt - e < 0.9 else e + rng.uniform(0.2, 0.8)
        out.append({"text": w["text"], "start": max(0.0, s), "end": max(s + 0.05, e)})
    return out


# ------------------------------------------------------------------ the cut against the audio


def T(s: str | None) -> Fraction:
    s = (s or "0s").rstrip("s")
    return Fraction(s) if s else Fraction(0)


def kept_ranges(fcpxml: Path) -> list[tuple[float, float]]:
    root = ET.parse(fcpxml).getroot()
    assets = {a.get("id"): a for a in root.iter("asset")}
    rough = next(p for p in root.iter("project") if "Rough Cut" in p.get("name", ""))
    out = []
    for el in rough.find("sequence/spine"):
        if el.tag != "asset-clip" or assets[el.get("ref")].get("name") != "talk":
            continue
        a = float(T(el.get("start")) - T(assets[el.get("ref")].get("start")))
        out.append((a, a + float(T(el.get("duration")))))
    out.sort()
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a - merged[-1][1] < 0.002:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0


def db(v: float) -> float:
    return 20 * np.log10(max(v, 1e-9))


def cause(edge: float, direction: int, truth_words: list[dict], heard: list[dict], matched: dict[int, int]) -> str:
    """Why a cut edge sits in speech: the word it cuts, and what Whisper said about it."""
    best, gap = None, 9.0
    for i, w in enumerate(truth_words):
        d = 0.0 if w["start"] <= edge <= w["end"] else min(abs(edge - w["start"]), abs(edge - w["end"]))
        if d < gap:
            best, gap = i, d
    if best is None or gap > 0.3:
        return "no word said near it"
    w = truth_words[best]
    if best not in matched:
        return f"Whisper missed {w['text']!r} ({w['start']:.2f}-{w['end']:.2f} s)"
    h = heard[matched[best]]
    return (
        f"{w['text']!r} said {w['start']:.2f}-{w['end']:.2f}, Whisper had {h['start']:.2f}-{h['end']:.2f} "
        f"(start {1000 * (h['start'] - w['start']):+.0f} ms, end {1000 * (h['end'] - w['end']):+.0f} ms)"
    )


def judge(out: Path, talk: Path, truth: dict, label: str, heard: list[dict] | None = None, matched: dict[int, int] | None = None, brief: bool = False) -> dict:
    kept = kept_ranges(next(out.glob("*.fcpxml")))
    audio = load_audio(talk)
    words = truth["words"]
    whole = gone = clipped = 0
    for w in words:
        inside = sum(max(0.0, min(w["end"], b) - max(w["start"], a)) for a, b in kept)
        removed = (w["end"] - w["start"]) - inside
        if inside < 0.5 * (w["end"] - w["start"]):
            gone += 1
        elif removed > 0.015:
            clipped += 1
        else:
            whole += 1
    win = int(0.010 * SR)
    quiet = []
    for p in truth["pauses"]:
        for i in range(int((p["after"] + 0.15) * SR), int((p["after"] + p["length"] - 0.15) * SR) - win, win * 5):
            quiet.append(rms(audio[i : i + win]))
    floor = float(np.median(quiet)) if quiet else 1e-3
    inside_speech = outside_speech = edges = 0
    flagged = []
    for k, (a, b) in enumerate(kept):
        for edge, direction in ((a, +1), (b, -1)):
            if (direction > 0 and k == 0 and a < 0.05) or (direction < 0 and b > truth["seconds"] - 0.05):
                continue
            edges += 1
            i = int(edge * SR)
            inner = audio[i : i + win] if direction > 0 else audio[i - win : i]
            outer = audio[i - win : i] if direction > 0 else audio[i : i + win]
            in_, out_ = rms(inner) > 3.0 * floor, rms(outer) > 3.0 * floor
            inside_speech += in_
            outside_speech += out_
            if (in_ or out_) and heard is not None:
                flagged.append((edge, "starts" if direction > 0 else "ends", "inside" if in_ else "outside", cause(edge, direction, words, heard, matched or {})))
    n = len(words)
    result = {
        "whole": whole / n, "clipped": clipped / n, "gone": gone / n, "in": inside_speech / max(edges, 1), "out": outside_speech / max(edges, 1),
        "edges": edges, "kept": sum(b - a for a, b in kept),
    }
    if brief:
        return result
    print(
        f"{label}: {n} words said: {100 * result['whole']:.1f}% whole, {100 * result['clipped']:.1f}% clipped by >15 ms, {100 * result['gone']:.1f}% not in the cut; "
        f"of {edges} cut edges {100 * result['in']:.1f}% start or end inside speech and {100 * result['out']:.1f}% threw a word's edge away (noise floor {db(floor):.0f} dBFS)"
    )
    for edge, kind, where, why in flagged[:12]:
        print(f"    a cut {kind} at {edge:.2f} s with speech {where}: {why}")
    if len(flagged) > 12:
        print(f"    ... and {len(flagged) - 12} more")
    return result


# ------------------------------------------------------------------ main


RUNS = [  # (voice, words per minute, seed)
    (None, 185, 11),
    ("Daniel", 170, 12),
    ("Alex", 200, 13),
]


DATA_LINES: list[str] = []  # printed after the summary, so the end of the log holds them


def dump(n: int, truth: dict, heard: list[dict], analysis: Path) -> None:
    """The run's data as log lines (`DATA n i/m chunk`: gzip + base64 of one JSON), so it can be rebuilt from the log and the cut
    tried again offline on real Whisper output: what was said, what Whisper heard, and the silence map the cut used."""
    clip = json.loads((analysis / "analysis.json").read_text())["clips"][0]
    blob = {
        "n": n,
        "seconds": truth["seconds"],
        "truth": [[w["text"], w["start"], w["end"]] for w in truth["words"]],
        "pauses": truth["pauses"],
        "heard": [[w["text"], round(w["start"], 3), round(w["end"], 3)] for w in heard],
        "silences": [[round(x, 3), round(y, 3)] for x, y in clip.get("silences", [])],
        "breaths": clip.get("breaths", []),
    }
    text = base64.b64encode(gzip.compress(json.dumps(blob, separators=(",", ":")).encode())).decode()
    chunks = [text[i : i + 3000] for i in range(0, len(text), 3000)]
    DATA_LINES.extend(f"DATA {n} {i}/{len(chunks)} {chunk}" for i, chunk in enumerate(chunks))


def build(shoot: Path, out: Path, style: str, cache: Path, engine: list[str], *, in_process: bool = False) -> None:
    """`roughcut build --provider none` on the shoot; in this process (output held back) when many are made one after another."""
    args = ["build", str(shoot), "--provider", "none", *engine, "--style", style, "-o", str(out), "--cache-dir", str(cache), "--no-menu"]
    if in_process:
        from roughcut import cli

        held = io.StringIO()
        with contextlib.redirect_stdout(held), contextlib.redirect_stderr(held):
            code = cli.main(args)
        if code:
            print(held.getvalue()[-1500:])
            raise SystemExit(1)
        return
    proc = subprocess.run([sys.executable, "-m", "roughcut", *args], capture_output=True, text=True)
    if proc.returncode:
        print(proc.stdout[-1500:], proc.stderr[-1500:])
        raise SystemExit(1)


def one_run(root: Path, shoots: Path, a: argparse.Namespace, voice: str | None, rate: int, seed: int, n: int) -> dict | None:
    shoot = shoots / f"shoot{n}"
    model = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    kept_truth, kept_heard = shoot / "truth.json", shoot / "heard.json"
    reuse = not a.dry and kept_truth.exists() and kept_heard.exists() and (shoot / "talk.mov").exists() and (shoot / "talk.json").exists()
    if reuse:
        truth, heard = json.loads(kept_truth.read_text()), json.loads(kept_heard.read_text())
        print(f"\n#### clip {n}: voice {voice or 'default'}, {rate} words a minute, {len(truth['words'])} words: the speech and Whisper's transcript of it are kept from an earlier run", flush=True)
        engine = ["--transcriber", "none"]
    else:
        try:
            truth = make_talk(shoot, a.minutes, seed, voice, rate, a.noise_db)
        except subprocess.CalledProcessError:
            if voice is None:
                raise
            print(f"voice {voice} isn't here; skipped")
            return None
        print(f"\n#### clip {n}: voice {voice or 'default'}, {rate} words a minute, {len(truth['words'])} words, {len(truth['pauses'])} pauses, noise bed {a.noise_db:g} dBFS", flush=True)
        if a.dry:
            heard = fake_heard(truth["words"])
            (shoot / "talk.json").write_text(json.dumps({"words": [{"start": h["start"], "end": h["end"], "text": h["text"], "prob": 0.9} for h in heard]}))
            print("dry run: the transcript is the truth with timing errors added")
        else:
            from roughcut.transcribe import Transcriber

            print(f"transcribing with {model} ...", flush=True)
            tr = Transcriber("mlx", model=model, verbatim=True).transcribe(shoot / "talk.mov")
            heard = [{"text": w.text, "start": w.start, "end": w.end} for w in tr.words]
            # The cuts are made from this very transcript (as a sidecar), so every build below, and the data printed at the end, share it.
            (shoot / "talk.json").write_text(json.dumps({"words": [{"start": w.start, "end": w.end, "text": w.text, "prob": w.prob} for w in tr.words]}))
            kept_heard.write_text(json.dumps(heard))
        engine = ["--transcriber", "none"]
    print("\n== Whisper's word times against the words that were said")
    matched = compare(truth["words"], heard)
    print("\n== the cut, made from that transcript")
    results = {}
    for style in ("tight", "medium"):
        out = root / f"out{n}_{style}"
        build(shoot, out, style, root / "cache", engine)
        results[style] = judge(out, shoot / "talk.mov", truth, style, heard, matched)
    dump(n, truth, heard, root / f"out{n}_tight")
    return {"n": n, "shoot": shoot, "truth": truth, "results": results}


# What the sweep tries on the tight cut (and the medium one at the end): the window after a word's end in which a sound still going is
# taken for its tail, and the transcript gap that is not taken for a pause when the silence map hears sound through it.
SWEEP = [  # (pacing, end slack, fake gap, pad_out or None)
    ("tight", 0.10, 0.00, None),  # the cut before the fix
    ("tight", 0.10, 0.30, None),
    ("tight", 0.25, 0.00, None),
    ("tight", 0.35, 0.00, None),
    ("tight", 0.50, 0.00, None),
    ("tight", 0.25, 0.30, None),
    ("tight", 0.35, 0.30, None),
    ("tight", 0.50, 0.30, None),
    ("tight", 0.35, 0.15, None),
    ("tight", 0.35, 0.50, None),
    ("tight", 0.35, 0.30, 0.16),
    ("tight", 0.35, 0.30, 0.20),
    ("medium", 0.10, 0.00, None),
    ("medium", 0.35, 0.30, None),
]


@contextlib.contextmanager
def tuned(style: str, end_slack: float, fake_gap: float, pad_out: float | None):
    from roughcut import timeline

    saved = (getattr(timeline, "EDGE_SLACK_END", None), getattr(timeline, "FAKE_GAP", None), timeline.STYLES[style])
    try:
        timeline.EDGE_SLACK_END = end_slack
        if saved[1] is not None:
            timeline.FAKE_GAP = fake_gap
        if pad_out is not None:
            timeline.STYLES[style] = dataclasses.replace(saved[2], pad_out=pad_out)
        yield
    finally:
        timeline.STYLES[style] = saved[2]
        if saved[0] is not None:
            timeline.EDGE_SLACK_END = saved[0]
        if saved[1] is not None:
            timeline.FAKE_GAP = saved[1]


def sweep(root: Path, clips: list[dict]) -> None:
    print("\n#### the same transcripts cut with other settings (every clip, the cut measured against the audio)")
    for k, (style, end_slack, fake_gap, pad_out) in enumerate(SWEEP):
        rows = []
        for c in clips:
            out = root / f"sw{k}_{c['n']}"
            with tuned(style, end_slack, fake_gap, pad_out):
                build(c["shoot"], out, style, root / "cache", ["--transcriber", "none"], in_process=True)
            rows.append(judge(out, c["shoot"] / "talk.mov", c["truth"], "", brief=True))
        m = {key: float(np.mean([r[key] for r in rows])) for key in ("whole", "clipped", "gone", "in", "out", "kept")}
        pad = f", pad_out {pad_out:g}" if pad_out is not None else ""
        per_clip = ", ".join("%.1f" % (100 * r["whole"]) for r in rows)
        print(
            f"{style:<6} end window +{end_slack:.2f} s, transcript gap +{fake_gap:.2f} s{pad}: words whole {100 * m['whole']:5.1f}%, clipped {100 * m['clipped']:5.1f}%, "
            f"not in the cut {100 * m['gone']:4.1f}%; edges inside speech {100 * m['in']:5.1f}%, a word's edge thrown away {100 * m['out']:5.1f}%; kept {m['kept']:6.1f} s"
            f"   (whole per clip: {per_clip})",
            flush=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--noise-db", type=float, default=-42)
    ap.add_argument("--runs", type=int, default=1, help="how many clips (different voices and speeds)")
    ap.add_argument("--dry", action="store_true", help="no model: the transcript is the truth with timing errors added")
    ap.add_argument("--keep", type=Path, default=None)
    ap.add_argument("--shoots", type=Path, default=None, help="where the speech and Whisper's transcript of it are kept for the next run (default: with the cuts)")
    ap.add_argument("--sweep", action="store_true", help="also cut the same transcripts with other settings and compare")
    a = ap.parse_args()
    root = a.keep or Path(tempfile.mkdtemp(prefix="whisper-timing-"))
    shoots = a.shoots or root
    shoots.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    print(f"speech by {'say' if shutil.which('say') else 'espeak-ng'} in {shoots}, cuts in {root}", flush=True)
    done = []
    for n, (voice, rate, seed) in enumerate(RUNS[: a.runs], 1):
        r = one_run(root, shoots, a, voice, rate, seed, n)
        if r:
            done.append(r)
    all_results = [r["results"] for r in done]
    print("\n#### all clips")
    for style in ("tight", "medium"):
        rs = [r[style] for r in all_results]
        if rs:
            m = {k: float(np.mean([x[k] for x in rs])) for k in ("whole", "clipped", "gone", "in", "out")}
            print(f"{style}: {len(rs)} clips; words whole {100 * m['whole']:.1f}%, clipped {100 * m['clipped']:.1f}%, not in the cut {100 * m['gone']:.1f}%; cut edges inside speech {100 * m['in']:.1f}%, a word's edge thrown away {100 * m['out']:.1f}%")
    ok = bool(all_results) and all(r[st]["whole"] >= 0.80 and r[st]["in"] <= 0.25 and r[st]["out"] <= 0.25 for r in all_results for st in r)
    print("\nwhisper timing: " + ("done" if ok else "the cut is clearly worse than it should be (see above)"))
    if a.sweep and done:
        sweep(root, done)
    print("\n#### data (gzip + base64 of one JSON per clip: what was said, what Whisper heard, the silence map)")
    print("\n".join(DATA_LINES), flush=True)
    return 0  # a measurement, not a gate


if __name__ == "__main__":
    raise SystemExit(main())

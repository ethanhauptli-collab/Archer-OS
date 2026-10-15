"""`roughcut check FOLDER`: which edits of a finished cut land in the middle of sound?

A cut that takes a word's last syllable, or starts a moment after the word did, is easy to miss on one listen and takes a second to
fix in Final Cut once you know where it is. This listens for you. It reads the newest version's FCPXML in a Roughcut output folder
(the file you imported), finds every edit on the spine where the dialogue is cut, plays the audio either side of it from the original
file, and names the edits with sound on both sides: the cut goes through a sound. It gives the timeline timecode to go to, the clip,
and the words around it from the transcript.

It also says how long the longest pause left in the dialogue is and which um/uh are still in the cut.

Read only: it writes nothing, calls no model and needs no key. It listens to the dialogue clips' audio once (about fifty times faster
than real time), so a long shoot takes a few minutes. It cannot tell a cut in loud noise from one in speech: where the background is
within 12 dB of the speech the clip is counted but not judged, and the report says so.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import ffmpeg_ops
from . import timecode as tc
from .textio import Stalled, bounded, read_text_lenient
from .transcribe import Word

WINDOW = 0.01  # seconds per loudness reading
SPEECH_OVER_FLOOR = 12.0  # dB over the clip's quiet floor that counts as sound (a breath or room noise stays under it)
FLOOR_LIMIT = -75.0  # a recording quieter than this has nothing in its pauses: judge against this floor instead
MIN_SEPARATION = 12.0  # dB between a clip's quiet floor and its loudest twentieth below which sound and background can't be told apart
MIN_RUN = 0.03  # seconds of sound cut off below which an edit isn't worth a look
QUIET_OVER_FLOOR = 6.0  # dB over the floor that still counts as a pause, for the pauses left in the dialogue
LISTEN_SECONDS = 300.0  # at least this long to listen to a clip's audio before giving up on it (more for a long clip)
WORDS_AROUND = 4  # words shown each side of an edit


@dataclass
class Edge:
    """One end of a dialogue clip on the spine, where the picture cuts."""

    kind: str  # "starts": the kept part begins here; "ends": it stops here
    asset: str  # the original file
    source: float  # seconds into it
    timeline: Fraction  # seconds into the cut
    clip: str  # its name in the project


@dataclass
class Flag:
    edge: Edge
    cut_off: float  # seconds of sound on the removed side of the edit, up to LOOK
    kept: float  # seconds of sound on the kept side
    words: str  # the words around it, with ‖ at the edit


@dataclass
class Report:
    fcpxml: str
    project: str
    frame_duration: Fraction
    edits: int = 0  # dialogue edges judged
    clips: int = 0  # dialogue clips listened to
    seconds: float = 0.0  # of dialogue audio listened to
    flagged: list[Flag] = field(default_factory=list)
    small: int = 0  # edits with sound on both sides for less than MIN_RUN
    unjudged: list[str] = field(default_factory=list)  # files too noisy to tell sound from background
    longest_pause: tuple[float, Fraction] | None = None
    long_pauses: int = 0  # kept pauses over a second
    fillers: list[tuple[str, Fraction]] = field(default_factory=list)  # (the word, where) still in the cut
    problems: list[str] = field(default_factory=list)


LOOK = 0.5  # seconds either side of an edit that are looked at for how long the sound goes on


# ------------------------------------------------------------------ the cut

def _time(text: str | None) -> Fraction:
    return tc.parse_fcpx_time(text) if text else Fraction(0)


def _path(src: str) -> str:
    """The file a media-rep src names."""
    parsed = urlparse(src)
    return unquote(parsed.path) if parsed.scheme == "file" else unquote(src)


def spine_edges(fcpxml: Path) -> tuple[str, Fraction, list[Edge], list[tuple[str, float, float, Fraction]]]:
    """(project name, frame duration, the dialogue edits, the kept dialogue ranges) of a Roughcut FCPXML.

    The ranges are (file, source start, source end, timeline start). An edit is not listed where the next spine clip carries on from
    this one in the same file, nor at the very start or end of a file.
    """
    root = ET.parse(fcpxml).getroot()
    assets: dict[str, dict] = {}
    for a in root.iter("asset"):
        rep = a.find("media-rep")
        src = rep.get("src") if rep is not None else a.get("src")
        assets[a.get("id", "")] = {"name": a.get("name", ""), "path": _path(src or ""), "start": _time(a.get("start")), "duration": _time(a.get("duration"))}
    formats = {f.get("id"): _time(f.get("frameDuration")) for f in root.iter("format")}
    projects = list(root.iter("project"))
    if not projects:
        raise ValueError("no project in this file")
    project = next((p for p in projects if "Rough Cut" in p.get("name", "")), projects[0])
    sequence = project.find("sequence")
    spine = sequence.find("spine") if sequence is not None else None
    if spine is None:
        raise ValueError("no spine in the project")
    frame = formats.get(sequence.get("format"), Fraction(1001, 30000))
    clips = []
    for el in spine:
        if el.tag != "asset-clip" or not (el.get("audioRole") or "").startswith("dialogue"):
            clips.append(None)  # something else on the spine: an edit on either side of it
            continue
        asset = assets.get(el.get("ref", ""))
        if asset is None:
            clips.append(None)
            continue
        origin = asset["start"]
        a = float(_time(el.get("start")) - origin)
        b = a + float(_time(el.get("duration")))
        clips.append((asset, el.get("name", asset["name"]), a, b, _time(el.get("offset")), _time(el.get("duration"))))
    edges: list[Edge] = []
    ranges: list[tuple[str, float, float, Fraction]] = []
    one_frame = float(frame)
    for i, c in enumerate(clips):
        if c is None:
            continue
        asset, name, a, b, offset, length = c
        ranges.append((asset["path"], a, b, offset))
        before, after = (clips[i - 1] if i else None), (clips[i + 1] if i + 1 < len(clips) else None)
        file_end = float(asset["duration"])
        if not (before and before[0]["path"] == asset["path"] and abs(before[3] - a) <= one_frame * 1.5) and a > 0.05:
            edges.append(Edge("starts", asset["path"], a, offset, name))
        if not (after and after[0]["path"] == asset["path"] and abs(after[2] - b) <= one_frame * 1.5) and b < file_end - 0.05:
            edges.append(Edge("ends", asset["path"], b, offset + length, name))
    return project.get("name", ""), frame, edges, ranges


# ------------------------------------------------------------------ the sound

def listen(path: str) -> list[float]:
    """The level (dBFS RMS) of each 10 ms of a file's first audio track."""
    return ffmpeg_ops.window_rms(Path(path), WINDOW)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(q * len(ordered))))]


def _run(levels: list[float], i: int, step: int, threshold: float, limit: int) -> int:
    """How many readings from index i, going by `step`, stay above the threshold (at most `limit`)."""
    n = 0
    while 0 <= i < len(levels) and n < limit and levels[i] > threshold:
        i += step
        n += 1
    return n


def judge(edges: list[Edge], levels: list[float], fillers: list[tuple[float, float]] = ()) -> tuple[list[tuple[Edge, float, float]], int, float | None]:
    """The edits of one file that go through sound: [(edit, seconds of sound cut off, seconds kept)], how many were a little sound
    only, and the quiet floor used (None: sound and background can't be told apart here, nothing is judged)."""
    if len(levels) < 100:
        return [], 0, None
    floor = max(_percentile(levels, 0.10), FLOOR_LIMIT)
    if _percentile(levels, 0.95) - floor < MIN_SEPARATION:
        return [], 0, None
    threshold = floor + SPEECH_OVER_FLOOR
    look = int(LOOK / WINDOW)
    found, small = [], 0
    for e in edges:
        i = int(round(e.source / WINDOW))
        if not 1 <= i < len(levels) - 1:
            continue
        if e.kind == "starts":  # kept: i onwards; cut away: before i
            kept_run, gone_run = _run(levels, i, +1, threshold, look), _run(levels, i - 1, -1, threshold, look)
        else:  # kept: before i; cut away: i onwards
            kept_run, gone_run = _run(levels, i - 1, -1, threshold, look), _run(levels, i, +1, threshold, look)
        if not kept_run or not gone_run:
            continue  # the sound stops (or starts) at the edit: a clean one
        away = (e.source - gone_run * WINDOW, e.source) if e.kind == "starts" else (e.source, e.source + gone_run * WINDOW)
        if sum(max(0.0, min(b, away[1]) - max(a, away[0])) for a, b in fillers) >= 0.5 * (away[1] - away[0]):
            continue  # what is cut away is mostly a spoken um or uh, which was meant to go
        if gone_run * WINDOW < MIN_RUN:
            small += 1
            continue
        found.append((e, gone_run * WINDOW, kept_run * WINDOW))
    return found, small, floor


def pauses(ranges: list[tuple[str, float, float, Fraction]], levels_by_file: dict[str, list[float]], floors: dict[str, float]) -> tuple[tuple[float, Fraction] | None, int]:
    """The longest quiet stretch inside the kept dialogue (seconds, where in the cut) and how many are over a second."""
    longest: tuple[float, Fraction] | None = None
    over = 0
    for path, a, b, offset in ranges:
        levels, floor = levels_by_file.get(path), floors.get(path)
        if not levels or floor is None:
            continue
        quiet = floor + QUIET_OVER_FLOOR
        i, end = max(0, int(round(a / WINDOW))), min(len(levels), int(round(b / WINDOW)))
        run_from = None
        for k in range(i, end + 1):
            low = k < end and levels[k] <= quiet
            if low and run_from is None:
                run_from = k
            elif not low and run_from is not None:
                length = (k - run_from) * WINDOW
                if length > 1.0:
                    over += 1
                if longest is None or length > longest[0]:
                    longest = (length, offset + Fraction(run_from * WINDOW - a).limit_denominator(1000))
                run_from = None
    return longest, over


# ------------------------------------------------------------------ the words

def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", str(path))


def transcripts(folder: Path) -> dict[str, list[Word]]:
    """{original file: its words} from the run's analysis.json (empty when it is gone)."""
    found = folder / "analysis.json"
    if not found.is_file():
        return {}
    try:
        data = json.loads(read_text_lenient(found))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[Word]] = {}
    for clip in data.get("clips", []):
        path = (clip.get("media") or {}).get("path")
        if not path:
            continue
        words = [Word(w["start"], w["end"], w["text"], w.get("prob", 1.0)) for s in clip.get("segments", []) for w in s.get("words", [])]
        if words:
            out[_norm(path)] = words
    return out


def words_around(words: list[Word], t: float, kind: str) -> str:
    """'…word word ‖ word word…' with the edit at ‖: the words that stay, then the words that go."""
    left = [w for w in words if w.end <= t + 0.02]
    right = [w for w in words if w.start >= t - 0.02]
    # A word the edit cuts through belongs to both sides.
    mid = [w for w in words if w.start < t - 0.02 and w.end > t + 0.02]
    if kind == "ends":
        kept, gone = left[-WORDS_AROUND:] + mid, right[:WORDS_AROUND]
        text = f"…{' '.join(w.text.strip() for w in kept)} ‖ {' '.join(w.text.strip() for w in gone)}…"
    else:
        gone, kept = left[-WORDS_AROUND:], mid + right[:WORDS_AROUND]
        text = f"…{' '.join(w.text.strip() for w in gone)} ‖ {' '.join(w.text.strip() for w in kept)}…"
    return text.replace("…  ‖", "… ‖").replace("‖  …", "‖ …")


# ------------------------------------------------------------------ the whole check

def find_cut(target: Path) -> tuple[Path, Path]:
    """(the FCPXML, the run's folder) for an output folder or an FCPXML inside one."""
    target = target.expanduser()
    if target.is_file():
        return target, target.parent
    if not target.is_dir():
        raise FileNotFoundError(f"{target} isn't a folder or a file")
    from . import versions

    cur = versions.current(target)
    name = (cur or {}).get("fcpxml")
    if name and (target / name).is_file():
        return target / name, target
    found = sorted(target.glob("*.fcpxml"), key=lambda p: p.stat().st_mtime)
    if not found:
        raise FileNotFoundError(f"no .fcpxml in {target}: is it a folder `roughcut build` wrote?")
    return found[-1], target


def check(target: Path, *, progress=None) -> Report:
    fcpxml, folder = find_cut(target)
    project, frame, edges, ranges = spine_edges(fcpxml)
    report = Report(str(fcpxml), project, frame)
    words = transcripts(folder)
    by_file: dict[str, list[Edge]] = {}
    for e in edges:
        by_file.setdefault(e.asset, []).append(e)
    files = list(dict.fromkeys(p for p, *_ in ranges))
    levels_by_file: dict[str, list[float]] = {}
    floors: dict[str, float] = {}
    for path in files:
        if progress:
            progress(f"Listening to {Path(path).name} ...")
        try:
            levels = bounded(listen, path, seconds=LISTEN_SECONDS, what=Path(path).name)
        except Stalled:
            report.problems.append(f"{Path(path).name}: didn't answer in {LISTEN_SECONDS:.0f} s (a network drive that stopped?); not checked")
            continue
        except Exception as e:  # noqa: BLE001 - one unreadable file must not stop the rest
            report.problems.append(f"{Path(path).name}: can't be read ({str(e).splitlines()[0][:80] if str(e) else type(e).__name__}); not checked")
            continue
        if not levels:
            report.problems.append(f"{Path(path).name}: no audio could be read; not checked")
            continue
        levels_by_file[path] = levels
        report.clips += 1
        report.seconds += len(levels) * WINDOW
        w = words.get(_norm(path), [])
        fillers = [(x.start, x.end) for x in w if x.is_filler]
        found, small, floor = judge(by_file.get(path, []), levels, fillers)
        if floor is None:
            report.unjudged.append(Path(path).name)
            continue
        floors[path] = floor
        report.edits += len(by_file.get(path, []))
        report.small += small
        for edge, gone, kept in found:
            report.flagged.append(Flag(edge, gone, kept, words_around(w, edge.source, edge.kind) if w else ""))
        # the um/uh still in the kept dialogue
        for start, end in [(x.start, x.end) for x in w if x.is_filler]:
            for p, a, b, offset in ranges:
                if p == path and a <= start and end <= b:
                    report.fillers.append((next(x.text.strip() for x in w if x.start == start), offset + Fraction(start - a).limit_denominator(1000)))
    report.flagged.sort(key=lambda f: f.edge.timeline)
    report.fillers.sort(key=lambda f: f[1])
    report.longest_pause, report.long_pauses = pauses(ranges, levels_by_file, floors)
    return report


# ------------------------------------------------------------------ saying it

def timecode(t: Fraction, frame: Fraction) -> str:
    """HH:MM:SS:FF for a time in the cut (a sequence starts at 00:00:00:00 here)."""
    fps = tc.nominal_fps(frame)
    frames = int(t / frame)
    ff = frames % fps
    s = frames // fps
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{ff:02d}"


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def render(report: Report, limit: int = 15) -> str:
    lines = [f"roughcut check: {report.project or Path(report.fcpxml).name}"]
    if not report.edits and not report.unjudged:
        lines.append("There are no dialogue edits in this cut to check." if not report.problems else "Nothing could be checked.")
    else:
        judged = report.edits
        total = len(report.flagged)
        lines.append(
            f"Listened to {report.clips} dialogue file{'s' if report.clips != 1 else ''} ({tc.seconds_to_clock(report.seconds)} of audio); "
            f"{judged} dialogue edit{'s' if judged != 1 else ''} judged."
        )
        if judged:
            share = f" ({100 * total / judged:.1f}%)" if judged else ""
            lines.append(f"  {total} go through sound{share}" + (f"; {report.small} more only just (under {_ms(MIN_RUN)})" if report.small else "") + ".")
        if report.longest_pause is not None:
            length, at = report.longest_pause
            lines.append(f"  The longest pause left in the dialogue is {length:.2f} s (at {timecode(at, report.frame_duration)}); {report.long_pauses} over a second.")
        if report.fillers:
            shown = ", ".join(f"{w} at {timecode(t, report.frame_duration)}" for w, t in report.fillers[:5])
            lines.append(f"  {len(report.fillers)} um/uh still in the cut: {shown}{' ...' if len(report.fillers) > 5 else ''}.")
        else:
            lines.append("  No um/uh left in the dialogue." if judged else "")
    for name in report.unjudged:
        lines.append(f"  {name}: the background is within {MIN_SEPARATION:.0f} dB of the speech, so a cut in sound can't be told from one in noise; not judged.")
    for p in report.problems:
        lines.append(f"  {p}")
    if report.flagged:
        lines.append("")
        lines.append("Edits to look at (go to the timecode in Final Cut, then listen across the edit):")
        for f in report.flagged[:limit]:
            e = f.edge
            amount = f"{_ms(f.cut_off)}{'+' if f.cut_off >= LOOK - 1e-6 else ''}"
            what = f"cuts off the first {amount} of a sound" if e.kind == "starts" else f"cuts off the last {amount} of a sound"
            lines.append(f"  {timecode(e.timeline, report.frame_duration)}  {e.clip} ({tc.seconds_to_clock(e.source, millis=True)} in the file): {what}" + (f"\n      {f.words}" if f.words else ""))
        if len(report.flagged) > limit:
            lines.append(f"  ... and {len(report.flagged) - limit} more (--limit {len(report.flagged)} shows all).")
    return "\n".join(line for line in lines if line is not None)


def as_json(report: Report) -> dict:
    return {
        "fcpxml": report.fcpxml,
        "project": report.project,
        "files": report.clips,
        "audio_seconds": round(report.seconds, 1),
        "edits": report.edits,
        "through_sound": [
            {
                "timecode": timecode(f.edge.timeline, report.frame_duration),
                "clip": f.edge.clip,
                "file": f.edge.asset,
                "source_seconds": round(f.edge.source, 3),
                "kind": f.edge.kind,
                "sound_cut_off_ms": round(f.cut_off * 1000),
                "sound_kept_ms": round(f.kept * 1000),
                "words": f.words,
            }
            for f in report.flagged
        ],
        "slight": report.small,
        "not_judged": report.unjudged,
        "longest_pause": None if report.longest_pause is None else {"seconds": round(report.longest_pause[0], 2), "timecode": timecode(report.longest_pause[1], report.frame_duration)},
        "pauses_over_a_second": report.long_pauses,
        "fillers_left": [{"word": w, "timecode": timecode(t, report.frame_duration)} for w, t in report.fillers],
        "problems": report.problems,
    }


def command(path: str, *, limit: int, as_json_: bool) -> int:
    try:
        report = check(Path(path), progress=None if as_json_ else (lambda line: print(line, file=sys.stderr, flush=True)))
    except (FileNotFoundError, ValueError, ET.ParseError) as e:
        print(f"roughcut: {e}", file=sys.stderr)
        return 1
    print(json.dumps(as_json(report)) if as_json_ else render(report, limit))
    return 0

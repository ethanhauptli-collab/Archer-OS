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
SPEECH_OVER_FLOOR = 12.0  # dB over the clip's quiet floor that counts as sound (room noise stays under it)
SPEECH_WINDOW = 30.0  # dB under the loud end of the kept dialogue that still counts as sound: in a very quiet room a breath is over the floor but under this
FLOOR_LIMIT = -75.0  # a recording quieter than this has nothing in its pauses: judge against this floor instead
MIN_SEPARATION = 12.0  # dB between a clip's quiet floor and its loudest twentieth below which sound and background can't be told apart
MIN_RUN = 0.03  # seconds of sound cut off below which an edit isn't worth a look
QUIET_OVER_FLOOR = 6.0  # dB over the floor that still counts as a pause, for the pauses left in the dialogue
CLICK_UNDER_SPEECH = 12.0  # dB under the loud end that a click (a lip smack, a knock: 50 ms at most) must stay for a pause to go on through it, as in the cut itself
TIGHT_GAP = 0.10  # seconds of quiet between two sounds with an edit in it, under which the edit may be inside a word (a stop consonant leaves one)
LISTEN_SECONDS = 300.0  # how long to listen to one file's audio before giving up on it (about fifty times real time: a four-hour clip fits)
STALLED_SECONDS = 60.0  # the same once a drive has stopped answering: the next files are most likely on it too
SMALL_SECONDS = 60.0  # the folder, the run's analysis.json
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
    gap: float = 0.0  # for a tight gap: seconds of quiet between the two sounds


@dataclass
class Report:
    fcpxml: str
    project: str
    frame_duration: Fraction
    edits: int = 0  # dialogue edges judged
    clips: int = 0  # dialogue clips listened to
    seconds: float = 0.0  # of dialogue audio listened to
    flagged: list[Flag] = field(default_factory=list)
    beside_fillers: list[Flag] = field(default_factory=list)  # edits with sound against them where an um/uh was cut out: the word beside it may be clipped
    tight_gaps: list[Flag] = field(default_factory=list)  # edits in under TIGHT_GAP of quiet between two sounds: maybe inside a word
    small: int = 0  # edits with sound on both sides for less than MIN_RUN
    unjudged: list[str] = field(default_factory=list)  # files too noisy to tell sound from background
    longest_pause: tuple[float, Fraction] | None = None
    long_pauses: int = 0  # kept pauses over a second
    long_runs: list[tuple[float, Fraction, str, float]] = field(default_factory=list)  # those pauses, longest first: (seconds, where in the cut, file, seconds into it)
    fillers: list[tuple[str, Fraction]] = field(default_factory=list)  # (the word, where) still in the cut
    with_words: int = 0  # dialogue files the run's transcript was found for
    no_words: list[str] = field(default_factory=list)  # and those it wasn't
    problems: list[str] = field(default_factory=list)


LOOK = 0.5  # seconds either side of an edit that are looked at for how long the sound goes on
SHOWN_INLINE = 8  # timecodes named in the line about edits beside an um/uh, and in tight gaps


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


def _reach(levels: list[float], i: int, step: int, threshold: float, limit: int, gap: int) -> tuple[int, int]:
    """(quiet readings from index i before a sound begins, how long that sound goes on: at most `limit`).

    The sound has to begin within `gap` readings; if it doesn't, (gap + 1, 0). With gap 0 the sound has to be right at index i.
    """
    quiet = 0
    while 0 <= i < len(levels) and levels[i] <= threshold:
        if quiet >= gap:
            return gap + 1, 0
        quiet += 1
        i += step
    return quiet, _run(levels, i, step, threshold, limit)


def _loud_end(levels: list[float], kept: list[tuple[float, float]] | None) -> float:
    """The level (95th percentile) of the loud end of the dialogue that is kept: the whole file's when that is too little to go by.

    A file can hold a minute of nat sound and one planned line: the line is a twentieth of it at most, so the whole file's loud end is
    the nat sound.
    """
    if kept:
        picked: list[float] = []
        for a, b in kept:
            picked.extend(levels[max(0, int(a / WINDOW)) : max(0, int(b / WINDOW))])
        if len(picked) >= 100:
            return _percentile(picked, 0.95)
    return _percentile(levels, 0.95)


@dataclass
class Judged:
    """What judge_detail found in one file."""

    found: list[tuple[Edge, float, float]] = field(default_factory=list)  # [(edit, seconds of sound cut off, seconds kept)]
    small: int = 0  # edits with sound on both sides for under MIN_RUN
    floor: float | None = None  # the quiet floor used (None: sound and background can't be told apart here, nothing is judged)
    loud: float | None = None  # and the loud end of the kept dialogue
    beside_fillers: list[tuple[Edge, float, float]] = field(default_factory=list)  # sound on both sides, where an um/uh was cut out
    tight_gaps: list[tuple[Edge, float, float, float]] = field(default_factory=list)  # (edit, sound cut off, kept, seconds of quiet between)


def judge_detail(edges: list[Edge], levels: list[float], fillers: list[tuple[float, float]] = (), kept: list[tuple[float, float]] | None = None) -> Judged:
    """The edits of one file that go through sound, and the two kinds that are worth a listen without being a fault.

    `kept` is the file's kept dialogue as (start, end) seconds: the loud end of the speech is read from it. Sound is what is at least
    SPEECH_OVER_FLOOR over the quiet floor and within SPEECH_WINDOW of the loud end, so a breath in a very quiet room isn't one.
    """
    if len(levels) < 100:
        return Judged()
    floor = max(_percentile(levels, 0.10), FLOOR_LIMIT)
    loud = _loud_end(levels, kept)
    if loud - floor < MIN_SEPARATION:
        return Judged()
    threshold = max(floor + SPEECH_OVER_FLOOR, loud - SPEECH_WINDOW)
    look = int(LOOK / WINDOW)
    side = int(round(TIGHT_GAP / WINDOW))
    out = Judged(floor=floor, loud=loud)
    for e in edges:
        i = int(round(e.source / WINDOW))
        if not 1 <= i < len(levels) - 1:
            continue
        # kept: i onwards for a start, before i for an end; cut away: the other side
        k_from, k_step, g_from, g_step = (i, +1, i - 1, -1) if e.kind == "starts" else (i - 1, -1, i, +1)
        kept_run, gone_run = _run(levels, k_from, k_step, threshold, look), _run(levels, g_from, g_step, threshold, look)
        if kept_run and gone_run:
            away = (e.source - gone_run * WINDOW, e.source) if e.kind == "starts" else (e.source, e.source + gone_run * WINDOW)
            if sum(max(0.0, min(b, away[1]) - max(a, away[0])) for a, b in fillers) >= 0.5 * (away[1] - away[0]):
                if gone_run * WINDOW >= MIN_RUN:  # what is cut away is mostly a spoken um or uh, which was meant to go
                    out.beside_fillers.append((e, gone_run * WINDOW, kept_run * WINDOW))  # ... but where the word beside it ends can't be told
                continue
            if gone_run * WINDOW < MIN_RUN:
                out.small += 1
                continue
            out.found.append((e, gone_run * WINDOW, kept_run * WINDOW))
            continue
        if kept_run or gone_run:
            continue  # the sound stops (or starts) at the edit: a clean one
        # Quiet at the edit on both sides. In a gap of a few readings between two sounds the edit may be inside a word (a stop
        # consonant is silent for 50 ms or more): not a fault, but worth hearing.
        (kq, kept_run), (gq, gone_run) = (_reach(levels, k_from, k_step, threshold, look, side), _reach(levels, g_from, g_step, threshold, look, side))
        if kept_run * WINDOW >= MIN_RUN and gone_run * WINDOW >= MIN_RUN and (kq + gq) * WINDOW <= TIGHT_GAP + 1e-9:
            out.tight_gaps.append((e, gone_run * WINDOW, kept_run * WINDOW, (kq + gq) * WINDOW))
    return out


def judge(edges: list[Edge], levels: list[float], fillers: list[tuple[float, float]] = (), kept: list[tuple[float, float]] | None = None) -> tuple[list[tuple[Edge, float, float]], int, float | None]:
    """The edits of one file that go through sound: [(edit, seconds of sound cut off, seconds kept)], how many were a little sound
    only, and the quiet floor used (None: sound and background can't be told apart here, nothing is judged)."""
    d = judge_detail(edges, levels, fillers, kept)
    return d.found, d.small, d.floor


def _quiet_mask(levels: list[float], i: int, end: int, floor: float, loud: float | None = None) -> list[bool]:
    """For each reading in [i, end): is it part of a pause? A pause is at most QUIET_OVER_FLOOR over the floor; a click (a rise of at
    most LEVEL_CLICK_MAX that stays under floor + LEVEL_CLICK_OVER, or CLICK_UNDER_SPEECH under the speech) between two quiet
    stretches doesn't end it: the cut treats a pause that way (ffmpeg_ops.level_silences; pauses.py for a quiet breath or click)."""
    quiet, click = floor + QUIET_OVER_FLOOR, floor + ffmpeg_ops.LEVEL_CLICK_OVER
    if loud is not None:
        click = max(click, loud - CLICK_UNDER_SPEECH)
    longest_click = int(round(ffmpeg_ops.LEVEL_CLICK_MAX / WINDOW))
    low = [levels[k] <= quiet for k in range(i, end)]
    k = 0
    while k < len(low):
        if low[k]:
            k += 1
            continue
        j = k
        while j < len(low) and not low[j]:
            j += 1
        if 0 < k and j < len(low) and j - k <= longest_click and all(levels[i + m] <= click for m in range(k, j)):
            for m in range(k, j):
                low[m] = True
        k = j
    return low


def quiet_runs(
    ranges: list[tuple[str, float, float, Fraction]],
    levels_by_file: dict[str, list[float]],
    floors: dict[str, float],
    at_least: float = 0.0,
    louds: dict[str, float] | None = None,
) -> list[tuple[float, Fraction, str, float]]:
    """Every quiet stretch inside the kept dialogue longer than `at_least`: (seconds, where in the cut, the file, seconds into it).

    `louds` is each file's loud end (judge_detail's), which sets how loud a click may be and still sit inside a pause.
    """
    runs: list[tuple[float, Fraction, str, float]] = []
    for path, a, b, offset in ranges:
        levels, floor = levels_by_file.get(path), floors.get(path)
        if not levels or floor is None:
            continue
        i, end = max(0, int(round(a / WINDOW))), min(len(levels), int(round(b / WINDOW)))
        low = _quiet_mask(levels, i, end, floor, (louds or {}).get(path))
        run_from = None
        for m in range(len(low) + 1):
            quiet = m < len(low) and low[m]
            if quiet and run_from is None:
                run_from = m
            elif not quiet and run_from is not None:
                length = (m - run_from) * WINDOW
                if length > at_least:
                    runs.append((length, offset + Fraction((i + run_from) * WINDOW - a).limit_denominator(1000), path, (i + run_from) * WINDOW))
                run_from = None
    return runs


def pauses(ranges: list[tuple[str, float, float, Fraction]], levels_by_file: dict[str, list[float]], floors: dict[str, float], louds: dict[str, float] | None = None) -> tuple[tuple[float, Fraction] | None, int]:
    """The longest quiet stretch inside the kept dialogue (seconds, where in the cut) and how many are over a second."""
    runs = quiet_runs(ranges, levels_by_file, floors, louds=louds)
    longest = max(runs, key=lambda r: r[0], default=None)
    return (None if longest is None else (longest[0], longest[1])), sum(1 for r in runs if r[0] > 1.0)


# ------------------------------------------------------------------ the words

def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", str(path))


class Transcripts(dict):
    """{original file: its words} from a run's analysis.json. A file is found by its path, else by its name when only one file has it
    (footage reached through a link or a mount that spells the path another way is the same file)."""

    def __init__(self) -> None:
        super().__init__()
        self._names: dict[str, list[list[Word]]] = {}

    def add(self, path: str, words: list[Word]) -> None:
        self[_norm(path)] = words
        self._names.setdefault(_norm(Path(path).name), []).append(words)

    def for_file(self, path: str) -> list[Word]:
        exact = self.get(_norm(path))
        if exact is not None:
            return exact
        same_name = self._names.get(_norm(Path(path).name), [])
        return same_name[0] if len(same_name) == 1 else []


def find_analysis(folder: Path) -> Path | None:
    """The run's analysis.json: in the folder, or, for a cut kept in history/ (history/v1 <date>/), in the run's folder above it."""
    candidates = [folder]
    if folder.parent.name == "history":
        candidates.append(folder.parent.parent)
    elif folder.name == "history":
        candidates.append(folder.parent)
    for d in candidates:
        if (d / "analysis.json").is_file():
            return d / "analysis.json"
    return None


def transcripts(folder: Path) -> Transcripts:
    """The run's words per original file (empty when analysis.json is gone or unreadable)."""
    out = Transcripts()
    found = find_analysis(folder)
    if found is None:
        return out
    try:
        data = json.loads(read_text_lenient(found))
    except (OSError, ValueError):
        return out
    for clip in data.get("clips", []):
        path = (clip.get("media") or {}).get("path")
        if not path:
            continue
        words = [Word(w["start"], w["end"], w["text"], w.get("prob", 1.0)) for s in clip.get("segments", []) for w in s.get("words", [])]
        if words:
            out.add(path, words)
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


def _drive(path: str) -> str | None:
    """The drive or share a path is on (/Volumes/Name on a Mac, /mnt/name and /media/user/name elsewhere); None for the startup disk."""
    parts = Path(path).parts
    if len(parts) >= 3 and parts[1] in ("Volumes", "mnt"):
        return str(Path(*parts[:3]))
    if len(parts) >= 4 and parts[1] == "media":
        return str(Path(*parts[:4]))
    return None


def check(target: Path, *, progress=None) -> Report:
    try:
        fcpxml, folder = bounded(find_cut, target, seconds=SMALL_SECONDS, what=str(target))
    except Stalled as e:
        raise ValueError(f"{e}: is the drive still there?") from e
    try:
        project, frame, edges, ranges = bounded(spine_edges, fcpxml, seconds=SMALL_SECONDS, what=fcpxml.name)
    except Stalled as e:
        raise ValueError(f"{e}: is the drive still there?") from e
    report = Report(str(fcpxml), project, frame)
    try:
        words = bounded(transcripts, folder, seconds=SMALL_SECONDS, what="analysis.json")
    except Stalled:
        words = Transcripts()
        report.problems.append("analysis.json didn't answer, so the words around the edits aren't shown")
    by_file: dict[str, list[Edge]] = {}
    for e in edges:
        by_file.setdefault(e.asset, []).append(e)
    kept_by_file: dict[str, list[tuple[float, float]]] = {}
    for path, a, b, _ in ranges:
        kept_by_file.setdefault(path, []).append((a, b))
    files = list(kept_by_file)
    # The um/uh still in the kept dialogue come from the transcript alone: they're listed whatever the audio turns out to be.
    texts: dict[str, list[Word]] = {}
    for path in files:
        texts[path] = words.for_file(path)
        if not texts[path]:
            report.no_words.append(Path(path).name)
            continue
        report.with_words += 1
        for x in texts[path]:
            if x.is_filler:
                for p, a, b, offset in ranges:
                    if p == path and a <= x.start and x.end <= b:
                        report.fillers.append((x.text.strip(), offset + Fraction(x.start - a).limit_denominator(1000)))
    levels_by_file: dict[str, list[float]] = {}
    floors: dict[str, float] = {}
    louds: dict[str, float] = {}
    stalled_drives: set[str] = set()
    stalled = False
    for path in files:
        name, drive = Path(path).name, _drive(path)
        if drive in stalled_drives:
            report.problems.append(f"{name}: skipped, the drive it is on stopped answering for an earlier file; not checked")
            continue
        if progress:
            progress(f"Listening to {name} ...")
        seconds = STALLED_SECONDS if stalled else LISTEN_SECONDS
        try:
            levels = bounded(listen, path, seconds=seconds, what=name)
        except Stalled:
            stalled = True
            if drive:
                stalled_drives.add(drive)
            report.problems.append(f"{name}: didn't answer in {seconds:.0f} s (a network drive that stopped?); not checked")
            continue
        except Exception as e:  # noqa: BLE001 - one unreadable file must not stop the rest
            report.problems.append(f"{name}: can't be read ({str(e).splitlines()[0][:80] if str(e) else type(e).__name__}); not checked")
            continue
        if not levels:
            report.problems.append(f"{name}: no audio could be read; not checked")
            continue
        levels_by_file[path] = levels
        report.clips += 1
        report.seconds += len(levels) * WINDOW
        w = texts[path]
        judged = judge_detail(by_file.get(path, []), levels, [(x.start, x.end) for x in w if x.is_filler], kept_by_file.get(path))
        if judged.floor is None:
            report.unjudged.append(name)
            continue
        floors[path] = judged.floor
        louds[path] = judged.loud
        report.edits += len(by_file.get(path, []))
        report.small += judged.small
        for edge, gone, kept in judged.found:
            report.flagged.append(Flag(edge, gone, kept, words_around(w, edge.source, edge.kind) if w else ""))
        for edge, gone, kept in judged.beside_fillers:
            report.beside_fillers.append(Flag(edge, gone, kept, words_around(w, edge.source, edge.kind) if w else ""))
        for edge, gone, kept, gap in judged.tight_gaps:
            report.tight_gaps.append(Flag(edge, gone, kept, words_around(w, edge.source, edge.kind) if w else "", gap=gap))
    for group in (report.flagged, report.beside_fillers, report.tight_gaps):
        group.sort(key=lambda f: f.edge.timeline)
    report.fillers.sort(key=lambda f: f[1])
    report.longest_pause, report.long_pauses = pauses(ranges, levels_by_file, floors, louds)
    report.long_runs = sorted(quiet_runs(ranges, levels_by_file, floors, at_least=1.0, louds=louds), key=lambda r: -r[0])
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
        if report.beside_fillers:
            shown = ", ".join(timecode(f.edge.timeline, report.frame_duration) for f in report.beside_fillers[:SHOWN_INLINE])
            lines.append(
                f"  {len(report.beside_fillers)} more are beside an um/uh that was cut out: the sound runs right up to the cut, and where the um began can't be told "
                f"from where the word beside it ended, so listen that the word is whole at {shown}{' ...' if len(report.beside_fillers) > SHOWN_INLINE else ''}."
            )
        if report.tight_gaps:
            shown = ", ".join(f"{timecode(f.edge.timeline, report.frame_duration)} ({_ms(f.gap)})" for f in report.tight_gaps[:SHOWN_INLINE])
            lines.append(
                f"  {len(report.tight_gaps)} more sit in a gap of under {_ms(TIGHT_GAP)} between two sounds, which is also how a stop consonant inside a word looks: "
                f"{shown}{' ...' if len(report.tight_gaps) > SHOWN_INLINE else ''}."
            )
        if report.longest_pause is not None:
            length, at = report.longest_pause
            lines.append(f"  The longest pause left in the dialogue is {length:.2f} s (at {timecode(at, report.frame_duration)}); {report.long_pauses} over a second.")
            if report.long_pauses > 1 and report.long_runs:
                shown = ", ".join(f"{timecode(t, report.frame_duration)} ({s:.1f} s)" for s, t, _, _ in report.long_runs[:5])
                lines.append(f"  Over a second: {shown}{' ...' if len(report.long_runs) > 5 else ''}.")
        if report.fillers:
            shown = ", ".join(f"{w} at {timecode(t, report.frame_duration)}" for w, t in report.fillers[:5])
            lines.append(f"  {len(report.fillers)} um/uh still in the cut: {shown}{' ...' if len(report.fillers) > 5 else ''}.")
        elif report.with_words:
            lines.append("  No um/uh left in the dialogue." + (f" (No transcript for {', '.join(report.no_words[:3])}{' ...' if len(report.no_words) > 3 else ''}.)" if report.no_words else ""))
        elif report.no_words:
            lines.append("  No transcript was found for these files (the run's analysis.json), so um/uh and the words around the edits aren't shown.")
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


def _flag_json(f: Flag, frame: Fraction) -> dict:
    return {
        "timecode": timecode(f.edge.timeline, frame),
        "clip": f.edge.clip,
        "file": f.edge.asset,
        "source_seconds": round(f.edge.source, 3),
        "kind": f.edge.kind,
        "sound_cut_off_ms": round(f.cut_off * 1000),
        "sound_kept_ms": round(f.kept * 1000),
        "words": f.words,
    }


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
        "beside_fillers": [_flag_json(f, report.frame_duration) for f in report.beside_fillers],
        "tight_gaps": [{**_flag_json(f, report.frame_duration), "gap_ms": round(f.gap * 1000)} for f in report.tight_gaps],
        "files_without_transcript": report.no_words,
        "slight": report.small,
        "not_judged": report.unjudged,
        "longest_pause": None if report.longest_pause is None else {"seconds": round(report.longest_pause[0], 2), "timecode": timecode(report.longest_pause[1], report.frame_duration)},
        "pauses_over_a_second": report.long_pauses,
        "long_pauses": [{"timecode": timecode(t, report.frame_duration), "seconds": round(s, 2), "file": p, "source_seconds": round(src, 3)} for s, t, p, src in report.long_runs],
        "fillers_left": [{"word": w, "timecode": timecode(t, report.frame_duration)} for w, t in report.fillers],
        "problems": report.problems,
    }


def command(path: str, *, limit: int, as_json_: bool) -> int:
    try:
        report = check(Path(path), progress=None if as_json_ else (lambda line: print(line, file=sys.stderr, flush=True)))
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"roughcut: {e}", file=sys.stderr)
        return 1
    print(json.dumps(as_json(report)) if as_json_ else render(report, limit))
    return 0

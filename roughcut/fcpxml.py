"""Stage 4: write the timeline(s) as FCPXML for Final Cut Pro.

The file references original media in place (nothing is rendered or copied),
so every cut keeps its handles and can be extended in Final Cut.

Coordinate rules (the part that's easy to get wrong):
- Spine clips: `offset` is timeline time; `start` is source time, which
  includes the asset's own timecode start.
- Anything attached to a spine clip (connected B-roll, music, markers) is
  positioned in that clip's *source* time: parent.start + time into parent.
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from . import timecode as tc
from .analyze import ROLE_LABELS, Analysis, Clip
from .media import MediaInfo
from .timeline import SpineClip, Timeline

FCPXML_VERSION = "1.10"

# Final Cut gives stills a one-hour origin; every real export uses start="3600s".
STILL_ORIGIN = Fraction(3600)


def origin(m: MediaInfo) -> Fraction:
    """Source-time origin of a clip: embedded timecode, or 3600s for stills."""
    return STILL_ORIGIN if m.kind == "image" else m.start


class _Resources:
    def __init__(self, analysis: Analysis):
        self.analysis = analysis
        self.el = ET.Element("resources")
        self._next = 1
        self.formats: dict[tuple, str] = {}
        self.assets: dict[str, str] = {}  # clip id -> resource id

    def _id(self) -> str:
        rid = f"r{self._next}"
        self._next += 1
        return rid

    def video_format(self, width: int, height: int, fd: Fraction) -> str:
        key = ("video", width, height, fd)
        if key not in self.formats:
            rid = self._id()
            attrs = {"id": rid}
            name = tc.fcp_format_name(width, height, fd)
            if name:
                attrs["name"] = name
            attrs.update(frameDuration=tc.fcpx_time(fd), width=str(width), height=str(height))
            ET.SubElement(self.el, "format", attrs)
            self.formats[key] = rid
        return self.formats[key]

    def still_format(self, width: int = 0, height: int = 0) -> str:
        """FFVideoFormatRateUndefined: used for stills (with size) and audio-only clips (without)."""
        key = ("still", width, height)
        if key not in self.formats:
            rid = self._id()
            attrs = {"id": rid, "name": "FFVideoFormatRateUndefined"}
            if width and height:
                attrs.update(width=str(width), height=str(height))
            ET.SubElement(self.el, "format", attrs)
            self.formats[key] = rid
        return self.formats[key]

    def asset(self, clip: Clip) -> str:
        if clip.id in self.assets:
            return self.assets[clip.id]
        m = clip.media
        fmt = None
        if m.kind == "image":
            fmt = self.still_format(m.width, m.height)
        elif m.kind == "video":
            fmt = self.video_format(m.width, m.height, m.frame_duration)
        rid = self._id()
        attrs = {"id": rid, "name": m.stem, "start": tc.fcpx_time(m.start, _timebase(m)), "duration": tc.fcpx_time(m.duration, _timebase(m))}
        if fmt:
            attrs.update(hasVideo="1", format=fmt, videoSources="1")
        if m.has_audio:
            attrs.update(hasAudio="1", audioSources="1", audioChannels=str(m.audio_channels or 2), audioRate=str(m.audio_rate or 48000))
        asset = ET.SubElement(self.el, "asset", attrs)
        ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": Path(m.path).resolve().as_uri()})
        self.assets[clip.id] = rid
        return rid


def _timebase(m: MediaInfo) -> int | None:
    if m.kind == "video" and m.frame_duration:
        return m.frame_duration.denominator
    if m.kind == "audio" and m.audio_rate:
        return m.audio_rate
    return None


AUTO_FILL = "auto-fill"  # the note timeline._fill_picture gives the shots it chose


def _roles(clip: Clip, use: str, note: str = "") -> tuple[str | None, str | None]:
    """(video role, audio role) for one use of a clip, so Timeline Index > Roles
    can mute the music bed, solo the planned B-roll or hide the auto-fill.

    `use` is "speech" (a story line in the main storyline, whatever the clip
    was classified as), "spine", "broll" (connected cutaway), "music",
    "graphic" or "browser". None leaves Final Cut's default.
    """
    m = clip.media
    if use == "graphic" or clip.role == "graphic":
        return "titles.Graphics", None
    if use == "music":
        return None, "music.Music Bed"
    if use == "speech":
        return None, "dialogue.Narration" if clip.role == "voiceover" else "dialogue"
    if use == "broll":
        video = "B-Roll.Auto-fill" if note == AUTO_FILL else "B-Roll.Planned"
        return video, "effects.Nat Sound" if m.has_audio else None
    # Spine and browser: by what the clip is.
    if clip.role in ("broll", "still"):
        return "B-Roll", "effects.Nat Sound" if m.has_audio else None
    if clip.role == "voiceover":
        return None, "dialogue.Narration"
    if clip.role == "aroll":
        return None, "dialogue"
    if clip.role == "music":
        return None, "music"
    return None, None


def _clip_element(
    parent: ET.Element,
    clip: Clip,
    res: "_Resources",
    *,
    offset: str | None,
    start: Fraction,
    duration: str,
    lane: int | None,
    timebase: int,
    use: str = "spine",
    note: str = "",
) -> ET.Element:
    """A story element referencing a clip's asset: <video> for stills, <asset-clip> otherwise."""
    m = clip.media
    tag = "video" if m.kind == "image" else "asset-clip"
    attrs = {"ref": res.asset(clip)}
    if lane is not None:
        attrs["lane"] = str(lane)
    if offset is not None:
        attrs["offset"] = offset
    attrs.update(name=m.stem, start=tc.fcpx_time(start, timebase), duration=duration)
    if m.kind == "audio":
        # Final Cut writes audio-only clips with a rate-undefined format.
        attrs["format"] = res.still_format()
    if m.kind == "video":
        attrs["tcFormat"] = "DF" if m.timecode and ";" in m.timecode else "NDF"
    video_role, audio_role = _roles(clip, use, note)
    if tag == "video":
        if video_role:
            attrs["role"] = video_role  # <video> has one role attribute
    else:
        if audio_role and m.has_audio:
            attrs["audioRole"] = audio_role
        if video_role and m.has_video:
            attrs["videoRole"] = video_role
    el = ET.SubElement(parent, tag, attrs)
    if note and note != AUTO_FILL:
        # The DTD puts <note> first in an asset-clip, and before everything but params in a <video>.
        ET.SubElement(el, "note").text = note
    return el


FADE_SHAPE = "easeIn"  # what Final Cut writes for its default (+3 dB) fade handles, in and out


def _volume(
    el: ET.Element,
    clip: Clip,
    volume_db: float | None,
    fade_in: Fraction = Fraction(0),
    fade_out: Fraction = Fraction(0),
    keyframes: list[tuple[Fraction, float]] | None = None,
    timebase: int | None = None,
) -> None:
    """The clip's <adjust-volume>, written the way Final Cut writes it:
    `amount` for a fixed level; fade handles as <fadeIn>/<fadeOut> inside
    <param name="amount">; volume automation as a <keyframeAnimation> there
    (times in the clip's own source time, like its `start`), which replaces
    `amount`.

    The DTD puts it after <note> and the video adjustments and before any
    anchored clip or marker, so call this before attaching children. Stills
    (<video>) and clips without audio get none.
    """
    if el.tag != "asset-clip" or not clip.media.has_audio:
        return
    if volume_db is None and not fade_in and not fade_out and not keyframes:
        return
    attrs = {"amount": f"{volume_db:g}dB"} if volume_db is not None and not keyframes else {}
    adjust = ET.SubElement(el, "adjust-volume", attrs)
    if fade_in or fade_out or keyframes:
        param = ET.SubElement(adjust, "param", {"name": "amount"})
        if fade_in:
            ET.SubElement(param, "fadeIn", {"type": FADE_SHAPE, "duration": tc.fcpx_time(fade_in, 48000)})
        if fade_out:
            ET.SubElement(param, "fadeOut", {"type": FADE_SHAPE, "duration": tc.fcpx_time(fade_out, 48000)})
        if keyframes:
            animation = ET.SubElement(param, "keyframeAnimation")
            for t, db in keyframes:
                ET.SubElement(animation, "keyframe", {"time": tc.fcpx_time(t, timebase), "value": f"{db:g}dB"})


def _local(tl: Timeline, sc: SpineClip, clip: Clip, frame: int) -> Fraction:
    """Absolute timeline frame -> time inside spine clip `sc`'s source."""
    return origin(clip.media) + sc.src_in + (frame - sc.offset) * tl.frame_duration


def _sequence(tl: Timeline, analysis: Analysis, res: _Resources) -> ET.Element:
    fd = tl.frame_duration
    tb = fd.denominator
    fmt = res.video_format(tl.width, tl.height, fd)
    project = ET.Element("project", {"name": tl.name})
    seq = ET.SubElement(
        project,
        "sequence",
        {
            "format": fmt,
            "duration": tc.fcpx_time(tl.total_frames * fd, tb),
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    spine = ET.SubElement(seq, "spine")

    # Group attachments by the spine clip they hang off.
    attached: dict[int, dict[str, list]] = {i: {"clips": [], "markers": []} for i in range(len(tl.spine))}

    def owner(frame: int) -> int:
        for i, sc in enumerate(tl.spine):
            if sc.offset <= frame < sc.end:
                return i
        return len(tl.spine) - 1

    for cc in tl.connected:
        attached[owner(cc.start)]["clips"].append(cc)
    for mk in tl.markers:
        attached[owner(mk.frame)]["markers"].append(mk)

    for i, sc in enumerate(tl.spine):
        clip = analysis.clip(sc.clip_id)
        el = _clip_element(
            spine,
            clip,
            res,
            offset=tc.fcpx_time(sc.offset * fd, tb),
            start=origin(clip.media) + sc.src_in,
            duration=tc.fcpx_time(sc.frames * fd, tb),
            lane=None,
            timebase=tb,
            use="speech" if sc.seg_ids else "spine",
        )
        _volume(el, clip, sc.volume_db, sc.fade_in, sc.fade_out)
        # Child order follows the FCPXML DTD: volume, anchored clips, then markers.
        for cc in sorted(attached[i]["clips"], key=lambda c: (c.start, c.lane)):
            child_clip = analysis.clip(cc.clip_id)
            child = _clip_element(
                el,
                child_clip,
                res,
                offset=tc.fcpx_time(_local(tl, sc, clip, cc.start), tb),
                start=origin(child_clip.media) + cc.src_in,
                duration=tc.fcpx_time(cc.frames * fd, tb),
                lane=cc.lane,
                timebase=tb,
                use=cc.kind,
                note=cc.note,
            )
            child_start = origin(child_clip.media) + cc.src_in
            keyframes = [(child_start + k * fd, db) for k, db in cc.keyframes]
            _volume(child, child_clip, cc.volume_db, cc.fade_in, cc.fade_out, keyframes, tb)
        for mk in attached[i]["markers"]:
            attrs = {"start": tc.fcpx_time(_local(tl, sc, clip, mk.frame), tb), "duration": tc.fcpx_time(fd, tb), "value": mk.value}
            if mk.kind == "chapter":
                attrs["posterOffset"] = "0s"
                ET.SubElement(el, "chapter-marker", attrs)
            else:
                if mk.kind == "todo":
                    attrs["completed"] = "0"
                if mk.note and mk.note != mk.value:
                    attrs["note"] = mk.note
                ET.SubElement(el, "marker", attrs)
    return project


def _keywords(clip: Clip) -> list[str]:
    words = [ROLE_LABELS.get(clip.role, clip.role)]
    if clip.visual:
        words.extend(t.replace(",", " ").strip() for t in clip.visual.get("tags", [])[:4])
    seen, out = set(), []
    for w in words:
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


ALT_TAKE = "Alt take"  # keyword on each other read of a line in the cut
ALT_READ = "Alt narration read"  # whole-clip keyword on a narration take the stringout doesn't use
DUPLICATE = "Duplicate export"  # whole-clip keyword on the same footage exported again


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def alternate_reads(tl: Timeline | None, analysis: Analysis, plan_alternates: list[dict] | None = None) -> dict[str, str]:
    """Segment ID of another read of a line -> the segment the cut uses for that line.

    From the planner's alternates, plus, without a model, the closest
    sentence in every other take of a narration script the cut reads from.
    """
    from .duplicates import TAKE_SIMILARITY

    used = set(tl.seg_ranges) if tl is not None else set()
    out: dict[str, str] = {}
    for alt in plan_alternates or []:
        chosen = str(alt.get("chosen", ""))
        for other in alt.get("others") or []:
            if other != chosen and other not in used and analysis.segment(other) is not None:
                out.setdefault(other, chosen)

    def takes(clip: Clip) -> list[Clip]:
        root = clip.take_of or clip.id
        return [c for c in analysis.clips if c.id != clip.id and not c.hidden and (c.id == root or c.take_of == root)]

    for seg_id in sorted(used):
        seg = analysis.segment(seg_id)
        clip = analysis.clip(seg.clip_id) if seg is not None else None
        if clip is None or not seg.text:
            continue
        words = _words(seg.text)
        for other in takes(clip):
            best, best_ratio = None, TAKE_SIMILARITY
            for cand in other.segments:
                if cand.id in used or cand.id in out:
                    continue
                matcher = difflib.SequenceMatcher(None, words, _words(cand.text), autojunk=False)
                if matcher.quick_ratio() >= best_ratio and (r := matcher.ratio()) >= best_ratio:
                    best, best_ratio = cand, r
            if best is not None:
                out[best.id] = seg_id
    return out


def _range_on_clip(m: MediaInfo, a: float, b: float) -> tuple[Fraction, Fraction] | None:
    """(start, duration) of clip-local seconds a..b in the clip's own time, on its own grid."""
    grid = m.frame_duration if m.kind == "video" and m.frame_duration else Fraction(1, m.audio_rate or 48000)
    lo = tc.floor_frames(Fraction(a).limit_denominator(1000000), grid) * grid
    hi = min(m.duration, tc.ceil_frames(Fraction(b).limit_denominator(1000000), grid) * grid)
    lo = max(Fraction(0), lo)
    if hi <= lo:
        return None
    return m.start + lo, hi - lo


def _browser_clip(event: ET.Element, clip: Clip, res: _Resources, alternates: dict[str, str] | None = None, cut_tl: Timeline | None = None) -> None:
    """An event (browser) clip carrying keywords, so footage arrives pre-sorted.

    Other reads of lines in the cut get an "Alt take" keyword range naming the
    line they could replace, so swapping a read is select-and-replace.
    """
    m = clip.media
    words = _keywords(clip)
    notes = []
    analysis = res.analysis
    if clip.duplicate_of and analysis.clip(clip.duplicate_of):
        keep = analysis.clip(clip.duplicate_of)
        words.append(DUPLICATE)
        notes.append(f"Same footage as {keep.media.name} ({keep.id}); roughcut cuts from that one")
    if clip.take_of and analysis.clip(clip.take_of):
        keep = analysis.clip(clip.take_of)
        words.append(ALT_READ)
        notes.append(f"Another read of the narration in {keep.media.name} ({keep.id}), {clip.same_words:.0%} the same words")
    keywords = ", ".join(words)
    if m.kind == "image":
        # Events can't hold a bare <video>; FCP wraps browser stills in a <clip>.
        start, duration = STILL_ORIGIN, Fraction(5)
        el = ET.SubElement(event, "clip", {"name": m.stem, "start": tc.fcpx_time(start), "duration": tc.fcpx_time(duration)})
        video = {"ref": res.asset(clip), "duration": "0s"}
        role = _roles(clip, "browser")[0]
        if role:
            video["role"] = role  # <clip> itself has no role attribute
        ET.SubElement(el, "video", video)
        ET.SubElement(el, "keyword", {"start": tc.fcpx_time(start), "duration": tc.fcpx_time(duration), "value": keywords})
        return  # stills have no lines, takes or duplicate exports
    tb = _timebase(m) or 1
    el = _clip_element(event, clip, res, offset=None, start=m.start, duration=tc.fcpx_time(m.duration, tb), lane=None, timebase=tb, use="browser")
    whole = {"start": tc.fcpx_time(m.start, tb), "duration": tc.fcpx_time(m.duration, tb), "value": keywords}
    if notes:
        whole["note"] = "; ".join(notes)
    ET.SubElement(el, "keyword", whole)
    ranges = []
    for seg in clip.segments:
        chosen = (alternates or {}).get(seg.id)
        if chosen is None:
            continue
        span = _range_on_clip(m, seg.start, seg.end)
        line = analysis.segment(chosen)
        if span is None or line is None:
            continue
        where = ""
        if cut_tl is not None and chosen in cut_tl.seg_ranges:
            where = f" at {tc.seconds_to_clock(float(cut_tl.seg_ranges[chosen][0] * cut_tl.frame_duration))} in the cut"
        text = line.text if len(line.text) <= 80 else line.text[:77].rstrip() + "..."
        ranges.append((span, f'{seg.id}: another read of {chosen}{where}: "{text}"'))
    for (start, duration), note in sorted(ranges):
        ET.SubElement(el, "keyword", {"start": tc.fcpx_time(start, tb), "duration": tc.fcpx_time(duration, tb), "value": ALT_TAKE, "note": note})


_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _scrub(root: ET.Element) -> None:
    """Model output, transcripts and file names can carry characters XML 1.0 forbids."""
    for el in root.iter():
        for key, value in el.attrib.items():
            if _XML_ILLEGAL.search(value):
                el.set(key, _XML_ILLEGAL.sub(" ", value))
        if el.text and _XML_ILLEGAL.search(el.text):
            el.text = _XML_ILLEGAL.sub(" ", el.text)


def build_fcpxml(
    timelines: list[Timeline], analysis: Analysis, *, event_name: str, browser_clips: bool = True, alternates: list[dict] | None = None
) -> str:
    """`timelines[0]` is the cut; `alternates` are the plan's (chosen line, other reads)."""
    res = _Resources(analysis)
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    root.append(res.el)
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": event_name})

    projects = [_sequence(tl, analysis, res) for tl in timelines if tl.spine]
    if browser_clips:
        cut_tl = timelines[0] if timelines else None
        alts = alternate_reads(cut_tl, analysis, alternates)
        for clip in analysis.clips:
            _browser_clip(event, clip, res, alts, cut_tl)
    for p in projects:
        event.append(p)

    _scrub(root)
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n"

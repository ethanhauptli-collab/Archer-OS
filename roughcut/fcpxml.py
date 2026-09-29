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


def _clip_element(
    parent: ET.Element, clip: Clip, res: "_Resources", *, offset: str | None, start: Fraction, duration: str, lane: int | None, timebase: int
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
    return ET.SubElement(parent, tag, attrs)


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
        )
        # Child order follows the FCPXML DTD: anchored clips, then markers.
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
            )
            if cc.volume_db is not None and child_clip.media.has_audio:
                ET.SubElement(child, "adjust-volume", {"amount": f"{cc.volume_db:g}dB"})
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


def _browser_clip(event: ET.Element, clip: Clip, res: _Resources) -> None:
    """An event (browser) clip carrying keywords, so footage arrives pre-sorted."""
    m = clip.media
    keywords = ", ".join(_keywords(clip))
    if m.kind == "image":
        # Events can't hold a bare <video>; FCP wraps browser stills in a <clip>.
        start, duration = STILL_ORIGIN, Fraction(5)
        el = ET.SubElement(event, "clip", {"name": m.stem, "start": tc.fcpx_time(start), "duration": tc.fcpx_time(duration)})
        ET.SubElement(el, "video", {"ref": res.asset(clip), "duration": "0s"})
        ET.SubElement(el, "keyword", {"start": tc.fcpx_time(start), "duration": tc.fcpx_time(duration), "value": keywords})
        return
    tb = _timebase(m) or 1
    el = _clip_element(event, clip, res, offset=None, start=m.start, duration=tc.fcpx_time(m.duration, tb), lane=None, timebase=tb)
    ET.SubElement(el, "keyword", {"start": tc.fcpx_time(m.start, tb), "duration": tc.fcpx_time(m.duration, tb), "value": keywords})


_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _scrub(root: ET.Element) -> None:
    """Model output, transcripts and file names can carry characters XML 1.0 forbids."""
    for el in root.iter():
        for key, value in el.attrib.items():
            if _XML_ILLEGAL.search(value):
                el.set(key, _XML_ILLEGAL.sub(" ", value))


def build_fcpxml(timelines: list[Timeline], analysis: Analysis, *, event_name: str, browser_clips: bool = True) -> str:
    res = _Resources(analysis)
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    root.append(res.el)
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": event_name})

    projects = [_sequence(tl, analysis, res) for tl in timelines if tl.spine]
    if browser_clips:
        for clip in analysis.clips:
            _browser_clip(event, clip, res)
    for p in projects:
        event.append(p)

    _scrub(root)
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n"

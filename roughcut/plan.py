"""The edit plan: what the model returns, how it's validated, and a no-AI fallback.

The model's output is treated as a suggestion. Every ID is checked against
the analysis, bad references are dropped with a warning, and nothing the
model says can produce an invalid timeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analyze import Analysis

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Working title for the video."},
        "logline": {"type": "string", "description": "One or two sentences: what this cut is and its arc."},
        "sections": {
            "type": "array",
            "description": "Story sections in final timeline order.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short timeline label, e.g. 'Cold open'."},
                    "purpose": {"type": "string", "description": "What this section does for the story."},
                    "items": {
                        "type": "array",
                        "description": (
                            "Primary storyline in order. Segment IDs like 'V01.S014', or visual ranges "
                            "'CLIPID@start-end' in seconds from the clip's start, e.g. 'V07@12.5-16' or 'I02@0-4'."
                        ),
                        "items": {"type": "string"},
                    },
                    "broll": {
                        "type": "array",
                        "description": "Cutaways layered over this section's spoken segments.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "clip": {"type": "string", "description": "B-roll clip or still ID, e.g. 'V05' or 'I02'."},
                                "over": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Consecutive segment IDs (from this section's items) the cutaway covers.",
                                },
                                "source_in": {
                                    "type": "number",
                                    "description": "Seconds into the B-roll clip to start from, or -1 to let the software choose.",
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["clip", "over", "source_in", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "purpose", "items", "broll"],
                "additionalProperties": False,
            },
        },
        "alternates": {
            "type": "array",
            "description": "Retakes: the chosen segment and the other takes of the same line.",
            "items": {
                "type": "object",
                "properties": {
                    "chosen": {"type": "string"},
                    "others": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["chosen", "others", "note"],
                "additionalProperties": False,
            },
        },
        "flags": {
            "type": "array",
            "description": "Decisions the editor should double-check. Become to-do markers.",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Segment or clip ID the note is about."},
                    "note": {"type": "string"},
                },
                "required": ["ref", "note"],
                "additionalProperties": False,
            },
        },
        "music": {
            "type": "array",
            "description": "Music beds. Section indexes are 0-based into 'sections'.",
            "items": {
                "type": "object",
                "properties": {
                    "clip": {"type": "string"},
                    "first_section": {"type": "integer"},
                    "last_section": {"type": "integer"},
                    "source_in": {"type": "number"},
                },
                "required": ["clip", "first_section", "last_section", "source_in"],
                "additionalProperties": False,
            },
        },
        "cut_notes": {"type": "string", "description": "What was left out and why, briefly."},
    },
    "required": ["title", "logline", "sections", "alternates", "flags", "music", "cut_notes"],
    "additionalProperties": False,
}


@dataclass
class Item:
    clip_id: str
    seg_id: str | None = None  # set for spoken segments
    start: float = 0.0  # clip-local seconds, for ranges
    end: float = 0.0

    @property
    def is_segment(self) -> bool:
        return self.seg_id is not None

    @property
    def ref(self) -> str:
        return self.seg_id if self.seg_id else f"{self.clip_id}@{self.start:g}-{self.end:g}"


@dataclass
class Broll:
    clip_id: str
    over: list[str]
    source_in: float = -1.0
    reason: str = ""


@dataclass
class Section:
    name: str
    purpose: str = ""
    items: list[Item] = field(default_factory=list)
    broll: list[Broll] = field(default_factory=list)


@dataclass
class MusicCue:
    clip_id: str
    first_section: int
    last_section: int
    source_in: float = 0.0


@dataclass
class Plan:
    title: str
    logline: str = ""
    sections: list[Section] = field(default_factory=list)
    alternates: list[dict] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    music: list[MusicCue] = field(default_factory=list)
    cut_notes: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "logline": self.logline,
            "sections": [
                {
                    "name": s.name,
                    "purpose": s.purpose,
                    "items": [i.ref for i in s.items],
                    "broll": [
                        {"clip": b.clip_id, "over": b.over, "source_in": b.source_in, "reason": b.reason} for b in s.broll
                    ],
                }
                for s in self.sections
            ],
            "alternates": self.alternates,
            "flags": self.flags,
            "music": [
                {"clip": m.clip_id, "first_section": m.first_section, "last_section": m.last_section, "source_in": m.source_in}
                for m in self.music
            ],
            "cut_notes": self.cut_notes,
            "source": self.source,
            "warnings": self.warnings,
        }


_RANGE = re.compile(r"^\s*([A-Za-z]\d+)\s*@\s*([\d:.]+)\s*[-–—]\s*([\d:.]+)\s*$")
_SEG = re.compile(r"^\s*([A-Za-z]\d+)\.(S\d+)\s*$", re.IGNORECASE)
_CLIP = re.compile(r"^\s*([A-Za-z]\d+)\s*$")


def _secs(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def normalize(raw: dict, analysis: Analysis, *, source: str = "", still_seconds: float = 4.0) -> Plan:
    """Validate model output against the analysis. Never raises on bad content."""
    warnings: list[str] = []
    plan = Plan(title=str(raw.get("title") or "Rough Cut"), logline=str(raw.get("logline") or ""), source=source)
    used_segments: set[str] = set()

    for s_idx, s_raw in enumerate(raw.get("sections") or []):
        if not isinstance(s_raw, dict):
            continue
        section = Section(name=str(s_raw.get("name") or f"Section {s_idx + 1}"), purpose=str(s_raw.get("purpose") or ""))
        for ref in s_raw.get("items") or []:
            item = _parse_item(str(ref), analysis, still_seconds, warnings)
            if item is None:
                continue
            if item.seg_id:
                if item.seg_id in used_segments:
                    warnings.append(f"{item.seg_id} used twice; kept the first use")
                    continue
                used_segments.add(item.seg_id)
            section.items.append(item)

        section_segs = {i.seg_id for i in section.items if i.seg_id}
        for b_raw in s_raw.get("broll") or []:
            if not isinstance(b_raw, dict):
                continue
            clip_id = str(b_raw.get("clip") or "").strip()
            clip = analysis.clip(clip_id)
            if clip is None or not clip.media.has_video:
                warnings.append(f"B-roll {clip_id!r} is not a video/still clip; skipped")
                continue
            over = [str(o).strip() for o in b_raw.get("over") or []]
            valid = [o for o in over if o in section_segs]
            if len(valid) < len(over):
                warnings.append(f"B-roll {clip_id}: ignored references not in section '{section.name}': {sorted(set(over) - set(valid))}")
            if not valid:
                continue
            try:
                source_in = float(b_raw.get("source_in", -1))
            except (TypeError, ValueError):
                source_in = -1.0
            section.broll.append(Broll(clip_id=clip_id, over=valid, source_in=source_in, reason=str(b_raw.get("reason") or "")))

        if section.items:
            plan.sections.append(section)
        else:
            warnings.append(f"section '{section.name}' had no usable items; dropped")

    for alt in raw.get("alternates") or []:
        if isinstance(alt, dict) and analysis.segment(str(alt.get("chosen", ""))):
            others = [o for o in alt.get("others") or [] if analysis.segment(str(o))]
            plan.alternates.append({"chosen": alt["chosen"], "others": others, "note": str(alt.get("note") or "")})

    for flag in raw.get("flags") or []:
        if isinstance(flag, dict) and flag.get("note"):
            plan.flags.append({"ref": str(flag.get("ref") or ""), "note": str(flag["note"])})

    n_sections = len(plan.sections)
    for cue in raw.get("music") or []:
        if not isinstance(cue, dict):
            continue
        clip = analysis.clip(str(cue.get("clip", "")))
        if clip is None or clip.media.kind != "audio":
            warnings.append(f"music {cue.get('clip')!r} is not an audio clip; skipped")
            continue
        try:
            first = max(0, int(cue.get("first_section", 0)))
            last = min(n_sections - 1, int(cue.get("last_section", n_sections - 1)))
            source_in = max(0.0, float(cue.get("source_in", 0)))
        except (TypeError, ValueError):
            continue
        if n_sections and first <= last:
            plan.music.append(MusicCue(clip.id, first, last, source_in))

    plan.cut_notes = str(raw.get("cut_notes") or "")
    plan.warnings = warnings
    return plan


def _parse_item(ref: str, analysis: Analysis, still_seconds: float, warnings: list[str]) -> Item | None:
    m = _SEG.match(ref)
    if m:
        seg_id = f"{m.group(1).upper()}.{m.group(2).upper()}"
        seg = analysis.segment(seg_id)
        if seg is None:
            warnings.append(f"unknown segment {ref!r}; skipped")
            return None
        return Item(clip_id=seg.clip_id, seg_id=seg_id, start=seg.start, end=seg.end)

    m = _RANGE.match(ref)
    clip_only = _CLIP.match(ref)
    if not m and not clip_only:
        warnings.append(f"can't read item {ref!r}; skipped")
        return None
    clip_id = (m or clip_only).group(1).upper()
    clip = analysis.clip(clip_id)
    if clip is None:
        warnings.append(f"unknown clip {ref!r}; skipped")
        return None
    if clip.media.kind == "image":
        length = still_seconds
        if m:
            length = max(0.5, _secs(m.group(3)) - _secs(m.group(2)))
        return Item(clip_id=clip_id, start=0.0, end=length)
    dur = clip.duration
    if m:
        start, end = _secs(m.group(2)), _secs(m.group(3))
    else:
        start = min(dur * 0.1, max(0.0, dur - 5.0))
        end = min(dur, start + 5.0)
    start, end = max(0.0, start), min(dur, end)
    if end - start < 0.2:
        warnings.append(f"range {ref!r} is empty after clamping to the clip; skipped")
        return None
    return Item(clip_id=clip_id, start=start, end=end)


def heuristic_plan(analysis: Analysis, *, title: str = "Stringout") -> Plan:
    """No-AI plan: every spoken segment in recording order, one section per clip.

    This is also the "stringout" project: all dialogue with dead air removed.
    """
    plan = Plan(title=title, logline="All spoken material in recording order with dead air removed.", source="heuristic")
    talking = [c for c in analysis.clips if c.role in ("aroll", "voiceover") and c.segments]
    for clip in talking:
        plan.sections.append(
            Section(
                name=clip.media.stem,
                items=[Item(clip_id=clip.id, seg_id=s.id, start=s.start, end=s.end) for s in clip.segments],
            )
        )
    if not talking:
        visuals = [c for c in analysis.clips if c.role in ("broll", "still")]
        items = []
        for clip in visuals:
            if clip.role == "still":
                items.append(Item(clip_id=clip.id, start=0.0, end=3.0))
            else:
                start = min(clip.duration * 0.15, max(0.0, clip.duration - 4.0))
                items.append(Item(clip_id=clip.id, start=start, end=min(clip.duration, start + 4.0)))
        if items:
            plan.sections.append(Section(name="Montage", items=items))
    return plan

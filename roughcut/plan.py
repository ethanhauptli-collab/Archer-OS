"""The edit plan: what the model returns, how it's validated, and a no-AI fallback.

The model's output is treated as a suggestion. Every ID is checked against
the analysis, bad references are dropped with a warning, and nothing the
model says can produce an invalid timeline.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass, field

from .analyze import Analysis
from .captions import clean_spelling, respell
from .duplicates import LINE_CONTAINED, another_read, contains_line, found_in, line_words, same_line, take_group

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
                        "description": (
                            "Cutaways layered over this section's spoken segments, in order. Consecutive entries with the "
                            "same 'over' play one after another and share that time."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "clip": {"type": "string", "description": "B-roll clip or still ID, e.g. 'V05' or 'I02'."},
                                "over": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Consecutive segment IDs (from this section's items) the cutaway covers.",
                                },
                                "words": {
                                    "type": "string",
                                    "description": (
                                        "Exact words copied from the covered lines where this shot should come in (a name, "
                                        "place or list item), or '' to follow the shot before it (or start with the line)."
                                    ),
                                },
                                "source_in": {
                                    "type": "number",
                                    "description": "Seconds into the B-roll clip to start from, or -1 to let the software choose.",
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["clip", "over", "words", "source_in", "reason"],
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
        "spellings": {
            "type": "array",
            "description": "Names the transcript misspells, as the brief, script or file names spell them. They fix the captions and the names in graphics' text and subtext.",
            "items": {
                "type": "object",
                "properties": {
                    "heard": {"type": "string", "description": "The name exactly as the transcript has it, without a possessive 's or punctuation after it, e.g. 'Roy Choy'."},
                    "correct": {"type": "string", "description": "The right spelling of the same words, also without a possessive 's or punctuation after it, e.g. 'Roy Choi'."},
                },
                "required": ["heard", "correct"],
                "additionalProperties": False,
            },
        },
        # Cut settings the software applies, not the plan: what the brief asks for, as the editor meant it.
        "pacing": {
            "type": "string",
            "enum": ["tight", "medium", "loose", "unspecified"],
            "description": (
                "How hard the brief asks for pauses to be trimmed. tight: tight cuts, snappy, punchy, fast-paced, no dead air. "
                "loose: let it breathe, slow, reflective, unhurried. medium: a natural pace in between, or tight and loose both asked "
                "for. unspecified: the brief doesn't say. 'Not too tight' is not tight; 'a tight deadline' or 'tight shots' aren't about pacing."
            ),
        },
        "punch_ins": {
            "type": "string",
            "enum": ["on", "off", "unspecified"],
            "description": (
                "Punch-ins are alternating zooms at every jump cut the B-roll doesn't cover. on: the brief asks for them across the "
                "cut ('punch in on the jump cuts', 'use punch-ins'). off: it turns them down. unspecified: otherwise, including a "
                "one-off reframe of a moment ('punch in on Roy's face for the last line'), which is not on."
            ),
        },
        "graphics_asked": {
            "type": "boolean",
            "description": (
                "Whether the brief asks for on-screen graphics, titles, names or text (lower thirds, title cards, names on screen, "
                "callouts, kinetic captions). false when it turns them down or doesn't mention them."
            ),
        },
    },
    "required": [
        "title", "logline", "sections", "alternates", "flags", "music", "cut_notes", "spellings", "pacing", "punch_ins", "graphics_asked",
    ],
    "additionalProperties": False,
}
PACINGS = ("tight", "medium", "loose")  # the plan's pacing when the brief says one ("unspecified" otherwise)


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
    words: str = ""  # where in the covered lines the shot comes in ("" = even split)


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


GRAPHIC_KINDS = ("lower_third", "stat", "title_card", "kinetic_caption", "quote", "list")
DEFAULT_SECONDS = {"lower_third": 4.0, "stat": 4.0, "title_card": 3.0, "kinetic_caption": 3.0, "quote": 5.0, "list": 6.0}

GRAPHICS_SCHEMA = {
    "type": "array",
    "description": "Motion graphics, designed and rendered after planning. Each is anchored on words in a segment of the cut.",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(GRAPHIC_KINDS)},
            "segment": {"type": "string", "description": "Segment ID (in this cut) the graphic appears over."},
            "words": {"type": "string", "description": "The exact words from that segment where it appears, copied."},
            "text": {"type": "string", "description": "Main on-screen text, short."},
            "subtext": {"type": "string", "description": "Optional second line, or empty."},
            "seconds": {"type": "number", "description": "How long it holds, 2-8; 0 for kinetic captions (they follow the speech)."},
            "direction": {"type": "string", "description": "Look or motion notes, or empty."},
        },
        "required": ["kind", "segment", "words", "text", "subtext", "seconds", "direction"],
        "additionalProperties": False,
    },
}


def plan_schema(graphics: bool = False) -> dict:
    """The plan schema, with a graphics list when the run asks for motion graphics."""
    if not graphics:
        return PLAN_SCHEMA
    schema = copy.deepcopy(PLAN_SCHEMA)
    schema["properties"]["graphics"] = GRAPHICS_SCHEMA
    schema["required"] = schema["required"] + ["graphics"]
    return schema


@dataclass
class GraphicSpec:
    """One graphic as the planner asked for it."""

    kind: str
    segment: str
    words: str
    text: str
    subtext: str = ""
    seconds: float = 0.0
    direction: str = ""


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
    graphics: list[GraphicSpec] = field(default_factory=list)
    target_seconds: float | None = None  # the length the brief asked for (not the model's)
    spellings: list[dict] = field(default_factory=list)  # {"heard", "correct"}: names the transcript gets wrong
    # What the planner read in the brief (or a reviser in the notes): Auto pacing and punch-ins follow it.
    pacing: str = "unspecified"  # tight / medium / loose / unspecified
    punch_ins: str = "unspecified"  # on / off / unspecified
    graphics_asked: bool = False

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
                        {"clip": b.clip_id, "over": b.over, "words": b.words, "source_in": b.source_in, "reason": b.reason}
                        for b in s.broll
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
            "graphics": [asdict(g) for g in self.graphics],
            "target_seconds": self.target_seconds,
            "spellings": self.spellings,
            "pacing": self.pacing,
            "punch_ins": self.punch_ins,
            "graphics_asked": self.graphics_asked,
        }


def _spelling_tokens(text: str) -> list[str]:
    from .transcribe import FILLERS

    return [t for t in (re.sub(r"[^\w]", "", w.lower()) for w in text.split()) if t and t not in FILLERS]  # "Marco, um, Belini"


def spelling_key(heard: str) -> str:
    """The words a name fix applies to, for telling two fixes of one name apart from fixes of two."""
    return " ".join(_spelling_tokens(clean_spelling(heard, heard)[0]))


def respell_graphics(specs: list[GraphicSpec], spellings: list[dict], keep: list[dict] | None = None) -> None:
    """Names on screen spelled as the brief spells them. A graphic's words stay as transcribed: they find it in the speech.
    `keep`: pairs in force, whose right forms stay as they are (when `spellings` are undone pairs, reversed)."""
    for g in specs:
        g.text, g.subtext = respell(g.text, spellings, keep), respell(g.subtext, spellings, keep)


def _spellings(raw, analysis: Analysis) -> list[dict]:
    """The planner's name fixes that name words the transcript really has (and change them)."""
    spoken = " " + " ".join(
        " ".join(_spelling_tokens(seg.text)) for c in analysis.clips for seg in c.segments
    ) + " "
    out: list[dict] = []
    for fix in raw or []:
        if not isinstance(fix, dict):
            continue
        heard, correct = clean_spelling(str(fix.get("heard") or ""), str(fix.get("correct") or ""))
        tokens = _spelling_tokens(heard)
        if not tokens or not correct or heard == correct or len(tokens) > 6:
            continue
        if f" {' '.join(tokens)} " in spoken or f" {' '.join(tokens)}s " in spoken:  # "Rousseau's" is "rousseaus"
            out.append({"heard": heard, "correct": correct})
    return out[:100]


_RANGE = re.compile(r"^\s*([A-Za-z]\d+)\s*@\s*([\d:.]+)\s*[-–—]\s*([\d:.]+)\s*$")
_SEG = re.compile(r"^\s*([A-Za-z]\d+)\.(S\d+)\s*$", re.IGNORECASE)
_CLIP = re.compile(r"^\s*([A-Za-z]\d+)\s*$")


def _secs(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def normalize(
    raw: dict,
    analysis: Analysis,
    *,
    source: str = "",
    still_seconds: float = 4.0,
    keep: Collection[str] = (),
    avoided: Callable[[str], bool] | None = None,
    played: Collection[str] | None = None,
) -> Plan:
    """Validate model output against the analysis. Never raises on bad content.

    Which take of a line plays follows the editor: `keep` (the segment IDs a revision's --keep names), `avoided`
    (is a segment left out by a standing --avoid?) and, for a revision, `played` (the lines the version it
    revises played). See `_one_take`.
    """
    warnings: list[str] = []
    plan = Plan(title=str(raw.get("title") or "Rough Cut"), logline=str(raw.get("logline") or ""), source=source)
    used_segments: set[str] = set()
    raw_to_plan: dict[int, int] = {}  # model's section index -> kept section index
    # The same line from another read or export plays once. Not in the stringout (`render` of a
    # --provider none run): it plays everything by design, and re-rendering must give the same cut.
    lines = _UsedLines(analysis, enabled=source != "heuristic")
    raw = {**raw, "alternates": _retakes(raw.get("alternates"), analysis, warnings)}
    if lines.enabled:  # and a retake as the planner's own alternates say
        raw = _one_take(raw, analysis, lines.replaced, warnings, keep=keep, avoided=avoided, played=played)
    # Every line the plan uses, in order: a sentence that holds a used line and more is a repeat
    # only when the rest is in the cut too, maybe further on.
    planned = [
        f"{m.group(1).upper()}.{m.group(2).upper()}"
        for s_raw in raw.get("sections") or [] if isinstance(s_raw, dict)
        for m in (_SEG.match(str(ref)) for ref in s_raw.get("items") or [])
        if m and analysis.segment(f"{m.group(1).upper()}.{m.group(2).upper()}")
    ]
    seen_lines = 0

    for s_idx, s_raw in enumerate(raw.get("sections") or []):
        if not isinstance(s_raw, dict):
            continue
        section = Section(name=str(s_raw.get("name") or f"Section {s_idx + 1}"), purpose=str(s_raw.get("purpose") or ""))
        for ref in s_raw.get("items") or []:
            item = _parse_item(str(ref), analysis, still_seconds, warnings)
            if item is None:
                continue
            if item.seg_id:
                seen_lines += 1
                if item.seg_id in used_segments:
                    warnings.append(f"{item.seg_id} used twice; kept the first use")
                    continue
                kept = lines.repeat_of(item.seg_id, later=planned[seen_lines:])
                if kept:
                    warnings.append(f"{item.seg_id} says the same line as {kept} (another read or export); kept {kept} only")
                    continue
                used_segments.add(item.seg_id)
                lines.add(item.seg_id)
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
            # Over a line dropped as a repeat: cover the use that was kept, when it's here.
            over = list(dict.fromkeys(lines.replaced[o] if lines.replaced.get(o) in section_segs else o for o in over))
            valid = [o for o in over if o in section_segs]
            if len(valid) < len(over):
                warnings.append(f"B-roll {clip_id}: ignored references not in section '{section.name}': {sorted(set(over) - set(valid))}")
            if not valid:
                continue
            try:
                source_in = float(b_raw.get("source_in", -1))
            except (TypeError, ValueError):
                source_in = -1.0
            section.broll.append(
                Broll(
                    clip_id=clip_id,
                    over=valid,
                    source_in=source_in,
                    reason=str(b_raw.get("reason") or ""),
                    words=str(b_raw.get("words") or "").strip()[:200],
                )
            )

        if section.items:
            raw_to_plan[s_idx] = len(plan.sections)
            plan.sections.append(section)
        else:
            warnings.append(f"section '{section.name}' had no usable items; dropped")

    plan.alternates = [dict(alt, others=list(alt["others"])) for alt in raw["alternates"]]
    for dropped, kept in lines.replaced.items():
        alt = next((a for a in plan.alternates if a["chosen"] == kept), None)
        if alt is None:
            plan.alternates.append({"chosen": kept, "others": [dropped], "note": "the same line from another read or export"})
        elif dropped not in alt["others"]:
            alt["others"].append(dropped)

    plan.spellings = _spellings(raw.get("spellings"), analysis)

    for flag in raw.get("flags") or []:
        if isinstance(flag, dict) and flag.get("note"):
            ref = str(flag.get("ref") or "")
            plan.flags.append({"ref": lines.replaced.get(ref, ref), "note": str(flag["note"])})
    for seg_id, earlier in lines.overlaps:
        said = analysis.segment(earlier).text
        warnings.append(f"{seg_id} says the line {earlier} says (another read or export) and more; kept both, so those words play twice")
        plan.flags.append({"ref": seg_id, "note": f'Repeats {earlier} ("{said[:60]}") from another read or export: trim it from one of them.'})

    for cue in raw.get("music") or []:
        if not isinstance(cue, dict):
            continue
        clip = analysis.clip(str(cue.get("clip", "")))
        if clip is None or clip.media.kind != "audio":
            warnings.append(f"music {cue.get('clip')!r} is not an audio clip; skipped")
            continue
        try:
            first = int(cue.get("first_section", 0))
            last = int(cue.get("last_section", first))
            source_in = max(0.0, float(cue.get("source_in", 0)))
        except (TypeError, ValueError):
            warnings.append(f"music {clip.id}: unreadable section range; skipped")
            continue
        # The model counts sections in its own list; map onto the sections we kept.
        kept = [plan_idx for raw_idx, plan_idx in raw_to_plan.items() if first <= raw_idx <= last]
        if not kept:
            warnings.append(f"music {clip.id}: sections {first}-{last} don't match any kept section; skipped")
            continue
        plan.music.append(MusicCue(clip.id, min(kept), max(kept), source_in))

    plan.cut_notes = str(raw.get("cut_notes") or "")
    # A plan from before these were asked for (or a model that left them out) says nothing.
    pacing = str(raw.get("pacing") or "").strip().lower()
    plan.pacing = pacing if pacing in PACINGS else "unspecified"
    punch = raw.get("punch_ins")
    punch = ("on" if punch else "off") if isinstance(punch, bool) else str(punch or "").strip().lower()
    plan.punch_ins = punch if punch in ("on", "off") else "unspecified"
    plan.graphics_asked = raw.get("graphics_asked") is True
    graphics = [
        dict(g, segment=lines.replaced.get(str(g.get("segment") or "").strip().upper(), g.get("segment"))) if isinstance(g, dict) else g
        for g in raw.get("graphics") or []
    ]
    plan.graphics = parse_specs(graphics, analysis, used_segments, warnings)
    respell_graphics(plan.graphics, plan.spellings)
    plan.warnings = warnings
    return plan


def chosen_takes(
    raw: dict, analysis: Analysis, *, keep: Collection[str] = (), avoided: Callable[[str], bool] | None = None, played: Collection[str] | None = None
) -> dict:
    """`raw` with its alternates as `normalize` reads them with the same arguments: real retakes only, each with
    "chosen" the take that plays. Saved in place of the model's own, a re-cut plays the takes this revision did
    (a swap the notes made, a take kept or left out) without being told why."""
    alternates = _retakes(raw.get("alternates"), analysis, [])
    return {**raw, "alternates": _one_take({**raw, "alternates": alternates}, analysis, {}, [], keep=keep, avoided=avoided, played=played)["alternates"]}


def _retakes(alternates, analysis: Analysis, warnings: list[str]) -> list[dict]:
    """The planner's alternates, each {"chosen", "others", "note"} with known segment IDs, and as "others" only the
    takes that read the chosen line: a flub or another line listed there is left out (and left where it is in the cut)."""
    out = []
    for alt in alternates if isinstance(alternates, list) else []:
        chosen = _seg_ref(alt.get("chosen"), analysis) if isinstance(alt, dict) else None
        if chosen is None:
            continue
        listed = alt.get("others") if isinstance(alt.get("others"), list) else []
        others, not_takes = [], []
        for other in dict.fromkeys(o for o in (_seg_ref(o, analysis) for o in listed) if o and o != chosen):
            (others if another_read(analysis.segment(other).text, analysis.segment(chosen).text) else not_takes).append(other)
        for other in not_takes:
            warnings.append(f"{other} doesn't say the line {chosen} says, so it isn't another take of it (alternates); left as it is")
        if others or not not_takes:
            out.append({"chosen": chosen, "others": others, "note": str(alt.get("note") or "")})
    return out


def _seg_ref(ref, analysis: Analysis) -> str | None:
    m = _SEG.match(str(ref))
    seg_id = f"{m.group(1).upper()}.{m.group(2).upper()}" if m else ""
    return seg_id if seg_id and analysis.segment(seg_id) else None


def _take_to_play(
    alt: dict, in_cut: list[str], keep: Collection[str], avoided: Callable[[str], bool], played: Collection[str] | None, warnings: list[str]
) -> str:
    """Which take of `alt`'s line plays: the chosen one, unless the editor or the revision says another.

    An other take in the cut plays instead when the chosen one is left out (--avoid), when it is kept (--keep),
    or, in a revision of a cut that played one take of the line, when the revision put it there: in place of the
    chosen take (notes that swap takes; the reviser may leave the alternates as they were), or as the take that
    played. A cut that played several takes (the stringout plays every one) swapped nothing.
    """
    chosen = alt["chosen"]
    present = [seg for seg in in_cut if seg in alt["others"] and not avoided(seg)]
    if avoided(chosen):
        if present:
            warnings.append(f"{chosen}, the take the plan chose (alternates), is left out as asked (--avoid); {present[0]}, another take of it, plays instead")
        return present[0] if present else chosen
    if chosen in keep:
        return chosen
    kept = next((seg for seg in present if seg in keep), None)
    if kept:
        warnings.append(f"{kept} plays as asked (--keep): it's another take of {chosen}, so it's the chosen take now (alternates) and {chosen} doesn't play")
        return kept
    if played is not None and present and chosen not in in_cut and len([t for t in [chosen, *alt["others"]] if t in played]) == 1:
        take = next((seg for seg in present if seg in played), present[0])
        if chosen in played:
            warnings.append(f"{take} plays in place of {chosen}, the take of this line the last version played: the revision swapped them, so it's the chosen take now (alternates)")
        return take
    return chosen


def _one_take(
    raw: dict,
    analysis: Analysis,
    replaced: dict[str, str],
    warnings: list[str],
    *,
    keep: Collection[str] = (),
    avoided: Callable[[str], bool] | None = None,
    played: Collection[str] | None = None,
) -> dict:
    """`raw` with its sections as its own alternates say: a take the planner lists among a line's "others" plays
    as the take it chose, once. Dropped when the chosen take is in the cut too, else the chosen one goes in its place.

    The editor's constraints come first (`_take_to_play`): an other take that plays instead becomes the chosen one,
    and the returned alternates say so. A take in `keep` is never dropped or replaced (unless the chosen take of its
    line is kept too: the line plays once, as that one, and the warnings say so), and a chosen take `avoided`
    leaves out never goes in place of another. Each other take -> the chosen one goes into `replaced`, so B-roll,
    flags and graphics on it follow. An alternate that contradicts an earlier one (choosing a take that one ruled
    out) is left alone. `raw["alternates"]` is `_retakes`'s; `raw` isn't changed.
    """
    sections = [dict(s, items=list(s["items"]) if isinstance(s.get("items"), list) else []) if isinstance(s, dict) else s
                for s in raw.get("sections") or []]
    items = [s["items"] for s in sections if isinstance(s, dict)]
    avoided = avoided or (lambda seg_id: False)
    chosen_before: set[str] = set()
    ruled_out: set[str] = set()
    alternates = []
    for alt in raw.get("alternates") or []:
        if alt["chosen"] in ruled_out:
            alternates.append(alt)
            continue
        in_cut = [seg for refs in items for seg in (_seg_ref(ref, analysis) for ref in refs if ref is not None) if seg]
        take = _take_to_play(alt, in_cut, keep, avoided, played, warnings)
        if take != alt["chosen"]:
            was = alt["chosen"]
            why = f"{was} left out as asked (--avoid)" if avoided(was) else "Kept as asked (--keep)" if take in keep else f"Played in place of {was} by a revision"
            alt = {"chosen": take, "others": [was] + [o for o in alt["others"] if o != take], "note": why}
        alternates.append(alt)
        chosen = alt["chosen"]
        others = {o for o in alt["others"] if o not in chosen_before}
        chosen_before.add(chosen)
        ruled_out |= others
        if avoided(chosen):
            continue  # left out as asked: it replaces nothing
        kept_too = [o for o in alt["others"] if o in others and o in keep]
        if chosen in keep and kept_too:
            # Kept twice over: the line still plays once, as the chosen take, and the other stays an alternate.
            also = ", ".join(kept_too)
            warnings.append(
                f"--keep names {chosen} and {also}, takes of one line: {chosen} plays (the chosen take, alternates) so the "
                f"line plays once, and {also} {'stays an alternate take' if len(kept_too) == 1 else 'stay alternate takes'}"
            )
        else:
            others -= set(keep)  # one the editor asked to keep stays
        playing = chosen in in_cut
        for refs in items:
            for k, ref in enumerate(refs):
                other = _seg_ref(ref, analysis) if ref is not None else None
                if other not in others:
                    continue
                replaced[other] = chosen
                if playing:
                    refs[k] = None
                    warnings.append(f"{other} is another take of {chosen}, which the plan chose (alternates); dropped, so the line plays once")
                else:
                    refs[k], playing = chosen, True
                    warnings.append(f"{other} is another take of {chosen}, which the plan chose (alternates); {chosen} plays in its place")
    for refs in items:
        refs[:] = [ref for ref in refs if ref is not None]
    return {**raw, "sections": sections, "alternates": alternates}


class _UsedLines:
    """Lines already in the cut, to catch the same line again from another read or export.

    Reads of one script split into sentences differently (Whisper's
    punctuation), so a line said again can be half of another read's
    sentence, or a sentence can hold a line already used and more.
    """

    def __init__(self, analysis: Analysis, enabled: bool = True):
        self.analysis = analysis
        self.enabled = enabled
        self.kept: list[tuple[str, list[str], str, str]] = []  # (segment, words, clip, take group)
        self.replaced: dict[str, str] = {}  # dropped repeat -> the use that was kept
        self.overlaps: list[tuple[str, str]] = []  # (kept line, the line before it whose words it says again)

    def _key(self, seg_id: str) -> tuple[list[str], str, str]:
        seg = self.analysis.segment(seg_id)
        clip = self.analysis.clip(seg.clip_id)
        return line_words(seg.text), clip.id, take_group(clip)

    def repeat_of(self, seg_id: str, later: list[str] = ()) -> str | None:
        """The kept line `seg_id` says again, when it adds nothing the cut doesn't say (or the plan's
        `later` lines won't): then it's dropped. None keeps it."""
        if not self.enabled:
            return None
        words, clip_id, group = self._key(seg_id)
        # Retakes inside one recording are the planner's call (rule 2); this catches other files.
        others = [k for k in self.kept if k[2] != clip_id]
        for kept_id, kept_words, _, kept_group in others:
            same = kept_group == group
            if same_line(words, kept_words, same_takes=same) or contains_line(kept_words, words, same):
                self.replaced[seg_id] = kept_id
                return kept_id
        for kept_id, kept_words, _, kept_group in others:
            if contains_line(words, kept_words, kept_group == group):
                # A kept line and more: a repeat only when the rest is said elsewhere in the cut too.
                if self._said_elsewhere(words, group, [s for s in later if s != seg_id]):
                    self.replaced[seg_id] = kept_id
                    return kept_id
                self.overlaps.append((seg_id, kept_id))
                return None
        return None

    def _said_elsewhere(self, words: list[str], group: str, later: list[str]) -> bool:
        heard: set[int] = set()
        lines = [(w, g) for _, w, _, g in self.kept] + [(w, g) for w, _, g in map(self._key, later)]
        for line, line_group in lines:
            if contains_line(words, line, line_group == group):
                heard |= found_in(words, line)
        return len(heard) >= LINE_CONTAINED * len(words)

    def add(self, seg_id: str) -> None:
        if self.enabled:
            self.kept.append((seg_id, *self._key(seg_id)))


def _parse_item(ref: str, analysis: Analysis, still_seconds: float, warnings: list[str]) -> Item | None:
    m = _SEG.match(ref)
    if m:
        seg_id = f"{m.group(1).upper()}.{m.group(2).upper()}"
        seg = analysis.segment(seg_id)
        if seg is None:
            warnings.append(f"unknown segment {ref!r}; skipped")
            return None
        return Item(clip_id=seg.clip_id, seg_id=seg_id, start=seg.start, end=seg.end)

    try:
        return _parse_non_segment(ref, analysis, still_seconds, warnings)
    except ValueError:
        warnings.append(f"can't read the times in {ref!r}; skipped")
        return None


def _parse_non_segment(ref: str, analysis: Analysis, still_seconds: float, warnings: list[str]) -> Item | None:
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
    # One version of each export and one take of the narration.
    talking = [c for c in analysis.clips if c.role in ("aroll", "voiceover") and c.segments and not c.hidden and not c.take_of]
    for clip in talking:
        plan.sections.append(
            Section(
                name=clip.media.stem,
                items=[Item(clip_id=clip.id, seg_id=s.id, start=s.start, end=s.end) for s in clip.segments],
            )
        )
    if not talking:
        visuals = [c for c in analysis.clips if c.role in ("broll", "still") and not c.hidden]
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


def parse_specs(raw: list, analysis: Analysis, used_segments: set[str], warnings: list[str]) -> list[GraphicSpec]:
    """The plan's graphics entries, checked against the transcript. Never raises."""
    specs = []
    for g in raw or []:
        if not isinstance(g, dict):
            continue
        seg_id = str(g.get("segment") or "").strip().upper()
        text = str(g.get("text") or "").strip()
        kind = str(g.get("kind") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if kind not in GRAPHIC_KINDS:
            kind = "quote"  # still designed; the direction says what it should be
        if not text:
            warnings.append(f"graphic on {seg_id or '?'} has no text; skipped")
            continue
        if analysis.segment(seg_id) is None:
            warnings.append(f"graphic {text!r}: unknown segment {seg_id!r}; skipped")
            continue
        if seg_id not in used_segments:
            warnings.append(f"graphic {text!r}: {seg_id} isn't in the cut; skipped")
            continue
        try:
            seconds = float(g.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        specs.append(
            GraphicSpec(
                kind=kind,
                segment=seg_id,
                words=str(g.get("words") or "").strip(),
                text=text[:120],
                subtext=str(g.get("subtext") or "").strip()[:160],
                seconds=seconds,
                direction=str(g.get("direction") or "").strip()[:400],
            )
        )
    return specs

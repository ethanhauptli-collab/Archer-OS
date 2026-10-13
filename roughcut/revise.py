"""`roughcut revise`: the editor's notes on a finished cut, turned into a new version.

The planner gets the same system prompt and the same (prompt-cached) media
block as the build, then the previous plan and the notes, and returns a whole
new plan. It still only names segment IDs; `plan.normalize` checks them as
always. Afterwards `--avoid` is enforced (this revision's and every earlier
one's, until a later `--keep` names it), missing `--keep` IDs are flagged,
and the paper edit lists exactly what changed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .analyze import Analysis
from .plan import Plan
from .prompts import render_brief
from .timecode import seconds_to_clock

REVISE_INSTRUCTION = """\
This cut has already been made, and the editor watched it in Final Cut Pro. Revise the previous plan \
according to the editor's notes. Change only what the notes ask for. Keep everything else exactly as \
it was: the same sections, segments and order, B-roll, music, alternates, flags, graphics and spellings, \
unless a note says otherwise or a change forces it. When a note swaps one take of a line for another, the \
take you use becomes that alternate's chosen one. Spellings you leave out still apply; to undo one the \
notes say is wrong, return it with "correct" the same as "heard", and write that name in graphics' text as \
heard. Earlier notes still apply. Return the complete revised plan in the same format, and in cut_notes say \
briefly what you changed for these notes.
For this revision, read pacing, punch_ins and graphics_asked only from <editor_notes>, never from the brief \
or <earlier_notes>: <cut_settings> is the editor's current choice, including any re-cut since the brief or the \
earlier notes. pacing: the pacing these notes ask for, read against <cut_settings>, so from medium "make it \
looser", "too tight" or "let him finish his thoughts" is loose, and "it drags", "speed it up" or "tighter \
overall" is tight; a note about one moment ("a tighter hook") isn't about pacing. punch_ins: on when they ask \
for punch-ins at the jump cuts, off when they ask to remove them. Return unspecified for either unless these \
notes ask to change it. graphics_asked: whether these notes ask for on-screen graphics, titles, names or text.
Return each graphic's direction exactly as <previous_plan> gives it unless these notes change it: a changed \
direction (or text) designs that graphic again. redesign_graphics: yes only when these notes ask to change how \
the graphics look or move (style, colour, font, size, position, speed, "make them bolder", "like this"), which \
designs every graphic again; no when they only change what a graphic says or ask to leave the look alone; \
unspecified when they don't mention the graphics."""


def _snippet(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def render_previous_plan(plan: Plan, analysis: Analysis) -> str:
    """The cut as it stands, in the plan's own vocabulary (IDs), with a little text for orientation."""
    L = [f"Title: {plan.title}"]
    if plan.logline:
        L.append(f"Logline: {plan.logline}")
    for i, section in enumerate(plan.sections):
        L.append(f'Section {i} "{section.name}"' + (f" ({section.purpose})" if section.purpose else ""))
        for item in section.items:
            if item.seg_id:
                seg = analysis.segment(item.seg_id)
                text = _snippet(seg.text) if seg is not None and seg.text else "(no transcript)"
                L.append(f'  {item.seg_id} "{text}"')
            else:
                clip = analysis.clip(item.clip_id)
                desc = _snippet((clip.visual or {}).get("description", "") if clip else "", 60)
                L.append(f"  {item.ref}" + (f" (visual: {desc})" if desc else ""))
        for b in section.broll:
            words = getattr(b, "words", "")
            at = f" from {b.source_in:g}s" if b.source_in >= 0 else ""
            L.append(f"  B-roll {b.clip_id} over {', '.join(b.over)}{at}" + (f' on "{words}"' if words else "") + (f": {b.reason}" if b.reason else ""))
    for m in plan.music:
        L.append(f"Music: {m.clip_id} under sections {m.first_section}-{m.last_section} from {m.source_in:g}s")
    for g in plan.graphics:
        L.append(
            f'Graphic: {g.kind} on {g.segment} at "{g.words}": "{g.text}"' + (f' / "{g.subtext}"' if g.subtext else "")
            + (f' (direction: "{g.direction}")' if g.direction else "")
        )
    for alt in plan.alternates:
        L.append(f"Alternate: {alt['chosen']} chosen over {', '.join(alt['others']) or 'n/a'}")
    for flag in plan.flags:
        L.append(f"Flag: {flag['ref']}: {flag['note']}")
    for fix in plan.spellings:
        if isinstance(fix, dict) and fix.get("heard") and fix.get("correct"):
            L.append(f'Spelling: "{fix["heard"]}" -> "{fix["correct"]}"')
    if plan.cut_notes:
        L.append(f"Cut notes: {plan.cut_notes}")
    return "\n".join(L)


def revision_request(
    previous: Plan,
    analysis: Analysis,
    notes: str,
    *,
    context: str,
    target_seconds: float | None,
    script: str | None,
    graphics: bool,
    graphics_style: str,
    earlier_notes: list[str],
    keep: list[str],
    avoid: list[str],
    preset_brief: str = "",
    style: str = "medium",
    punch_in: bool = False,
) -> str:
    """The one non-cached part of a revision request (the media part is the build's, byte for byte).
    `style`, `punch_in`: the cut's settings, which notes like "make it looser" are read against."""
    brief = render_brief(context, target_seconds, script, graphics=graphics, graphics_style=graphics_style, preset_brief=preset_brief)
    brief = brief.removesuffix("Build the rough cut plan.").rstrip()
    settings = (
        f"This cut is at {style} pacing (tight, medium or loose: how hard pauses are trimmed), with punch-ins "
        f"{'on' if punch_in else 'off'} and motion graphics {'on' if graphics else 'off'}. This is the editor's current "
        "choice: return pacing and punch_ins as unspecified unless these notes ask to change it."
    )
    parts = [brief, "<cut_settings>\n" + settings + "\n</cut_settings>", "<previous_plan>\n" + render_previous_plan(previous, analysis) + "\n</previous_plan>"]
    if earlier_notes:
        parts.append("<earlier_notes>\n" + "\n".join(f"- {n}" for n in earlier_notes) + "\n</earlier_notes>")
    parts.append("<editor_notes>\n" + (notes.strip() or "(no notes; see the requirements below)") + "\n</editor_notes>")
    if keep:
        parts.append("<must_keep>\n" + ", ".join(keep) + "\n</must_keep>")
    if avoid:
        parts.append("<leave_out>\n" + ", ".join(avoid) + "\n</leave_out>")
    parts.append(REVISE_INSTRUCTION)
    return "\n\n".join(parts)


# ------------------------------------------------------------------ --keep / --avoid


_SEG = re.compile(r"^([A-Za-z]\d+)\.(S\d+)$", re.IGNORECASE)
_CLIP = re.compile(r"^([A-Za-z]\d+)$")
_RANGE = re.compile(r"^([A-Za-z]\d+)@([\d.]+)-([\d.]+)$")


def split_ids(values: list[str]) -> list[str]:
    """`--keep V01.S003,V01.S004 --keep V07` -> ['V01.S003', 'V01.S004', 'V07']."""
    out = []
    for v in values:
        out += [t.strip().upper() for t in re.split(r"[,\s]+", v) if t.strip()]
    return list(dict.fromkeys(out))


def _span(token: str, analysis: Analysis | None) -> tuple[float, float] | None:
    """The seconds of its clip a line or range covers (None for a whole clip, or a line `analysis` can't find)."""
    if m := _RANGE.match(token):
        try:
            a, b = float(m.group(2)), float(m.group(3))
        except ValueError:
            return None
        return min(a, b), max(a, b)
    seg = analysis.segment(token) if analysis is not None and _SEG.match(token) else None
    return (seg.start, seg.end) if seg is not None else None


def _lifts(keep: str, avoid: str, analysis: Analysis | None = None) -> bool:
    """A --keep undoes an earlier --avoid only where they overlap: the same line, clip or range; a range of a
    kept clip; a range that a kept line (its times from `analysis`) or range overlaps.

    A line left out by name stays out until it's kept by name: keeping its clip doesn't bring it back.
    A line kept from a clip left out whole brings the whole clip back: a cut can't leave out all of a
    clip but one line (`wide_lifts` says so).
    """
    if keep == avoid:
        return True
    if _SEG.match(avoid) or re.split(r"[.@]", keep, maxsplit=1)[0] != re.split(r"[.@]", avoid, maxsplit=1)[0]:
        return False
    if _CLIP.match(keep):
        return True
    kept = _span(keep, analysis)
    if kept is None:  # a line that isn't in the footage
        return False
    if _CLIP.match(avoid):
        return True
    left_out = _span(avoid, analysis)
    return left_out is not None and kept[0] < left_out[1] and left_out[0] < kept[1]


def wide_lifts(avoids: list[str], keep: list[str], analysis: Analysis | None = None) -> list[str]:
    """Warnings for a kept line or range that brings back a whole clip an earlier --avoid left out."""
    return [
        f"--keep {k} brings back all of {a}, which an earlier --avoid left out (a cut can't leave out a clip "
        f"but one line of it); --avoid {a}@START-END leaves out the parts you still don't want"
        for a in avoids if _CLIP.match(a) for k in keep if k != a and _lifts(k, a, analysis)
    ]


def standing_avoids(history: list, keep: list[str] | None = None, *, analysis: Analysis | None = None) -> list[str]:
    """Every --avoid of the revisions in `history` (plan.json's notes_history, oldest first) still in force.

    What the editor said to leave out stays out of every later version and re-cut, until a later
    --keep (an entry's own, or `keep`: this run's) overlaps it (`_lifts`).
    """
    out: list[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        lifted = split_ids([str(k) for k in entry.get("keep") or []])
        out = [a for a in out if not any(_lifts(k, a, analysis) for k in lifted)]
        out += [a for a in split_ids([str(a) for a in entry.get("avoid") or []]) if a not in out]
    return [a for a in out if not any(_lifts(k, a, analysis) for k in keep or [])]


@dataclass
class _Rule:
    clip: str
    seg: str | None = None
    span: tuple[float, float] | None = None

    def hits(self, clip_id: str, seg_id: str | None, start: float, end: float) -> bool:
        if clip_id != self.clip:
            return False
        if self.seg:
            return seg_id == self.seg
        if self.span:
            a, b = self.span
            return start < b and end > a
        return True

    def hits_broll(self, clip_id: str, source_in: float) -> bool:
        """A cutaway from an avoided clip, or one that could start in an avoided range (auto picks might)."""
        if clip_id != self.clip or self.seg:
            return False
        if self.span:
            a, b = self.span
            return source_in < 0 or a <= source_in < b
        return True


@dataclass
class Enforced:
    warnings: list[str] = field(default_factory=list)
    missing_keeps: list[str] = field(default_factory=list)


def _rules(avoid: list[str], warnings: list[str]) -> list[_Rule]:
    rules = []
    for token in avoid:
        if m := _SEG.match(token):
            rules.append(_Rule(m.group(1).upper(), seg=f"{m.group(1).upper()}.{m.group(2).upper()}"))
        elif m := _RANGE.match(token):
            try:
                a, b = float(m.group(2)), float(m.group(3))
            except ValueError:
                warnings.append(f"--avoid {token}: can't read that range; ignored")
                continue
            rules.append(_Rule(m.group(1).upper(), span=(min(a, b), max(a, b))))
        elif m := _CLIP.match(token):
            rules.append(_Rule(m.group(1).upper()))
        else:
            warnings.append(f"--avoid {token}: not a segment, clip or CLIP@start-end range; ignored")
    return rules


def avoided_lines(avoid: list[str], analysis: Analysis) -> Callable[[str], bool] | None:
    """Is a segment left out by these --avoids? For `plan.normalize`: a take the editor left out never plays in
    place of another take of its line, and another take the plan has in the cut plays instead. None when nothing is avoided."""
    rules = _rules(avoid, [])
    if not rules:
        return None

    def hit(seg_id: str) -> bool:
        seg = analysis.segment(seg_id)
        return seg is not None and any(r.hits(seg.clip_id, seg_id, seg.start, seg.end) for r in rules)

    return hit


def _anchor(missing: str, previous: Plan | None, plan: Plan) -> str | None:
    """A segment in the new cut next to where `missing` was, for its to-do marker."""
    now = [i.seg_id for s in plan.sections for i in s.items if i.seg_id]
    if not now:
        return None
    before = [i.seg_id for s in previous.sections for i in s.items if i.seg_id] if previous else []
    if missing in before:
        k = before.index(missing)
        for seg_id in before[k + 1 :] + before[:k][::-1]:
            if seg_id in now:
                return seg_id
    return now[0]


def missing_keep_note(token: str) -> str:
    """The to-do marker's note for a --keep the revision left out."""
    return f"You asked to keep {token} (--keep), but the revision left it out."


def enforce(plan: Plan, analysis: Analysis, *, keep: list[str], avoid: list[str], previous: Plan | None = None) -> Enforced:
    """Apply --avoid to the revised plan and check --keep. Never raises."""
    result = Enforced()
    rules = _rules(avoid, result.warnings)
    if rules:
        for section in plan.sections:
            kept_items = []
            for item in section.items:
                if any(r.hits(item.clip_id, item.seg_id, item.start, item.end) for r in rules):
                    result.warnings.append(f"{item.ref} left out as asked (--avoid)")
                else:
                    kept_items.append(item)
            section.items = kept_items
            present = {i.seg_id for i in kept_items if i.seg_id}
            kept_broll = []
            for b in section.broll:
                if any(r.hits_broll(b.clip_id, b.source_in) for r in rules):
                    result.warnings.append(f"B-roll {b.clip_id} left out as asked (--avoid)")
                    continue
                b.over = [o for o in b.over if o in present]
                if b.over:
                    kept_broll.append(b)
            section.broll = kept_broll
        index = {}  # old section number -> new, for the music cues
        kept_sections = []
        for i, s in enumerate(plan.sections):
            if s.items:
                index[i] = len(kept_sections)
                kept_sections.append(s)
            else:
                result.warnings.append(f"section '{s.name}' was left empty by --avoid; dropped")
        plan.sections = kept_sections
        music = []
        for cue in plan.music:
            mapped = [index[i] for i in range(cue.first_section, cue.last_section + 1) if i in index]
            if mapped:
                cue.first_section, cue.last_section = min(mapped), max(mapped)
                music.append(cue)
        plan.music = music
        used = {i.seg_id for s in plan.sections for i in s.items if i.seg_id}
        plan.graphics = [g for g in plan.graphics if g.segment in used]

    in_cut = {i.seg_id for s in plan.sections for i in s.items if i.seg_id}
    clips_in_cut = {i.clip_id for s in plan.sections for i in s.items} | {b.clip_id for s in plan.sections for b in s.broll}
    # A take kept with the chosen take of its line: the line plays, once, as that one (plan.normalize says so).
    played_as = {o for alt in plan.alternates if alt["chosen"] in in_cut and alt["chosen"] in keep for o in alt["others"]}
    for token in keep:
        present = token in in_cut or token in played_as if _SEG.match(token) else token in clips_in_cut
        known = analysis.segment(token) is not None if _SEG.match(token) else analysis.clip(token) is not None
        if not known:
            result.warnings.append(f"--keep {token}: no such segment or clip in this footage")
        elif not present:
            result.missing_keeps.append(token)
            # The marker goes where the line was (it can't sit on something that isn't in the cut).
            anchor = _anchor(token, previous, plan) or token
            plan.flags.append({"ref": anchor, "note": missing_keep_note(token)})
    return result


# ------------------------------------------------------------------ what changed


LISTED = 10  # lines or shots named one by one; the rest are counted
CHANGE_NOTES = ("Pacing:", "Punch-ins:", "Redesigning the graphics")  # settings notes that say something changed (the rest only explain)


def _refs(plan: Plan) -> list[str]:
    """Every storyline item: segment IDs and visual ranges like V07@12.5-16."""
    return [i.ref for s in plan.sections for i in s.items]


def _length_item(before: float, after: float, *, tenths: bool = False) -> str:
    """'Length: 0:15 → 0:12 (−0:03)'; `tenths`: with tenths of a second when the change is under a second."""
    delta = after - before
    sign = "+" if delta >= 0 else "−"
    millis = tenths and abs(delta) < 1
    return f"Length: {seconds_to_clock(before, millis=millis)} → {seconds_to_clock(after, millis=millis)} ({sign}{seconds_to_clock(abs(delta), millis=millis)})"


def _quoted(text: str) -> str:
    return '"' + _snippet(text, 50) + '"'


RETIME = 0.3  # seconds: a graphic held this much longer or shorter than before is re-timed (its design is kept)


def _held(spec) -> float:
    """How long a graphic holds, as the plan asks (a plan that left it out holds its kind's usual time)."""
    from .plan import DEFAULT_SECONDS

    return float(spec.seconds) if spec.seconds and spec.seconds > 0 else DEFAULT_SECONDS.get(spec.kind, 4.0)


def _graphic_changes(
    previous: list, revised: list, *, redesigned: bool, in_cut: set[int] | None = None, fell_back: set[int] | frozenset[int] = frozenset(),
    placed: set[int] | None = None,
) -> tuple[list[str], list[str]]:
    """(what changed, what stayed) about the graphics: kept / redesigned / new / removed, with a changed text or direction in words.

    `redesigned`: every graphic is designed again (new references, notes asking for another look, --redesign-graphics).
    A graphic is "kept" when everything its design is made from is the same (plan.spec_key): the earlier design is reused,
    and one held for another time is re-timed (RETIME). What stayed is only said (as one line) when nothing about the graphics changed.

    What the run did, once its graphics are made: `in_cut` holds the plan indexes that made it into the cut (None before that:
    every graphic is taken to), and `fell_back` the ones whose new design never came back and that kept their earlier one.
    A graphic this version meant to make or change that isn't in the cut isn't counted as made: it could not be made when it had
    room (`placed`: the plan indexes the cut had room for; None: not known, every graphic is taken to have had), and had no room
    when it was never placed, which is no failure.
    """
    from .plan import spec_key

    left = list(previous)
    found: list[list] = []  # [index in the revised plan, revised spec, the earlier one it continues or None, the design's inputs are the same]
    for n, r in enumerate(revised):
        i = next((i for i, p in enumerate(left) if spec_key(p) == spec_key(r)), None)
        found.append([n, r, left.pop(i) if i is not None else None, i is not None])
    for entry in found:  # then: the same moment (kind, line, words) with its text or direction changed
        if entry[2] is None:
            r = entry[1]
            i = next((i for i, p in enumerate(left) if (p.kind, p.segment, p.words.lower().split()) == (r.kind, r.segment, r.words.lower().split())), None)
            if i is not None:
                entry[2], entry[3] = left.pop(i), False
    same = changed = new = retimed = 0
    stale = {"kept": 0, "left out": 0, "no room": 0}  # meant to be made or redesigned, and wasn't
    detail: list[str] = []
    for n, r, was, is_same in found:
        meant = redesigned or was is None or not is_same
        why = "kept" if n in fell_back else ""
        if not why and in_cut is not None and n not in in_cut and meant:
            why = "no room" if placed is not None and n not in placed else "left out"
        if why:
            stale[why] += 1
            detail.append(
                f"Graphic {_quoted(r.text)}: no new design came back, so it keeps its earlier one" if why == "kept"
                else f"Graphic {_quoted(r.text)}: had no room in this cut (see Warnings)" if why == "no room"
                else f"Graphic {_quoted(r.text)}: could not be made, so it is not in this version"
            )
        elif was is None:
            new += 1
            detail.append(f"New graphic: {_quoted(r.text)} ({r.kind.replace('_', ' ')})")
        elif not is_same:
            changed += 1
            said = []
            if was.text.strip() != r.text.strip():
                said.append(f"text now {_quoted(r.text)}")
            if was.subtext.strip() != r.subtext.strip():
                said.append(f"subtext now {_quoted(r.subtext)}" if r.subtext.strip() else "subtext removed")
            if " ".join(was.direction.lower().split()) != " ".join(r.direction.lower().split()):
                said.append(f"direction now {_quoted(r.direction)}" if r.direction.strip() else "direction removed")
            detail.append(f"Graphic {_quoted(was.text)}: {'; '.join(said) or 'changed'}; designed again")
        elif not redesigned and abs(_held(r) - _held(was)) >= RETIME:
            retimed += 1
            detail.append(f"Graphic {_quoted(r.text)}: now holds {_held(r):g} s (was {_held(was):g} s); design kept")
        else:
            same += 1
    detail += [f"Removed graphic: {_quoted(was.text)}" for was in left]
    unmade = sum(stale.values())
    if not (changed or new or left or retimed or unmade or (redesigned and same)):
        return [], ([f"Graphics: {same} kept as they were"] if same else [])
    parts = []
    if redesigned:
        if same + changed:
            parts.append(f"{same + changed} redesigned")
    else:
        if same:
            parts.append(f"{same} kept as they were")
        if retimed:
            parts.append(f"{retimed} kept with another length")
        if changed:
            parts.append(f"{changed} changed and designed again")
    if new:
        parts.append(f"{new} new")
    if stale["kept"]:
        parts.append(f"{stale['kept']} got no new design and {'keeps its' if stale['kept'] == 1 else 'keep their'} earlier one")
    if stale["left out"]:
        parts.append(f"{stale['left out']} could not be made and {'is' if stale['left out'] == 1 else 'are'} not in this version")
    if stale["no room"]:
        parts.append(f"{stale['no room']} had no room in this cut")
    if left:
        parts.append(f"{len(left)} removed")
    summary = "Graphics: " + ", ".join(parts)
    if unmade:
        summary += " (see Warnings)"
    elif redesigned and same + changed:
        summary += " (every graphic is designed again)"
    more = [f"…and {len(detail) - LISTED} more graphic changes"] if len(detail) > LISTED else []
    return [summary] + detail[:LISTED] + more, []


def _spelling_changes(previous: Plan, revised: Plan) -> list[str]:
    from .plan import spelling_key

    def pairs(plan: Plan) -> dict[str, tuple[str, str]]:
        return {spelling_key(f["heard"]): (f["heard"], f["correct"]) for f in plan.spellings if isinstance(f, dict) and f.get("heard") and f.get("correct")}

    old, new = pairs(previous), pairs(revised)
    out = []
    for key, (heard, correct) in new.items():
        if key not in old:
            out.append(f'Spelling: "{heard}" is written "{correct}"')
        elif old[key][1] != correct:
            out.append(f'Spelling changed: "{heard}" was "{old[key][1]}", now "{correct}"')
    for key, (heard, correct) in old.items():
        if key not in new:
            out.append(f'Spelling undone: "{heard}" is no longer written "{correct}"')
    return out


def _level_text(value, unit: str) -> str:
    return "as recorded" if value is None else f"{value:g} {unit}"


def _on_off(value) -> str:
    return "on" if value else "off"


def setting_deltas(was: dict, now: dict, skip: set[str] | frozenset = frozenset()) -> list[str]:
    """What a cut setting changed from the version before (`skip`: ones a note already says): pacing, punch-ins,
    format, fillers, and the render settings that change how it sounds or what it covers (levels, fades, picture
    over narration, the stringout project).

    `was`: the earlier version's saved settings (plan.json); a setting it doesn't have is unknown, not different.
    `now`: the same settings as this version has them (pipeline.RENDER_FIELDS; a setting `now` leaves out isn't compared).
    """
    out = []
    old, new = was.get("style"), now.get("style")
    if "style" not in skip and old and new and old != new:
        out.append(f"Pacing: {old} → {new}")
    if "punch_in" not in skip and "punch_in" in was and bool(was.get("punch_in")) != bool(now.get("punch_in")):
        out.append(f"Punch-ins: {'off → on' if now.get('punch_in') else 'on → off'}")
    if "format" in was and (was.get("format") or None) != (now.get("format") or None):
        out.append(f"Format: {was.get('format') or 'from the footage'} → {now.get('format') or 'from the footage'}")
    if "keep_fillers" in was and bool(was.get("keep_fillers")) != bool(now.get("keep_fillers")):
        out.append("Fillers (um, uh): " + ("cut → kept" if now.get("keep_fillers") else "kept → cut"))

    def differs(key: str) -> bool:
        return key in was and key in now and was[key] != now[key]

    for key, label, unit in (("dialogue_level", "Dialogue level", "LUFS"), ("broll_db", "B-roll sound", "dB"), ("music_db", "Music level", "dB")):
        if differs(key):
            out.append(f"{label}: {_level_text(was[key], unit)} → {_level_text(now[key], unit)}")
    for key, label in (("fades", "Fades"), ("fill", "Picture over bare narration"), ("stringout", "Stringout project")):
        if key in was and key in now and bool(was[key]) != bool(now[key]):
            out.append(f"{label}: {_on_off(was[key])} → {_on_off(now[key])}")
    return out


def _noted(notes: list[str]) -> set[str]:
    """The cut settings a settings note already says."""
    return {key for key, lead in (("style", "Pacing:"), ("punch_in", "Punch-ins:")) if any(n.startswith(lead) for n in notes)}


def change_items(
    previous: Plan,
    revised: Plan,
    analysis: Analysis,
    *,
    before_seconds: float | None,
    after_seconds: float,
    previous_version: int | None,
    graphics_on: bool = True,
    redesigned: bool = False,
    settings_notes: list[str] | None = None,
    was: dict | None = None,
    now: dict | None = None,
    outcome: dict | None = None,
) -> tuple[list[str], bool]:
    """What a revision changed, as plain sentences, and whether it changed nothing a viewer would see: (items, unchanged).

    The paper edit's revision block and the app's list of changes are both these. `settings_notes`: what the notes did to the
    cut's settings (settings_from_notes), listed too; `was`/`now` (setting_deltas) add settings a flag changed.
    `outcome`: once the graphics are made, {"in_cut": plan indexes that made it into the cut, "fell_back": plan indexes whose
    new design never came back and that kept the earlier one, "placed": plan indexes the cut had room for} (_graphic_changes),
    so the list never claims a graphic that isn't there, and never calls one that had no room a failure.
    """
    notes = list(settings_notes or [])
    plan_changes: list[str] = []
    old, new = _refs(previous), _refs(revised)
    removed = [r for r in old if r not in set(new)]
    added = [r for r in new if r not in set(old)]

    def said(ref: str) -> str:
        if "@" in ref:
            return f"{ref} (picture)"
        seg = analysis.segment(ref)
        return f'{ref} "{_snippet(seg.text, 70)}"' if seg is not None and seg.text else ref

    for verb, refs in (("Removed", removed), ("Added", added)):
        plan_changes += [f"{verb} {said(r)}" for r in refs[:LISTED]]
        if len(refs) > LISTED:
            plan_changes.append(f"…and {len(refs) - LISTED} more lines {verb.lower()}")
    if [r for r in old if r in set(new)] != [r for r in new if r in set(old)]:
        plan_changes.append("Reordered: lines that stayed are in a different order")
    old_names, new_names = [s.name for s in previous.sections], [s.name for s in revised.sections]
    gone = [n for n in old_names if n not in new_names]
    came = [n for n in new_names if n not in old_names]
    if gone:
        plan_changes.append("Sections removed: " + ", ".join(f'"{n}"' for n in gone))
    if came:
        plan_changes.append("Sections added: " + ", ".join(f'"{n}"' for n in came))
    b_old = sum(len(s.broll) for s in previous.sections)
    b_new = sum(len(s.broll) for s in revised.sections)

    def start_of(clip_id: str, source_in: float) -> float:
        """Where in its footage a shot starts: -1 when the software picks (a picture has no start)."""
        clip = analysis.clip(clip_id)
        return -1.0 if clip is not None and clip.media.kind == "image" else round(source_in, 1)

    shots = lambda plan: {  # noqa: E731
        (b.clip_id, tuple(b.over), " ".join(b.words.lower().split()), start_of(b.clip_id, b.source_in)) for s in plan.sections for b in s.broll
    }
    if b_old != b_new:
        plan_changes.append(f"B-roll shots: {b_old} → {b_new}")
    elif shots(previous) != shots(revised):
        plan_changes.append(
            f"B-roll: {len(shots(previous) - shots(revised))} of {b_new} shots changed (other footage, other lines or words, or another part of the footage)"
        )
    songs = lambda plan: [analysis.clip(c.clip_id).media.name if analysis.clip(c.clip_id) else c.clip_id for c in plan.music]  # noqa: E731
    if songs(previous) != songs(revised):
        plan_changes.append("Music: " + (", ".join(songs(previous)) or "none") + " → " + (", ".join(songs(revised)) or "none"))
    elif [(c.first_section, c.last_section) for c in previous.music] != [(c.first_section, c.last_section) for c in revised.music]:
        plan_changes.append("Music runs under other sections")
    else:
        # Where in the song it starts, for a cue that ends before the cut does (the one that runs to the end is back-timed
        # from the song's ending, whatever it says).
        starts = lambda plan: [round(c.source_in, 1) if c.last_section < len(plan.sections) - 1 else None for c in plan.music]  # noqa: E731
        if starts(previous) != starts(revised):
            plan_changes.append("Music starts at another point in the song")
    plan_changes += _spelling_changes(previous, revised)
    gfx_changes, gfx_same = (
        _graphic_changes(
            previous.graphics, revised.graphics, redesigned=redesigned,
            in_cut=(outcome or {}).get("in_cut"), fell_back=(outcome or {}).get("fell_back") or frozenset(), placed=(outcome or {}).get("placed"),
        )
        if graphics_on else ([], [])
    )
    if not graphics_on and previous.graphics:
        gfx_changes = [f"Motion graphics are off: the {len(previous.graphics)} graphics of {'v' + str(previous_version) if previous_version else 'the previous cut'} are not in this version"]
    plan_changes += gfx_changes

    skip = _noted(notes)
    deltas = setting_deltas(was or {}, now or {}, skip) if was is not None and now is not None else []
    said_changes = [n for n in notes if n.startswith(CHANGE_NOTES)]
    advice = [n for n in notes if not n.startswith(CHANGE_NOTES)]
    unchanged = not (plan_changes or deltas or said_changes)
    version = f"v{previous_version}" if previous_version else "the previous cut"
    if unchanged:
        if not graphics_on:
            tail = "Motion graphics are off for this cut: turn them on to add or change them."
            body = f"This version plays the same lines and B-roll as {version}. "
        elif previous.graphics or revised.graphics:
            tail = (
                "If you meant how the graphics look, attach reference images or turn on Redesign the graphics "
                "(Terminal: --graphics-ref IMAGE or --redesign-graphics)."
            )
            body = f"This version plays the same lines, B-roll and graphics as {version}. "
        else:
            told = any("no graphics" in n for n in advice)  # the notes already say it
            tail = "" if told else "This cut has no graphics: ask for them in your notes, for example a lower third when someone is named."
            body = f"This version plays the same lines and B-roll as {version}." + ("" if told else " ")
        return [body + tail] + advice, True  # what to try comes first: it is the line the app highlights
    longer = before_seconds is not None and abs(after_seconds - before_seconds) >= 0.05
    items = [_length_item(before_seconds, after_seconds)] if longer else []
    return items + notes + deltas + plan_changes + ([] if gfx_changes else gfx_same), False


_BOLD_REF = re.compile(r"^(Removed|Added) (\S+)")


def changes_lines(items: list[str], previous_version: int | None, *, skip: list[str] | None = None) -> list[str]:
    """Markdown for the paper edit: the items as a list under a "Changes from vN:" heading (`skip`: ones said elsewhere)."""
    head = f"Changes from v{previous_version}:" if previous_version else "Changes from the previous cut:"
    lines = [_BOLD_REF.sub(r"\1 **\2**", item) for item in items if item not in (skip or [])]
    return [head, ""] + [f"- {line}" for line in lines]


def changes_record(items: list[str], unchanged: bool, from_version: int | None) -> dict:
    """result_summary's "changes": what the app lists after a revision or re-cut."""
    return {"from_version": from_version, "items": items, "unchanged": unchanged}


def describe_changes(previous: Plan, revised: Plan, analysis: Analysis, *, before_seconds: float | None, after_seconds: float, previous_version: int | None, **more) -> list[str]:
    """A deterministic list of what the revision changed, for the paper edit (markdown)."""
    items, _ = change_items(
        previous, revised, analysis, before_seconds=before_seconds, after_seconds=after_seconds, previous_version=previous_version, **more
    )
    return changes_lines(items, previous_version)


_GRAPHIC_CLIP = re.compile(r"^G\d+-(.+?)(?:-[0-9a-f]{6})?\.mov$")  # G01-lower-third-1a2b3c.mov (the digest is missing from older names)


def new_graphics(made: set[str], known: list[str]) -> tuple[int, int]:
    """(added, rendered again): of the graphics clips a re-cut has that the version before didn't, how many are on the timeline
    for the first time and how many take the place of a clip that version had.

    A clip takes the place of one when that version had a clip of the same kind that this cut no longer uses (a design edited
    by hand, or timing that moved: both make a new file); any other is added, as all are when the earlier version had none
    (a redesign that stopped before its designs came back, finished later with `render --design-missing`).
    """
    kind = lambda name: m.group(1) if (m := _GRAPHIC_CLIP.match(name)) else name  # noqa: E731
    gone = Counter(kind(n) for n in known if n not in made)
    added = again = 0
    for name in sorted(made - set(known)):
        if gone[kind(name)] > 0:
            gone[kind(name)] -= 1
            again += 1
        else:
            added += 1
    return added, again


def recut_items(
    was: dict, now: dict, previous_version: int | None, *, before: dict, after: dict, rerendered: int | None = None, added: int | None = None,
) -> tuple[list[str], bool]:
    """What a re-cut at other settings changed from the version before, in numbers: (items, unchanged).

    `was`/`now`: the settings (setting_deltas). `before`/`after`: seconds, edits and broll of the two cuts (any may be missing
    for a folder from before they were saved). `rerendered`: graphics clips this cut has in the place of ones the earlier one
    had; `added`: graphics clips it has that are new to the cut (new_graphics).
    """
    deltas = setting_deltas(was, now)
    if not deltas and not was.get("style") and before.get("seconds") is None and before.get("edits") is None:
        return ["The earlier version didn't save its settings or its length, so there is nothing to compare this re-cut with."], False
    numbers = []
    if before.get("seconds") is not None and abs(after["seconds"] - before["seconds"]) >= 0.05:
        numbers.append(_length_item(before["seconds"], after["seconds"], tenths=True))
    for key, label in (("edits", "Edits"), ("broll", "B-roll shots")):
        if before.get(key) is not None and before[key] != after[key]:
            numbers.append(f"{label}: {before[key]} → {after[key]}")
    if added:
        numbers.append(f"Graphics: {added} added to the cut")
    if rerendered:
        numbers.append(f"Graphics: {rerendered} {'was' if rerendered == 1 else 'were'} rendered again (a design you edited, or timing that moved)")
    if not (deltas or numbers):
        version = f"v{previous_version}" if previous_version else "the previous cut"
        return [
            f"This is the same cut as {version}: its settings are the same, so it plays exactly as {version} does. "
            "To change it, pick another pacing, format or punch-in setting, or revise it with notes."
        ], True
    if deltas and not numbers:
        why = []
        if any(d.startswith("Punch-ins") for d in deltas):
            why.append("punch-ins only change the framing")
        if any(d.startswith(("Dialogue level", "B-roll sound", "Music level", "Fades")) for d in deltas):
            why.append("levels and fades only change how it sounds")
        numbers.append("Length, edits and B-roll came out the same" + (f" ({'; '.join(why)})" if why else ""))
    return deltas + numbers, False


def report_block(notes: str, earlier: list[str], changes: list[str], missing_keeps: list[str], settings_notes: list[str] | None = None) -> list[str]:
    """Markdown for the top of the paper edit. `settings_notes`: the pacing and punch-ins the notes changed (or a flag
    kept), and graphics they asked for while they're off."""
    L = ["## Revision notes", ""]
    L += [f"> {line}" if line.strip() else ">" for line in (notes.strip() or "(no notes)").splitlines()] + [""]
    for n in settings_notes or []:
        L += [f"**{n}**", ""]
    if missing_keeps:
        one = len(missing_keeps) == 1
        L += [f"**You asked to keep {', '.join(missing_keeps)}, but the revision left {'it' if one else 'them'} out.** "
              + ("It's a to-do marker in Final Cut." if one else "Each one is a to-do marker in Final Cut."), ""]
    L += changes + [""]
    if earlier:
        L += ["Earlier notes, still applied:", ""] + [f"- {n}" for n in earlier] + [""]
    return L


def recover_brief(prompt_md: str) -> dict:
    """Brief settings from prompt.md, for output folders whose plan.json has no saved settings."""
    if "# Brief\n" not in prompt_md:
        return {}
    brief = prompt_md.rsplit("# Brief\n", 1)[1]
    out: dict = {}
    if m := re.search(r"<brief>\n(.*?)\n</brief>", brief, re.S):
        text = m.group(1).strip()
        out["context"] = "" if text.startswith("No brief given.") else text
    if m := re.search(r"<target_length>about .*?\((\d+) seconds\)</target_length>", brief):
        out["target_seconds"] = float(m.group(1))
    if m := re.search(r"<script>\n.*?where possible\.\n\n(.*?)\n</script>", brief, re.S):
        out["script"] = m.group(1)
    out["graphics"] = "<motion_graphics>" in brief
    if m := re.search(r"<graphics_style>\n(.*?)\n</graphics_style>", brief, re.S):
        out["graphics_style"] = m.group(1).strip()
    return out

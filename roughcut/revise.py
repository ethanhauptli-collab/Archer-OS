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
from dataclasses import dataclass, field

from .analyze import Analysis
from .plan import Plan
from .prompts import render_brief
from .timecode import seconds_to_clock

REVISE_INSTRUCTION = """\
This cut has already been made, and the editor watched it in Final Cut Pro. Revise the previous plan \
according to the editor's notes. Change only what the notes ask for. Keep everything else exactly as \
it was: the same sections, segments and order, B-roll, music, alternates, flags, graphics and spellings, \
unless a note says otherwise or a change forces it. Spellings you leave out still apply; to undo one the \
notes say is wrong, return it with "correct" the same as "heard". Earlier notes still apply. Return the \
complete revised plan in the same format, and in cut_notes say briefly what you changed for these notes."""


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
        L.append(f'Graphic: {g.kind} on {g.segment} at "{g.words}": "{g.text}"' + (f' / "{g.subtext}"' if g.subtext else ""))
    for alt in plan.alternates:
        L.append(f"Alternate: {alt['chosen']} chosen over {', '.join(alt['others']) or 'n/a'}")
    for flag in plan.flags:
        L.append(f"Flag: {flag['ref']}: {flag['note']}")
    for fix in plan.spellings:
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
) -> str:
    """The one non-cached part of a revision request (the media part is the build's, byte for byte)."""
    brief = render_brief(context, target_seconds, script, graphics=graphics, graphics_style=graphics_style)
    brief = brief.removesuffix("Build the rough cut plan.").rstrip()
    parts = [brief, "<previous_plan>\n" + render_previous_plan(previous, analysis) + "\n</previous_plan>"]
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


def _lifts(keep: str, avoid: str) -> bool:
    """A --keep undoes an earlier --avoid of the same line, or of the clip (or the part of a clip) it's from.

    A line left out by name stays out until it's kept by name: keeping its clip doesn't bring it back.
    """
    if keep == avoid:
        return True
    if _SEG.match(avoid):
        return False
    return re.split(r"[.@]", keep, maxsplit=1)[0] == re.split(r"[.@]", avoid, maxsplit=1)[0]


def standing_avoids(history: list, keep: list[str] | None = None) -> list[str]:
    """Every --avoid of the revisions in `history` (plan.json's notes_history, oldest first) still in force.

    What the editor said to leave out stays out of every later version and re-cut, until a later
    --keep (an entry's own, or `keep`: this run's) names it again.
    """
    out: list[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        lifted = split_ids([str(k) for k in entry.get("keep") or []])
        out = [a for a in out if not any(_lifts(k, a) for k in lifted)]
        out += [a for a in split_ids([str(a) for a in entry.get("avoid") or []]) if a not in out]
    return [a for a in out if not any(_lifts(k, a) for k in keep or [])]


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
    for token in keep:
        present = token in in_cut if _SEG.match(token) else token in clips_in_cut
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


def _refs(plan: Plan) -> list[str]:
    """Every storyline item: segment IDs and visual ranges like V07@12.5-16."""
    return [i.ref for s in plan.sections for i in s.items]


def describe_changes(previous: Plan, revised: Plan, analysis: Analysis, *, before_seconds: float | None, after_seconds: float, previous_version: int | None) -> list[str]:
    """A deterministic list of what the revision changed, for the paper edit."""
    L = []
    if before_seconds is not None:
        delta = after_seconds - before_seconds
        sign = "+" if delta >= 0 else "−"
        L.append(f"- Length: {seconds_to_clock(before_seconds)} → {seconds_to_clock(after_seconds)} ({sign}{seconds_to_clock(abs(delta))})")
    old, new = _refs(previous), _refs(revised)
    removed = [s for s in old if s not in set(new)]
    added = [s for s in new if s not in set(old)]

    def said(ref: str) -> str:
        if "@" in ref:
            return f"**{ref}** (picture)"
        seg = analysis.segment(ref)
        return f'**{ref}** "{_snippet(seg.text, 70)}"' if seg is not None and seg.text else f"**{ref}**"

    for seg_id in removed:
        L.append(f"- Removed {said(seg_id)}")
    for seg_id in added:
        L.append(f"- Added {said(seg_id)}")
    common_old = [s for s in old if s in set(new)]
    common_new = [s for s in new if s in set(old)]
    if common_old != common_new:
        L.append("- Reordered: lines that stayed are in a different order")
    old_names, new_names = [s.name for s in previous.sections], [s.name for s in revised.sections]
    gone = [n for n in old_names if n not in new_names]
    came = [n for n in new_names if n not in old_names]
    if gone:
        L.append("- Sections removed: " + ", ".join(f'"{n}"' for n in gone))
    if came:
        L.append("- Sections added: " + ", ".join(f'"{n}"' for n in came))
    b_old = sum(len(s.broll) for s in previous.sections)
    b_new = sum(len(s.broll) for s in revised.sections)
    if b_old != b_new:
        L.append(f"- B-roll shots: {b_old} → {b_new}")
    if len(previous.graphics) != len(revised.graphics):
        L.append(f"- Graphics: {len(previous.graphics)} → {len(revised.graphics)}")
    if len(previous.music) != len(revised.music):
        L.append(f"- Music cues: {len(previous.music)} → {len(revised.music)}")
    if len(L) == (1 if before_seconds is not None else 0):
        L.append("- No lines added or removed")
    head = f"Changes from v{previous_version}:" if previous_version else "Changes from the previous cut:"
    return [head, ""] + L


def report_block(notes: str, earlier: list[str], changes: list[str], missing_keeps: list[str]) -> list[str]:
    """Markdown for the top of the paper edit."""
    L = ["## Revision notes", ""]
    L += [f"> {line}" if line.strip() else ">" for line in (notes.strip() or "(no notes)").splitlines()] + [""]
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

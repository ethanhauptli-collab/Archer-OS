"""A readable paper edit to go with the FCPXML."""

from __future__ import annotations

import re

from .analyze import ROLE_LABELS, Analysis
from .plan import Plan
from .providers import UsageLog, estimate_cost
from .timecode import seconds_to_clock
from .timeline import Timeline, dead_air


def _tc(frames: int, tl: Timeline) -> str:
    return seconds_to_clock(float(frames * tl.frame_duration))


def collapse_warnings(warnings: list[str]) -> list[str]:
    """Merge warnings that differ only in clip IDs ("visual log failed for V01, I002: X" x 46)."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for w in warnings:
        ids = re.findall(r"\b[VAI]\d{2,}(?:\.S\d+)?\b", w)
        key = re.sub(r"\b[VAI]\d{2,}(?:\.S\d+)?\b(?:,\s*)?", "", w)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((w, ids))
    out = []
    for key in order:
        items = groups[key]
        if len(items) == 1:
            out.append(items[0][0])
        else:
            ids = [i for _, found in items for i in found]
            shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
            out.append(f"{items[0][0]}  (x{len(items)}: {shown})")
    return out


def build_report(
    plan: Plan,
    tl: Timeline,
    analysis: Analysis,
    *,
    stringout: Timeline | None = None,
    usage: list[UsageLog] | None = None,
    timings: dict[str, float] | None = None,
    extra_warnings: list[str] | None = None,
    graphics: list | None = None,
) -> str:
    raw = sum(float(c.media.duration) for c in analysis.clips if c.media.kind != "image" and c.role != "graphic")
    speech = sum(c.speech_seconds for c in analysis.clips if c.has_transcript)
    L = [f"# {plan.title}", ""]
    failures = [w for w in (extra_warnings or []) if "planning failed" in w or "no usable sections" in w]
    if failures:
        L += ["> **The AI didn't plan this cut.** It's the stringout: all speech in recording order with dead air removed, no B-roll.", ">"]
        L += [f"> {w}" for w in failures] + [""]
    if plan.logline:
        L += [f"_{plan.logline}_", ""]
    L += [
        f"- **Rough cut:** {seconds_to_clock(tl.seconds)} ({tl.width}x{tl.height}, {tl.frame_duration.denominator / tl.frame_duration.numerator:.3f} fps)",
        f"- **Raw media:** {seconds_to_clock(raw)} across {sum(1 for c in analysis.clips if c.role != 'graphic')} clips; {seconds_to_clock(speech)} of transcribed speech",
    ]
    if stringout is not None and stringout.spine:
        L.append(f"- **Stringout** (all dialogue, dead air removed): {seconds_to_clock(stringout.seconds)}")
        talk, kept = dead_air(analysis.clips, stringout)
        if talk > 0:
            L.append(f"- **Dead air removed:** {seconds_to_clock(max(0.0, talk - kept))} of {seconds_to_clock(talk)} talking footage ({max(0.0, 1 - kept / talk):.0%})")
    L.append(f"- **Planned by:** {plan.source}")
    if usage:
        calls = [c for u in usage for c in u.calls]
        if calls:
            tokens_in = sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in calls)
            tokens_out = sum(c.output_tokens for c in calls)
            cost = sum(filter(None, (estimate_cost(u) for u in usage)))
            cost_txt = f", about ${cost:.2f}" if cost else ""
            L.append(f"- **Model usage:** {len(calls)} calls, {tokens_in:,} input / {tokens_out:,} output tokens{cost_txt}")
    if timings:
        L.append("- **Processing:** " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()))
    L.append("")
    notes = list(analysis.notes) + list(tl.notes)
    if notes:
        L += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]

    if len(tl.section_starts) > 1:
        L += ["## Chapters", "", "```"]
        for frame, name in tl.section_starts:
            L.append(f"{_tc(frame, tl)} {name}")
        L += ["```", ""]

    L += ["## Paper edit", ""]
    broll_by_start: dict[int, list] = {}
    for cc in tl.connected:
        broll_by_start.setdefault(cc.start, []).append(cc)
    for s_idx, section in enumerate(plan.sections):
        start = tl.section_frames.get(s_idx)
        head = f"### {section.name}" + (f"  `{_tc(start, tl)}`" if start is not None else "")
        L += [head, ""]
        if section.purpose:
            L += [f"_{section.purpose}_", ""]
        for item in section.items:
            clip = analysis.clip(item.clip_id)
            if item.seg_id:
                seg = analysis.segment(item.seg_id)
                rng = tl.seg_ranges.get(item.seg_id)
                at = f"`{_tc(rng[0], tl)}` " if rng else ""
                text = seg.text or "(no transcript)"
                L.append(f"- {at}**{item.seg_id}** {text}  ")
                L.append(f"  <sub>{clip.media.name} @ {seconds_to_clock(seg.start, millis=True)}</sub>")
            else:
                L.append(f"- **{item.ref}** visual: {clip.media.name} ({(clip.visual or {}).get('description', '')[:80]})")
        for b in section.broll:
            clip = analysis.clip(b.clip_id)
            L.append(f"  - B-roll **{b.clip_id}** ({clip.media.name}) over {', '.join(b.over)}: {b.reason}")
        L.append("")

    if plan.music:
        L += ["## Music", ""]
        for cue in plan.music:
            clip = analysis.clip(cue.clip_id)
            L.append(f"- {clip.media.name}: sections {cue.first_section + 1}-{cue.last_section + 1}")
        L.append("")

    if plan.alternates:
        L += ["## Alternate takes", ""]
        for alt in plan.alternates:
            L.append(f"- **{alt['chosen']}** chosen over {', '.join(alt['others']) or 'n/a'}. {alt.get('note', '')}")
        L.append("")

    if plan.flags:
        L += ["## Check these", ""]
        for flag in plan.flags:
            L.append(f"- [ ] {flag['ref']}: {flag['note']}")
        L.append("")

    if plan.cut_notes:
        L += ["## What was left out", "", plan.cut_notes, ""]

    if graphics:
        rendered = {c.id: c for c in analysis.clips if c.role == "graphic"}
        L += ["## Motion graphics", "", "Rendered clips and their HTML designs are in `graphics/`. Edit a design and run `roughcut render` to re-render just that one.", ""]
        L += ["| Time | ID | Kind | On screen | File |", "|---|---|---|---|---|"]
        for g in graphics:
            text = g.spec.text + (f" / {g.spec.subtext}" if g.spec.subtext else "")
            clip = rendered.get(g.id)
            file = f"`graphics/{clip.media.name}`" if clip else "not rendered (see Warnings)"
            L.append(f"| `{_tc(g.start, tl)}` | {g.id} | {g.spec.kind.replace('_', ' ')} | {text.replace('|', '/')} | {file} |")
        L.append("")

    L += ["## Media", "", "| ID | File | Role | Length | Notes |", "|---|---|---|---|---|"]
    for c in analysis.clips:
        if c.role == "graphic":
            continue  # listed under Motion graphics
        length = "still" if c.media.kind == "image" else seconds_to_clock(float(c.media.duration))
        desc = (c.visual or {}).get("description", "")
        if not desc and c.has_transcript and c.segments:
            desc = c.segments[0].text
        desc = desc.replace("|", "/")[:90]
        L.append(f"| {c.id} | {c.media.name} | {ROLE_LABELS.get(c.role, c.role)} | {length} | {desc} |")
    L.append("")

    warnings = collapse_warnings(list(analysis.warnings) + list(plan.warnings) + list(tl.warnings) + list(extra_warnings or []))
    if warnings:
        L += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    return "\n".join(L)

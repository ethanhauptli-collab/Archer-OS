"""A readable paper edit to go with the FCPXML."""

from __future__ import annotations

import re

from .analyze import ROLE_LABELS, Analysis
from .media import frame_rate_warnings, vfr_warning_covered
from .plan import Plan
from .providers import UsageLog, cost_breakdown, describe_cost
from .timecode import seconds_to_clock
from .timeline import Timeline, dead_air


def _tc(frames: int, tl: Timeline) -> str:
    return seconds_to_clock(float(frames * tl.frame_duration))


# Warnings about what the brief or the revise notes asked for that this cut doesn't do: also at the top
# of the report and at the end of the CLI's output, not only in the Warnings.
HEADLINE_WARNINGS = ("The brief asks for graphics", "Your notes ask for", "The output folder is inside the Venture Studio library")


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


def levels_section(tl: Timeline, analysis: Analysis) -> list[str]:
    """What level matching measured and the clip volume it set for each source."""
    if tl.dialogue_level is None:
        return []
    L = [
        "## Levels",
        "",
        f"Speech is matched to about {tl.dialogue_level:g} LUFS, B-roll and music sit under it. Volume is each clip's "
        "Volume in Final Cut's audio inspector; change it there, or set `--dialogue-level` and run `roughcut render`.",
        "",
    ]
    if tl.levels:
        L += ["| Clip | Used as | Measured | Volume | Note |", "|---|---|---|---|---|"]
        for r in tl.levels:
            clip = analysis.clip(r["clip"])
            measured = f"{r['lufs']:.1f} LUFS" + (" (speech)" if r.get("words") else "")
            volume = f"{r['volume']:+g} dB" if r["volume"] else "as recorded"
            L.append(f"| {clip.media.name} | {r['use']} | {measured} | {volume} | {r['why']} |")
    leveled = {r["clip"] for r in tl.levels}
    used = {c.clip_id for c in tl.spine} | {c.clip_id for c in tl.connected}
    unmeasured = sorted(c.media.name for c in analysis.clips if c.id in used and c.media.has_audio and c.id not in leveled and c.role != "graphic")
    if unmeasured:
        L += ["", "Not measured, so left as recorded: " + ", ".join(unmeasured) + "."]
    return L + [""]


# ------------------------------------------------------------------ Venture Studio

# What a file's status means in the paper edit. "not_checked": a re-cut added it after the check was made.
_STUDIO_RELATED = ("matched", "ambiguous", "near_miss", "not_in_snapshot", "not_in_kit")


def _name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _names(paths: list[str], limit: int = 8) -> str:
    shown = ", ".join(f"`{_name(p)}`" for p in paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def studio_section(
    record: dict | None,
    projects: list,
    *,
    refused: str | None = None,
    unavailable: str | None = None,
    earlier: bool = False,
    out_dir=None,
    now: float | None = None,
) -> list[str]:
    """The paper edit's "Venture Studio" section: the snapshot it was checked against, credits for the Studio items the
    cut uses, how many its Project history review will match, near misses, files not in the snapshot yet, and tips.

    `record`: studio.match_media's (or the one saved in plan.json, `earlier`: Studio isn't available now). `projects`:
    the projects in this version's FCPXML (studio.cut_projects), so a path the record lacks reads "not checked".
    `unavailable`: why Studio couldn't be read; with no record, the section is that one line. No section at all when
    nothing in the cut is Studio's (or Studio isn't installed and nothing was saved).
    """
    import shlex
    import time

    from .studio import STALE_HOURS, _age, _when

    if record is None:
        return ["## Venture Studio", "", f"Couldn't check this cut against Venture Studio: {unavailable}.", ""] if unavailable else []
    files = record.get("files") if isinstance(record.get("files"), dict) else {}
    refused = refused or record.get("refused")

    def info(path: str) -> dict:
        found = files.get(path)
        return found if isinstance(found, dict) else {"status": "not_checked", "assets": [], "why": ""}

    every = list(dict.fromkeys(p for project in projects for p, _ in project.paths()))
    if refused and not every:
        every = list(files)
    if not refused and not any(info(p).get("status") in _STUDIO_RELATED for p in every):
        return []

    kit = record.get("kit") if isinstance(record.get("kit"), dict) else None
    snap = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else None
    L = ["## Venture Studio", ""]
    old = False
    if snap and isinstance(snap.get("created_at"), int):
        hours = max(0.0, ((now if now is not None else time.time()) - snap["created_at"]) / 3600)
        old = hours > STALE_HOURS
        source = f"Venture Studio's catalog snapshot from {_when(snap['created_at'])} ({_age(hours)})"
    elif kit:
        exported = f", exported {_when(kit['exported_at'])}" if isinstance(kit.get("exported_at"), int) else ""
        source = f"the Venture Studio kit {kit.get('name')!r}{exported}: files not in it read \"not in this kit\", though Studio's own review checks its whole catalog"
    else:
        source = "Venture Studio's catalog"
    if earlier:
        why = f" ({unavailable})" if unavailable else ""
        L += [f"Venture Studio couldn't be read for this version{why}, so this is from the check made earlier against {source}.", ""]
    else:
        L += [f"Checked against {source}: anything added to Studio since isn't in it.", ""]

    if refused:
        L.append(f"- **Studio's Project history review would refuse this file:** {refused}.")
    else:
        counts = []
        for project in projects:
            paths = list(dict.fromkeys(p for p, _ in project.paths()))
            items = {a["id"] for p in paths if info(p).get("status") == "matched" for a in info(p).get("assets") or [] if isinstance(a, dict)}
            matched = sum(1 for p in paths if info(p).get("status") == "matched")
            unchecked = sum(1 for p in paths if info(p).get("status") == "not_checked")
            more = f", {unchecked} not checked" if unchecked else ""
            counts.append(
                f'{len(items)} Studio item{"s" if len(items) != 1 else ""} in "{project.name}" '
                f"({matched} of its {len(paths)} file{'s' if len(paths) != 1 else ''}{more})"
            )
        if counts:
            L.append("- **Studio's Project history review** should match " + "; ".join(counts) + ", if Final Cut keeps the files where they are.")

    credits: dict[tuple[str, str], list[str]] = {}
    for path in every:
        if info(path).get("status") != "matched":
            continue
        for a in info(path).get("assets") or []:
            if isinstance(a, dict):
                key = (str(a.get("provider") or ""), str(a.get("license") or ""))
                name = str(a.get("name") or _name(path))
                if name not in credits.setdefault(key, []):
                    credits[key].append(name)
    if credits:
        L.append("- **Credits** (provider and license as Studio has them):")
        for (provider, lic), names in sorted(credits.items(), key=lambda kv: (kv[0][0] == "", kv[0][0].lower(), kv[0][1].lower())):
            who = f"**{provider}**" if provider else "Provider not set in Studio"
            L.append(f"  - {who}, {lic + ' license' if lic else 'license not set'}: {', '.join(names)}")

    def group(status: str) -> list[str]:
        return [p for p in every if info(p).get("status") == status]

    near = group("near_miss")
    if near:
        L.append("- **Near misses** (Studio compares paths exactly, so these won't match as they are):")
        for p in near:
            other = info(p).get("studio_path")
            L.append(f"  - `{p}`: " + (f"Studio has `{other}`; " if other else "") + str(info(p).get("why") or "a near miss"))
    ambiguous = group("ambiguous")
    if ambiguous:
        L.append("- **Ambiguous** (more than one Studio item has the same path, so Studio's review counts none of them):")
        for p in ambiguous:
            names = ", ".join(f"{a.get('name')} (item {a.get('id')})" for a in info(p).get("assets") or [] if isinstance(a, dict))
            L.append(f"  - `{p}`: {names}")
    pending = group("not_in_snapshot")
    if pending:
        L.append(f"- **Not in this snapshot yet** (inside a Studio library; probably matched after Studio's next scan): {_names(pending)}")
    outside_kit = group("not_in_kit")
    if outside_kit:
        L.append(f"- **Not in this kit** (Studio's own review may still match them): {_names(outside_kit)}")
    unchecked = group("not_checked")
    if unchecked:
        L.append(f"- **Not checked** (new in this version, after the check against Studio): {_names(unchecked)}")
    unmatchable = group("unmatchable")
    if unmatchable:
        L.append("- **Not matchable:** " + "; ".join(f"`{_name(p)}`: {info(p).get('why')}" for p in unmatchable))
    outside = group("outside")
    if outside:
        L.append(f"- Outside every Studio library: {_names(outside, 5)}")
    renders = group("render")
    if renders:
        L.append(f"- Roughcut's own renders, which Studio has no record of: {len(renders)} file{'s' if len(renders) != 1 else ''}")
    L.append(
        "- Review only your final Final Cut export in Studio's Project history, and only once: Confirm records every project "
        "in the file (a Stringout too) each time you confirm."
    )
    if snap:
        refresh = (
            "click Back up catalog now in Studio" + (f", then `roughcut render {shlex.quote(str(out_dir))}`" if out_dir is not None else "")
            + " to check against a fresh snapshot"
        )
        L.append(f"- **This snapshot is old:** {refresh}." if old else f"- If you've added or moved files in Studio since the snapshot, {refresh}.")
    return L + [""]


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
    revision: list[str] | None = None,
    studio: list[str] | None = None,
) -> str:
    raw = sum(float(c.media.duration) for c in analysis.clips if c.media.kind != "image" and c.role != "graphic")
    speech = sum(c.speech_seconds for c in analysis.clips if c.has_transcript)
    L = [f"# {plan.title}", ""]
    failures = [w for w in (extra_warnings or []) if "planning failed" in w or "no usable sections" in w]
    if failures:
        L += ["> **The AI didn't plan this cut.** It's the stringout: all speech in recording order with dead air removed, no B-roll.", ">"]
        L += [f"> {w}" for w in failures] + [""]
    for w in extra_warnings or []:
        if w.startswith(HEADLINE_WARNINGS) and not any(w in line for line in revision or []):
            L += [f"> {w}", ""]
    if revision:  # `roughcut revise`: the notes and what they changed, before anything else
        L += list(revision)
    if plan.logline:
        L += [f"_{plan.logline}_", ""]
    L += [
        f"- **Rough cut:** {tl.review.length_line() if tl.review else seconds_to_clock(tl.seconds)} ({tl.width}x{tl.height}, {tl.frame_duration.denominator / tl.frame_duration.numerator:.3f} fps)",
        f"- **Raw media:** {seconds_to_clock(raw)} across {sum(1 for c in analysis.clips if c.role != 'graphic')} clips; {seconds_to_clock(speech)} of transcribed speech",
    ]
    if stringout is not None and stringout.spine:
        L.append(f"- **Stringout** (all dialogue, dead air removed): {seconds_to_clock(stringout.seconds)}")
    talk, kept = tl.dead_air or dead_air(analysis.clips, stringout) or (0.0, 0.0)  # measured on the stringout, written or not
    if talk > 0:
        L.append(f"- **Dead air removed:** {seconds_to_clock(max(0.0, talk - kept))} of {seconds_to_clock(talk)} talking footage ({max(0.0, 1 - kept / talk):.0%})")
    L.append(f"- **Planned by:** {plan.source}")
    if usage:
        calls = [c for u in usage for c in u.calls]
        if calls:
            tokens_in = sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in calls)
            tokens_out = sum(c.output_tokens for c in calls)
            cost = describe_cost(cost_breakdown(usage))
            cost_txt = f"; {cost}" if cost else ""
            L.append(f"- **Model usage:** {len(calls)} calls, {tokens_in:,} input / {tokens_out:,} output tokens{cost_txt}")
    if timings:
        L.append("- **Processing:** " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items()))
    L.append("")
    notes = list(analysis.notes) + list(tl.notes)
    if notes:
        L += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    if tl.review is not None:
        L += ["## Cut check", ""] + [f"- {line}" for line in tl.review.lines(tl.frame_duration)] + [""]

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
        if rendered:
            L += ["Open `graphics/preview.html` to watch each one over the shot it sits on.", ""]
        L += ["| Time | ID | Kind | On screen | File |", "|---|---|---|---|---|"]
        for g in graphics:
            text = g.spec.text + (f" / {g.spec.subtext}" if g.spec.subtext else "")
            clip = rendered.get(g.id)
            file = f"`graphics/{clip.media.name}`" if clip else "not rendered (see Warnings)"
            L.append(f"| `{_tc(g.start, tl)}` | {g.id} | {g.spec.kind.replace('_', ' ')} | {text.replace('|', '/')} | {file} |")
        L.append("")

    L += levels_section(tl, analysis)
    L += list(studio or [])

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

    used = {c.clip_id for c in tl.spine} | {c.clip_id for c in tl.connected if c.kind != "graphic"}
    used_media = [c.media for c in analysis.clips if c.id in used]
    rates = frame_rate_warnings(tl.frame_duration, used_media)
    probe = [w for w in analysis.warnings if not vfr_warning_covered(w, used_media)]  # said once, in the rate lines
    warnings = collapse_warnings(probe + list(plan.warnings) + list(tl.warnings) + list(extra_warnings or []) + rates)
    if warnings:
        L += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    return "\n".join(L)

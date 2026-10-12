"""Prompt text for the planning and visual-description calls.

The media block is rendered deterministically (sorted, no timestamps of the
run itself) so it can be prompt-cached: re-planning the same footage with a
different brief only pays for the brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .analyze import ROLE_LABELS, Analysis, Clip
from .timecode import seconds_to_clock

if TYPE_CHECKING:  # pragma: no cover
    from .timeline import Style

PLANNER_SYSTEM = """\
You are an experienced documentary and YouTube editor acting as an assistant editor. You receive an \
inventory of raw media with timestamped transcripts and produce a first-pass paper edit: the rough \
cut a human editor will open in Final Cut Pro and refine. Your job is structure and selection; \
the editor handles polish.

How material is referenced
- Every clip has an ID: V = video, A = audio-only, I = still image.
- Every transcribed sentence has a segment ID like V03.S012. Select spoken material by listing \
segment IDs. Never write timestamps for speech: the software computes frame-accurate cuts, trims \
pauses inside sentences and removes "um"/"uh" on its own.
- To put a visual moment on the main storyline (establishing shot, reaction, montage beat, photo, \
graphic), write a range item "CLIPID@start-end" in seconds from the start of that clip, e.g. \
"V07@12.5-16". For stills, "I02@0-4" means four seconds on screen. Use these as short breathers \
(2-5 seconds) that belong to the story around them, not as jumps to unrelated footage.
- Clips marked as another take or version of the narration script are alternatives: choose one \
read per line. Footage exported more than once is listed once.

What a good first cut does
1. Story first. Open on the strongest hook available: a compelling line or moment, not a greeting \
or throat-clearing. Then build sections with a clear arc. Recording order is a sensible default, \
not a rule; follow the brief.
2. Retakes: when a line is delivered more than once, keep the best complete take (often the last) \
and record the others in "alternates": "chosen" is the take in your sections, and the others stay \
out of them (an other take in the sections is replaced by the chosen one).
3. Cut false starts, fragments that trail off, repeated phrases, "is it recording?", camera and \
setup talk, and off-topic chatter. If the brief asks for a candid or raw feel, keep genuine \
unscripted moments.
4. Keep dependent lines together. Don't strand a "because..." without its setup, or a punchline \
without its setup.
5. Length: if a target is given, hit it within about 10%. Each segment shows its length as \
recorded, then after the arrow how long it plays in the cut once its pauses and um/uh are \
trimmed (and how many edits that takes). The cut is the sum of those kept lengths and your ranges.
6. B-roll: cover jump cuts and illustrate what is being said. Each cutaway sits "over" one or more \
consecutive segments of the same section. Several cutaways with the same "over" play one after \
another and share that time, so a 20-second line can get four 5-second shots. Hold shots about \
3-6 seconds (stills 3-5). Choose clips by their visual descriptions and file names, matching \
what the line is about: when the narration names a place, project or person, show that. Prefer \
variety; don't repeat a shot. source_in is where to start inside the B-roll clip in seconds, or \
-1 to let the software pick a good spot. Long clips list timed Moments: set source_in to the \
time of the usable moment that fits the line. "words": when a shot belongs on a name, place or \
list item, copy those exact words from the line, and the shot comes in as they are spoken; \
otherwise "", and it follows the shot before it (or starts with the line). On long lines, add \
enough shots to cover the whole line.
6b. Narration (Voiceover clips) has no picture of its own. When the piece is carried by \
narration, use one read of it as the backbone, bring in interview soundbites where they back up \
what was just said, and cover every narrated segment with B-roll or stills from start to end. \
Keep each interview soundbite to a complete thought; don't hop between interviewees mid-idea. \
Any narration you leave uncovered is filled automatically with the closest-matching clips, which \
is a fallback, not a substitute for your choices.
7. Music: only when audio clips classified as Music exist and the brief doesn't rule it out. Give \
the 0-based section range it should run under.
8. Flags: when unsure (a cut line that might matter, a coin-flip between takes, a missing shot you \
would want), add a flag. Flags become to-do markers in Final Cut.
9. Use only IDs that appear in the inventory. Each segment can be used once, and so can each line: \
the same words from another read or another export of a promo play only once (a later repeat is \
dropped).
10. Spellings: the transcript is speech recognition and often misspells names. When the brief, \
script or file names spell a name the transcript gets wrong (a person, business, place or brand), \
add {"heard": the name exactly as the transcript has it, without a possessive 's or punctuation \
after it, "correct": the right spelling of the same words, also without a possessive 's or \
punctuation after it}. They fix the captions and the names in graphics' text and subtext; a \
graphic's words stay as the transcript has them. Only names you can confirm from \
what you were given; never guess.
11. Cut settings: the software trims the pauses and can punch in at jump cuts; say what the brief \
asks for, read as the editor meant it. "pacing": tight for tight cuts, snappy, punchy or no dead \
air; loose for let it breathe, slow or reflective; medium for a natural pace; unspecified when it \
doesn't say ("not too tight" isn't tight, and "a tight deadline" or "tight shots" aren't about \
pacing). "punch_ins": on only when it asks for punch-ins (alternating zooms) at the jump cuts \
throughout, not for a one-off reframe of a moment; off when it turns them down; otherwise \
unspecified. "graphics_asked": whether it asks for on-screen graphics, titles, names or text. When \
there is a <preset_brief>, the editor's own <brief> wins where they differ, and only the editor's \
own brief counts for graphics_asked.

Name sections the way an editor labels a timeline ("Cold open", "Drive up", "The summit", \
"Wrap"). Use cut_notes to summarize briefly what you left out and why.
"""

VISION_SYSTEM = """\
You are logging footage for a video editor. For each clip you get one to three frames sampled \
from it. Describe what the clip shows so an editor searching for B-roll can find it: subjects, \
action, setting, time of day, shot type and camera movement if evident. Be concrete and short. \
Tags are lowercase keywords (people, places, objects, activities, mood) useful as Final Cut \
keyword collections; three to six per clip. Note usable-quality problems (very shaky, dark, out \
of focus, lens obstructed) in quality, otherwise "good".
"""

VISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "shot": {"type": "string", "description": "e.g. wide, medium, close-up, aerial, POV, selfie"},
                    "quality": {"type": "string"},
                },
                "required": ["id", "description", "tags", "shot", "quality"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


MOMENTS_SYSTEM = """\
You are logging long B-roll clips for a video editor, so a cutaway can start on the good part \
instead of a fixed spot. Each clip comes with frames sampled through it, each labelled with its \
time in the clip. For every frame, write a short note of what it shows (subject, shot size, \
camera movement, anything distinctive an editor would search for) and say whether it is usable \
as a cutaway: false when it is shaky, blurred, blocked, badly exposed, or shows nothing. "at" is \
the frame's time in seconds, as labelled.
"""

MOMENTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "moments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "at": {"type": "number", "description": "The frame's time in the clip, in seconds."},
                                "note": {"type": "string"},
                                "usable": {"type": "boolean"},
                            },
                            "required": ["at", "note", "usable"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "moments"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def _clip_header(clip: Clip, analysis: Analysis | None = None) -> str:
    m = clip.media
    bits = [f'{clip.id} "{m.name}"', m.kind]
    if m.kind != "image":
        seconds = float(m.duration)
        bits.append(seconds_to_clock(seconds, millis=seconds < 60))
    if m.kind == "video":
        bits.append(f"{m.width}x{m.height} {m.rate}p")
    elif m.kind == "image" and m.width:
        bits.append(f"{m.width}x{m.height}")
    if m.creation_time:
        bits.append(f"recorded {m.creation_time[:16].replace('T', ' ')}")
    bits.append(f"role {ROLE_LABELS.get(clip.role, clip.role)}")
    if clip.has_transcript:
        bits.append(f"{seconds_to_clock(clip.speech_seconds)} of speech")
    if analysis is not None:
        copies = [c for c in analysis.clips if c.duplicate_of == clip.id]
        if copies:
            bits.append("also exported as " + ", ".join(c.id for c in copies) + " (hidden; use this one)")
    if clip.bed == "music":
        finished = "a finished edit, " if (clip.cuts_per_min or 0) >= 4 else ""
        bits.append(
            f"{finished}speech over a music bed: take consecutive lines together so the music flows "
            "(every jump in the music gets a to-do marker); B-roll optional"
        )
    if clip.take_of:
        kind = "another take" if clip.same_words >= 0.9 else "a different version"
        bits.append(
            f"{kind} of the narration script in {clip.take_of} ({clip.same_words:.0%} same words): "
            "pick the better read for each line and never use the same line twice"
        )
    return ", ".join(bits)


def render_media(analysis: Analysis, style: "Style") -> str:
    """The media inventory the planner reads. Deterministic for a given analysis and style.

    `style` is the run's (pipeline.style_for): each line's kept length depends on
    its padding and pause limits, so there's deliberately no default.
    """
    from .timeline import kept_length

    clips = [c for c in analysis.clips if not c.hidden]
    total = sum(float(c.media.duration) for c in clips)
    speech = sum(c.speech_seconds for c in clips if c.has_transcript)
    counts = {}
    for c in clips:
        counts[ROLE_LABELS.get(c.role, c.role)] = counts.get(ROLE_LABELS.get(c.role, c.role), 0) + 1
    lines = [
        f"{len(clips)} clips, {seconds_to_clock(total)} of media, {seconds_to_clock(speech)} of transcribed speech.",
        "Roles: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
        "",
    ]
    for clip in clips:
        lines.append("## " + _clip_header(clip, analysis))
        if clip.visual and clip.visual.get("description"):
            v = clip.visual
            tags = ", ".join(v.get("tags", []))
            q = v.get("quality", "")
            q_note = f" Quality: {q}." if q and q.lower() != "good" else ""
            desc = v.get("description", "").strip().rstrip(".")
            lines.append(f"Visual: {desc}. Shot: {v.get('shot', '')}. Tags: {tags}.{q_note}")
        if clip.visual and clip.visual.get("moments"):
            lines.append(
                "Moments: "
                + "; ".join(
                    f"{mo['at']:g}s {mo['note']}" + ("" if mo.get("usable", True) else " (not usable)")
                    for mo in clip.visual["moments"]
                )
            )
        if clip.has_transcript:
            for s in clip.segments:
                kept, cuts = kept_length(s, clip, analysis, style)
                edits = f", {cuts} edit{'s' if cuts > 1 else ''}" if cuts else ""
                lines.append(
                    f"{s.id} [{seconds_to_clock(s.start, millis=True)}-{seconds_to_clock(s.end, millis=True)} | "
                    f"{s.duration:.1f}s \u2192 {kept:.1f}s{edits}] {s.text}"
                )
        lines.append("")
    return "\n".join(lines)


GRAPHICS_BRIEF = """\
<motion_graphics>
Also plan animated motion graphics for this cut in "graphics". They are designed and rendered \
after you plan; you choose the moments and the words, the software times them to the speech.
- kind: lower_third (a name and role taken from the transcript, file names or brief, never \
invented, the name spelled as the brief, script or file names spell it (your spellings), never \
as the transcript mishears it: who is speaking, the first time each on-camera person talks, or \
a person, place or business the speech names when the brief asks to show them), stat (a number \
that matters, e.g. "$325M"), title_card (a section or chapter title on the section's first \
line), kinetic_caption (the spoken words animated word by word, for the hook or a punchline), \
quote (a short key line pulled out on screen), list (two to four short items the speaker names \
together, all within the graphic's seconds).
- When the brief asks to show names that come up one at a time over several lines (a rundown \
of restaurants, chefs, vendors, speakers; "list the names" in a brief means this), they get one \
graphic each, on the words that say that name: a lower_third (text: the name, spelled as the \
brief spells it; subtext: the chef, role or detail), never one list graphic: a list only holds \
what is spoken while it is on screen. Each name the brief asks for gets its graphic when it is \
said, however close together; the next one replaces it.
- segment: a segment ID that is in your cut. words: copy the exact words from that segment where \
the graphic should appear, as the transcript has them, misheard names included. text: what is \
on screen, short (six words or fewer for the main line). subtext: an optional second line, or \
"". seconds: how long it holds, 2-8 (0 for kinetic captions). direction: a line on look or \
motion when it matters, or "".
- Follow the brief's directions about graphics, including where on screen they go (put that \
in direction). Apart from a rundown the brief asks for, space them out (usually one every 10-20 \
seconds at most, never two at once), and add none where the footage gives no good moment.
</motion_graphics>"""


GRAPHICS_OFF = """\
<motion_graphics_off>
This cut has no motion graphics (they are off for it; the editor adds any in Final Cut), so plan \
none and don't describe any (lower thirds, titles, names or text on screen) in the logline, flags \
or cut_notes. graphics_asked still says whether they are asked for.
</motion_graphics_off>"""


def pacing_choice(analysis: Analysis, styles: dict[str, "Style"]) -> str:
    """For Auto pacing (the planner picks it): the media block shows kept lengths at medium, so say what tight
    and loose make of them for this footage. `styles`: the run's tight, medium and loose (pipeline.style_for).

    Deterministic for a given analysis, like the media block; "" when nothing is transcribed."""
    from .timeline import kept_length

    total = {
        name: sum(kept_length(seg, clip, analysis, style)[0] for clip in analysis.clips if not clip.hidden and clip.has_transcript for seg in clip.segments)
        for name, style in styles.items()
    }
    medium = total.get("medium") or 0.0
    if medium <= 0 or "tight" not in total or "loose" not in total:
        return ""

    def change(seconds: float) -> str:
        pct = round(100 * (seconds / medium - 1))
        return "about the same" if pct == 0 else f"about {abs(pct)}% {'longer' if pct > 0 else 'shorter'}"

    return (
        "<pacing>\nYou pick the pacing (rule 11). The kept lengths in the media are at medium pacing: every line "
        f"together keeps {seconds_to_clock(medium)}. For this footage, tight plays them {change(total['tight'])} "
        f"({seconds_to_clock(total['tight'])}) and loose {change(total['loose'])} ({seconds_to_clock(total['loose'])}). "
        "Plan the length at the pacing you pick: each line's kept length changes about the same way.\n</pacing>"
    )


def own_words(context: str, preset_brief: str) -> str:
    """The brief without the preset's part, which comes first."""
    context, preset_brief = (context or "").strip(), (preset_brief or "").strip()
    return context[len(preset_brief) :].strip() if preset_brief and context.startswith(preset_brief) else context


def render_brief(
    context: str, target_seconds: float | None, script: str | None, *, graphics: bool = False, graphics_style: str = "", preset_brief: str = "",
    pacing: str = "",
) -> str:
    """`preset_brief`: the preset's part of `context` (it comes first), shown apart so the editor's own words can win.
    `pacing`: for Auto pacing, what tight and loose make of the kept lengths (`pacing_choice`)."""
    parts = []
    preset_brief = (preset_brief or "").strip()
    if preset_brief and (context or "").strip().startswith(preset_brief):
        parts.append(
            "<preset_brief>\nThe standing brief of the preset this video uses (its channel or brand). The editor's own brief "
            "below adds to it and wins where they differ.\n\n" + preset_brief + "\n</preset_brief>"
        )
        context = own_words(context, preset_brief) or "No brief of its own: follow the preset's."
    parts.append("<brief>\n" + (context.strip() or "No brief given. Make the strongest cut the footage supports.") + "\n</brief>")
    if target_seconds:
        parts.append(f"<target_length>about {seconds_to_clock(target_seconds)} ({target_seconds:.0f} seconds)</target_length>")
    else:
        parts.append("<target_length>none given: keep everything that earns its place</target_length>")
    if pacing:
        parts.append(pacing)
    if script and script.strip():
        parts.append(
            "<script>\nThe editor's script, outline or reference transcript. Follow its structure and match "
            "footage to it where possible.\n\n" + script.strip() + "\n</script>"
        )
    if graphics:
        parts.append(GRAPHICS_BRIEF)
        if graphics_style.strip():
            parts.append("<graphics_style>\n" + graphics_style.strip() + "\n</graphics_style>")
    else:
        parts.append(GRAPHICS_OFF)
    parts.append("Build the rough cut plan.")
    return "\n\n".join(parts)

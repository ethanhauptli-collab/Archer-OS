"""End-to-end: folder in, FCPXML + paper edit out."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from . import progress, versions
from .analyze import Analysis, Cache, analyze, default_cache_dir, log, measure_levels
from .captions import clean_spelling
from .captions import srt as captions_srt
from .fcpxml import build_fcpxml
from .media import AUDIO_EXTS, require_tools, scan
from .plan import Plan, heuristic_plan, normalize, plan_schema, respell_graphics, spelling_key
from .prompts import PLANNER_SYSTEM, own_words, render_brief, render_media
from .providers import Provider, ProviderAuthError, ProviderError, ProviderLimitError, TextPart, cost_breakdown, make_provider
from .report import build_report
from .report_html import report_page
from .timeline import DIALOGUE_LEVEL, STYLES, Style, Timeline, build_timeline, dead_air
from .transcribe import Transcriber, resolve_backend
from .versions import atomic_write, planner_label, safe_name, slug


@dataclass
class Options:
    inputs: list[Path]
    out: Path | None = None
    name: str | None = None
    context: str = ""
    preset_brief: str = ""  # the preset's part of `context` (it comes first): the planner sees it apart, so your own words win
    script: str | None = None
    target_seconds: float | None = None
    style: str = "medium"  # "auto": what the planner reads in the brief (build only)
    keep_fillers: bool = False
    provider: str = "anthropic"
    model: str | None = None
    effort: str | None = "high"
    base_url: str | None = None
    api_key_env: str | None = None
    vision: bool | None = None  # None = on when the provider can see
    vision_model: str | None = None
    transcriber: str = "auto"
    whisper_model: str | None = None
    language: str | None = None
    verbatim: bool = True
    transcript_dirs: list[Path] = field(default_factory=list)
    format: str | None = None
    broll_db: float | None = -20.0
    music_db: float = -14.0
    dialogue_level: float | None = DIALOGUE_LEVEL  # LUFS speech is matched to; None = clip levels as recorded
    fades: bool = True  # de-click fades at audio edits, soft fades on B-roll nat sound
    stringout: bool = True
    fill: bool = True  # cover narration the plan left without picture
    punch_in: bool | None = None  # punch in on jump cuts; None: when the planner reads the brief asking for it
    graphics: bool = False  # Claude-designed motion graphics, rendered to ProRes 4444
    graphics_style: str = ""  # look and brand for them: colors, fonts, mood
    graphics_logo: str | None = None  # logo file designs may show (<img src="brand:logo">)
    graphics_look: tuple[str, ...] = ()  # approved designs (.html) new ones should match
    music: list[Path] = field(default_factory=list)  # --music: songs to lay under the cut (not transcribed)
    cache_dir: Path | None = None
    workers: int = 4


class RoughcutError(RuntimeError):
    """A problem with the inputs the user can fix (no media, nothing to cut...)."""


@dataclass
class Result:
    out_dir: Path
    fcpxml: Path
    report: Path
    plan: Plan
    timeline: Timeline
    stringout: Timeline | None
    fell_back: bool = False
    warnings: list[str] = field(default_factory=list)
    dead_air: tuple[float, float] | None = None  # (talking footage, stringout) seconds
    version: int = 0  # this cut's number in the output folder (versions.py)
    project: str = ""  # the Rough Cut project's name in Final Cut
    event: str = ""  # the event it's imported into
    summary: dict = field(default_factory=dict)  # what made it in, what was left out, what it cost (run_summary)
    settings: dict = field(default_factory=dict)  # the run settings saved with this version (plan.json): what `render` starts from

    @property
    def captions(self) -> Path | None:
        """The cut's captions, `<name> vN.srt` beside its FCPXML, when anything in it is spoken."""
        path = self.fcpxml.with_suffix(".srt")
        return path if path.is_file() else None


CUT_SETTINGS = ("style", "format", "keep_fillers", "stringout", "punch_in")  # result_summary's "cut"


def result_summary(result: Result) -> dict:
    """What the Mac app shows when a run finishes (the `done` event payload).

    Keys may be added, never removed or renamed: the app decodes this.
    """
    tl = result.timeline
    return {
        "title": result.plan.title,
        "out_dir": str(result.out_dir),
        "fcpxml": str(result.fcpxml),
        "report": str(result.report),
        "captions": str(result.captions) if result.captions else None,
        "rough_seconds": round(tl.seconds, 3),
        "stringout_seconds": round(result.stringout.seconds, 3) if result.stringout is not None else None,
        "edits": len(tl.spine),
        "broll": sum(1 for c in tl.connected if c.kind == "broll"),
        "sections": [name for _, name in tl.section_starts],
        "planned_by": result.plan.source,
        "fell_back": result.fell_back,
        "warnings": result.warnings,
        "version": result.version,
        "project": result.project,
        "event": result.event,
        "graphics": result.summary.get("graphics"),
        "visual_log": result.summary.get("visual_log"),
        "skipped": result.summary.get("skipped") or {"files": [], "plan_refs": 0},
        "usage": result.summary.get("usage"),
        "report_html": str(result.report.with_suffix(".html")),
        # What a re-cut keeps unless told otherwise; the app's Re-cut shows it. None for a Result without saved settings.
        "cut": {f: result.settings.get(f) for f in CUT_SETTINGS} if result.settings else None,
    }


def save_result(result: Result, *, partial: bool = False) -> Result:
    """result.json: the done payload, kept with the version it describes (for a recent-runs list).

    `partial`: the cut written before graphics, in case the run stops while they're made;
    the finished run replaces it.
    """
    data = result_summary(result)
    if partial:
        data["partial"] = True
    atomic_write(result.out_dir / "result.json", json.dumps(data, indent=1))
    return result


def run_summary(
    plan: Plan,
    *,
    graphics_on: bool,
    placed: list,
    with_graphics: Analysis,
    usage: list | None = None,
    skipped_files: list[str] | None = None,
    visual_log: dict | None = None,
    graphics_note: str | None = None,
    planned_graphics: int | None = None,
    undesigned: int = 0,
) -> dict:
    """Counts for the end of a run: graphics planned/placed/rendered, visual-log coverage, what was skipped, cost.

    `undesigned`: graphics with no design yet that would have a place in this cut; "placed" counts them too.
    """
    graphics = None
    if graphics_on:
        graphics = {
            "planned": len(plan.graphics) if planned_graphics is None else planned_graphics,
            "placed": len(placed) + undesigned,
            "rendered": sum(1 for c in with_graphics.clips if c.role == "graphic"),
            "note": graphics_note,
        }
    return {
        "graphics": graphics,
        "visual_log": visual_log,
        "skipped": {"files": list(skipped_files or []), "plan_refs": len(plan.warnings)},
        "usage": cost_breakdown(usage) if usage and any(u.calls for u in usage) else None,
    }


def limit_stop(e: ProviderLimitError, analysis: Analysis) -> ProviderLimitError:
    """The plan or credit ran out: say that nothing is lost and how to carry on."""
    cov = visual_coverage(analysis)
    done = "transcripts"
    if any(c.visual for c in analysis.clips):
        done += f" and the visual descriptions of {cov['described']} of {cov['total']} clips"
    return ProviderLimitError(
        f"{e} Nothing is lost: the {done} are cached, and no new cut was written (an earlier one in this folder is untouched). "
        "Run the same command again after the reset; it picks up from the cache instead of starting over."
    )


def visual_coverage(analysis: Analysis) -> dict:
    """How many clips with pictures got a visual description (duplicates hidden from the planner don't count)."""
    seen = [c for c in analysis.clips if c.media.has_video and not c.hidden]
    return {"described": sum(1 for c in seen if c.visual), "total": len(seen)}


# Cut settings `roughcut render` takes from the saved run unless a flag overrides them.
RENDER_FIELDS = ("style", "format", "keep_fillers", "broll_db", "music_db", "fill", "stringout", "dialogue_level", "fades", "punch_in")


def run_settings(opts: Options, provider: Provider | None = None) -> dict:
    """Everything needed to re-cut (render) or re-plan (revise) this run later, saved in plan.json."""
    return {
        **{f: getattr(opts, f) for f in RENDER_FIELDS},
        "context": opts.context,
        "preset_brief": opts.preset_brief,
        "target_seconds": opts.target_seconds,
        "script": opts.script,
        "graphics": opts.graphics,
        "graphics_style": opts.graphics_style,
        "graphics_logo": opts.graphics_logo,  # absolute paths (the CLI resolves them), for designs a revision adds
        "graphics_look": list(opts.graphics_look),
        "provider": opts.provider,
        "model": provider.model if provider is not None else opts.model,
        "vision_model": opts.vision_model,
        "effort": opts.effort,
        "base_url": opts.base_url,
        "api_key_env": opts.api_key_env,
    }


def render_options(opts: Options, settings: dict, overrides: set[str] | None = None) -> Options:
    """The saved run's cut settings, except the ones given now.

    `overrides` names the settings given on the command line. Without it,
    any setting that differs from the default counts as given.
    """
    if overrides is None:
        default = Options(inputs=[])
        overrides = {f for f in RENDER_FIELDS if getattr(opts, f) != getattr(default, f)}
    saved = {f: settings[f] for f in RENDER_FIELDS if f in settings and f not in overrides}
    if saved.get("style") not in STYLES:
        saved.pop("style", None)
    out = replace(opts, **saved)
    if out.style not in STYLES:  # "auto" with nothing saved: what the run's brief asked for
        out = replace(out, style=style_from_brief(str(settings.get("context") or ""), str(settings.get("preset_brief") or ""))[0])
    return out


# What a brief asks for that's a cut setting, not a choice of lines: pacing (--style auto), punch-ins on
# jump cuts, and graphics while they're off. The planner reads the brief and says (the plan's pacing,
# punch_ins and graphics_asked). Without a model (--provider none, or the stringout when the model fails)
# nothing has read it, so a few plain wording rules stand in: tight or loose only for a clear cue, punch-ins
# only when asked for at the cuts, medium and off otherwise. A cue counts only inside its own clause: a "no",
# "don't" or "avoid" earlier in that clause turns it down ("don't cut it too tight", "no punch-ins"), one in
# another clause doesn't ("there's no B-roll, so punch in on the jump cuts").

# Where a clause ends: punctuation, a bullet, a spaced dash, or a word that starts a new thought.
_CLAUSE_END = re.compile(r"[.;:!?,()\n•]|\s[-–—]\s|[–—]|\b(?:and|but|so|then|instead|because|though|although|yet|plus|also)\b", re.I)
# "no"/"without"/"skip" turn down only what they name ("no punch-ins", "without zoom cuts"), not
# a request after it ("without B-roll we need punch-ins"); "don't"/"never"/"avoid" reach further
# ("we really don't want you to punch in").
_NOUN_NOT = r"\b(?:no|without|skip|none of|nothing|zero|less)\b(?!-)"  # not "a no-nonsense promo"
_VERB_NOT = r"\b(?:not|never|avoid(?:ing)?|cannot|(?:do|does|did|is|are|was|were|wo|ca|should|would|could|must)n['’]?t|no need (?:to|for))\b"
_NOT = re.compile(rf"(?P<verb>{_VERB_NOT})|(?P<noun>{_NOUN_NOT})", re.I)
_NOT_REALLY = re.compile(r"(?:\w+n['’]?t|not|never) (?:forget|be afraid|hesitate|mind)\b", re.I)  # "don't forget to punch in"
_TOO = re.compile(r"\b(?:too|overly)\s*$", re.I)  # "the last one was too tight" isn't asking for tight
# "Punch-ins: none", "Punch-ins - no thanks"; not "punch in on the cuts - no B-roll was shot".
_NO_AFTER = re.compile(r"\s*[:?=–—-]\s*(?:no|none|nope|off|never)\b(?=\s*(?:$|[.,;:!?)\n]|please\b|thanks?\b|thank you\b))", re.I)


def _clause_before(text: str, start: int) -> str:
    before = text[max(0, start - 100) : start]
    ends = list(_CLAUSE_END.finditer(before))
    return before[ends[-1].end() :] if ends else before


def _turned_down(text: str, m: re.Match) -> bool:
    """A cue its own clause turns down: "not too tight", "no punch-ins", "Punch-ins: none"."""
    clause = _clause_before(text, m.start())
    if _TOO.search(clause) or _NO_AFTER.match(text, m.end()):
        return True
    for n in _NOT.finditer(clause):
        if _NOT_REALLY.match(clause, n.start()):
            continue
        if len(clause[n.end() :].split()) <= (2 if n.group("noun") else 6):
            return True
    return False


def _cues(pattern: re.Pattern, text: str) -> list[tuple[re.Match, bool]]:
    """Each match, and whether its clause turns it down. One joined to a turned-down cue by "and"/"or"
    goes with it ("don't make it tight and snappy", "no zoom-ins or punch-ins")."""
    out: list[tuple[re.Match, bool]] = []
    for m in pattern.finditer(text):
        down = _turned_down(text, m)
        if not down and out and out[-1][1] and re.fullmatch(r"\s*,?\s*(?:and|or|nor)?\s*", text[out[-1][0].end() : m.start()], re.I):
            down = True
        out.append((m, down))
    return out


# Tight: "tight", "tighter", "snappy", "punchy", "fast-paced", "no dead air"; never "tight-knit", "tight shots"
# or "a tight deadline". Loose: "let it breathe", "loose pacing", "keep it loose", "slow-paced", "looser".
_TIGHT = re.compile(
    r"\btight(?:er)?\b(?![- ]?knit)(?!\s+(?:on|shots?|close[- ]?ups?|cu|framing|budget|deadline|schedule|turnaround)\b)"
    r"|\b(?:snapp|punch)(?:y|ier)\b|\bfast[- ]paced\b|\bno dead air\b",
    re.I,
)
_LOOSE = re.compile(
    r"\blet (?:[\w'’]+\s+){0,3}breathe\b|\broom to breathe\b|\bbreathing room\b"
    r"|\bloose(?:ly)?[- ]?(?:pac(?:e|ed|ing)|cuts?|edit(?:s|ed)?)\b|\b(?:keep|make) (?:it|this) loose\b"
    r"|\blooser\b(?![- ]?(?:shots?|framing|fit))|\bslow(?:er)?[- ]pac(?:e|ed|ing)\b|\bpac(?:e|ing)\s*[:=–—-]\s*loose\b",
    re.I,
)


def _pace(text: str) -> tuple[str, str | None] | None:
    """What this text says about pacing: (style, the words that say it; None when it asks for both), or None for nothing."""
    tight, loose = _cues(_TIGHT, text), _cues(_LOOSE, text)
    asked_tight = [m for m, down in tight if not down]
    asked_loose = [m for m, down in loose if not down]
    if asked_tight and not asked_loose:
        return "tight", asked_tight[0].group(0).strip()
    if asked_loose and not asked_tight:
        return "loose", asked_loose[0].group(0).strip()
    if asked_tight and asked_loose:
        return "medium", None
    if tight or loose:  # only turned down: medium, quoting the words that turn it down
        m = (tight + loose)[0][0]
        return "medium", " ".join((_clause_before(text, m.start()) + m.group(0)).split()[-6:])
    return None


def style_from_brief(brief: str, preset_brief: str = "") -> tuple[str, str]:
    """The cut style the brief's wording asks for, without a model: tight or loose for a clear cue, else medium.
    Returns (style, why).

    `preset_brief`: the preset's part of the brief. Your own words decide first; the preset's only when yours say nothing about pace.
    """
    for text, whose in ((own_words(brief, preset_brief), "the brief"), (preset_brief, "the preset's brief")):
        found = _pace(text or "")
        if found:
            style, said = found
            return style, f'{whose} says "{said}"' if said else f"{whose} asks for both tight and loose pacing"
    return "medium", "the brief doesn't ask for tight or loose pacing"


# Punch-ins: "punch-ins" / "zoom-ins", or "punch in" / "zoom in" on the cuts. "Punch in on Roy's face" is one
# reframe, not a punch-in at every jump cut.
_PUNCH_IN = re.compile(
    r"\b(?:punch|zoom)[- ]?ins\b"
    r"|\b(?:punch|zoom)(?:ing)?[- ]in(?:\s+(?:on|at))?\s+(?:(?:the|every|each|all)\s+)?(?:other\s+)?(?:jump[- ]?)?cuts?\b",
    re.I,
)


def _punch(text: str) -> tuple[bool, str] | None:
    found = _cues(_PUNCH_IN, text)
    for m, down in found:
        if not down:
            return True, m.group(0)
    if found:
        m = found[0][0]
        return False, " ".join((_clause_before(text, m.start()) + m.group(0)).split()[-6:])
    return None


def read_punch_in(brief: str, preset_brief: str = "") -> tuple[bool | None, str]:
    """The brief's wording, without a model: (True, its words) when it asks for punch-ins on the jump cuts,
    (False, its words) when it turns them down, (None, "") when it says nothing. Your own words decide before the preset's."""
    for text in (own_words(brief, preset_brief), preset_brief):
        found = _punch(text or "")
        if found:
            return found
    return None, ""


def punch_in_from_brief(brief: str, preset_brief: str = "") -> str:
    """The words in the brief that ask for punch-ins on jump cuts ("" if none; "no punch-ins" doesn't count)."""
    asked, words = read_punch_in(brief, preset_brief)
    return words if asked else ""


# Graphics a brief asks for, without a model: "lower thirds", "names on screen", "list the chef names"
# (not "the graphics designer").
_GRAPHICS = re.compile(
    r"\b(?:motion graphics?|graphics|a graphic)\b(?!\s+(?:design|department))"
    r"|\b(?:lower[- ]thirds?|title cards?|callouts?|kinetic (?:captions?|type|text)"
    r"|on[- ]screen (?:text|names?|titles?|labels?|graphics?)|name (?:titles?|supers?|plates?|cards?)"
    r"|(?:list|show|put|display)\s+(?:[\w'’&]+\s+){0,4}?names?)\b",
    re.I,
)


def graphics_from_brief(brief: str) -> str:
    """The words in the brief that ask for graphics ("" if none; "no graphics" doesn't count)."""
    return next((m.group(0) for m, down in _cues(_GRAPHICS, brief or "") if not down), "")


def settings_from_plan(opts: Options, plan: Plan) -> Options:
    """Auto pacing (--style auto) and Auto punch-ins (no --punch-in flag), as the planner read the brief. A plan
    no model made (--provider none, or the stringout after the model failed) goes by the brief's wording.
    Logged either way, so a misreading shows."""
    by_model = plan.source != "heuristic"
    if opts.style not in STYLES:
        if by_model:
            style = plan.pacing if plan.pacing in STYLES else "medium"
            why = "the brief asks for it" if plan.pacing in STYLES else "the brief doesn't say"
        else:
            style, why = style_from_brief(opts.context, opts.preset_brief)
        opts = replace(opts, style=style)
        log(f"pacing: {style} ({why})")
    if opts.punch_in is None:
        if by_model:
            asked = {"on": True, "off": False}.get(plan.punch_ins)
            why = "the brief asks for it" if asked else "the brief turns them down"
        else:
            asked, words = read_punch_in(opts.context, opts.preset_brief)
            why = f'the brief says "{words}"'
        opts = replace(opts, punch_in=bool(asked))
        if asked:
            log(f"punching in on jump cuts ({why})")
        elif asked is False:  # said either way, so a misreading shows
            log(f"not punching in ({why})")
    return opts


def settings_from_notes(plan: Plan, opts: Options, given: set[str], previous: int | None) -> tuple[Options, list[str]]:
    """The pacing and punch-ins a revision's notes ask for, as the reviser read them ("make it looser", "remove the
    punch-ins"), unless a flag given to revise sets them. Returns this version's options and what to say about them."""
    was = f"v{previous}" if previous else "the previous cut"
    said = []
    if plan.pacing in STYLES and plan.pacing != opts.style:
        if "style" in given:
            said.append(f"Your notes ask for {plan.pacing} pacing, but --style {opts.style} was given, so this version is {opts.style}.")
        else:
            said.append(f"Pacing: {plan.pacing}, as your notes ask ({was} was {opts.style}).")
            opts = replace(opts, style=plan.pacing)
    asked = {"on": True, "off": False}.get(plan.punch_ins)
    if asked is not None and asked != bool(opts.punch_in):
        if "punch_in" in given:
            flag, has = ("--punch-in", "has them") if opts.punch_in else ("--no-punch-in", "has none")
            said.append(f"Your notes ask for {'punch-ins' if asked else 'no punch-ins'}, but {flag} was given, so this version {has}.")
        else:
            said.append(f"Punch-ins: {'on' if asked else 'off'}, as your notes ask ({was} {'had none' if asked else 'had them'}).")
            opts = replace(opts, punch_in=asked)
    return opts, said


def graphics_off_note(opts: Options, out_dir: Path, *, asked: bool | None, made_without_ai: bool) -> str | None:
    """With motion graphics off, a brief that asks for them gets none: say so, and how to add them.

    `asked`: the planner's reading (plan.graphics_asked). None when no model read the brief: then the wording
    of your own words decides (a preset's brief describing its graphics, turned off on purpose, doesn't count).
    """
    if opts.graphics:
        return None
    said = ""
    if asked is None:
        words = graphics_from_brief(own_words(opts.context, opts.preset_brief))
        if not words:
            return None
        said = f' ("{words}")'
    elif not asked:
        return None
    add = f"`roughcut revise {shlex.quote(str(out_dir))} --graphics {shlex.quote('add the graphics the brief asks for')}`"
    if made_without_ai:
        return (
            f"The brief asks for graphics{said}, but this cut was made without AI, which designs them, so it has none. "
            f"{add} adds them without re-transcribing (with your Claude plan: add --provider claude-code)."
        )
    return (
        f"The brief asks for graphics{said}, but motion graphics are off, so this cut has none. "
        f"Turn on Motion graphics (--graphics in Terminal) and build again, or {add} adds them without re-transcribing."
    )


def analysis_digest(analysis: Analysis) -> str:
    """Which clips, in which order: segment IDs only mean the same thing for the same digest."""
    h = hashlib.sha1()
    for c in analysis.clips:
        h.update(f"{c.id}|{c.media.fingerprint}\n".encode())
    return h.hexdigest()[:16]


def plan_record(name: str, plan: Plan, raw_plan: dict | None, analysis: Analysis, settings: dict | None) -> dict:
    record = {"name": name, "plan": plan.to_json(), "raw": raw_plan, "analysis_digest": analysis_digest(analysis)}
    if settings is not None:
        record["settings"] = settings
    return record


# The output folder whose plan this build or revise has saved, for a stop that arrives as a
# signal (the app's cancel, a closed Terminal) and not as an exception: the CLI's signal
# handler reads it to say how to finish the cut. None before the checkpoint.
PLAN_SAVED: Path | None = None


def _plan_saved_at(out_dir: Path | None) -> None:
    global PLAN_SAVED
    PLAN_SAVED = out_dir


def save_checkpoint(out_dir: Path, analysis: Analysis, prompt: str, record: dict) -> None:
    """The paid-for part of a run, saved before anything that can still fail (cutting, graphics).

    From here on `roughcut render` can finish the cut without calling the model again.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / "analysis.json", json.dumps(analysis.to_json(), indent=1))
    atomic_write(out_dir / "prompt.md", prompt)
    atomic_write(out_dir / "plan.json", json.dumps(record, indent=1))
    _plan_saved_at(out_dir)
    if record.get("raw"):
        log("plan saved: `roughcut render` can finish this cut without calling the model again")


def _mark_plan_saved(e: BaseException, out_dir: Path) -> None:
    """Tell the CLI the plan survived, so it can print how to finish the cut.

    Not for a stop whose cut is already written (`e.cut_written`): `render` would only make it again.
    """
    if getattr(e, "cut_written", False):
        return
    try:
        e.plan_saved = out_dir  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - some exception types refuse attributes
        pass


def default_out_dir(inputs: list[Path]) -> Path:
    first = inputs[0].expanduser().resolve()
    base = first if first.is_dir() else first.parent
    return base.parent / f"{base.name}_roughcut"


def style_for(opts: Options) -> Style:
    base = STYLES["medium" if opts.style == "auto" else opts.style]  # auto: medium until the planner has read the brief
    if opts.keep_fillers:
        return replace(base, remove_fillers=False)  # keeps the style's B-roll pacing too
    return base


def music_problem(song: Path | str) -> str | None:
    """What's wrong with a --music file, or None: one that isn't there is told apart from one roughcut can't read."""
    path = Path(song).expanduser()
    if not path.exists():
        return f"--music {song}: can't find that file (moved, renamed, or a typo in the path?)"
    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
        return f"--music {song}: not an audio file roughcut reads ({' '.join(sorted(AUDIO_EXTS))})"
    return None


def missing_media(analysis: Analysis, *timelines: Timeline | None) -> list[str]:
    """Clips a cut uses whose files are gone from where the footage was read.

    `render` and `revise` reuse that reading, so a song taken out of Downloads or a moved
    footage folder would otherwise only show up as missing media in Final Cut.
    """
    used = {c.clip_id for tl in timelines if tl is not None for c in [*tl.spine, *tl.connected]}
    gone = [c for c in analysis.clips if c.id in used and not Path(c.media.path).exists()]
    if not gone:
        return []
    named = "; ".join(f"{c.media.name} ({'the music bed, ' if c.music_bed else ''}was at {c.media.path})" for c in gone[:3])
    more = f" and {len(gone) - 3} more" if len(gone) > 3 else ""
    return [
        f"{len(gone)} file{'s' if len(gone) != 1 else ''} this cut uses {'are' if len(gone) != 1 else 'is'} no longer where the footage was read: "
        f"{named}{more}. Final Cut will show {'them' if len(gone) != 1 else 'it'} as missing media: put {'them' if len(gone) != 1 else 'it'} back, "
        "or relink in Final Cut (File > Relink Files)."
    ]


def run_analysis(opts: Options, out_dir: Path, skipped: list[str] | None = None) -> Analysis:
    """Scan, probe, map silence and transcribe. `skipped` collects the names of files that couldn't be read."""
    require_tools()
    progress.stage("scan", "Finding media")
    for song in opts.music:
        problem = music_problem(song)
        if problem:
            raise RoughcutError(problem)
    files = scan(list(opts.inputs) + list(opts.music), exclude=[out_dir])
    if not files:
        raise RoughcutError("No media files found. Supported: video (.mov .mp4 ...), audio (.wav .m4a ...) and images.")
    log(f"found {len(files)} media files")
    transcriber = None
    if opts.transcriber != "none":
        backend = resolve_backend(opts.transcriber)
        transcriber = Transcriber(backend, opts.whisper_model, opts.language, opts.verbatim)
        log(f"transcriber: {backend} ({transcriber.model})")
    cache = Cache(opts.cache_dir or default_cache_dir())
    prefer_vertical = False
    if opts.format:
        from .timeline import parse_format

        w, h, _ = parse_format(opts.format)
        prefer_vertical = h > w
    analysis = analyze(
        files, transcriber=transcriber, cache=cache, transcript_dirs=opts.transcript_dirs, workers=opts.workers, prefer_vertical=prefer_vertical,
        music=opts.music,
    )
    if skipped is not None:
        read = {Path(c.media.path).resolve() for c in analysis.clips}
        skipped += [f.name for f in files if Path(f).resolve() not in read]
    return analysis


def media_part(analysis: Analysis, style: Style) -> TextPart:
    """The footage inventory, a prompt-cache breakpoint: build and revise send it byte for byte the same
    (for the same cut style: each line shows what that style keeps of it)."""
    return TextPart("<media>\n" + render_media(analysis, style) + "\n</media>", cache=True)


def plan_with_model(provider: Provider, analysis: Analysis, opts: Options) -> tuple[Plan, dict]:
    parts = [
        media_part(analysis, style_for(opts)),
        TextPart(render_brief(
            opts.context, opts.target_seconds, opts.script, graphics=opts.graphics, graphics_style=opts.graphics_style, preset_brief=opts.preset_brief,
        )),
    ]
    # Thinking counts against max_tokens; a long narration plan needs the room.
    raw = provider.complete_json(
        PLANNER_SYSTEM, parts, plan_schema(opts.graphics), schema_name="edit_plan", purpose="edit plan", effort=opts.effort, max_tokens=128000
    )
    return normalize(raw, analysis, source=f"{provider.name}:{provider.model}"), raw


def make_timelines(plan: Plan, analysis: Analysis, opts: Options, name: str) -> tuple[Timeline, Timeline | None]:
    progress.stage("cut", f"Cutting ({opts.style})")
    style = style_for(opts)
    plan.target_seconds = opts.target_seconds or plan.target_seconds  # saved with the plan, so `render` reports it too
    rough = build_timeline(
        plan, analysis, style=style, name=f"{name} - Rough Cut", format_override=opts.format, broll_db=opts.broll_db, music_db=opts.music_db, fill=opts.fill,
        dialogue_level=opts.dialogue_level, fades=opts.fades,
        target_seconds=plan.target_seconds, punch_in=bool(opts.punch_in),
    )
    stringout = None
    if opts.stringout:
        s_plan = heuristic_plan(analysis)
        if s_plan.sections:
            stringout = build_timeline(
                s_plan,
                analysis,
                style=style,
                name=f"{name} - Stringout",
                format_override=opts.format or f"{rough.width}x{rough.height}@{float(1 / rough.frame_duration):.3f}",
                broll_db=opts.broll_db,
                fill=opts.fill,
                dialogue_level=opts.dialogue_level,
                fades=opts.fades,
                music_beds=False,  # the stringout is for reading the speech
            )
    return rough, stringout


def write_outputs(
    out_dir: Path,
    name: str,
    record: dict,
    plan: Plan,
    analysis: Analysis,
    rough: Timeline,
    stringout: Timeline | None,
    report_kwargs: dict,
    *,
    version: int,
    command: str = "build",
    entry: dict | None = None,
    quiet: bool = False,
) -> tuple[Path, Path]:
    """Write version `version`: the FCPXML, plan.json and the paper edit.

    The previous version's files are copied into history/ first. `quiet`
    writes an interim cut (before graphics) without a `write` stage event:
    the app ticks stages off in order and `write` comes last. Everything is
    built in memory before the first file changes, so an error leaves the
    previous version as it was.
    """
    if not quiet:
        progress.stage("write", "Writing the Final Cut file")
    timelines = [rough] + ([stringout] if stringout and stringout.spine else [])
    event = event_name(name)
    xml = build_fcpxml(timelines, analysis, event_name=event, alternates=plan.alternates)
    subtitles = captions_srt(rough, analysis, plan.spellings)
    report = build_report(plan, rough, analysis, stringout=stringout, **report_kwargs)
    page = report_page(report, title=f"{plan.title} - paper edit", subtitle=f"{rough.name} · event {event}")
    out_dir.mkdir(parents=True, exist_ok=True)
    versions.snapshot(out_dir, before=version)
    fcpxml_path = out_dir / f"{slug(name)} v{version}.fcpxml"
    atomic_write(fcpxml_path, xml)
    captions_path = fcpxml_path.with_suffix(".srt")
    if subtitles:
        atomic_write(captions_path, subtitles)
    elif captions_path.is_file():  # an interim write of this version had some; this cut has none
        captions_path.unlink()
    atomic_write(out_dir / "plan.json", json.dumps({**record, "version": version}, indent=1))
    report_path = out_dir / "edit_report.md"
    atomic_write(report_path, report)
    atomic_write(out_dir / "edit_report.html", page)
    versions.record(
        out_dir,
        {
            "n": version,
            "command": command,
            "fcpxml": fcpxml_path.name,
            **({"captions": captions_path.name, "files": [fcpxml_path.name, captions_path.name]} if subtitles else {}),
            "project": rough.name,
            "stringout_project": stringout.name if stringout is not None and stringout.spine else None,
            "event": event,
            "rough_seconds": round(rough.seconds, 3),
            "planned_by": plan.source,
            **(entry or {}),
            **({"partial": True} if quiet else {}),
        },
    )
    versions.finish(out_dir, version, prune=not quiet)
    return fcpxml_path, report_path


def event_name(name: str) -> str:
    return f"{safe_name(name)} ({date.today().isoformat()})"


def name_timelines(rough: Timeline, stringout: Timeline | None, name: str, version: int, style: str, source: str) -> None:
    """'OCVIBE - Rough Cut v3 (tight, Opus)': every version is its own project in Final Cut."""
    base = safe_name(name)
    shape = ", vertical" if rough.height > rough.width else ", square" if rough.height == rough.width else ""
    rough.name = f"{base} - Rough Cut v{version} ({style}{shape}, {planner_label(source)})"
    if stringout is not None:
        stringout.name = f"{base} - Stringout v{version}"


def start_log(out_dir: Path, command: str) -> None:
    """Every run leaves roughcut.log in its output folder: what ran, and how it ended."""
    from .analyze import log_to

    out_dir.mkdir(parents=True, exist_ok=True)
    unfinished = unfinished_run(out_dir / "roughcut.log")
    log_to(out_dir / "roughcut.log")
    progress.start_watch(out_dir / "roughcut.log")  # heartbeat, and a stack dump there if it ever stalls
    log(f"=== roughcut {command} started (output: {out_dir})")
    if unfinished:
        log(f"Note: the previous run here (started {unfinished}) never finished (it was killed, or the Mac slept or shut down); anything it hadn't written yet is still from the run before.")


def unfinished_run(log_path: Path) -> str | None:
    """When the last run in roughcut.log started but never logged FINISHED or STOPPED: its start time."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for i in range(len(lines) - 1, -1, -1):
        if "=== roughcut " in lines[i] and " started" in lines[i]:
            if any(("FINISHED" in ln or "STOPPED" in ln) for ln in lines[i + 1 :]):
                return None
            stamp = lines[i][:19]
            try:
                when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return "earlier"
            return when.strftime("%H:%M") if when.date() == date.today() else when.strftime("%Y-%m-%d %H:%M")
    return None


def build(opts: Options) -> Result:
    t_start = time.monotonic()
    timings: dict[str, float] = {}
    out_dir = (opts.out or default_out_dir(opts.inputs)).expanduser().resolve()
    name = opts.name or (opts.inputs[0].resolve().name if opts.inputs[0].is_dir() else opts.inputs[0].stem)
    _plan_saved_at(None)
    start_log(out_dir, "build")

    # Check the key before spending minutes on transcription: a rejected key
    # is fatal, never a silent fallback to the stringout.
    provider = make_provider(opts.provider, model=opts.model, base_url=opts.base_url, api_key_env=opts.api_key_env, effort=opts.effort)
    if provider is not None:
        log(f"checking {provider.name} access ({provider.model})")
        provider.verify()
    if opts.graphics:
        # Same idea as the key check: find out now, not after transcribing everything.
        if provider is None:
            raise RoughcutError("Motion graphics are designed by the AI, so they need a provider (not --provider none).")
        from . import graphics

        problem = graphics.renderer_problem()
        if problem:
            raise RoughcutError(problem)

    skipped_files: list[str] = []
    analysis = run_analysis(opts, out_dir, skipped=skipped_files)
    timings["analysis"] = time.monotonic() - t_start
    visual_log = None

    usage = []
    extra_warnings: list[str] = []
    if provider is not None:
        usage.append(provider.usage)
        want_vision = opts.vision if opts.vision is not None else provider.supports_vision
        if want_vision:
            from .vision import describe_clips

            vision_provider = provider
            if opts.vision_model and opts.vision_model != provider.model:
                vision_provider = make_provider(opts.provider, model=opts.vision_model, base_url=opts.base_url, api_key_env=opts.api_key_env, vision=True)
                vision_provider.verify()
                usage.append(vision_provider.usage)
            t = time.monotonic()
            cache = Cache(opts.cache_dir or default_cache_dir())
            try:
                with progress.doing(f"describing clips visually with {vision_provider.model}"):
                    extra_warnings += describe_clips(analysis, vision_provider, cache, workers=opts.workers)
            except ProviderLimitError as e:
                raise limit_stop(e, analysis) from e
            timings["visual log"] = time.monotonic() - t
            visual_log = visual_coverage(analysis)

    prompt = (
        "# System\n\n" + PLANNER_SYSTEM + "\n\n# Media\n\n" + render_media(analysis, style_for(opts)) + "\n\n# Brief\n\n"
        + render_brief(opts.context, opts.target_seconds, opts.script, graphics=opts.graphics, graphics_style=opts.graphics_style, preset_brief=opts.preset_brief)
    )

    fell_back = False
    raw_plan = None
    if provider is None:
        plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
    else:
        t = time.monotonic()
        progress.stage("plan", f"Planning the edit with {provider.model}")
        log(f"planning the edit with {provider.name}:{provider.model}")
        try:
            with progress.doing(f"planning the edit with {provider.model}"):
                plan, raw_plan = plan_with_model(provider, analysis, opts)
        except ProviderAuthError:
            raise
        except ProviderLimitError as e:  # not a stringout that looks like a finished cut
            raise limit_stop(e, analysis) from e
        except ProviderError as e:
            log(f"AI planning failed: {e}")
            extra_warnings.append(f"AI planning failed, so this cut is the stringout only: {e}")
            plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
            fell_back = True
        timings["planning"] = time.monotonic() - t
        if not plan.sections:
            extra_warnings.append("The model's plan had no usable sections; fell back to the stringout.")
            plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
            raw_plan = None  # so `render` rebuilds the fallback, not the empty plan
            fell_back = True
    if not plan.sections:
        raise RoughcutError("Nothing to cut: no speech or visual clips were found.")
    # Auto pacing and punch-ins, as the planner read the brief (saved with the run, so re-cuts keep them).
    opts = settings_from_plan(opts, plan)
    asks_graphics = graphics_off_note(opts, out_dir, asked=plan.graphics_asked if plan.source != "heuristic" else None, made_without_ai=provider is None)
    if asks_graphics:
        log(asks_graphics)
        extra_warnings.insert(0, asks_graphics)

    # Save what the model was paid for before anything else can fail. Nothing
    # before this point touches the output folder's earlier files, so a run
    # that stops early leaves the previous cut (and `render`) working.
    version = versions.next_number(out_dir)
    versions.snapshot(out_dir, before=version)  # the previous version's plan and designs, into history/
    record = plan_record(name, plan, raw_plan, analysis, run_settings(opts, provider))
    save_checkpoint(out_dir, analysis, prompt, record)
    # Graphics belong to the cut they were made for: a new build makes its own or none.
    (out_dir / "graphics.json").unlink(missing_ok=True)
    try:
        return _finish_build(
            opts, out_dir, name, provider, analysis, plan, record, fell_back, usage, timings, extra_warnings, t_start, version,
            summary_base={"skipped_files": skipped_files, "visual_log": visual_log},
        )
    except BaseException as e:
        _mark_plan_saved(e, out_dir)
        raise


def _finish_build(
    opts, out_dir, name, provider, analysis, plan, record, fell_back, usage, timings, extra_warnings, t_start, version,
    *, command: str = "build", carry: dict | None = None, report_extra=None, entry_extra: dict | None = None, summary_base: dict | None = None,
) -> Result:
    """Cut, make graphics, write the version. `carry` (revise): earlier graphic designs to reuse."""
    rough, stringout = make_timelines(plan, analysis, opts, name)
    name_timelines(rough, stringout, name, version, opts.style, plan.source)
    extra_warnings += missing_media(analysis, rough, stringout)
    entry = {"style": opts.style, "planner": planner_label(plan.source), **(entry_extra or {})}
    extra = report_extra(rough) if report_extra else {}
    placed, with_graphics = [], analysis
    graphics_note = None
    limit_hit, limit_missing = None, 0
    if opts.graphics and provider is not None and not fell_back:
        if plan.graphics:
            from . import graphics

            # A complete cut without graphics first: if rendering stops, there's still a cut to open.
            pending = [
                "Motion graphics were still being made when this cut was written; if the run stopped there, this cut has none. "
                "`roughcut render` adds any whose designs were saved, and `roughcut render --design-missing` designs the rest."
            ]
            interim = write_outputs(
                out_dir, name, record, plan, analysis, rough, stringout, dict(usage=usage, extra_warnings=extra_warnings + pending, **extra),
                version=version, command=command, entry=entry, quiet=True,
            )
            # result.json follows the folder: this version is the newest cut now, even if graphics never finish.
            save_result(Result(
                out_dir, *interim, plan, rough, stringout, fell_back, analysis.warnings + plan.warnings + rough.warnings + extra_warnings + pending,
                dead_air(analysis.clips, stringout), version=version, project=rough.name, event=event_name(name), settings=record.get("settings") or {},
                summary=run_summary(
                    plan, graphics_on=True, placed=[], with_graphics=analysis, usage=usage, graphics_note="still being made when this cut was written",
                    skipped_files=(summary_base or {}).get("skipped_files"), visual_log=(summary_base or {}).get("visual_log"),
                ),
            ), partial=True)
            t = time.monotonic()
            designer = StopAtLimit(provider)
            with progress.doing(f"making {len(plan.graphics)} motion graphics (design, then render)"):
                if carry is not None:
                    saved_html, house = _carry_designs(designer, plan.graphics, rough, analysis, carry, opts, extra_warnings, out_dir, plan.spellings)
                    g_clips, placed, g_warnings = graphics.make(
                        None, plan.graphics, rough, analysis, out_dir, brief=opts.context, style=opts.graphics_style, workers=opts.workers,
                        saved_html=saved_html, house_style=house,
                    )
                else:
                    g_clips, placed, g_warnings = graphics.make(
                        designer, plan.graphics, rough, analysis, out_dir, brief=opts.context, style=opts.graphics_style, workers=opts.workers,
                        logo=opts.graphics_logo, look=opts.graphics_look, spellings=plan.spellings,
                    )
            extra_warnings += g_warnings
            with_graphics = replace(analysis, clips=analysis.clips + g_clips)
            timings["graphics"] = time.monotonic() - t
            limit_hit = designer.limit
            if limit_hit is not None:
                limit_missing = undesigned(out_dir)
                if limit_missing:
                    graphics_note = f"{limit_missing} not designed: the usage limit was reached"
                    extra_warnings.append(design_missing_hint(out_dir, limit_missing, after_reset=True))
        else:
            extra_warnings.append("Motion graphics were on, but the plan didn't ask for any.")
            graphics_note = "the plan asked for none"
    elif opts.graphics and fell_back:
        extra_warnings.append("Motion graphics were on, but AI planning failed, so this cut has none.")
        graphics_note = "none: AI planning failed"
    timings["total"] = time.monotonic() - t_start
    fcpxml_path, report_path = write_outputs(
        out_dir,
        name,
        record,
        plan,
        with_graphics,
        rough,
        stringout,
        dict(usage=usage, timings=timings, extra_warnings=extra_warnings, graphics=placed, **extra),
        version=version,
        command=command,
        entry=entry,
    )
    warnings = analysis.warnings + plan.warnings + rough.warnings + extra_warnings
    base = summary_base or {}
    summary = run_summary(
        plan, graphics_on=opts.graphics, placed=placed, with_graphics=with_graphics, usage=usage,
        skipped_files=base.get("skipped_files"), visual_log=base.get("visual_log"), graphics_note=graphics_note,
    )
    result = save_result(Result(
        out_dir, fcpxml_path, report_path, plan, rough, stringout, fell_back, warnings, dead_air(analysis.clips, stringout),
        version=version, project=rough.name, event=event_name(name), summary=summary, settings=record.get("settings") or {},
    ))
    if limit_hit is not None:
        raise graphics_limit_stop(limit_hit, result, limit_missing)
    return result


def rerender(out_dir: Path, opts: Options, *, overrides: set[str] | None = None, design_missing: bool = False) -> Result:
    """Rebuild the FCPXML from a saved analysis + plan (no model calls).

    Cut settings (style, format, fillers, levels...) come from the saved run
    unless `overrides` (or, without it, a non-default value in `opts`) says otherwise.
    `design_missing`: also design the planned graphics that have no saved design (a run
    that stopped, or ran out of plan or credit, before designing them). That calls the
    run's model for those graphics only; the cut itself isn't re-planned.
    """
    _plan_saved_at(None)
    _require_output_folder(out_dir)
    start_log(out_dir, "render")
    analysis, saved = _load_saved(out_dir)
    settings = saved.get("settings") or {}
    opts = render_options(opts, settings, overrides)
    measure_missing_levels(analysis, opts)
    name = opts.name or saved.get("name") or out_dir.name.removesuffix("_roughcut")
    source = saved["plan"].get("source", "saved plan")
    plan = normalize(saved.get("raw") or saved["plan"], analysis, source=source)
    plan.target_seconds = saved["plan"].get("target_seconds") or settings.get("target_seconds")
    if saved.get("raw"):
        from .revise import enforce, missing_keep_note, standing_avoids

        # The model's plan, as the revisions that made it left it: what the editor said to leave out stays out...
        revisions = [h for h in saved.get("notes_history") or [] if isinstance(h, dict)]
        avoid = standing_avoids(revisions, analysis=analysis)
        if avoid:
            enforce(plan, analysis, keep=[], avoid=avoid)
        # ...and a line the last one was asked to keep and left out keeps its to-do marker, where that revision put it.
        asked = {missing_keep_note(str(k)) for k in (revisions[-1].get("keep") or [] if revisions else [])}
        plan.flags += [f for f in saved["plan"].get("flags") or [] if isinstance(f, dict) and f.get("note") in asked and f not in plan.flags]
    plan.spellings = _saved_spellings(saved, plan.spellings)  # a revision's, with the ones it carried over
    _shown_texts(plan.graphics, saved)
    respell_graphics(plan.graphics, plan.spellings)
    if not plan.sections:
        plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
    have_manifest = (out_dir / "graphics.json").is_file()
    # graphics.json appears once graphics are designed: a plan with graphics on and no
    # graphics.json stopped before designing them (or none of them fit its cut).
    planned_only = not have_manifest and bool(settings.get("graphics")) and bool(plan.graphics)
    graphics_on = have_manifest or planned_only
    if design_missing and not graphics_on:
        raise RoughcutError(
            "--design-missing: this cut has no motion graphics to design. To add some, revise it with graphics on, e.g.\n  "
            f"roughcut revise {shlex.quote(str(out_dir))} --graphics {shlex.quote('add a title card and lower thirds')}"
        )
    # The next render starts from this one's settings.
    record = {**saved, "name": name, "settings": {**settings, **{f: getattr(opts, f) for f in RENDER_FIELDS}}}
    version = versions.next_number(out_dir)
    rough, stringout = make_timelines(plan, analysis, opts, name)
    name_timelines(rough, stringout, name, version, opts.style, plan.source)
    graphics_warnings: list[str] = missing_media(analysis, rough, stringout)
    placed, with_graphics = [], analysis
    planned_graphics, graphics_note = None, None
    usage: list = []
    limit_hit, undone = None, 0
    if graphics_on:
        # Re-time the saved designs (and any hand edits) to the new cut; only changes re-render.
        from . import graphics

        problem = graphics.renderer_problem()
        if problem:
            graphics_warnings.append(f"Graphics left out of this re-cut: {problem}")
            graphics_note = "left out: the renderer isn't available"
        else:
            if have_manifest:
                # Every graphic of the plan at its index: graphics.json has the last cut's, placed or left out (with
                # their designs), and the plan any an older folder's graphics.json doesn't list.
                specs, html, style = graphics.saved_designs(out_dir)
                specs = graphics.with_planned(specs, _saved_graphics(saved))
                house = saved_house_style(out_dir)
            else:
                specs, html, style, house = list(plan.graphics), {}, str(settings.get("graphics_style") or ""), ""
            planned_graphics = sum(1 for g in specs if g.text)
            # The graphics without a design this cut has room for beside the designed ones: what --design-missing designs.
            # One that would only fit by pushing a designed one out stays out: designing it would just swap them.
            designed = set(html)
            fits, pushes = graphics.room_for(specs, designed, rough, analysis)
            if design_missing:
                if fits:
                    with_room = [g if i in designed or i in fits else replace(g, text="") for i, g in enumerate(specs)]
                    missing = [g for g in graphics.place(with_room, rough, analysis, []) if g.index in fits]
                    designer = StopAtLimit(_saved_provider(settings, source))
                    usage.append(designer.usage)
                    with progress.doing(f"designing {len(missing)} graphics that have no design yet"):
                        html, house = _design_new(
                            designer, missing, html, rough, analysis, house, warnings=graphics_warnings, brief=str(settings.get("context") or ""),
                            style=style, logo=_saved_logo(settings, graphics_warnings), look=tuple(str(p) for p in settings.get("graphics_look") or ()),
                            spellings=plan.spellings,
                        )
                    limit_hit = designer.limit
                elif any(g.text and i not in designed for i, g in enumerate(specs)):
                    log("--design-missing: this cut has no room for a graphic that has no design yet")
                else:
                    log("--design-missing: every graphic in the plan has a design already")
            # Only designed graphics are placed: one without a design never pushes a designed one off the timeline.
            # make() keeps the rest in graphics.json, so a later re-cut (or --design-missing) can place them.
            shown = set(html)
            if shown - designed:
                # A new design goes in where it still fits beside the designed ones (one that didn't come back can change that).
                new, _ = graphics.room_for([g if i in html else replace(g, text="") for i, g in enumerate(specs)], designed, rough, analysis)
                shown = designed | set(new)
            ready = [g if i in shown else replace(g, text="") for i, g in enumerate(specs)]
            # make() rewrites graphics.json and the designs: keep this version's copies first.
            versions.snapshot(out_dir, before=version)
            versions.adopt_old_graphics(out_dir)  # clips named before the digest was in the name are reused, not rendered again
            with progress.doing("rendering the saved motion graphics"):
                g_clips, placed, g_warnings = graphics.make(
                    None, ready, rough, analysis, out_dir, brief="", style=style, workers=opts.workers, saved_html=html, house_style=house, planned=specs,
                )
            graphics_warnings += g_warnings
            with_graphics = replace(analysis, clips=analysis.clips + g_clips)
            undone = sum(1 for i in fits if i not in html)
            for i, g in enumerate(specs):
                if g.text and i in html and i not in shown:
                    graphics_warnings.append(f"graphic {g.text!r} is designed now, but has no room beside the others in this cut; its design waits in graphics/left-out/")
                if not g.text or i in html or i in fits:
                    continue
                if i in pushes:
                    graphics_warnings.append(
                        f"graphic {g.text!r} has no design yet; designing it would push out {', '.join(map(repr, pushes[i]))}, so it's left out"
                    )
                else:
                    graphics_warnings.append(
                        f"graphic {g.text!r} has no design yet, and no room in this cut (its words aren't in it, or it would cover another graphic); left out"
                    )
            if undone:
                graphics_note = (
                    f"{undone} not designed: the usage limit was reached" if limit_hit is not None
                    else f"{undone} not designed yet; `roughcut render --design-missing` adds {'them' if undone > 1 else 'it'}"
                )
                graphics_warnings.append(design_missing_hint(out_dir, undone, after_reset=limit_hit is not None))
    fcpxml_path, report_path = write_outputs(
        out_dir, name, record, plan, with_graphics, rough, stringout, {"graphics": placed, "extra_warnings": graphics_warnings, "usage": usage},
        version=version, command="render", entry={"style": opts.style, "planner": planner_label(plan.source)},
    )
    warnings = plan.warnings + rough.warnings + graphics_warnings
    summary = run_summary(
        plan, graphics_on=graphics_on, placed=placed, with_graphics=with_graphics, planned_graphics=planned_graphics, graphics_note=graphics_note,
        visual_log=visual_coverage(analysis) if any(c.visual for c in analysis.clips) else None, usage=usage, undesigned=undone,
    )
    result = save_result(Result(
        out_dir, fcpxml_path, report_path, plan, rough, stringout, warnings=warnings, dead_air=dead_air(analysis.clips, stringout),
        version=version, project=rough.name, event=event_name(name), summary=summary, settings=record["settings"],
    ))
    if limit_hit is not None:
        raise graphics_limit_stop(limit_hit, result, undone)
    return result


# ------------------------------------------------------------------ revise


def measure_missing_levels(analysis: Analysis, opts: Options) -> None:
    """Folders from before level matching have no loudness saved: measure it now (cached) when levels are on."""
    if opts.dialogue_level is not None:
        measure_levels(analysis.clips, Cache(opts.cache_dir or default_cache_dir()), workers=opts.workers, only_missing=True)


def _saved_logo(settings: dict, warnings: list[str]) -> str | None:
    """The build's logo, for graphics a revision designs; None (with a warning) if the file has gone since."""
    logo = settings.get("graphics_logo")
    if not logo:
        return None
    if not Path(logo).is_file():
        warnings.append(f"The logo {logo} the build used isn't there any more, so new graphics in this revision were designed without it.")
        return None
    return str(logo)


def _require_output_folder(out_dir: Path) -> None:
    if not (out_dir / "analysis.json").is_file() or not (out_dir / "plan.json").is_file():
        raise RoughcutError(f"{out_dir} isn't a roughcut output folder (no analysis.json/plan.json)")


def _load_saved(out_dir: Path) -> tuple[Analysis, dict]:
    """The saved analysis and plan, refusing a plan whose segment IDs belong to other clips."""
    analysis = Analysis.load(out_dir / "analysis.json")
    saved = json.loads((out_dir / "plan.json").read_text())
    digest = saved.get("analysis_digest")
    if digest and digest != analysis_digest(analysis):
        raise RoughcutError(
            "plan.json was made for a different set of clips than analysis.json (the footage changed since it was planned), "
            "so its segment IDs would point at the wrong clips. Run `roughcut build` again."
        )
    return analysis, saved


def _spec_key(spec) -> tuple:
    """What a design shows: a graphic whose text or subtext changed (a name spelled another way) is designed again."""
    return (spec.kind, spec.segment, " ".join(spec.words.lower().split()), spec.text.strip(), spec.subtext.strip())


def saved_house_style(out_dir: Path) -> str:
    """The design system the graphics designer described for this video (in graphics.json), if any."""
    try:
        return str(json.loads((out_dir / "graphics.json").read_text()).get("house_style") or "")
    except (OSError, ValueError, AttributeError):
        return ""


class StopAtLimit:
    """The run's provider, for designing graphics: once a request hits the usage limit, the rest aren't sent.

    Graphics design turns a failed request into a warning and carries on with the next batch;
    after a used-up plan or credit every one would fail the same way. `limit` keeps the first
    limit error, so the run can stop (after writing what it has) and say so.
    """

    def __init__(self, provider):
        self._provider = provider
        self.limit: ProviderLimitError | None = None

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete_json(self, *args, **kwargs):
        if self.limit is not None:
            raise ProviderLimitError("not sent: the usage limit was already reached")
        try:
            return self._provider.complete_json(*args, **kwargs)
        except ProviderLimitError as e:
            if self.limit is None:
                self.limit = e
            raise


def undesigned(out_dir: Path) -> int:
    """Graphics placed in this folder's graphics.json that have no saved design (not the ones the cut left out)."""
    try:
        entries = json.loads((out_dir / "graphics.json").read_text()).get("graphics") or []
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for g in entries if isinstance(g, dict) and g.get("text") and not g.get("html") and g.get("placed") is not False)


def _saved_spellings(saved: dict, fallback: list[dict]) -> list[dict]:
    """The name spellings a saved version used: its plan's, which hold the ones a revision carried over."""
    found = saved.get("plan", {}).get("spellings")
    if not isinstance(found, list):
        return list(fallback)
    # A hand-edited plan.json may hold anything: only whole pairs count.
    return [
        dict(f) for f in found
        if isinstance(f, dict) and all(isinstance(f.get(k), str) and f[k].strip() for k in ("heard", "correct"))
    ]


def _shown_texts(specs: list, saved: dict) -> None:
    """Graphics re-made from the model's plan, with the text and subtext the saved version gave them: a revision
    that undid a name spelling took it off them, and the model's own plan still has it."""
    shown: dict[tuple, list] = {}
    for g in _saved_graphics(saved):
        shown.setdefault((g.kind, g.segment, g.words), []).append(g)
    for g in specs:
        kept = (shown.get((g.kind, g.segment, g.words)) or [None]).pop(0)
        if kept is not None and kept.text:
            g.text, g.subtext = kept.text, kept.subtext


def _saved_graphics(saved: dict) -> list:
    """The graphics of the saved plan (after a revision's --avoid), at the positions graphics.json's indexes refer to."""
    from .plan import GraphicSpec

    out = []
    for g in saved.get("plan", {}).get("graphics") or []:
        try:
            out.append(GraphicSpec(**{k: g[k] for k in ("kind", "segment", "words", "text")}, subtext=str(g.get("subtext") or ""),
                                   seconds=float(g.get("seconds") or 0), direction=str(g.get("direction") or "")))
        except (KeyError, TypeError, ValueError):
            out.append(GraphicSpec(kind="quote", segment="", words="", text=""))
    return out


def design_missing_hint(out_dir: Path, n: int, *, after_reset: bool = False) -> str:
    """How to get graphics that have no design yet, without re-planning the cut."""
    them = "them" if n > 1 else "it"
    return (
        f"{n} graphic{'s' if n > 1 else ''} in the plan {'have' if n > 1 else 'has'} no design yet, so this cut leaves {them} out. "
        + ("After the reset, " if after_reset else "")
        + f"`roughcut render {shlex.quote(str(out_dir))} --design-missing` asks the model for {'those designs' if n > 1 else 'that design'} only "
        f"and makes a new version with {them}; the cut stays as it is and no designed graphic leaves the timeline "
        "(one next to it may come in a moment later or leave sooner to make room)."
    )


def graphics_limit_stop(e: ProviderLimitError, result: Result, missing: int) -> ProviderLimitError:
    """The plan or credit ran out while designing graphics: the cut is written; say with what, and how to finish it."""
    rendered = sum(1 for c in result.timeline.connected if c.kind == "graphic")
    rest = f"the other {missing}" if missing else "the rest"
    stop = ProviderLimitError(
        f"{e} {result.fcpxml.name} is written with {rendered} motion graphic{'s' if rendered != 1 else ''}; the plan and every design "
        f"that came back are saved. After the reset, `roughcut render {shlex.quote(str(result.out_dir))} --design-missing` designs "
        f"{rest} without re-planning the cut."
    )
    stop.cut_written = True  # type: ignore[attr-defined] - plain `render` would only make the same cut again
    return stop


def _saved_provider(settings: dict, source: str) -> Provider:
    """The model that planned this cut (from plan.json), checked, for designing what's missing."""
    name, model = _pick_provider(settings, source, None, None)
    p = make_provider(name, model=model, base_url=settings.get("base_url"), api_key_env=settings.get("api_key_env"), effort=settings.get("effort") or "high")
    if p is None:
        raise RoughcutError("Designing graphics needs a model, and this cut was made with --provider none.")
    log(f"checking {p.name} access ({p.model})")
    p.verify()
    return p


def _design_new(
    provider, new: list, saved_html: dict[int, str], rough: Timeline, analysis: Analysis, house: str, *,
    warnings: list[str], brief: str, style: str, logo: str | None, look: tuple[str, ...], sample: str | None = None,
    spellings: list[dict] | None = None,
) -> tuple[dict[int, str], str]:
    """Design the placed graphics `new` in the look of the ones already made. Returns (html by plan index, house style)."""
    from . import graphics
    from .presets import design_style, inline_logo

    saved_html = dict(saved_html)
    sample = sample if sample is not None else next(iter(saved_html.values()), None)
    if sample:
        style = (style + "\n\n" if style else "") + (
            "Match the graphics already made for this video exactly (type, colors, plates, corner radii, motion). One of them:\n" + sample[:8000]
        )
    found = {"house_style": house}
    graphics.look_under(new, rough, analysis)
    designed = graphics.design(
        provider, new, analysis, rough.frame_duration, brief=brief, style=design_style(style, logo, look),
        width=rough.width, height=rough.height, warnings=warnings, house_style=house, found=found, spellings=spellings,
    )
    for g in new:
        if g.id in designed:
            # With the timing it was made for: the cut may place it differently once every design is in.
            saved_html[g.index] = graphics.with_timing(inline_logo(designed[g.id], logo), g.timing(rough.frame_duration))
    return saved_html, found["house_style"]


def _design_pool(out_dir: Path) -> dict:
    """The current version's graphic designs, for a revision to reuse: by spec key, with where each is saved,
    the house style, and each rendered clip's QC notes (by file name: they belong to that render)."""
    from . import graphics

    pool: dict = {"designs": {}, "paths": {}, "house_style": saved_house_style(out_dir), "notes": {}}
    try:
        manifest = json.loads((out_dir / "graphics.json").read_text())
        entries = [g for g in manifest.get("graphics") or [] if isinstance(g, dict)]
    except (OSError, ValueError, AttributeError):
        return pool
    specs, html, _ = graphics.saved_designs(out_dir)
    for i, page in html.items():
        if i < len(specs) and specs[i].text:
            pool["designs"][_spec_key(specs[i])] = page
    for g in entries:
        try:
            index = int(g.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(specs) and index in html and g.get("html"):
            pool["paths"].setdefault(_spec_key(specs[index]), str(g["html"]))
        if g.get("file") and g.get("notes"):
            pool["notes"][Path(str(g["file"])).name] = [str(n) for n in g["notes"]]
    return pool


def _carried_manifest(out_dir: Path, specs: list, carry: dict, style: str) -> None:
    """graphics.json for a revision until its graphics are placed: every planned graphic, with the earlier
    design where it's unchanged. A run that stops before then leaves `roughcut render` the designs that
    still apply, never the old texts of graphics the notes changed."""
    entries = []
    for i, spec in enumerate(specs):
        entry = {"index": i, **asdict(spec)}
        rel = (carry.get("paths") or {}).get(_spec_key(spec))
        if rel:
            entry["html"] = rel
        entries.append(entry)
    house = carry.get("house_style") or ""
    atomic_write(out_dir / "graphics.json", json.dumps({"style": style, **({"house_style": house} if house else {}), "graphics": entries}, indent=1))


def _provisional_manifest(
    out_dir: Path, specs: list, placed: list, saved_html: dict[int, str], rough: Timeline, style: str, house: str, notes_by_file: dict,
) -> None:
    """graphics.json for this version before any new design is asked for: the carried designs, the clips
    they already have, and those clips' QC notes (and the planned graphics this cut leaves out, with any
    design they have). If the run stops while new ones are designed, `roughcut render` re-times these and
    says the rest have no design yet."""
    from . import graphics

    html = {g.id: saved_html[g.index] for g in placed if g.index in saved_html}
    folder = out_dir / "graphics"
    graphics.save_designs(placed, html, folder)
    waiting = graphics.keep_left_out(specs, placed, saved_html, out_dir)
    kept = graphics.unchanged(placed, html, folder, rough, {})
    notes = {gid: list(notes_by_file[path.name]) for gid, path in kept.items() if path.name in notes_by_file}
    graphics.write_manifest(out_dir, placed, kept, html, rough, style, notes=notes, house_style=house, left_out=waiting)


def _carry_designs(
    provider, specs, rough: Timeline, analysis: Analysis, carry: dict, opts: Options, warnings: list[str], out_dir: Path,
    spellings: list[dict] | None = None,
) -> tuple[dict[int, str], str]:
    """HTML for each planned graphic: the earlier design when the graphic is unchanged, else a new one in the same look.

    `carry`: _design_pool() of the version being revised. Returns (html by plan index, house style).
    graphics.json is rewritten with the carried designs before any model call.
    """
    from . import graphics

    designs, house = carry.get("designs") or {}, carry.get("house_style") or ""
    saved_html = {i: designs[_spec_key(spec)] for i, spec in enumerate(specs) if _spec_key(spec) in designs}
    placed = graphics.place(specs, rough, analysis, [])  # make() places again and reports what didn't fit
    if placed:
        _provisional_manifest(out_dir, specs, placed, saved_html, rough, opts.graphics_style, house, carry.get("notes") or {})
    else:
        (out_dir / "graphics.json").unlink(missing_ok=True)  # none fit this cut: this version has no graphics
    new = [g for g in placed if g.index not in saved_html]
    if new and provider is not None:
        saved_html, house = _design_new(
            provider, new, saved_html, rough, analysis, house, warnings=warnings, brief=opts.context, style=opts.graphics_style,
            logo=opts.graphics_logo, look=opts.graphics_look, sample=next(iter(designs.values()), None), spellings=spellings,
        )
    log(f"graphics: {len(placed) - len(new)} kept from the previous version, {len(new)} new")
    return saved_html, house


def _pick_provider(settings: dict, source: str, provider: str | None, model: str | None) -> tuple[str, str | None]:
    """The revision uses the run's own planner unless told otherwise."""
    saved_name = settings.get("provider")
    from_source = source.split(":", 1)[0] if ":" in source else None
    name = provider or (saved_name if saved_name and saved_name != "none" else None) or from_source or "anthropic"
    if model:
        return name, model
    if saved_name == name and settings.get("model"):
        return name, settings["model"]
    if from_source == name:
        return name, source.split(":", 1)[1]
    return name, None


def revise(
    out_dir: Path,
    notes: str,
    opts: Options,
    *,
    keep: list[str] | None = None,
    avoid: list[str] | None = None,
    overrides: set[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    target_seconds: float | None = None,
    graphics: bool | None = None,
) -> Result:
    """Re-plan the latest version from the editor's notes, as a new version. No re-transcription.

    The planner sees the build's system prompt and media block (prompt-cached),
    the previous plan and the notes. Arguments left as None come from the saved run.
    """
    from .revise import describe_changes, enforce, recover_brief, report_block, revision_request, split_ids, standing_avoids, wide_lifts

    t_start = time.monotonic()
    out_dir = out_dir.expanduser().resolve()
    _plan_saved_at(None)
    _require_output_folder(out_dir)
    start_log(out_dir, "revise")
    analysis, saved = _load_saved(out_dir)
    settings = dict(saved.get("settings") or {})
    if "context" not in settings and (out_dir / "prompt.md").is_file():
        # A folder from before settings were saved: the brief is in the prompt it sent.
        for k, v in recover_brief((out_dir / "prompt.md").read_text(encoding="utf-8")).items():
            settings.setdefault(k, v)
    # The cut settings given to revise: they win over what the notes ask for.
    given = set(overrides) if overrides is not None else {f for f in RENDER_FIELDS if getattr(opts, f) != getattr(Options(inputs=[]), f)}
    opts = render_options(opts, settings, given)
    opts = replace(
        opts,
        stringout=False,  # one new Rough Cut; the stringout doesn't change with notes
        context=settings.get("context") or "",
        preset_brief=settings.get("preset_brief") or "",
        script=settings.get("script"),
        target_seconds=target_seconds if target_seconds is not None else settings.get("target_seconds"),
        graphics=graphics if graphics is not None else bool(settings.get("graphics")),
        graphics_style=settings.get("graphics_style") or "",
        graphics_look=tuple(str(page) for page in settings.get("graphics_look") or ()),
        base_url=settings.get("base_url"),
        api_key_env=settings.get("api_key_env"),
        effort=effort or settings.get("effort") or "high",
    )
    name = opts.name or saved.get("name") or out_dir.name.removesuffix("_roughcut")
    source = saved["plan"].get("source", "")
    provider_name, model_name = _pick_provider(settings, source, provider, model)
    opts = replace(opts, provider=provider_name, model=model_name)
    p = make_provider(provider_name, model=model_name, base_url=opts.base_url, api_key_env=opts.api_key_env, effort=opts.effort)
    if p is None:
        raise RoughcutError("Revising re-plans the cut with a model, so it needs a provider (not --provider none).")
    log(f"checking {p.name} access ({p.model})")
    p.verify()
    if opts.graphics:
        from . import graphics as gfx

        problem = gfx.renderer_problem()
        if problem:
            raise RoughcutError(problem)
    extra_warnings: list[str] = []
    opts = replace(opts, graphics_logo=_saved_logo(settings, extra_warnings if opts.graphics else []))
    measure_missing_levels(analysis, opts)

    revisions = [h for h in saved.get("notes_history") or [] if isinstance(h, dict)]
    history = [h for h in revisions if h.get("notes")]  # a revision may be only --keep/--avoid
    keep_ids, avoid_ids = split_ids(keep or []), split_ids(avoid or [])
    previous = normalize(saved.get("raw") or saved["plan"], analysis, source=source)
    made_without = standing_avoids(revisions, analysis=analysis)
    if made_without:
        # The cut as it was made: what earlier notes left out isn't in it (the model's own plan may still have it).
        enforce(previous, analysis, keep=[], avoid=made_without)
    previous.spellings = _saved_spellings(saved, previous.spellings)  # with the ones it carried over: the reviser sees them all
    _shown_texts(previous.graphics, saved)
    respell_graphics(previous.graphics, previous.spellings)
    # Everything the editor has said to leave out, in these notes or earlier ones, unless these notes keep it.
    leave_out = standing_avoids(revisions + [{"keep": keep_ids, "avoid": avoid_ids}], analysis=analysis)
    # Only a clip these notes don't leave out again comes back: "--keep V01.S005 --avoid V01" keeps V01 out.
    for note in wide_lifts([a for a in made_without if a not in leave_out], keep_ids, analysis):
        log(note)
        extra_warnings.append(note)
    request = revision_request(
        previous, analysis, notes, context=opts.context, target_seconds=opts.target_seconds, script=opts.script,
        graphics=opts.graphics, graphics_style=opts.graphics_style, earlier_notes=[h["notes"] for h in history], keep=keep_ids, avoid=leave_out,
        preset_brief=opts.preset_brief, style=opts.style, punch_in=bool(opts.punch_in),
    )
    prev_entry = versions.current(out_dir) or {}
    prev_n = prev_entry.get("n")
    cut_style = style_for(opts)  # the media block shows what this cut keeps of each line (the build sent the same at its pacing)
    t = time.monotonic()
    progress.stage("plan", f"Revising the edit with {p.model}")
    log(f"revising the edit with {p.name}:{p.model}: {notes.strip()[:200]}")
    try:
        with progress.doing(f"revising the edit with {p.model}"):
            raw = p.complete_json(
                PLANNER_SYSTEM, [media_part(analysis, cut_style), TextPart(request)], plan_schema(opts.graphics),
                schema_name="edit_plan", purpose="revision", effort=opts.effort, max_tokens=128000,
            )
    except ProviderAuthError:
        raise
    except ProviderError as e:
        still = f"v{prev_n} is still the latest cut." if prev_n else "the previous cut is still there."
        if isinstance(e, ProviderLimitError):
            raise ProviderLimitError(f"{e} Nothing changed: {still} Run the same revise again after the reset.") from e
        raise ProviderError(f"{e} Nothing changed: {still}") from e
    timings = {"revision": time.monotonic() - t}
    plan = normalize(raw, analysis, source=f"{p.name}:{p.model}")
    enforced = enforce(plan, analysis, keep=keep_ids, avoid=leave_out, previous=previous)
    plan.warnings += enforced.warnings
    # Name spellings found before still hold: the revision's own first, then the earlier ones it didn't
    # repeat, except one it undid by returning it as heard ("correct" the same as "heard").
    heard = {spelling_key(f["heard"]) for f in plan.spellings}
    for fix in raw.get("spellings") or []:
        h, c = clean_spelling(str(fix.get("heard") or ""), str(fix.get("correct") or "")) if isinstance(fix, dict) else ("", "")
        if h and h == c:
            heard.add(spelling_key(h))
    for fix in previous.spellings:
        h, c = clean_spelling(str(fix.get("heard") or ""), str(fix.get("correct") or ""))
        if h and c and h != c and spelling_key(h) not in heard:
            plan.spellings.append({"heard": h, "correct": c})
            heard.add(spelling_key(h))
    # One it undid or spelled another way comes off the graphics an earlier version respelled with it
    # ("Up Mount Baldy" is "Up the mountain" again), before the ones in force go on; a name a pair in force
    # spells keeps its spelling ("Chef Rémy Lovain" while "Rémy" alone is undone).
    in_force = {(spelling_key(f["heard"]), f["correct"]) for f in plan.spellings}
    dropped = []
    for fix in previous.spellings:
        h, c = clean_spelling(str(fix.get("heard") or ""), str(fix.get("correct") or ""))
        if h and c and h != c and (spelling_key(h), c) not in in_force:
            dropped.append({"heard": c, "correct": h})
    respell_graphics(plan.graphics, dropped, keep=plan.spellings)
    respell_graphics(plan.graphics, plan.spellings)
    if not plan.sections:
        raise RoughcutError("The revised plan had no usable sections, so nothing changed. Try the notes again, worded differently.")

    carry = _design_pool(out_dir) if opts.graphics else None

    before_seconds = prev_entry.get("rough_seconds")
    if before_seconds is None:
        before_seconds = build_timeline(previous, analysis, style=style_for(opts), name="previous", format_override=opts.format, fill=opts.fill).seconds

    # Pacing and punch-ins as the reviser read the notes ("make it looser", "remove the punch-ins"), unless a flag says otherwise.
    opts, settings_notes = settings_from_notes(plan, opts, given, prev_n)
    if not opts.graphics and plan.graphics_asked:
        settings_notes.append(
            f"Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none. "
            f"Revise with them on: `roughcut revise {shlex.quote(str(out_dir))} --graphics` and the same notes."
        )
        extra_warnings.append(settings_notes[-1])
    for note in settings_notes:
        log(note)

    version = versions.next_number(out_dir)
    versions.snapshot(out_dir, before=version)
    new_settings = {**settings, **run_settings(opts, p), "stringout": settings.get("stringout", True), "vision_model": settings.get("vision_model")}
    record = plan_record(name, plan, raw, analysis, new_settings)
    record["notes_history"] = revisions + [{"version": version, "notes": notes.strip(), "keep": keep_ids, "avoid": avoid_ids}]
    if prev_n:
        record["revision_of"] = prev_n
    prompt = "# System\n\n" + PLANNER_SYSTEM + "\n\n# Media\n\n" + render_media(analysis, cut_style) + "\n\n# Brief\n\n" + request
    save_checkpoint(out_dir, analysis, prompt, record)
    if opts.graphics:
        versions.adopt_old_graphics(out_dir)  # unchanged graphics from an older release's folder: reuse their clips
    if carry is not None and plan.graphics:
        # The new version's graphics, with the designs it keeps, from now on: a stop before they're
        # made leaves `roughcut render` those designs (never the old texts of changed graphics).
        _carried_manifest(out_dir, plan.graphics, carry, opts.graphics_style)
    else:
        (out_dir / "graphics.json").unlink(missing_ok=True)  # this version has no graphics

    def report_extra(rough: Timeline) -> dict:
        changes = describe_changes(previous, plan, analysis, before_seconds=before_seconds, after_seconds=rough.seconds, previous_version=prev_n)
        return {"revision": report_block(notes, [h["notes"] for h in history], changes, enforced.missing_keeps, settings_notes)}

    try:
        return _finish_build(
            opts, out_dir, name, p, analysis, plan, record, False, [p.usage], timings, extra_warnings, t_start, version,
            command="revise", carry=carry, report_extra=report_extra, entry_extra={"notes": notes.strip(), "revision_of": prev_n},
        )
    except BaseException as e:
        _mark_plan_saved(e, out_dir)
        raise

"""End-to-end: folder in, FCPXML + paper edit out."""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from . import progress, versions
from .analyze import Analysis, Cache, analyze, default_cache_dir, log, measure_levels
from .fcpxml import build_fcpxml
from .media import AUDIO_EXTS, require_tools, scan
from .plan import Plan, heuristic_plan, normalize, plan_schema
from .prompts import PLANNER_SYSTEM, render_brief, render_media
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
    script: str | None = None
    target_seconds: float | None = None
    style: str = "medium"
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
) -> dict:
    """Counts for the end of a run: graphics planned/placed/rendered, visual-log coverage, what was skipped, cost."""
    graphics = None
    if graphics_on:
        graphics = {
            "planned": len(plan.graphics) if planned_graphics is None else planned_graphics,
            "placed": len(placed),
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
RENDER_FIELDS = ("style", "format", "keep_fillers", "broll_db", "music_db", "fill", "stringout", "dialogue_level", "fades")


def run_settings(opts: Options, provider: Provider | None = None) -> dict:
    """Everything needed to re-cut (render) or re-plan (revise) this run later, saved in plan.json."""
    return {
        **{f: getattr(opts, f) for f in RENDER_FIELDS},
        "context": opts.context,
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
    return replace(opts, **saved)


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
    base = STYLES[opts.style]
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
        TextPart(render_brief(opts.context, opts.target_seconds, opts.script, graphics=opts.graphics, graphics_style=opts.graphics_style)),
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
        target_seconds=plan.target_seconds,
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
    report = build_report(plan, rough, analysis, stringout=stringout, **report_kwargs)
    page = report_page(report, title=f"{plan.title} - paper edit", subtitle=f"{rough.name} · event {event}")
    out_dir.mkdir(parents=True, exist_ok=True)
    versions.snapshot(out_dir, before=version)
    fcpxml_path = out_dir / f"{slug(name)} v{version}.fcpxml"
    atomic_write(fcpxml_path, xml)
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
        + render_brief(opts.context, opts.target_seconds, opts.script, graphics=opts.graphics, graphics_style=opts.graphics_style)
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
                dead_air(analysis.clips, stringout), version=version, project=rough.name, event=event_name(name),
                summary=run_summary(
                    plan, graphics_on=True, placed=[], with_graphics=analysis, usage=usage, graphics_note="still being made when this cut was written",
                    skipped_files=(summary_base or {}).get("skipped_files"), visual_log=(summary_base or {}).get("visual_log"),
                ),
            ), partial=True)
            t = time.monotonic()
            designer = StopAtLimit(provider)
            with progress.doing(f"making {len(plan.graphics)} motion graphics (design, then render)"):
                if carry is not None:
                    saved_html, house = _carry_designs(designer, plan.graphics, rough, analysis, carry, opts, extra_warnings, out_dir)
                    g_clips, placed, g_warnings = graphics.make(
                        None, plan.graphics, rough, analysis, out_dir, brief=opts.context, style=opts.graphics_style, workers=opts.workers,
                        saved_html=saved_html, house_style=house,
                    )
                else:
                    g_clips, placed, g_warnings = graphics.make(
                        designer, plan.graphics, rough, analysis, out_dir, brief=opts.context, style=opts.graphics_style, workers=opts.workers,
                        logo=opts.graphics_logo, look=opts.graphics_look,
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
        version=version, project=rough.name, event=event_name(name), summary=summary,
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
                specs, html, style = graphics.saved_designs(out_dir)
                house = saved_house_style(out_dir)
            else:
                specs, html, style, house = list(plan.graphics), {}, str(settings.get("graphics_style") or ""), ""
            planned_graphics = sum(1 for g in specs if g.text)
            if design_missing:
                missing = [g for g in graphics.place(specs, rough, analysis, []) if g.index not in html]
                if missing:
                    designer = StopAtLimit(_saved_provider(settings, source))
                    usage.append(designer.usage)
                    with progress.doing(f"designing {len(missing)} graphics that have no design yet"):
                        html, house = _design_new(
                            designer, missing, html, rough, analysis, house, warnings=graphics_warnings, brief=str(settings.get("context") or ""),
                            style=style, logo=_saved_logo(settings, graphics_warnings), look=tuple(str(p) for p in settings.get("graphics_look") or ()),
                        )
                    limit_hit = designer.limit
                else:
                    log("--design-missing: no graphic placed in this cut is missing a design")
            # make() rewrites graphics.json and the designs: keep this version's copies first.
            versions.snapshot(out_dir, before=version)
            versions.adopt_old_graphics(out_dir)  # clips named before the digest was in the name are reused, not rendered again
            with progress.doing("rendering the saved motion graphics"):
                g_clips, placed, g_warnings = graphics.make(
                    None, specs, rough, analysis, out_dir, brief="", style=style, workers=opts.workers, saved_html=html, house_style=house
                )
            graphics_warnings += g_warnings
            with_graphics = replace(analysis, clips=analysis.clips + g_clips)
            undone = sum(1 for g in placed if g.index not in html)
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
        visual_log=visual_coverage(analysis) if any(c.visual for c in analysis.clips) else None, usage=usage,
    )
    result = save_result(Result(
        out_dir, fcpxml_path, report_path, plan, rough, stringout, warnings=warnings, dead_air=dead_air(analysis.clips, stringout),
        version=version, project=rough.name, event=event_name(name), summary=summary,
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
    return (spec.kind, spec.segment, " ".join(spec.words.lower().split()), spec.text.strip())


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
    """Graphics in this folder's graphics.json that have no saved design."""
    try:
        entries = json.loads((out_dir / "graphics.json").read_text()).get("graphics") or []
    except (OSError, ValueError, AttributeError):
        return 0
    return sum(1 for g in entries if isinstance(g, dict) and g.get("text") and not g.get("html"))


def design_missing_hint(out_dir: Path, n: int, *, after_reset: bool = False) -> str:
    """How to get graphics that have no design yet, without re-planning the cut."""
    them = "them" if n > 1 else "it"
    return (
        f"{n} graphic{'s' if n > 1 else ''} in the plan {'have' if n > 1 else 'has'} no design yet, so this cut leaves {them} out. "
        + ("After the reset, " if after_reset else "")
        + f"`roughcut render {shlex.quote(str(out_dir))} --design-missing` asks the model for {'those designs' if n > 1 else 'that design'} only "
        f"and makes a new version with {them}; the cut and the other graphics stay as they are."
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
        width=rough.width, height=rough.height, warnings=warnings, house_style=house, found=found,
    )
    for g in new:
        if g.id in designed:
            saved_html[g.index] = inline_logo(designed[g.id], logo)
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


def _provisional_manifest(out_dir: Path, placed: list, saved_html: dict[int, str], rough: Timeline, style: str, house: str, notes_by_file: dict) -> None:
    """graphics.json for this version before any new design is asked for: the carried designs, the clips
    they already have, and those clips' QC notes. If the run stops while new ones are designed,
    `roughcut render` re-times these and says the rest have no design yet."""
    from . import graphics

    html = {g.id: saved_html[g.index] for g in placed if g.index in saved_html}
    folder = out_dir / "graphics"
    graphics.save_designs(placed, html, folder)
    kept = graphics.unchanged(placed, html, folder, rough, {})
    notes = {gid: list(notes_by_file[path.name]) for gid, path in kept.items() if path.name in notes_by_file}
    graphics.write_manifest(out_dir, placed, kept, html, rough, style, notes=notes, house_style=house)


def _carry_designs(
    provider, specs, rough: Timeline, analysis: Analysis, carry: dict, opts: Options, warnings: list[str], out_dir: Path,
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
        _provisional_manifest(out_dir, placed, saved_html, rough, opts.graphics_style, house, carry.get("notes") or {})
    else:
        (out_dir / "graphics.json").unlink(missing_ok=True)  # none fit this cut: this version has no graphics
    new = [g for g in placed if g.index not in saved_html]
    if new and provider is not None:
        saved_html, house = _design_new(
            provider, new, saved_html, rough, analysis, house, warnings=warnings, brief=opts.context, style=opts.graphics_style,
            logo=opts.graphics_logo, look=opts.graphics_look, sample=next(iter(designs.values()), None),
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
    from .revise import describe_changes, enforce, recover_brief, report_block, revision_request, split_ids

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
    opts = render_options(opts, settings, overrides)
    opts = replace(
        opts,
        stringout=False,  # one new Rough Cut; the stringout doesn't change with notes
        context=settings.get("context") or "",
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

    previous = normalize(saved.get("raw") or saved["plan"], analysis, source=source)
    history = [h for h in saved.get("notes_history") or [] if isinstance(h, dict) and h.get("notes")]
    keep_ids, avoid_ids = split_ids(keep or []), split_ids(avoid or [])
    request = revision_request(
        previous, analysis, notes, context=opts.context, target_seconds=opts.target_seconds, script=opts.script,
        graphics=opts.graphics, graphics_style=opts.graphics_style, earlier_notes=[h["notes"] for h in history], keep=keep_ids, avoid=avoid_ids,
    )
    prev_entry = versions.current(out_dir) or {}
    prev_n = prev_entry.get("n")
    t = time.monotonic()
    progress.stage("plan", f"Revising the edit with {p.model}")
    log(f"revising the edit with {p.name}:{p.model}: {notes.strip()[:200]}")
    try:
        with progress.doing(f"revising the edit with {p.model}"):
            raw = p.complete_json(
                PLANNER_SYSTEM, [media_part(analysis, style_for(opts)), TextPart(request)], plan_schema(opts.graphics),
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
    enforced = enforce(plan, analysis, keep=keep_ids, avoid=avoid_ids, previous=previous)
    plan.warnings += enforced.warnings
    if not plan.sections:
        raise RoughcutError("The revised plan had no usable sections, so nothing changed. Try the notes again, worded differently.")

    carry = _design_pool(out_dir) if opts.graphics else None

    before_seconds = prev_entry.get("rough_seconds")
    if before_seconds is None:
        before_seconds = build_timeline(previous, analysis, style=style_for(opts), name="previous", format_override=opts.format, fill=opts.fill).seconds

    version = versions.next_number(out_dir)
    versions.snapshot(out_dir, before=version)
    new_settings = {**settings, **run_settings(opts, p), "stringout": settings.get("stringout", True), "vision_model": settings.get("vision_model")}
    record = plan_record(name, plan, raw, analysis, new_settings)
    record["notes_history"] = history + [{"version": version, "notes": notes.strip(), "keep": keep_ids, "avoid": avoid_ids}]
    if prev_n:
        record["revision_of"] = prev_n
    prompt = "# System\n\n" + PLANNER_SYSTEM + "\n\n# Media\n\n" + render_media(analysis, style_for(opts)) + "\n\n# Brief\n\n" + request
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
        return {"revision": report_block(notes, [h["notes"] for h in history], changes, enforced.missing_keeps)}

    try:
        return _finish_build(
            opts, out_dir, name, p, analysis, plan, record, False, [p.usage], timings, extra_warnings, t_start, version,
            command="revise", carry=carry, report_extra=report_extra, entry_extra={"notes": notes.strip(), "revision_of": prev_n},
        )
    except BaseException as e:
        _mark_plan_saved(e, out_dir)
        raise

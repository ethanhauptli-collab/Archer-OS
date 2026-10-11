"""End-to-end: folder in, FCPXML + paper edit out."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from . import progress
from .analyze import Analysis, Cache, analyze, default_cache_dir, log
from .fcpxml import build_fcpxml
from .media import require_tools, scan
from .plan import Plan, heuristic_plan, normalize, plan_schema
from .prompts import PLANNER_SYSTEM, render_brief, render_media
from .providers import Provider, ProviderAuthError, ProviderError, TextPart, make_provider
from .report import build_report
from .timeline import STYLES, Style, Timeline, build_timeline, dead_air
from .transcribe import Transcriber, resolve_backend
from . import versions
from .versions import atomic_write, planner_label, safe_name


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
    stringout: bool = True
    fill: bool = True  # cover narration the plan left without picture
    graphics: bool = False  # Claude-designed motion graphics, rendered to ProRes 4444
    graphics_style: str = ""  # look and brand for them: colors, fonts, mood
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
    }


# Cut settings `roughcut render` takes from the saved run unless a flag overrides them.
RENDER_FIELDS = ("style", "format", "keep_fillers", "broll_db", "music_db", "fill", "stringout")


def run_settings(opts: Options, provider: Provider | None = None) -> dict:
    """Everything needed to re-cut (render) or re-plan (revise) this run later, saved in plan.json."""
    return {
        **{f: getattr(opts, f) for f in RENDER_FIELDS},
        "context": opts.context,
        "target_seconds": opts.target_seconds,
        "script": opts.script,
        "graphics": opts.graphics,
        "graphics_style": opts.graphics_style,
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


def save_checkpoint(out_dir: Path, analysis: Analysis, prompt: str, record: dict) -> None:
    """The paid-for part of a run, saved before anything that can still fail (cutting, graphics).

    From here on `roughcut render` can finish the cut without calling the model again.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / "analysis.json", json.dumps(analysis.to_json(), indent=1))
    atomic_write(out_dir / "prompt.md", prompt)
    atomic_write(out_dir / "plan.json", json.dumps(record, indent=1))
    if record.get("raw"):
        log("plan saved: `roughcut render` can finish this cut without calling the model again")


def _mark_plan_saved(e: BaseException, out_dir: Path) -> None:
    """Tell the CLI the plan survived, so it can print how to finish the cut."""
    try:
        e.plan_saved = out_dir  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - some exception types refuse attributes
        pass


def slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"\s+", " ", s) or "Roughcut"


def default_out_dir(inputs: list[Path]) -> Path:
    first = inputs[0].expanduser().resolve()
    base = first if first.is_dir() else first.parent
    return base.parent / f"{base.name}_roughcut"


def style_for(opts: Options) -> Style:
    base = STYLES[opts.style]
    if opts.keep_fillers:
        return Style(base.pad_in, base.pad_out, base.max_gap, remove_fillers=False, min_broll=base.min_broll)
    return base


def run_analysis(opts: Options, out_dir: Path) -> Analysis:
    require_tools()
    progress.stage("scan", "Finding media")
    files = scan(opts.inputs, exclude=[out_dir])
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
    return analyze(
        files, transcriber=transcriber, cache=cache, transcript_dirs=opts.transcript_dirs, workers=opts.workers, prefer_vertical=prefer_vertical
    )


def plan_with_model(provider: Provider, analysis: Analysis, opts: Options) -> tuple[Plan, dict]:
    parts = [
        TextPart("<media>\n" + render_media(analysis) + "\n</media>", cache=True),
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
    rough = build_timeline(
        plan, analysis, style=style, name=f"{name} - Rough Cut", format_override=opts.format, broll_db=opts.broll_db, music_db=opts.music_db, fill=opts.fill
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
    xml = build_fcpxml(timelines, analysis, event_name=event)
    report = build_report(plan, rough, analysis, stringout=stringout, **report_kwargs)
    out_dir.mkdir(parents=True, exist_ok=True)
    versions.snapshot(out_dir, before=version)
    fcpxml_path = out_dir / f"{slug(name)} v{version}.fcpxml"
    atomic_write(fcpxml_path, xml)
    atomic_write(out_dir / "plan.json", json.dumps({**record, "version": version}, indent=1))
    report_path = out_dir / "edit_report.md"
    atomic_write(report_path, report)
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

    analysis = run_analysis(opts, out_dir)
    timings["analysis"] = time.monotonic() - t_start

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
            extra_warnings += describe_clips(analysis, vision_provider, cache, workers=opts.workers)
            timings["visual log"] = time.monotonic() - t

    prompt = (
        "# System\n\n" + PLANNER_SYSTEM + "\n\n# Media\n\n" + render_media(analysis) + "\n\n# Brief\n\n"
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
            plan, raw_plan = plan_with_model(provider, analysis, opts)
        except ProviderAuthError:
            raise
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
        return _finish_build(opts, out_dir, name, provider, analysis, plan, record, fell_back, usage, timings, extra_warnings, t_start, version)
    except BaseException as e:
        _mark_plan_saved(e, out_dir)
        raise


def _finish_build(opts, out_dir, name, provider, analysis, plan, record, fell_back, usage, timings, extra_warnings, t_start, version) -> Result:
    rough, stringout = make_timelines(plan, analysis, opts, name)
    name_timelines(rough, stringout, name, version, opts.style, plan.source)
    entry = {"style": opts.style, "planner": planner_label(plan.source)}
    placed, with_graphics = [], analysis
    if opts.graphics and provider is not None and not fell_back:
        if plan.graphics:
            from . import graphics

            # A complete cut without graphics first: if rendering stops, there's still a cut to open.
            pending = ["Motion graphics were still being made when this cut was written; if the run stopped there, this cut has none. `roughcut render` adds any whose designs were saved."]
            write_outputs(
                out_dir, name, record, plan, analysis, rough, stringout, dict(usage=usage, extra_warnings=extra_warnings + pending),
                version=version, entry=entry, quiet=True,
            )
            t = time.monotonic()
            g_clips, placed, g_warnings = graphics.make(
                provider, plan.graphics, rough, analysis, out_dir, brief=opts.context, style=opts.graphics_style, workers=opts.workers
            )
            extra_warnings += g_warnings
            with_graphics = replace(analysis, clips=analysis.clips + g_clips)
            timings["graphics"] = time.monotonic() - t
        else:
            extra_warnings.append("Motion graphics were on, but the plan didn't ask for any.")
    elif opts.graphics and fell_back:
        extra_warnings.append("Motion graphics were on, but AI planning failed, so this cut has none.")
    timings["total"] = time.monotonic() - t_start
    fcpxml_path, report_path = write_outputs(
        out_dir,
        name,
        record,
        plan,
        with_graphics,
        rough,
        stringout,
        dict(usage=usage, timings=timings, extra_warnings=extra_warnings, graphics=placed),
        version=version,
        entry=entry,
    )
    warnings = analysis.warnings + plan.warnings + rough.warnings + extra_warnings
    return Result(
        out_dir, fcpxml_path, report_path, plan, rough, stringout, fell_back, warnings, dead_air(analysis.clips, stringout),
        version=version, project=rough.name, event=event_name(name),
    )


def rerender(out_dir: Path, opts: Options, *, overrides: set[str] | None = None) -> Result:
    """Rebuild the FCPXML from a saved analysis + plan (no model calls).

    Cut settings (style, format, fillers, levels...) come from the saved run
    unless `overrides` (or, without it, a non-default value in `opts`) says otherwise.
    """
    if not (out_dir / "analysis.json").is_file() or not (out_dir / "plan.json").is_file():
        raise RoughcutError(f"{out_dir} isn't a roughcut output folder (no analysis.json/plan.json)")
    start_log(out_dir, "render")
    analysis = Analysis.load(out_dir / "analysis.json")
    saved = json.loads((out_dir / "plan.json").read_text())
    digest = saved.get("analysis_digest")
    if digest and digest != analysis_digest(analysis):
        raise RoughcutError(
            "plan.json was made for a different set of clips than analysis.json (the footage changed since it was planned), "
            "so its segment IDs would point at the wrong clips. Run `roughcut build` again."
        )
    settings = saved.get("settings") or {}
    opts = render_options(opts, settings, overrides)
    name = opts.name or saved.get("name") or out_dir.name.removesuffix("_roughcut")
    source = saved["plan"].get("source", "saved plan")
    plan = normalize(saved.get("raw") or saved["plan"], analysis, source=source)
    if not plan.sections:
        plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
    # The next render starts from this one's settings.
    record = {**saved, "name": name, "settings": {**settings, **{f: getattr(opts, f) for f in RENDER_FIELDS}}}
    version = versions.next_number(out_dir)
    rough, stringout = make_timelines(plan, analysis, opts, name)
    name_timelines(rough, stringout, name, version, opts.style, plan.source)
    graphics_warnings: list[str] = []
    placed, with_graphics = [], analysis
    if (out_dir / "graphics.json").is_file():
        # Re-time the saved designs (and any hand edits) to the new cut; only changes re-render.
        from . import graphics

        problem = graphics.renderer_problem()
        if problem:
            graphics_warnings.append(f"Graphics left out of this re-cut: {problem}")
        else:
            specs, html, style = graphics.saved_designs(out_dir)
            g_clips, placed, g_warnings = graphics.make(None, specs, rough, analysis, out_dir, brief="", style=style, workers=opts.workers, saved_html=html)
            graphics_warnings += g_warnings
            with_graphics = replace(analysis, clips=analysis.clips + g_clips)
    fcpxml_path, report_path = write_outputs(
        out_dir, name, record, plan, with_graphics, rough, stringout, {"graphics": placed, "extra_warnings": graphics_warnings},
        version=version, command="render", entry={"style": opts.style, "planner": planner_label(plan.source)},
    )
    warnings = plan.warnings + rough.warnings + graphics_warnings
    return Result(
        out_dir, fcpxml_path, report_path, plan, rough, stringout, warnings=warnings, dead_air=dead_air(analysis.clips, stringout),
        version=version, project=rough.name, event=event_name(name),
    )

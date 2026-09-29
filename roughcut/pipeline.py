"""End-to-end: folder in, FCPXML + paper edit out."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import progress
from .analyze import Analysis, Cache, analyze, default_cache_dir, log
from .fcpxml import build_fcpxml
from .media import require_tools, scan
from .plan import PLAN_SCHEMA, Plan, heuristic_plan, normalize
from .prompts import PLANNER_SYSTEM, render_brief, render_media
from .providers import Provider, ProviderError, TextPart, make_provider
from .report import build_report
from .timeline import STYLES, Style, Timeline, build_timeline
from .transcribe import Transcriber, resolve_backend


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


def result_summary(result: Result) -> dict:
    """What the Mac app shows when a run finishes (the `done` event payload)."""
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
    }


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
    return analyze(files, transcriber=transcriber, cache=cache, transcript_dirs=opts.transcript_dirs, workers=opts.workers)


def plan_with_model(provider: Provider, analysis: Analysis, opts: Options) -> tuple[Plan, dict]:
    parts = [
        TextPart("<media>\n" + render_media(analysis) + "\n</media>", cache=True),
        TextPart(render_brief(opts.context, opts.target_seconds, opts.script)),
    ]
    raw = provider.complete_json(PLANNER_SYSTEM, parts, PLAN_SCHEMA, schema_name="edit_plan", purpose="edit plan", effort=opts.effort)
    return normalize(raw, analysis, source=f"{provider.name}:{provider.model}"), raw


def make_timelines(plan: Plan, analysis: Analysis, opts: Options, name: str) -> tuple[Timeline, Timeline | None]:
    progress.stage("cut", f"Cutting ({opts.style})")
    style = style_for(opts)
    rough = build_timeline(plan, analysis, style=style, name=f"{name} - Rough Cut", format_override=opts.format, broll_db=opts.broll_db, music_db=opts.music_db)
    stringout = None
    if opts.stringout:
        s_plan = heuristic_plan(analysis)
        if s_plan.sections:
            stringout = build_timeline(s_plan, analysis, style=style, name=f"{name} - Stringout", format_override=opts.format or f"{rough.width}x{rough.height}@{float(1 / rough.frame_duration):.3f}")
    return rough, stringout


def write_outputs(out_dir: Path, name: str, plan: Plan, raw_plan: dict | None, analysis: Analysis, rough: Timeline, stringout: Timeline | None, report_kwargs: dict) -> tuple[Path, Path]:
    progress.stage("write", "Writing the Final Cut file")
    out_dir.mkdir(parents=True, exist_ok=True)
    timelines = [rough] + ([stringout] if stringout and stringout.spine else [])
    xml = build_fcpxml(timelines, analysis, event_name=f"{name} ({date.today().isoformat()})")
    fcpxml_path = out_dir / f"{slug(name)}.fcpxml"
    fcpxml_path.write_text(xml, encoding="utf-8")
    (out_dir / "plan.json").write_text(json.dumps({"name": name, "plan": plan.to_json(), "raw": raw_plan}, indent=1))
    report_path = out_dir / "edit_report.md"
    report_path.write_text(build_report(plan, rough, analysis, stringout=stringout, **report_kwargs), encoding="utf-8")
    return fcpxml_path, report_path


def build(opts: Options) -> Result:
    t_start = time.monotonic()
    timings: dict[str, float] = {}
    out_dir = (opts.out or default_out_dir(opts.inputs)).expanduser().resolve()
    name = opts.name or (opts.inputs[0].resolve().name if opts.inputs[0].is_dir() else opts.inputs[0].stem)

    analysis = run_analysis(opts, out_dir)
    timings["analysis"] = time.monotonic() - t_start
    out_dir.mkdir(parents=True, exist_ok=True)
    analysis.save(out_dir / "analysis.json")

    provider = make_provider(opts.provider, model=opts.model, base_url=opts.base_url, api_key_env=opts.api_key_env, effort=opts.effort)
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
                usage.append(vision_provider.usage)
            t = time.monotonic()
            cache = Cache(opts.cache_dir or default_cache_dir())
            extra_warnings += describe_clips(analysis, vision_provider, cache, workers=opts.workers)
            timings["visual log"] = time.monotonic() - t
            analysis.save(out_dir / "analysis.json")

    (out_dir / "prompt.md").write_text(
        "# System\n\n" + PLANNER_SYSTEM + "\n\n# Media\n\n" + render_media(analysis) + "\n\n# Brief\n\n" + render_brief(opts.context, opts.target_seconds, opts.script)
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

    rough, stringout = make_timelines(plan, analysis, opts, name)
    timings["total"] = time.monotonic() - t_start
    fcpxml_path, report_path = write_outputs(
        out_dir,
        name,
        plan,
        raw_plan,
        analysis,
        rough,
        stringout,
        dict(usage=usage, timings=timings, extra_warnings=extra_warnings),
    )
    warnings = analysis.warnings + plan.warnings + rough.warnings + extra_warnings
    return Result(out_dir, fcpxml_path, report_path, plan, rough, stringout, fell_back, warnings)


def rerender(out_dir: Path, opts: Options) -> Result:
    """Rebuild the FCPXML from a saved analysis + plan (no model calls)."""
    if not (out_dir / "analysis.json").is_file() or not (out_dir / "plan.json").is_file():
        raise RoughcutError(f"{out_dir} isn't a roughcut output folder (no analysis.json/plan.json)")
    analysis = Analysis.load(out_dir / "analysis.json")
    saved = json.loads((out_dir / "plan.json").read_text())
    name = opts.name or saved.get("name") or out_dir.name.removesuffix("_roughcut")
    source = saved["plan"].get("source", "saved plan")
    plan = normalize(saved.get("raw") or saved["plan"], analysis, source=source)
    if not plan.sections:
        plan = heuristic_plan(analysis, title=f"{name} (dead air removed)")
    rough, stringout = make_timelines(plan, analysis, opts, name)
    fcpxml_path, report_path = write_outputs(out_dir, name, plan, saved.get("raw"), analysis, rough, stringout, {})
    return Result(out_dir, fcpxml_path, report_path, plan, rough, stringout, warnings=plan.warnings + rough.warnings)

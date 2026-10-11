"""Command line: `roughcut build ./footage --context "..." --target 8m`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, progress
from .providers import PROVIDERS, ProviderError
from .timecode import parse_duration, seconds_to_clock


def _read(path: str | None) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8") if path else ""


def _add_render_args(p: argparse.ArgumentParser) -> None:
    # Defaults are None so `render` can tell "not given" (keep the saved run's setting) from a choice.
    g = p.add_argument_group("cut style (render and revise: the earlier run's settings unless given)")
    g.add_argument("--style", choices=["tight", "medium", "loose"], help="how aggressively pauses are trimmed (default: medium)")
    g.add_argument("--keep-fillers", action=argparse.BooleanOptionalAction, help="don't cut um/uh (--no-keep-fillers cuts them again)")
    g.add_argument("--format", help="sequence format: 1080p, 4k, vertical, square, or WxH@fps (default: from the footage)")
    g.add_argument("--broll-db", type=float, help="B-roll audio level in dB (default: -20)")
    g.add_argument("--music-db", type=float, help="music level in dB (default: -14)")
    g.add_argument("--stringout", action=argparse.BooleanOptionalAction, help="add the 'Stringout' project (default: on; --no-stringout leaves it out)")
    g.add_argument("--fill", action=argparse.BooleanOptionalAction, help="cover narration the plan left without picture (default: on; --no-fill leaves it)")
    p.add_argument("--name", help="project/event name (default: folder name)")
    p.add_argument("--open", action="store_true", help="open the new cut in Final Cut Pro when it's done (Mac)")
    p.add_argument("--progress-json", action="store_true", help="emit JSON-lines progress on stdout (used by the Mac app)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roughcut", description="Dump footage in, get a Final Cut Pro rough cut out.")
    parser.add_argument("--version", action="version", version=f"roughcut {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="analyze footage and build a rough cut FCPXML")
    b.add_argument("inputs", nargs="+", help="folders and/or media files")
    b.add_argument("-o", "--out", help="output folder (default: <folder>_roughcut next to the footage)")
    brief = b.add_argument_group("brief")
    brief.add_argument("-c", "--context", default="", help="what this video is, who it's for, tone, must-keep moments")
    brief.add_argument("--context-file", help="read the brief from a text/markdown file")
    brief.add_argument("--script", help="script, outline or reference transcript to follow (text file)")
    brief.add_argument("-t", "--target", help="target length, e.g. 8m, 90s, 8:30")
    ai = b.add_argument_group("model")
    ai.add_argument("--provider", choices=PROVIDERS, default="anthropic", help="who plans the edit (default: anthropic = API key; claude-code = your Claude Pro/Max subscription through Claude Code; 'none' = silence cutting only)")
    ai.add_argument("--model", help="Claude: opus (default) or sonnet (faster, half the price); spellings like 'Sonnet 5.5' work. Leave it off in Terminal to pick from a menu. Other providers: the model's name")
    ai.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high", help="reasoning effort for the plan (Claude)")
    ai.add_argument("--base-url", help="endpoint for openai / ollama / openai-compatible providers")
    ai.add_argument("--api-key-env", help="environment variable holding the API key for openai-compatible providers")
    vis = ai.add_mutually_exclusive_group()
    vis.add_argument("--vision", dest="vision", action="store_true", default=None, help="describe clips from sampled frames (default when the provider supports images)")
    vis.add_argument("--no-vision", dest="vision", action="store_false", help="skip visual descriptions")
    ai.add_argument("--vision-model", help="a different model for describing clips, e.g. sonnet to save on big shoots while opus plans")
    ai.add_argument("--no-menu", dest="menu", action="store_false", help="don't ask which Claude model to use; take the default (Opus)")
    gfx = b.add_argument_group("motion graphics")
    gfx.add_argument("--graphics", action="store_true", help="have Claude design animated graphics (lower thirds, stats, kinetic captions...) from the transcript, rendered to ProRes 4444 above the footage")
    gfx.add_argument("--graphics-style", default="", help="look for the graphics: brand colors, fonts, mood, e.g. 'orange #F26B21, Gotham, bold and minimal'")
    tr = b.add_argument_group("transcription")
    tr.add_argument("--transcriber", choices=["auto", "mlx", "faster-whisper", "none"], default="auto")
    tr.add_argument("--whisper-model", help="override the Whisper model")
    tr.add_argument("--language", help="spoken language code, e.g. en (default: detect)")
    tr.add_argument("--no-verbatim", dest="verbatim", action="store_false", help="don't prime Whisper to transcribe um/uh")
    tr.add_argument("--transcripts", action="append", default=[], help="folder of sidecar transcripts (.srt/.vtt/.json named like the clips)")
    b.add_argument("--cache-dir", help="analysis cache (default: ~/Library/Caches/roughcut)")
    b.add_argument("--workers", type=int, default=4)
    _add_render_args(b)

    r = sub.add_parser("render", help="rebuild the FCPXML from a previous run's plan (no model calls)")
    r.add_argument("out_dir", help="a folder written by `roughcut build`")
    _add_render_args(r)

    rv = sub.add_parser("revise", help="re-plan the latest cut from your notes, as a new version (no re-transcription)")
    rv.add_argument("out_dir", help="a folder written by `roughcut build`")
    rv.add_argument("notes", nargs="?", default="", help='what to change, e.g. "tighter hook, drop the second Roy Choi line, more drone over the intro"')
    rv.add_argument("--notes-file", help="read the notes from a text file")
    rv.add_argument("--keep", action="append", default=[], metavar="IDS", help="segment or clip IDs the new cut must keep (comma-separated or repeated)")
    rv.add_argument("--avoid", action="append", default=[], metavar="IDS", help="segment IDs, clip IDs or CLIP@start-end ranges to leave out")
    rv.add_argument("-t", "--target", help="new target length, e.g. 60s or 1:30 (default: the build's)")
    rv.add_argument("--provider", choices=[p for p in PROVIDERS if p != "none"], help="who re-plans (default: the one that planned this cut)")
    rv.add_argument("--model", help="model for the revision (default: the one that planned this cut)")
    rv.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="reasoning effort (default: the build's)")
    rv.add_argument("--graphics", action=argparse.BooleanOptionalAction, help="motion graphics (default: as in the build; unchanged ones are reused, not redesigned)")
    rv.add_argument("--workers", type=int, default=4)
    _add_render_args(rv)

    d = sub.add_parser("doctor", help="check that ffmpeg, transcription and API keys are set up")
    d.add_argument("--json", action="store_true", help="print the report as JSON")
    d.add_argument("--offline", action="store_true", help="don't test the API key against Anthropic")

    k = sub.add_parser("key", help="save, check or remove your API key (stored in the macOS Keychain, shared with the Mac app)")
    k.add_argument("action", choices=["set", "status", "remove"])
    k.add_argument("--openai", action="store_true", help="the OpenAI / compatible-server key instead of Claude's")
    return parser


def _key_command(action: str, openai: bool) -> int:
    import getpass

    from . import keys
    from .providers.anthropic_provider import NOT_API_KEYS, _mask, key_problem_hint, override_note

    account = "OPENAI_API_KEY" if openai else "ANTHROPIC_API_KEY"
    label = "OpenAI" if openai else "Claude (Anthropic)"
    if action == "status":
        source = keys.source_of(account)
        if source is None:
            print(f"No {label} key found. Save one with: roughcut key set" + (" --openai" if openai else ""))
            return 1
        value = os.environ.get(account, "").strip() if source == "environment" else keys.keychain_get(account) or ""
        print(f"{label} key {_mask(value)} from the {source}" + (" (exported in your shell; this wins over the Keychain)" if source == "environment" else ""))
        if not openai:
            os.environ[account] = value
            status, message = check_anthropic_key()
            print("Anthropic accepted it." if status == "ok" else message)
            return 0 if status == "ok" else 1
        return 0
    if action == "remove":
        removed = keys.keychain_delete(account)
        print(f"Removed the {label} key from the Keychain." if removed else f"No {label} key in the Keychain.")
        if os.environ.get(account, "").strip():
            print(f"{account} is still exported in this shell (probably ~/.zshrc); delete that line too if it's old.")
        return 0

    # set
    exported = os.environ.get(account, "").strip()
    if not keys.keychain_available():
        print(f"The macOS Keychain isn't available here. Add this to your shell profile instead:\n  export {account}=your-key")
        return 1
    value = getpass.getpass(f"Paste your {label} API key (it won't show as you paste), then press Return: ").strip()
    if not value:
        print("Nothing entered; no changes made.")
        return 1
    if "..." in value or "…" in value or "*" in value:
        print(
            f"That's the shortened key from the list on the API Keys page ({_mask(value)}), not the key itself. "
            "The full key is shown only once, right after you create it: create a new key, click Copy in that "
            "window, and run `roughcut key set` again."
        )
        return 1
    if not openai:
        # Anthropic decides what's valid (new console keys start sk-ant-usr-, older ones
        # sk-ant-api03-); only kinds that can never run Claude are stopped here.
        if not value.startswith("sk-ant-") or value.startswith(NOT_API_KEYS):
            print(key_problem_hint({"ANTHROPIC_API_KEY": value}))
            return 1
        status, message = check_anthropic_key(pasted=value)
        if status == "rejected":
            print(message + "\nNot saved.")
            return 1
        if status != "ok":
            print(f"Couldn't check the key right now ({message}). Saving it anyway.")
    try:
        keys.keychain_set(account, value)
    except keys.KeychainError as e:
        print(e)
        return 1
    print(f"Saved {_mask(value)} to your Keychain. Terminal runs and the Roughcut app both use it now.")
    if exported and exported != value:
        print(f"Note: a different {account} is exported in this shell and takes priority over the Keychain, so runs would still use the old key.")
        print(override_note(account) or f"Remove the `export {account}=` line from your shell profile, then open a new Terminal window.")
    return 0


# The Terminal version of the Mac app's model menus: (label, why, planner, describer or None = same).
MODEL_MENU = [
    ("Claude Opus 5.5 plans and looks at clips", "best judgment", "claude-opus-5-5", None),
    ("Claude Opus 5.5 plans, Claude Sonnet 5.5 looks at clips", "cheaper on big shoots", "claude-opus-5-5", "claude-sonnet-5-5"),
    ("Claude Sonnet 5.5 for both", "faster, about half the price", "claude-sonnet-5-5", None),
]
MODEL_MENU_NO_VISION = [
    ("Claude Opus 5.5", "best judgment", "claude-opus-5-5", None),
    ("Claude Sonnet 5.5", "faster, about half the price", "claude-sonnet-5-5", None),
]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose_claude_models(vision: bool, ask=input) -> tuple[str, str | None]:
    """Numbered menu; Return takes the first (default) choice."""
    options = MODEL_MENU if vision else MODEL_MENU_NO_VISION
    print("Which Claude model should cut this?")
    for i, (label, why, _, _) in enumerate(options, 1):
        print(f"  {i}  {label}  ({why}){'  [default]' if i == 1 else ''}")
    while True:
        try:
            answer = ask(f"Press Return for 1, or type 1-{len(options)}: ").strip()
        except EOFError:
            answer = ""
        if not answer:
            answer = "1"
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            label, _, planner, describer = options[int(answer) - 1]
            print(f"Using {label}.\n")
            return planner, describer
        print(f"Type a number from 1 to {len(options)}.")


KEY_SOURCES: dict[str, str] = {}  # filled from keys.load_into_environ() at startup


def check_anthropic_key(offline: bool = False, pasted: str | None = None) -> tuple[str, str]:
    """(status, message): status is ok / rejected / missing / unreachable / not checked.

    `pasted` checks a key the user is about to save instead of the one in the environment.
    """
    from .providers import ProviderAuthError
    from .providers.anthropic_provider import DEFAULT_MODEL, AnthropicProvider, _mask, key_problem_hint, refused_message

    key = pasted or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not (key or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return "missing", key_problem_hint()
    if offline:
        return "not checked", ""
    try:
        import anthropic

        AnthropicProvider(client=anthropic.Anthropic(api_key=key or None, max_retries=0, timeout=10)).verify()
    except ProviderAuthError as e:
        if not pasted:
            return "rejected", str(e)
        said = getattr(e, "said", "") or str(e)
        if getattr(e, "status", None) == 403:
            return "rejected", refused_message(said, DEFAULT_MODEL)
        return "rejected", (
            f"Anthropic rejected the key you pasted ({_mask(pasted)}): {said}. Copy it again from "
            "console.anthropic.com → API Keys. The full key is shown only once, right after you create it."
        )
    except ProviderError as e:
        return "unreachable", str(e)
    return "ok", ""


def doctor_report(offline: bool = False) -> dict:
    mlx = importlib.util.find_spec("mlx_whisper") is not None
    fw = importlib.util.find_spec("faster_whisper") is not None
    apple_silicon = sys.platform == "darwin" and os.uname().machine == "arm64"
    transcriber = "mlx" if (mlx and apple_silicon) else "faster-whisper" if fw else None
    key_status, key_message = check_anthropic_key(offline)
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "transcriber": transcriber,
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "anthropic_key_status": key_status,
        "anthropic_key_message": key_message,
        "anthropic_key_source": KEY_SOURCES.get("ANTHROPIC_API_KEY"),
        "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
        "openai_installed": importlib.util.find_spec("openai") is not None,
        **claude_code_report(offline),
        "graphics": _graphics_problem() if not offline else "not checked",
    }


def _graphics_problem() -> str | None:
    """None when motion graphics can render here, else what to install."""
    from .graphics import renderer_problem

    return renderer_problem()


def claude_code_report(offline: bool = False) -> dict:
    """Is Claude Code installed and signed in with a subscription (for --provider claude-code)?"""
    from .providers import ProviderError
    from .providers.claude_code import auth_status, find_claude

    binary = find_claude()
    method = None
    if binary and not offline:
        try:
            info = auth_status(binary)
        except ProviderError:
            info = {}
        method = info.get("authMethod") if info.get("loggedIn") else None
    return {"claude_code": binary, "claude_code_auth": method}


def _claude_code_detail(r: dict) -> str:
    if not r.get("claude_code"):
        return "optional: install it to use your Claude subscription (--provider claude-code)"
    auth = r.get("claude_code_auth")
    if auth is None:
        return "installed, not signed in: run `claude` and type /login to use your subscription"
    if auth == "api_key":
        return "signed in with an API key, not a subscription"
    return "signed in with your subscription: --provider claude-code uses it"


def _print_doctor(r: dict) -> int:
    rows = [
        ("ffmpeg", r["ffmpeg"] and r["ffprobe"], r["ffmpeg"] or "missing: brew install ffmpeg"),
        ("transcriber", r["transcriber"], r["transcriber"] or f"missing: {shlex.quote(sys.executable)} -m pip install mlx-whisper"),
        (
            "Claude API key",
            r["anthropic_key_status"] == "ok",
            {"ok": "accepted by Anthropic", "not checked": "set (not tested)"}.get(r["anthropic_key_status"], r["anthropic_key_message"])
            + (f" (from the {r['anthropic_key_source']})" if r.get("anthropic_key_source") and r["anthropic_key_status"] in ("ok", "not checked") else ""),
        ),
        ("OpenAI", r["openai_key"] and r["openai_installed"], "ready" if r["openai_key"] and r["openai_installed"] else "optional, not set up"),
        ("Claude Code", r.get("claude_code_auth") not in (None, "api_key"), _claude_code_detail(r)),
        ("Graphics", r.get("graphics") is None, "ready for --graphics" if r.get("graphics") is None else f"optional: {r.get('graphics')}"),
    ]
    print(f"roughcut {r['version']} (Python {r['python']}, {r['executable']})")
    for label, ok, detail in rows:
        print(f"  {'ok ' if ok else '-- '} {label:<15} {detail}")
    return 0 if (r["ffmpeg"] and r["ffprobe"]) else 1


def resume_command(e: BaseException) -> str | None:
    """`roughcut render '<out_dir>'` when the run stopped after its plan was saved."""
    out_dir = getattr(e, "plan_saved", None)
    return f"roughcut render {shlex.quote(str(out_dir))}" if out_dir else None


def resume_hint(e: BaseException) -> str | None:
    command = resume_command(e)
    if not command:
        return None
    return f"The edit plan was saved before this stopped. Finish the cut without calling the model again:\n  {command}"


def _log_end(line: str) -> None:
    from .analyze import log_file_only, log_to

    log_file_only(line)
    log_to(None)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "key":
        # Works on the real environment: what the shell exports vs the Keychain.
        return _key_command(args.action, args.openai)
    # Keys saved by `roughcut key set` or the Mac app live in the Keychain.
    from . import keys

    KEY_SOURCES.clear()
    KEY_SOURCES.update(keys.load_into_environ())
    if args.command == "doctor":
        report = doctor_report(offline=args.offline)
        if args.json:
            print(json.dumps(report))
            return 0
        return _print_doctor(report)

    json_mode = bool(args.progress_json)
    progress.enable(json_mode)
    # Only a real command line (not a test calling main()) takes over the process group.
    restore = _install_stop_handlers(json_mode, own_group=argv is None)
    try:
        return _run(args, json_mode)
    finally:
        _restore_handlers(restore)


def _run(args, json_mode: bool) -> int:
    from .pipeline import Options, build, rerender, result_summary

    # Only the cut settings given on the command line; the rest are defaults (build) or the saved run's (render).
    given = {
        k: v
        for k, v in dict(
            style=args.style,
            keep_fillers=args.keep_fillers,
            format=args.format,
            broll_db=args.broll_db,
            music_db=args.music_db,
            stringout=args.stringout,
            fill=args.fill,
        ).items()
        if v is not None
    }
    common = dict(name=args.name, **given)
    try:
        if args.command == "render":
            result = rerender(Path(args.out_dir).expanduser().resolve(), Options(inputs=[], **common), overrides=set(given))
        elif args.command == "revise":
            from .pipeline import revise

            notes = "\n\n".join(t.strip() for t in (args.notes, _read(args.notes_file)) if t.strip())
            if not (notes or args.keep or args.avoid or args.target):
                raise ValueError('Say what to change, e.g.  roughcut revise <folder> "tighter hook, more drone over the intro"')
            model = args.model
            if model and args.provider in (None, "anthropic", "claude-code"):
                from .providers.anthropic_provider import resolve_model

                model = resolve_model(model)
            result = revise(
                Path(args.out_dir),
                notes,
                Options(inputs=[], workers=args.workers, **common),
                keep=args.keep,
                avoid=args.avoid,
                overrides=set(given),
                provider=args.provider,
                model=model,
                effort=args.effort,
                target_seconds=parse_duration(args.target) if args.target else None,
                graphics=args.graphics,
            )
        else:
            if args.provider in ("anthropic", "claude-code"):
                from .providers.anthropic_provider import resolve_model

                if args.model is None and args.vision_model is None and args.menu and not json_mode and _interactive():
                    args.model, args.vision_model = choose_claude_models(vision=args.vision is not False)
                args.model = resolve_model(args.model) if args.model else None
                args.vision_model = resolve_model(args.vision_model) if args.vision_model else None
            context = "\n\n".join(t for t in (args.context, _read(args.context_file)) if t.strip())
            opts = Options(
                inputs=[Path(p) for p in args.inputs],
                out=Path(args.out) if args.out else None,
                context=context,
                script=_read(args.script) or None,
                target_seconds=parse_duration(args.target) if args.target else None,
                provider=args.provider,
                model=args.model,
                effort=args.effort,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                vision=args.vision,
                vision_model=args.vision_model,
                transcriber=args.transcriber,
                whisper_model=args.whisper_model,
                language=args.language,
                verbatim=args.verbatim,
                transcript_dirs=[Path(p) for p in args.transcripts],
                cache_dir=Path(args.cache_dir) if args.cache_dir else None,
                workers=args.workers,
                graphics=args.graphics,
                graphics_style=args.graphics_style,
                **common,
            )
            result = build(opts)
    except (ProviderError, ValueError, RuntimeError, OSError) as e:
        hint = resume_hint(e)
        print(f"roughcut: {e}", file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        progress.emit("error", message=str(e), **({"resume": resume_command(e)} if hint else {}))
        _log_end(f"STOPPED: {e}" + (f"\n{hint}" if hint else ""))
        return 1
    except KeyboardInterrupt as e:
        hint = resume_hint(e)
        if hint:
            print(f"\n{hint}", file=sys.stderr)
        _log_end("STOPPED: interrupted (Ctrl-C)" + (f"\n{hint}" if hint else ""))
        raise
    except Exception as e:  # a bug: tell the app, then show the traceback
        progress.emit("error", message=f"Unexpected error ({type(e).__name__}): {e}")
        import traceback

        hint = resume_hint(e)
        if hint:
            print(hint, file=sys.stderr)
        _log_end("STOPPED by a bug:\n" + traceback.format_exc() + (f"\n{hint}" if hint else ""))
        raise
    _log_end(f"FINISHED: {result.fcpxml.name} and {result.report.name} written")
    opened = open_in_final_cut(result.fcpxml) if args.open else None

    if json_mode:
        if opened:
            progress.emit("log", message=opened)
        progress.emit("done", result=result_summary(result))
        return 3 if result.fell_back else 0

    tl = result.timeline
    if result.fell_back:
        print("\n*** The AI step failed, so this is only the stringout (all speech, dead air removed, no B-roll). ***")
        for w in result.warnings:
            if "planning failed" in w or "no usable sections" in w:
                print(f"*** {w}")
    print(f"\nRough cut: {seconds_to_clock(tl.seconds)} ({len(tl.spine)} edits, {sum(1 for c in tl.connected if c.kind == 'broll')} B-roll)")
    if result.stringout is not None:
        print(f"Stringout: {seconds_to_clock(result.stringout.seconds)}")
    if result.dead_air and result.dead_air[0] > 0:
        talk, kept = result.dead_air
        print(f"Dead air removed: {seconds_to_clock(max(0.0, talk - kept))} of {seconds_to_clock(talk)} talking footage ({max(0.0, 1 - kept / talk):.0%})")
    for w in result.warnings:
        if "almost no pauses" in w:
            print(f"  ! {w}")
    print(f"FCPXML:    {result.fcpxml}")
    print(f"Report:    {result.report}")
    if result.version > 1:
        print(f"This is version {result.version}; earlier cuts are in {result.out_dir / 'history'}.")
    if opened:
        print(f"\n{opened}")
    else:
        print("\nIn Final Cut Pro: File > Import > XML..., then pick the .fcpxml above.")
    print(f"Then open the project \"{result.project or tl.name}\" in the event \"{result.event}\". The clips in the browser stay your untouched originals.")
    return 3 if result.fell_back else 0


def open_in_final_cut(path: Path, run=subprocess.run) -> str:
    """--open: hand the new FCPXML to Final Cut Pro (macOS), else to whatever opens .fcpxml files."""
    if sys.platform != "darwin":
        return "--open only works on a Mac; import the .fcpxml with File > Import > XML... in Final Cut Pro."
    for cmd, said in (
        (["open", "-a", "Final Cut Pro", str(path)], "Opened in Final Cut Pro: pick a library in the import window."),
        (["open", str(path)], "Opened with the app your Mac uses for .fcpxml files."),
    ):
        try:
            if run(cmd, capture_output=True, timeout=60).returncode == 0:
                return said
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "Couldn't open Final Cut Pro automatically: import the .fcpxml with File > Import > XML..."


def _install_stop_handlers(json_mode: bool, own_group: bool) -> dict:
    """Log how the run ended when the app cancels it (SIGTERM) or Terminal closes (SIGHUP).

    Under the Mac app (--progress-json) roughcut leads its own process group,
    so cancelling also stops the `claude`, ffmpeg and browser processes it started.
    """
    import signal

    in_group = False
    if json_mode and own_group and hasattr(os, "setpgrp"):
        try:
            os.setpgrp()
            in_group = True
        except OSError:
            pass

    def stop(signum, frame):  # noqa: ARG001
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the group signal below reaches us too
        except (ValueError, OSError):
            pass
        reason = "cancelled" if signum == signal.SIGTERM else "the Terminal window was closed"
        _log_end(f"STOPPED: {reason}")
        if in_group:
            try:
                os.killpg(0, signal.SIGTERM)
            except OSError:
                pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):  # not the main thread
            pass
    return previous


def _restore_handlers(previous: dict) -> None:
    import signal

    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        except (ValueError, OSError, TypeError):
            pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Command line: `roughcut build ./footage --context "..." --target 8m`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, progress
from .providers import PROVIDERS, ProviderError
from .timecode import parse_duration, seconds_to_clock


def _read(path: str | None) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8") if path else ""


def _add_render_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("cut style")
    g.add_argument("--style", choices=["tight", "medium", "loose"], default="medium", help="how aggressively pauses are trimmed (default: medium)")
    g.add_argument("--keep-fillers", action="store_true", help="don't cut um/uh")
    g.add_argument("--format", help="sequence format: 1080p, 4k, vertical, square, or WxH@fps (default: from the footage)")
    g.add_argument("--broll-db", type=float, default=-20.0, help="B-roll audio level in dB (default: -20)")
    g.add_argument("--music-db", type=float, default=-14.0, help="music level in dB (default: -14)")
    g.add_argument("--no-stringout", action="store_true", help="don't add the 'Stringout' project")
    g.add_argument("--no-fill", action="store_true", help="leave narration without picture where the plan has no B-roll")
    p.add_argument("--name", help="project/event name (default: folder name)")
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
    ai.add_argument("--provider", choices=PROVIDERS, default="anthropic", help="who plans the edit (default: anthropic; 'none' = silence cutting only)")
    ai.add_argument("--model", help="model id. Claude: claude-opus-5-5 (default) or claude-sonnet-5-5 (faster, half the price); 'opus'/'sonnet' work too")
    ai.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high", help="reasoning effort for the plan (Claude)")
    ai.add_argument("--base-url", help="endpoint for openai / ollama / openai-compatible providers")
    ai.add_argument("--api-key-env", help="environment variable holding the API key for openai-compatible providers")
    vis = ai.add_mutually_exclusive_group()
    vis.add_argument("--vision", dest="vision", action="store_true", default=None, help="describe clips from sampled frames (default when the provider supports images)")
    vis.add_argument("--no-vision", dest="vision", action="store_false", help="skip visual descriptions")
    ai.add_argument("--vision-model", help="a different model for describing clips, e.g. sonnet to save on big shoots while opus plans")
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
    }


def _print_doctor(r: dict) -> int:
    rows = [
        ("ffmpeg", r["ffmpeg"] and r["ffprobe"], r["ffmpeg"] or "missing: brew install ffmpeg"),
        ("transcriber", r["transcriber"], r["transcriber"] or "missing: pip install -e '.[mac]'"),
        (
            "Claude API key",
            r["anthropic_key_status"] == "ok",
            {"ok": "accepted by Anthropic", "not checked": "set (not tested)"}.get(r["anthropic_key_status"], r["anthropic_key_message"])
            + (f" (from the {r['anthropic_key_source']})" if r.get("anthropic_key_source") and r["anthropic_key_status"] in ("ok", "not checked") else ""),
        ),
        ("OpenAI", r["openai_key"] and r["openai_installed"], "ready" if r["openai_key"] and r["openai_installed"] else "optional, not set up"),
    ]
    print(f"roughcut {r['version']} (Python {r['python']}, {r['executable']})")
    for label, ok, detail in rows:
        print(f"  {'ok ' if ok else '-- '} {label:<15} {detail}")
    return 0 if (r["ffmpeg"] and r["ffprobe"]) else 1


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

    from .pipeline import Options, build, rerender, result_summary

    json_mode = bool(args.progress_json)
    progress.enable(json_mode)

    common = dict(
        name=args.name,
        style=args.style,
        keep_fillers=args.keep_fillers,
        format=args.format,
        broll_db=args.broll_db,
        music_db=args.music_db,
        stringout=not args.no_stringout,
        fill=not args.no_fill,
    )
    try:
        if args.command == "render":
            result = rerender(Path(args.out_dir).expanduser().resolve(), Options(inputs=[], **common))
        else:
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
                **common,
            )
            result = build(opts)
    except (ProviderError, ValueError, RuntimeError, OSError) as e:
        print(f"roughcut: {e}", file=sys.stderr)
        progress.emit("error", message=str(e))
        return 1
    except Exception as e:  # a bug: tell the app, then show the traceback
        progress.emit("error", message=f"Unexpected error ({type(e).__name__}): {e}")
        raise

    if json_mode:
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
    print(f"FCPXML:    {result.fcpxml}")
    print(f"Report:    {result.report}")
    print("\nIn Final Cut Pro: File > Import > XML..., then pick the .fcpxml above.")
    return 3 if result.fell_back else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

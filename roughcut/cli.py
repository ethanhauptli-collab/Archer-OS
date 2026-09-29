"""Command line: `roughcut build ./footage --context "..." --target 8m`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
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
    p.add_argument("--name", help="project/event name (default: folder name)")


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
    ai.add_argument("--model", help="model id (default for anthropic: claude-opus-5-5)")
    ai.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high", help="reasoning effort for the plan (Claude)")
    ai.add_argument("--base-url", help="endpoint for openai / ollama / openai-compatible providers")
    ai.add_argument("--api-key-env", help="environment variable holding the API key for openai-compatible providers")
    vis = ai.add_mutually_exclusive_group()
    vis.add_argument("--vision", dest="vision", action="store_true", default=None, help="describe clips from sampled frames (default when the provider supports images)")
    vis.add_argument("--no-vision", dest="vision", action="store_false", help="skip visual descriptions")
    ai.add_argument("--vision-model", help="use a different model for visual descriptions")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .pipeline import Options, build, rerender

    common = dict(
        name=args.name,
        style=args.style,
        keep_fillers=args.keep_fillers,
        format=args.format,
        broll_db=args.broll_db,
        music_db=args.music_db,
        stringout=not args.no_stringout,
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
    except (ProviderError, ValueError, RuntimeError) as e:
        print(f"roughcut: {e}", file=sys.stderr)
        return 1

    tl = result.timeline
    print(f"\nRough cut: {seconds_to_clock(tl.seconds)} ({len(tl.spine)} edits, {sum(1 for c in tl.connected if c.kind == 'broll')} B-roll)")
    if result.stringout is not None:
        print(f"Stringout: {seconds_to_clock(result.stringout.seconds)}")
    print(f"FCPXML:    {result.fcpxml}")
    print(f"Report:    {result.report}")
    print("\nIn Final Cut Pro: File > Import > XML..., then pick the .fcpxml above.")
    return 3 if result.fell_back else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

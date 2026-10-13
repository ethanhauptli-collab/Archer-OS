"""Command line: `roughcut build ./footage --context "..." --target 8m`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, progress
from .providers import PROVIDERS, ProviderError
from .timecode import parse_duration, seconds_to_clock


def _read(path: str | None) -> str:
    """A brief, script or notes file, in whatever encoding its editor saved it."""
    from .textio import read_user_text

    return read_user_text(path) if path else ""


LEVEL_OFF = "off"  # --dialogue-level off: given, as opposed to left out (None)


def _level(text: str) -> float | str:
    """--dialogue-level: LUFS (-16, -16LUFS) or off."""
    t = text.strip().lower().removesuffix("lufs").strip()
    if t in ("off", "none", "no"):
        return LEVEL_OFF
    try:
        value = float(t)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a loudness like -16 or 'off', got {text!r}") from None
    if not -40.0 <= value <= -5.0:
        raise argparse.ArgumentTypeError(f"{value:g} LUFS is outside -40 to -5; online video is usually -16 to -14")
    return value


def _add_render_args(p: argparse.ArgumentParser) -> None:
    # Defaults are None so `render` can tell "not given" (keep the saved run's setting) from a choice.
    g = p.add_argument_group("cut style (render and revise: the earlier run's settings unless given)")
    g.add_argument(
        "--style", choices=["auto", "tight", "medium", "loose"], action=_StyleAction,
        help="how aggressively pauses are trimmed (default, auto: the pacing the planner reads in the brief, medium when it says none; without AI, from its wording)",
    )
    g.add_argument("--keep-fillers", action=argparse.BooleanOptionalAction, help="don't cut um/uh (--no-keep-fillers cuts them again)")
    g.add_argument("--format", help="sequence format: 1080p, 4k, vertical, square, WxH@fps, or auto (from the footage; the default)")
    g.add_argument("--broll-db", type=float, help="B-roll nat sound level in dB, relative to the dialogue (default: -20)")
    g.add_argument("--music-db", type=float, help="music bed level in dB, relative to the dialogue (default: -14)")
    g.add_argument(
        "--dialogue-level", type=_level, metavar="LUFS",
        help="match every clip's speech to this loudness in LUFS, e.g. -16 (default), -14 or -14LUFS; 'off' keeps clip levels as recorded",
    )
    g.add_argument(
        "--fades", choices=["on", "off"],
        help="short de-click fades at audio edits and soft fades on B-roll nat sound (default: on); the music bed fades in and out either way",
    )
    g.add_argument("--stringout", action=argparse.BooleanOptionalAction, help="add the 'Stringout' project (default: on; --no-stringout leaves it out)")
    g.add_argument(
        "--punch-in", action=argparse.BooleanOptionalAction,
        help="alternate a 112%% punch-in at each jump cut no B-roll covers (default: when the planner reads the brief asking for it, e.g. 'punch in on jump cuts')",
    )
    g.add_argument("--fill", action=argparse.BooleanOptionalAction, help="cover narration the plan left without picture (default: on; --no-fill leaves it)")
    p.add_argument("--name", help="project/event name (default: folder name)")
    p.add_argument("--open", action="store_true", help="open the new cut in Final Cut Pro when it's done (Mac)")
    p.add_argument("--progress-json", action="store_true", help="emit JSON-lines progress on stdout (used by the Mac app)")


class _StyleAction(argparse.Action):
    """--style; "auto" (what the app sends by default) never overrides a pacing given before it, e.g. a preset's."""

    def __call__(self, parser, namespace, values, option_string=None):
        if values == "auto" and getattr(namespace, self.dest, None) not in (None, "auto"):
            return
        setattr(namespace, self.dest, values)


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
    brief.add_argument("--preset", help="start from a preset's brief, length, pacing and graphics look (`roughcut preset list`); flags you type win")
    brief.add_argument("--preset-brief", default="", help=argparse.SUPPRESS)  # filled in by --preset, or by the app with its brief
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
    ai.add_argument("--no-menu", dest="menu", action="store_false", help="don't ask anything: take the default model (Opus) and the default output folder, even inside a Venture Studio library")
    b.add_argument(
        "--music", action="append", default=[], metavar="FILE",
        help="a song to lay under the cut (from anywhere, e.g. Downloads, or studio:ID for a Venture Studio song from `roughcut studio music`): "
        "not transcribed, ducked under speech, faded, and timed to end with the cut. Repeat for more; the first plays when the plan picks none",
    )
    b.add_argument(
        "--music-from", metavar="NAME",
        help="also offer the planner up to --music-max songs from this Venture Studio collection or kit (collection:NAME or kit:NAME when "
        "both have the name, or an exported kit .json): favorites first, then the ones whose tags the brief mentions. Any --music you "
        "give stays first. If Studio, the collection or its folder isn't available, the cut is built without them",
    )
    b.add_argument("--no-music-from", dest="music_from", action="store_const", const="", help="no Studio songs, even if the preset names a collection")
    b.add_argument("--music-max", type=_positive, default=4, metavar="N", help="at most this many --music-from songs (default 4)")
    gfx = b.add_argument_group("motion graphics")
    gfx.add_argument("--graphics", action="store_true", help="have Claude design animated graphics (lower thirds, stats, kinetic captions...) from the transcript, rendered to ProRes 4444 above the footage")
    gfx.add_argument("--no-graphics", dest="graphics", action="store_false", help="no motion graphics, even if the preset has them")
    gfx.add_argument("--graphics-style", default="", help="look for the graphics: brand colors, fonts, mood, e.g. 'one bold accent color, geometric sans, minimal'")
    gfx.add_argument("--graphics-logo", help="a PNG, SVG, JPEG or WebP logo the graphics may show")
    gfx.add_argument("--graphics-look", action="append", metavar="FILE", help="a graphic you approved (an .html from an earlier run's graphics folder) for new designs to match; repeat for more")
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
    r.add_argument("out_dir", nargs="+", metavar="out_dir", help="a folder written by `roughcut build`")
    r.add_argument("--preset", help="use a preset's pacing and format; flags you type win")
    r.add_argument(
        "--design-missing", action="store_true",
        help="also design the planned motion graphics that have no design yet (the run stopped or ran out of plan before designing them): "
        "calls the run's model for those only; the cut isn't re-planned",
    )
    _add_render_args(r)

    rv = sub.add_parser("revise", help="re-plan the latest cut from your notes, as a new version (no re-transcription)")
    rv.add_argument("out_dir", nargs="+", metavar="out_dir", help="a folder written by `roughcut build`")
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
    d.add_argument("--offline", action="store_true", help="don't test the API key against Anthropic or check whether Venture Studio's folders are online")

    st = sub.add_parser("studio", help="Venture Studio: what Roughcut can read from it (read only; never Studio's live files)")
    st_sub = st.add_subparsers(dest="studio_command", required=True)
    sts = st_sub.add_parser("status", help="the catalog snapshot Roughcut reads, its age, item count and folders")
    sts.add_argument("--json", action="store_true", help="print the status as JSON")
    sts.add_argument("--where", metavar="DIR", help="also say whether DIR is inside a Venture Studio library, and where a build of it would write")
    sts.add_argument("--offline", action="store_true", help="don't check whether the library folders are online")
    sts.add_argument("--cache-dir", help="where Roughcut keeps its copy of Studio's catalog snapshot (default: Roughcut's cache, as for build)")
    stm = st_sub.add_parser(
        "music", help="Studio audio a music bed can use, from its catalog snapshot (read only; no file on a library folder is touched)",
    )
    stm.add_argument("words", nargs="*", help="search words, as Studio searches: names, folders, tags, notes, providers, collections")
    stm.add_argument("--collection", metavar="NAME", help="only this Studio collection")
    stm.add_argument("--kit", metavar="NAME|FILE", help="only this Studio kit, by name or an exported kit .json")
    stm.add_argument("--favorites", action="store_true", help="only favorites")
    stm.add_argument("--json", action="store_true", help="print a JSON array, one object per file")
    stm.add_argument("--cache-dir", help="where Roughcut keeps its copy of Studio's catalog snapshot (default: Roughcut's cache)")

    k = sub.add_parser("key", help="save, check or remove your API key (stored in the macOS Keychain, shared with the Mac app)")
    k.add_argument("action", choices=["set", "status", "remove"])
    k.add_argument("--openai", action="store_true", help="the OpenAI / compatible-server key instead of Claude's")

    sub.add_parser("update", help="get the latest roughcut (git pull in its folder; reinstalls when dependencies changed)")
    from .presets import add_commands

    add_commands(sub)
    return parser


def _apply_preset(args, argv: list[str]):
    """Re-parse with the preset's flags before the user's, so the user's win. Returns (args, error)."""
    from .presets import PresetError, flags, load, logo_problem, look_problem

    if getattr(args, "preset", None):
        try:
            preset = load(args.preset)
        except PresetError as e:
            return args, str(e)
        k = argv.index(args.command)
        typed = args
        args = build_parser().parse_args(argv[: k + 1] + flags(preset, args.command) + argv[k + 1 :])
        if args.command == "build" and typed.graphics_look:
            args.graphics_look = typed.graphics_look  # yours replace the preset's approved designs
        skipped = ""
        if args.command == "build" and args.graphics and not typed.graphics:
            if args.provider == "none":
                args.graphics = False  # the preset's graphics need a model; silence-only runs skip them
                skipped = " Its graphics are skipped: they need a model."
            else:
                from .graphics import renderer_problem

                problem = renderer_problem()
                if problem:
                    return args, (
                        f"the {preset.name} preset turns motion graphics on, but they can't be rendered on this computer yet. {problem}\n"
                        f"Or cut without them: add --no-graphics, or run `roughcut preset save {preset.name} --no-graphics` once."
                    )
        print(f"Using the {preset.name} preset ({preset.summary()}); flags you typed win.{skipped}", file=sys.stderr)
    if args.command == "build" and args.graphics and args.graphics_logo:
        problem = logo_problem(args.graphics_logo)
        if problem:
            return args, problem
        args.graphics_logo = str(Path(args.graphics_logo).expanduser().resolve())
    if args.command == "build" and args.graphics and args.graphics_look:
        for page in args.graphics_look:
            problem = look_problem(page)
            if problem:
                return args, problem
        args.graphics_look = [str(Path(p).expanduser().resolve()) for p in args.graphics_look]
    return args, None


def _resolve_paths(args, argv: list[str]) -> str | None:
    """Rejoin paths zsh split at spaces (build inputs, render's and revise's folder). Returns an error, or None."""
    from .paths import PathInputError, leading_folder, resolve

    note = lambda line: print(line, file=sys.stderr)  # noqa: E731
    try:
        if args.command == "build":
            args.inputs = resolve(args.inputs, argv, notify=note)
        elif args.command == "render":
            found = resolve(args.out_dir, argv, notify=note)
            if len(found) != 1:
                return "render takes one output folder; put a path with spaces in quotes."
            args.out_dir = found[0]
        elif args.command == "revise":
            # The folder, then the notes: a folder path with spaces typed without quotes arrives in pieces.
            pieces = list(args.out_dir) + ([args.notes] if args.notes else [])
            args.out_dir, rest = leading_folder(pieces, wanted="plan.json", notify=note)
            args.notes = " ".join(rest)
    except PathInputError as e:
        return str(e)
    return None


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("can't be negative")
    return value


STUDIO_SONG = re.compile(r"studio:(\d+)\Z", re.I)


def _studio_songs(args) -> str | None:
    """--music studio:ID: the Venture Studio song's file, checked (with a deadline: its folder may be a network share)
    before the output folder is made. Any problem stops the run: this song was asked for by name."""
    args.music_library = {}
    if args.command != "build":
        return None
    from . import studio
    from .pipeline import studio_song_problem

    cache = Path(args.cache_dir).expanduser() if args.cache_dir else None
    for i, song in enumerate(args.music):
        m = STUDIO_SONG.match(str(song).strip())
        if not m or Path(song).exists():
            continue
        try:
            copy = studio.newest_copy(cache_dir=cache)
            with studio.catalog(copy) as con:
                found = studio.song(con, int(m.group(1)))
        except studio.StudioUnavailable as e:
            return f"--music {song}: " + ("Venture Studio isn't installed on this Mac" if e.silent else e.reason)
        problem = studio_song_problem(found.path)
        if problem:
            return problem.replace(f"--music {found.path}", f"--music {song} ({found.path})") + (
                f". Studio's catalog is as of {copy.snapshot.when()}: in Studio, Back up catalog now refreshes it" if "isn't there" in problem else ""
            )
        path = str(Path(found.path))
        args.music[i] = path
        args.music_library[path] = _library_info(found, f"--music {song}")
    # A song given by its path (the app's picks from Studio) that is a Studio item: the same deadlines, and the planner sees its tags.
    plain = {os.path.abspath(os.path.expanduser(str(s))): s for s in args.music if not STUDIO_SONG.match(str(s).strip())}
    plain = {p: s for p, s in plain.items() if p not in args.music_library}
    if plain and studio.installed():
        try:
            copy = studio.newest_copy(cache_dir=cache)
            with studio.catalog(copy) as con:
                found_at = studio.songs_at(con, list(plain))
        except studio.StudioUnavailable:
            found_at = {}  # a song given by its path never needs Studio
        for path, asset in found_at.items():
            args.music = [path if s == plain[path] else s for s in args.music]
            args.music_library[path] = _library_info(asset, f"--music {plain[path]}")
    return None


def _library_info(asset, given: str) -> dict:
    """What a run keeps about a Venture Studio song (Clip.library)."""
    return {
        "studio_id": asset.id, "title": asset.title, "provider": asset.provider, "license": asset.license,
        "tags": list(asset.tags), "notes": asset.notes, "favorite": asset.favorite, "from": given,
    }


def _music_problem(args) -> str | None:
    """A --music file that's missing or not audio, found before the output folder is made."""
    if args.command != "build":
        return None
    from .pipeline import music_problem

    return next((p for p in (music_problem(song) for song in args.music) if p), None)


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
        "venture_studio": studio_report(offline=offline),
    }


def studio_report(offline: bool = False, cache_dir: Path | None = None) -> dict:
    """Venture Studio's status for doctor and `studio status`; a problem is a reason in it, never an error."""
    from . import studio

    try:
        return studio.status(cache_dir=cache_dir, offline=offline)
    except Exception as e:  # noqa: BLE001 - doctor always answers
        return {"installed": True, "available": False, "reason": f"couldn't read Venture Studio: {e}", "summary": f"couldn't read Venture Studio: {e}",
                "libraries": [], "notes": [], "offline": offline}


def _studio_detail(vs: dict) -> str:
    if not vs.get("installed"):
        return "optional: not installed"
    if not vs.get("available"):
        return vs.get("reason") or "not available"
    notes = vs.get("notes") or []
    return (vs.get("summary") or "") + (f" ({len(notes)} note{'s' if len(notes) != 1 else ''}: roughcut studio status)" if notes else "")


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
    vs = r.get("venture_studio")
    if isinstance(vs, dict):  # informational: never changes doctor's exit code
        rows.append(("Venture Studio", bool(vs.get("available")) and not vs.get("notes"), _studio_detail(vs)))
    print(f"roughcut {r['version']} (Python {r['python']}, {r['executable']})")
    for label, ok, detail in rows:
        print(f"  {'ok ' if ok else '-- '} {label:<15} {detail}")
    return 0 if (r["ffmpeg"] and r["ffprobe"]) else 1


def _error_kind(e: BaseException) -> dict:
    """For the app: a used-up plan or credit means "come back later", not "fix something"."""
    from .providers import ProviderLimitError

    return {"kind": "usage_limit"} if isinstance(e, ProviderLimitError) else {}


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

    progress.stop_watch()
    log_file_only(line)
    log_to(None)


def _glue_levels(argv: list[str]) -> list[str]:
    """argparse takes `-14LUFS` after `--dialogue-level` for another option
    (only bare numbers like -14 pass), so join it as `--dialogue-level=-14LUFS`."""
    out: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "--dialogue-level":
            value = next(it, None)
            out.append(a if value is None else f"{a}={value}")
        else:
            out.append(a)
    return out


def main(argv: list[str] | None = None) -> int:
    # Only a real command line (not a test calling main()) takes over the process group.
    command_line = argv is None
    argv = _glue_levels(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    if args.command == "update":
        from .update import update

        return update()
    if args.command == "preset":
        from .presets import command

        return command(args)
    if args.command in ("build", "render", "revise"):
        # Before anything runs or any folder is created.
        args, problem = _apply_preset(args, argv) if args.command != "revise" else (args, None)
        problem = problem or _resolve_paths(args, argv) or _studio_songs(args) or _music_problem(args)
        if problem:
            progress.enable(bool(args.progress_json))
            print(f"roughcut: {problem}", file=sys.stderr)
            progress.emit("error", message=problem)
            return 1
    if args.command == "studio":
        return _studio_command(args)
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
    restore = _install_stop_handlers(json_mode, own_group=command_line)
    awake = keep_awake()  # after setpgrp, so a cancel from the app stops it too
    started = time.monotonic()
    _OUTCOME.clear()
    try:
        return _run(args, json_mode)
    finally:
        if awake is not None:
            _stop(awake)
        _restore_handlers(restore)
        if _OUTCOME and not json_mode and _interactive() and time.monotonic() - started >= NOTIFY_AFTER:
            signal_done(_OUTCOME["text"])


# ------------------------------------------------------------------ Venture Studio


def where_report(folder: str, report: dict) -> dict:
    """`studio status --where DIR`: is DIR inside a Studio library, and where would a build of it write?"""
    from . import studio
    from .pipeline import default_out_dir

    path = Path(folder).expanduser().resolve()
    out = default_out_dir([path])
    found: dict = {"path": str(path), "output": str(out), "inside": None, "library": None, "root": None, "output_inside": None, "elsewhere": None}
    if not report.get("installed"):
        return {**found, "inside": False, "output_inside": False}  # no Studio, no libraries
    if not report.get("available"):
        return found  # couldn't tell
    libs = [studio.Library(0, lib["path"], lib["name"]) for lib in report.get("libraries") or []]
    roots = [lib.path for lib in libs]
    root = studio.inside_library(path, roots)
    out_root = studio.inside_library(out, roots)
    lib = next((x for x in libs if x.path == (root if root is not None else out_root)), None)
    out_lib = next((x for x in libs if x.path == out_root), None) if out_root is not None else None
    elsewhere = studio.elsewhere_for(out, out_lib, roots) if out_lib is not None else None
    found.update(
        inside=root is not None, library=lib.name if lib else None, root=lib.path if lib else None, output_inside=out_root is not None,
        elsewhere=str(elsewhere) if elsewhere else None,
    )
    return found


def _where_lines(w: dict) -> list[str]:
    if w["inside"] is None:
        return [f"Couldn't tell whether {w['path']} is inside a Venture Studio library."]
    lines = [
        f"{w['path']} is inside the Venture Studio library {w['library']!r} ({w['root']})." if w["inside"]
        else f"{w['path']} isn't inside a Venture Studio library."
    ]
    if w["output_inside"]:
        lines.append(
            f"A build of it writes to {w['output']} by default, inside the library"
            + (f": in Terminal, Roughcut offers {w['elsewhere']} instead (or choose a folder with -o)." if w["elsewhere"] else ": choose a folder outside it with -o.")
        )
    return lines


def _studio_music(args) -> int:
    """`roughcut studio music`: plain lines, or a JSON array (never an object, so the app's event reader can't mistake it)."""
    from . import studio

    try:
        listing = studio.music_listing(
            args.words, collection=args.collection, kit=args.kit, favorites=args.favorites,
            cache_dir=Path(args.cache_dir).expanduser() if args.cache_dir else None,
        )
    except studio.StudioUnavailable as e:
        print(f"roughcut: {'Venture Studio is not installed on this Mac (looked in ' + str(studio.studio_dir()) + ')' if e.silent else e.reason}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{**row, "as_of": listing.source} for row in listing.rows]))
        return 0
    for line in studio.listing_lines(listing):
        print(line)
    return 0


def _studio_command(args) -> int:
    if args.studio_command == "music":
        return _studio_music(args)
    report = studio_report(offline=args.offline, cache_dir=Path(args.cache_dir).expanduser() if args.cache_dir else None)
    if args.where:
        report["where"] = where_report(args.where, report)
    if args.json:
        print(json.dumps(report))
        return 0
    from .studio import status_lines

    for line in status_lines(report):
        print(line)
    if args.where:
        for line in _where_lines(report["where"]):
            print(line)
    return 0


def offer_outside_library(inputs: list[Path], cache_dir: Path | None, ask=None) -> Path | None:
    """Terminal builds with the default output folder: when it would land inside a Venture Studio library,
    offer a folder outside every library (~/Movies/Roughcut/<library>/<path below it>). Its answer, or None."""
    from . import studio, versions
    from .pipeline import default_out_dir

    out = default_out_dir(inputs).expanduser().resolve()
    guard, _ = studio.output_guard(out, cache_dir=cache_dir)
    if guard is None or guard.elsewhere is None or versions.entries(out):
        return None  # an existing project folder keeps its versions where they are (the run warns)
    print(
        f"The default output folder, {out}, is inside the Venture Studio library {guard.library.name!r}. "
        "Studio would catalog everything Roughcut writes there."
    )
    try:
        answer = (ask or input)(f"Save this cut in {guard.elsewhere} instead? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer in ("", "y", "yes"):
        print(f"Saving in {guard.elsewhere}.\n")
        return guard.elsewhere
    print("Keeping the default folder (the paper edit's Warnings will say it's inside the library).\n")
    return None


# ------------------------------------------------------------------ walking away from a long run

NOTIFY_AFTER = 60.0  # seconds: shorter runs finish while you're still watching
_OUTCOME: dict = {}  # how the run ended, for the notification


def keep_awake(popen=subprocess.Popen):
    """macOS: keep the Mac from idle-sleeping (and from sleeping on power) until this process exits.

    A MacBook that sleeps partway through drops the Claude request and wastes the run.
    """
    if sys.platform != "darwin":
        return None
    try:
        return popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _stop(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - it exits with us anyway (-w)
        pass


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def signal_done(text: str, run=subprocess.run) -> None:
    """The end of a long Terminal run: a bell (the Dock icon bounces) and, on a Mac, a notification banner."""
    sys.stdout.write("\a")
    sys.stdout.flush()
    if sys.platform != "darwin":
        return
    script = f"display notification {_applescript(text[:220])} with title \"roughcut\""
    try:
        run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# `render --format auto`: the footage's own format again. The Mac app's Re-cut (RecutOptions.arguments())
# follows the Terminal rule with --progress-json too: a setting it sends wins ("Match footage" is
# --format auto; fillers and stringout are explicit on/off flags) and one it leaves out keeps the saved
# run's, so a vertical cut re-cut "tighter" stays vertical and "Finish without AI" uses the run's settings.
FORMAT_FROM_FOOTAGE = ("auto", "footage", "from footage")


def _run(args, json_mode: bool) -> int:
    from .pipeline import Options, build, rerender, result_summary
    from .report import HEADLINE_WARNINGS

    # Only the cut settings given on the command line; the rest are defaults (build) or the saved run's (render).
    given = {
        k: v
        for k, v in dict(
            # auto: a build picks the pacing from its brief; a re-cut or revision keeps the run's own.
            style=args.style or "auto" if args.command == "build" else (None if args.style == "auto" else args.style),
            keep_fillers=args.keep_fillers,
            format=args.format,
            broll_db=args.broll_db,
            music_db=args.music_db,
            stringout=args.stringout,
            fill=args.fill,
            punch_in=args.punch_in,
            dialogue_level=args.dialogue_level,
            fades=None if args.fades is None else args.fades == "on",
        ).items()
        if v is not None
    }
    if str(args.format or "").strip().lower() in FORMAT_FROM_FOOTAGE:
        given["format"] = None  # given, and it means "the footage's own format", e.g. after a vertical build
    if args.dialogue_level == LEVEL_OFF:
        given["dialogue_level"] = None  # given: keep clip levels as recorded
    common = dict(name=args.name, **given)
    try:
        if args.command == "render":
            result = rerender(
                Path(args.out_dir).expanduser().resolve(), Options(inputs=[], **common), overrides=set(given), design_missing=args.design_missing
            )
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
            out = Path(args.out) if args.out else None
            if out is None and args.menu and not json_mode and _interactive():
                out = offer_outside_library([Path(p) for p in args.inputs], Path(args.cache_dir).expanduser() if args.cache_dir else None)
            if args.provider in ("anthropic", "claude-code"):
                from .providers.anthropic_provider import resolve_model

                if args.model is None and args.vision_model is None and args.menu and not json_mode and _interactive():
                    args.model, args.vision_model = choose_claude_models(vision=args.vision is not False)
                args.model = resolve_model(args.model) if args.model else None
                args.vision_model = resolve_model(args.vision_model) if args.vision_model else None
            context = "\n\n".join(t for t in (args.preset_brief, args.context, _read(args.context_file)) if t.strip())
            if args.preset_brief.strip() and args.context.strip().startswith(args.preset_brief.strip()):
                # The app's brief starts with the preset's, sent apart only to say which part is the preset's.
                context = "\n\n".join(t for t in (args.context, _read(args.context_file)) if t.strip())
            opts = Options(
                inputs=[Path(p) for p in args.inputs],
                out=out,
                context=context,
                preset_brief=args.preset_brief.strip(),
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
                graphics_logo=args.graphics_logo,
                graphics_look=tuple(args.graphics_look or ()),
                music=[Path(p).expanduser() for p in args.music],
                music_library=dict(getattr(args, "music_library", {}) or {}),
                music_from=args.music_from or None,
                music_max=args.music_max,
                **common,
            )
            result = build(opts)
    except (ProviderError, ValueError, RuntimeError, OSError) as e:
        hint, command = resume_hint(e), resume_command(e)
        print(f"roughcut: {e}", file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        progress.emit("error", message=str(e), **({"resume": command} if command else {}), **_error_kind(e))
        _log_end(f"STOPPED: {e}" + (f"\n{hint}" if hint else ""))
        _OUTCOME["text"] = f"Stopped: {e}"
        return 1
    except KeyboardInterrupt as e:
        # Ctrl-C isn't a crash: no traceback scrolling the hint away; it's the last thing printed.
        hint = resume_hint(e)
        print("\nStopped (Ctrl-C)." + (f"\n{hint}" if hint else ""), file=sys.stderr)
        _log_end("STOPPED: interrupted (Ctrl-C)" + (f"\n{hint}" if hint else ""))
        return 130
    except Exception as e:  # a bug: tell the app, then show the traceback
        hint, command = resume_hint(e), resume_command(e)
        # With the command, the app still offers to finish the paid-for plan without AI.
        progress.emit("error", message=f"Unexpected error ({type(e).__name__}): {e}", **({"resume": command} if command else {}), **_error_kind(e))
        import traceback

        if hint:
            print(hint, file=sys.stderr)
        _log_end("STOPPED by a bug:\n" + traceback.format_exc() + (f"\n{hint}" if hint else ""))
        _OUTCOME["text"] = f"Stopped by an unexpected error ({type(e).__name__}); details in roughcut.log"
        raise
    _log_end(f"FINISHED: {result.fcpxml.name} and {result.report.name} written")
    _OUTCOME["text"] = (
        f"Only the stringout: the AI step failed ({seconds_to_clock(result.timeline.seconds)})" if result.fell_back
        else f"{result.project or result.timeline.name} is ready ({seconds_to_clock(result.timeline.seconds)})"
    )
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
    print(f"\nRough cut: {tl.review.length_line() if tl.review else seconds_to_clock(tl.seconds)} ({len(tl.spine)} edits, {sum(1 for c in tl.connected if c.kind == 'broll')} B-roll)")
    if result.stringout is not None:
        print(f"Stringout: {seconds_to_clock(result.stringout.seconds)}")
    if result.dead_air and result.dead_air[0] > 0:
        talk, kept = result.dead_air
        print(f"Dead air removed: {seconds_to_clock(max(0.0, talk - kept))} of {seconds_to_clock(talk)} talking footage ({max(0.0, 1 - kept / talk):.0%})")
    for w in result.warnings:
        if "almost no pauses" in w or w.startswith(HEADLINE_WARNINGS):
            print(f"  ! {w}")
    for line in summary_lines(result.summary):
        print(line)
    print(f"FCPXML:    {result.fcpxml}")
    if result.captions:
        print(f"Captions:  {result.captions}  (in Final Cut, with the project open: File > Import > Captions)")
    print(f"Report:    {result.report.with_suffix('.html')}  (Markdown: {result.report.name})")
    preview = result.out_dir / "graphics" / "preview.html"
    if any(c.kind == "graphic" for c in tl.connected) and preview.is_file():
        print(f"Graphics:  {preview}")
    if result.version > 1:
        print(f"This is version {result.version}; earlier cuts are in {result.out_dir / 'history'}.")
    if opened:
        print(f"\n{opened}")
    else:
        print("\nIn Final Cut Pro: File > Import > XML..., then pick the .fcpxml above.")
    print(f"Then open the project \"{result.project or tl.name}\" in the event \"{result.event}\". The clips in the browser stay your untouched originals.")
    return 3 if result.fell_back else 0


def summary_lines(summary: dict) -> list[str]:
    """What made it into the cut, what was left out, and what the model calls cost: before opening the report."""
    from .providers import describe_cost

    if not summary:
        return []
    made, left = [], []
    vis = summary.get("visual_log")
    if vis and vis.get("total"):
        (made if vis["described"] == vis["total"] else left).append(
            f"visual log {vis['described']} of {vis['total']} clips" + ("" if vis["described"] == vis["total"] else " described (the rest are in Warnings)")
        )
    g = summary.get("graphics")
    if g is not None:
        if g.get("note"):
            # As the app shows it: the count, then why.
            left.append(f"graphics {g['rendered']} of {g['planned']} on the timeline ({g['note']})" if g.get("planned") else f"graphics: {g['note']}")
        elif g["planned"]:
            text = f"graphics {g['rendered']} of {g['planned']} on the timeline"
            why = []
            if g["placed"] < g["planned"]:
                why.append(f"{g['planned'] - g['placed']} couldn't be placed on their words")
            if g["rendered"] < g["placed"]:
                why.append(f"{g['placed'] - g['rendered']} didn't design or render")
            (left if why else made).append(text + (f" ({'; '.join(why)}; see Warnings)" if why else ""))
    skipped = summary.get("skipped") or {}
    files = skipped.get("files") or []
    if files:
        shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
        left.append(f"{len(files)} file{'s' if len(files) != 1 else ''} couldn't be read ({shown})")
    if skipped.get("plan_refs"):
        n = skipped["plan_refs"]
        left.append(f"{n} plan reference{'s' if n != 1 else ''} dropped or fixed (see Warnings)")
    lines = []
    if made:
        lines.append("In the cut: " + "; ".join(made))
    if left:
        lines.append("Left out:   " + "; ".join(left))
    cost = describe_cost(summary.get("usage") or {})
    if cost:
        lines.append("AI usage:   " + cost)
    return lines


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
    A signal the process was started ignoring (`nohup roughcut build ...`) stays ignored.
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
        # A signal isn't an exception: nothing marks it with the saved plan, so ask the pipeline.
        saved = getattr(sys.modules.get(f"{__package__}.pipeline"), "PLAN_SAVED", None)
        command = f"roughcut render {shlex.quote(str(saved))}" if saved else None
        hint = f"The edit plan was saved before this stopped. Finish the cut without calling the model again:\n  {command}" if command else None
        _log_end(f"STOPPED: {reason}" + (f"\n{hint}" if hint else ""))
        if hint:
            _write_fd(2, f"\n{hint}\n")
        if json_mode:
            # Straight to the file descriptor: print() from a signal handler can collide with a write in progress.
            event = {"event": "error", "message": f"Stopped: {reason}.", "kind": "cancelled", **({"resume": command} if command else {})}
            _write_fd(1, json.dumps(event) + "\n")
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
            if signal.getsignal(sig) == signal.SIG_IGN:
                continue  # started with nohup (or told to ignore it): closing Terminal must not stop the run
            previous[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):  # not the main thread
            pass
    return previous


def _write_fd(fd: int, text: str) -> None:
    try:
        os.write(fd, text.encode("utf-8"))
    except OSError:  # the app or Terminal has gone already
        pass


def _restore_handlers(previous: dict) -> None:
    import signal

    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        except (ValueError, OSError, TypeError):
            pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

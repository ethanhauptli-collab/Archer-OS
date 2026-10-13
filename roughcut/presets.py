"""Presets: a brand's or channel's usual settings, picked with `--preset NAME`.

A preset holds the brief, target length, pacing, sequence format, whether
motion graphics are on, the graphics look, an optional logo, approved
designs from an earlier run for new graphics to match, and (yours only)
sound effects for accents: folders and Venture Studio collections. Two are
built in (venture-oc, save-the-kid). `roughcut preset save` stores your
own, or your version of a built-in one (exact brand colors, a logo), as JSON
in your user config folder.

Flags typed on the command line always win over the preset: its flags are
inserted before yours, so argparse keeps the last value. The preset's brief
and your own `-c` / `--context-file` brief are combined.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .textio import read_user_text, real_files

# The graphics look for each built-in preset: one constant each, used nowhere else.
# Venture OC's palette and typefaces are the ones ventureoc.com uses (navy text and
# plates, orange buttons, gold highlights; Jost headings, Inter body, Fraunces
# display), and those three fonts ship with roughcut so renders never fall back.
# Save the Kid has no published brand, so its look stays a description.
VENTURE_OC_LOOK = (
    "Venture Orange County house look (from ventureoc.com): deep navy #0A2540 for plates and dark text, "
    "orange #FD8C27 as the one accent (rules, underlines, highlighted words), warm gold #E8C778 sparingly for "
    "big numbers, white or soft gray #E6E9EB text on navy. Type: Jost bold for headlines and names, Inter for "
    "small labels and roles, Fraunces italic only for pulled quotes. Softly rounded plates (about 14 px corners), "
    "crisp confident ease-out motion, premium and energetic, no gimmicks."
)
SAVE_THE_KID_LOOK = (
    "Understated and warm: clean white type with a soft shadow or a thin translucent plate, no loud colors, "
    "slow gentle fades, small and out of the way so the moment stays the focus."
)

VENTURE_OC_BRIEF = """\
What it is: a Venture Orange County promo about local business, food, culture and the people behind them.
Tone: energetic, confident and upbeat. Short and punchy: every line earns its place.
Structure: open cold on the strongest, most quotable line or moment, not on an introduction. Keep it moving with quick B-roll changes and no logistics talk. End on a clear, upbeat closing line or call to action if one is said.
Must keep: names, places and numbers people will remember.
Graphics: a lower third for each speaker the first time they appear (only names and roles the footage or brief states), callouts for big numbers, and a kinetic caption on the hook."""

SAVE_THE_KID_BRIEF = """\
What it is: an episode of Save the Kid, a personal YouTube channel about fatherhood and adventure.
Tone: intimate, reflective and candid: a dad talking honestly, not a presenter. Let moments breathe; keep the laughter and the unscripted moments with the kids.
Structure: open on the most honest or surprising moment, then follow the day or the story in order, and end on a reflective line to camera if there is one. Cut logistics and false starts.
B-roll: scenery and small family moments over talking stretches; hold shots longer than a promo would.
Music: a gentle bed under montage passages, never under long talking.
Graphics: minimal. An episode title card near the open and a name lower third only when someone new is introduced; no stat callouts or kinetic captions."""

STYLES = ("tight", "medium", "loose")
LOGO_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
LOGO_MAX_BYTES = 5_000_000
LOGO_SRC = "brand:logo"
LOGO_DIRECTION = (
    f'Brand logo: an image is available as <img src="{LOGO_SRC}" alt="">, the one exception to the no-images rule. '
    "Put it on every title card, and small at one end of each lower third; leave it off captions, stats, quotes "
    "and lists. Set its height and leave the width auto; never stretch, recolor or redraw it."
)
LOOK_MAX = 3  # approved designs: the designer reads every one on every request
LOOK_MAX_CHARS = 60_000  # each, without its inlined logo
LOOK_DIRECTION = (
    "Approved designs: the graphics below are from an earlier video in this series and were approved as the house "
    "look, some of them adjusted by hand. Match them exactly (typefaces, sizes, the exact colors in their CSS, plates, "
    "corner radii, logo placement, easing and timing); where they differ from the style direction above, they win. "
    "Adapt the layout to each new graphic's kind and text, and never reuse their words."
)
NO_LOGO_NOTE = f'There is no logo this time: leave out any <img src="{LOGO_SRC}">.'
_DATA_IMAGE = re.compile(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=]+")  # designs' only images are the logo


class PresetError(ValueError):
    """A preset that can't be found, read or saved as given."""


@dataclass
class Preset:
    name: str
    description: str = ""
    brief: str = ""
    target: str | None = None  # "90s", "10m", "8:30"
    style: str | None = None  # tight / medium / loose (None: auto, from the brief)
    format: str | None = None  # 1080p, 4k, vertical... (None: from the footage)
    graphics: bool = False
    graphics_style: str = ""
    logo: str | None = None  # PNG/SVG/JPEG/WebP the graphics may show
    look: tuple[str, ...] = ()  # approved designs (.html) new graphics should match
    # Sound effects for accents (--sfx, --sfx-from). Only your presets have them: the built-in ones run on Macs without them.
    sfx: tuple[str, ...] = ()  # files or folders
    sfx_from: tuple[str, ...] = ()  # Venture Studio collections or kits by name
    music_from: str | None = None  # a Venture Studio collection or kit whose songs a build offers (yours only; never a built-in's)
    source: str = field(default="built in", compare=False)  # built in / saved / saved over built in

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("source")
        d["look"] = list(self.look)
        d["sfx"] = list(self.sfx)
        d["sfx_from"] = list(self.sfx_from)
        return d

    def summary(self) -> str:
        from .timecode import parse_duration, seconds_to_clock

        bits = []
        if self.target:
            try:
                bits.append(seconds_to_clock(parse_duration(self.target)))
            except ValueError:
                bits.append(self.target)
        bits.append(f"{self.style} pacing" if self.style else "pacing from the brief")
        bits.append(self.format or "format from the footage")
        bits.append("graphics on" if self.graphics else "graphics off")
        if self.logo:
            bits.append("logo")
        if self.look:
            bits.append(f"approved look ({len(self.look)} design{'s' if len(self.look) != 1 else ''})")
        if self.sfx or self.sfx_from:
            bits.append("sound effects")
        if self.music_from:
            bits.append(f"music from Studio {self.music_from!r}")
        return ", ".join(bits)


BUILT_IN = {
    p.name: p
    for p in (
        Preset(
            name="venture-oc",
            description="Venture Orange County promos: energetic and short",
            brief=VENTURE_OC_BRIEF,
            target="90s",
            style="tight",
            graphics=True,
            graphics_style=VENTURE_OC_LOOK,
        ),
        Preset(
            name="save-the-kid",
            description="Save the Kid, a personal YouTube channel about fatherhood and adventure: intimate and reflective, minimal graphics",
            brief=SAVE_THE_KID_BRIEF,
            target="10m",
            style="loose",
            graphics=True,
            graphics_style=SAVE_THE_KID_LOOK,
        ),
    )
}


# ------------------------------------------------------------------ storage


def config_dir() -> Path:
    """Per-user settings: ~/Library/Application Support/roughcut on a Mac."""
    if os.environ.get("ROUGHCUT_CONFIG_DIR"):
        return Path(os.environ["ROUGHCUT_CONFIG_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "roughcut"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "roughcut"


def presets_dir() -> Path:
    return config_dir() / "presets"


def _clean_name(name: str) -> str:
    clean = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", clean):
        raise PresetError(f"{name!r} can't be a preset name: use letters, numbers and dashes, like venture-oc.")
    return clean


def _file(name: str) -> Path:
    return presets_dir() / f"{_clean_name(name)}.json"


FIELDS = (
    "description", "brief", "target", "style", "format", "graphics", "graphics_style", "logo", "look", "sfx", "sfx_from", "music_from",
)
TEXT_FIELDS = ("description", "brief", "graphics_style")  # "" when unset; the others are None


def _from_file(path: Path) -> Preset:
    """A saved preset. One saved over a built-in holds only what differs, so it keeps
    picking up improvements to the built-in brief after `roughcut update`."""
    fix = f"Fix that file, or delete it with `roughcut preset remove {path.stem}`."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PresetError(f"can't read the preset {path}: {e}. {fix}") from e
    if not isinstance(data, dict):
        raise PresetError(f"can't read the preset {path}: not a JSON object. {fix}")
    built_in = path.stem in BUILT_IN
    preset = replace(BUILT_IN[path.stem]) if built_in else Preset(name=path.stem)
    for key in FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == "graphics":
            value = bool(value)
        elif key == "look":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise PresetError(f"the preset {path} has a problem: look should be a list of .html files. {fix}")
            value = tuple(value)
        elif key in ("sfx", "sfx_from"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise PresetError(f"the preset {path} has a problem: {key} should be a list of {'paths' if key == 'sfx' else 'names'}. {fix}")
            value = tuple(v for v in value if v.strip())
        elif key in TEXT_FIELDS:
            value = "" if value is None else str(value)
        else:
            value = None if value in (None, "") else str(value)
        setattr(preset, key, value)
    preset.source = "saved over built in" if built_in else "saved"
    problem = check(preset, need_files=False)
    if problem:
        raise PresetError(f"the preset {path} has a problem: {problem}. {fix}")
    return preset


def names() -> list[str]:
    saved = sorted(p.stem for p in real_files(presets_dir().glob("*.json"))) if presets_dir().is_dir() else []
    return list(BUILT_IN) + [n for n in saved if n not in BUILT_IN]


def load(name: str) -> Preset:
    """A saved preset (yours win over a built-in one of the same name), or a built-in one."""
    path = _file(name)
    if path.is_file():
        return _from_file(path)
    if path.stem in BUILT_IN:
        return replace(BUILT_IN[path.stem])
    raise PresetError(f"there's no preset called {name!r}. Presets: {', '.join(names())} (see `roughcut preset list`).")


def all_presets() -> list[Preset]:
    out = []
    for n in names():
        try:
            out.append(load(n))
        except PresetError as e:
            print(f"roughcut: {e}", file=sys.stderr)
    return out


def save(preset: Preset) -> Path:
    problem = check(preset)
    if problem:
        raise PresetError(problem)
    path = _file(preset.name)
    preset.look = _own_look(preset)
    data = {k: v for k, v in preset.to_json().items() if k in FIELDS}
    if preset.name in BUILT_IN:  # only your changes, layered over the built-in one
        base = BUILT_IN[preset.name].to_json()
        data = {k: v for k, v in data.items() if v != base.get(k)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def look_dir(name: str) -> Path:
    """Where a preset keeps its own copies of its approved designs."""
    return presets_dir() / f"{_clean_name(name)}.look"


def _own_look(preset: Preset) -> tuple[str, ...]:
    """The approved designs, copied into the preset's own folder (logo taken out), so a run
    folder or another preset being deleted can't break it. Copies no longer used are removed."""
    folder = look_dir(preset.name)
    kept: list[Path] = []
    for n, page in enumerate(preset.look, 1):
        src = Path(page).expanduser().resolve()
        if src.parent == folder.resolve():
            kept.append(src)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{n}-{src.name}"
        dst.write_text(approved_html(src), encoding="utf-8")
        kept.append(dst.resolve())
    if folder.is_dir():
        for old in folder.iterdir():
            if old.resolve() not in kept:
                old.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
    return tuple(str(p) for p in kept)


def remove(name: str) -> Path | None:
    shutil.rmtree(look_dir(name), ignore_errors=True)
    path = _file(name)
    if path.is_file():
        path.unlink()
        return path
    return None


# ------------------------------------------------------------------ checks and the logo


def logo_problem(logo: str) -> str | None:
    path = Path(logo).expanduser()
    if not path.is_file():
        return f"the logo {logo} doesn't exist"
    if path.suffix.lower() not in LOGO_TYPES:
        return f"the logo {path.name} should be a PNG, SVG, JPEG or WebP file"
    if path.stat().st_size > LOGO_MAX_BYTES:
        return f"the logo {path.name} is {path.stat().st_size / 1e6:.0f} MB; use a smaller PNG or an SVG (under 5 MB)"
    return None


def look_problem(page: str) -> str | None:
    path = Path(page).expanduser()
    if not path.is_file():
        return f"the approved design {page} doesn't exist"
    if path.suffix.lower() not in (".html", ".htm"):
        return f"the approved design {path.name} should be an .html file from a run's graphics folder"
    try:
        size = len(approved_html(path))
    except (OSError, UnicodeDecodeError) as e:
        return f"can't read the approved design {path.name}: {e}"
    if size > LOOK_MAX_CHARS:
        return f"the approved design {path.name} is too big to show the designer ({size // 1000} KB; the limit is {LOOK_MAX_CHARS // 1000} KB)"
    return None


def approved_html(path: Path) -> str:
    """A saved design with its inlined logo turned back into brand:logo (keeps it short)."""
    return _DATA_IMAGE.sub(LOGO_SRC, Path(path).expanduser().read_text(encoding="utf-8"))


def _graphic_id(text: str) -> str:
    m = re.fullmatch(r"[Gg]?0*(\d+)", text.strip())
    return f"G{int(m.group(1)):02d}" if m else text.strip().upper()


def approved_from_run(out_dir: Path, ids: list[str]) -> tuple[str, ...]:
    """Design files of graphics from an earlier run: the ids given, or the first two of different kinds."""
    from .graphics import MANIFEST

    out_dir = Path(out_dir).expanduser()
    try:
        data = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PresetError(f"{out_dir} has no graphics to approve. Use the output folder of a build that made graphics.") from None
    designs: dict[str, tuple[str, Path]] = {}
    entries = data.get("graphics") if isinstance(data, dict) else None
    for g in entries if isinstance(entries, list) else []:
        if isinstance(g, dict) and g.get("id") and g.get("html") and (out_dir / str(g["html"])).is_file():  # not one the cut left out
            designs[str(g.get("id", "")).upper()] = (str(g.get("kind", "")), out_dir / str(g["html"]))
    if not designs:
        raise PresetError(f"{out_dir} has no saved graphic designs to approve.")
    if ids:
        chosen = list(dict.fromkeys(_graphic_id(i) for i in ids))
        missing = [g for g in chosen if g not in designs]
        if missing:
            raise PresetError(f"{out_dir} has no design for {', '.join(missing)}; it has {', '.join(designs)}.")
    else:
        kinds: set[str] = set()
        chosen = []
        for gid, (kind, _) in designs.items():
            if kind not in kinds and len(chosen) < 2:
                kinds.add(kind)
                chosen.append(gid)
    if len(chosen) > LOOK_MAX:
        raise PresetError(f"approve up to {LOOK_MAX} designs: the designer reads every one each time.")
    pages = tuple(str(designs[g][1].resolve()) for g in chosen)
    for page in pages:
        problem = look_problem(page)
        if problem:
            raise PresetError(problem)
    return pages


def check(preset: Preset, *, need_files: bool = True) -> str | None:
    """What's wrong with a preset's values, or None."""
    from .timecode import parse_duration
    from .timeline import parse_format

    if preset.target:
        try:
            if parse_duration(preset.target) <= 0:
                raise ValueError
        except ValueError:
            return f"target {preset.target!r} isn't a length; try 90s, 8m or 8:30"
    if preset.style and preset.style not in STYLES:
        return f"pacing {preset.style!r} should be one of {', '.join(STYLES)}"
    if preset.format:
        try:
            parse_format(preset.format)
        except ValueError as e:
            return str(e)
    if not need_files:
        return None
    problems = ([logo_problem(preset.logo)] if preset.logo else []) + [look_problem(page) for page in preset.look]
    return next((p for p in problems if p), None)


def with_logo(style: str, logo: str | None) -> str:
    """The graphics style direction, plus how to place the logo when there is one."""
    if not logo:
        return style
    return (style.strip() + "\n\n" + LOGO_DIRECTION).strip()


def design_style(style: str, logo: str | None = None, look=()) -> str:
    """What the designer gets as style direction: the look, where the logo goes, and the approved designs."""
    text = with_logo(style, logo)
    pages = []
    for n, page in enumerate(look or (), 1):
        try:
            pages.append(f'<approved_design n="{n}">\n{approved_html(Path(page))}\n</approved_design>')
        except (OSError, UnicodeDecodeError):
            continue  # checked before the run; one deleted since is left out
    if pages:
        note = "\n" + NO_LOGO_NOTE if not logo and any(LOGO_SRC in p for p in pages) else ""
        text = (text.strip() + "\n\n" + LOOK_DIRECTION + note + "\n" + "\n".join(pages)).strip()
    return text


def inline_logo(html: str, logo: str | None) -> str:
    """Swap brand:logo for the logo itself as a data: URI (designs can't load files or URLs)."""
    if not logo or LOGO_SRC not in html:
        return html
    path = Path(logo).expanduser()
    mime = LOGO_TYPES.get(path.suffix.lower(), "image/png")
    uri = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return html.replace(LOGO_SRC, uri)


# ------------------------------------------------------------------ command line


def flags(preset: Preset, command: str) -> list[str]:
    """The preset as command-line flags, to go before the user's own (so theirs win)."""
    out = []
    if preset.style:
        out.append(f"--style={preset.style}")
    if preset.format:
        out.append(f"--format={preset.format}")
    if command == "build":
        if preset.brief:
            out.append(f"--preset-brief={preset.brief}")
        if preset.target:
            out.append(f"--target={preset.target}")
        if preset.graphics:
            out.append("--graphics")
        if preset.graphics_style:
            out.append(f"--graphics-style={preset.graphics_style}")
        if preset.logo:
            out.append(f"--graphics-logo={preset.logo}")
        for page in preset.look:
            out.append(f"--graphics-look={page}")
        for path in preset.sfx:
            out.append(f"--sfx={path}")
        for name in preset.sfx_from:
            out.append(f"--preset-sfx-from={name}")  # one Studio doesn't have (any more) is a warning, never a stop
        if preset.music_from:
            out.append(f"--music-from={preset.music_from}")  # soft: without Studio, the build goes on without its songs
    return out


def add_commands(sub) -> None:
    """`roughcut preset list|show|save|remove`."""
    p = sub.add_parser("preset", help="list, show, save or remove presets: a brand's or channel's usual brief, length, pacing and graphics look")
    actions = p.add_subparsers(dest="preset_command", required=True)
    ls = actions.add_parser("list", help="the built-in presets and yours")
    ls.add_argument("--json", action="store_true", help="print them as JSON")
    show = actions.add_parser("show", help="everything a preset sets")
    show.add_argument("name")
    show.add_argument("--json", action="store_true", help="print it as JSON")
    s = actions.add_parser(
        "save",
        help="save a preset of your own, or your version of a built-in one; fields you leave out keep their current values",
    )
    s.add_argument("name")
    s.add_argument("--from", dest="base", help="start from this preset (default: the one being saved, if it exists)")
    s.add_argument("--description")
    s.add_argument("--brief", help="the brief: what these videos are, audience, tone, structure")
    s.add_argument("--brief-file", help="read the brief from a text/markdown file")
    s.add_argument("-t", "--target", help="target length, e.g. 90s, 8m, 8:30 ('' for none)")
    s.add_argument("--style", choices=STYLES, help="pacing")
    s.add_argument("--format", help="1080p, 4k, vertical, square, or WxH@fps ('' = from the footage)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--graphics", dest="graphics", action="store_true", default=None, help="motion graphics on")
    g.add_argument("--no-graphics", dest="graphics", action="store_false", help="motion graphics off")
    s.add_argument("--graphics-style", help="the look: brand colors, fonts, mood")
    lg = s.add_mutually_exclusive_group()
    lg.add_argument("--logo", help="a PNG, SVG, JPEG or WebP logo the graphics may show")
    lg.add_argument("--no-logo", action="store_true", help="drop the logo")
    lk = s.add_mutually_exclusive_group()
    lk.add_argument(
        "--look-from",
        nargs="+",
        metavar=("OUT_DIR", "G01"),
        help=f"approve graphics from an earlier run as the look new ones match: its output folder, then up to {LOOK_MAX} graphic ids (default: the first two of different kinds)",
    )
    lk.add_argument("--no-look", action="store_true", help="drop the approved designs")
    fx = s.add_mutually_exclusive_group()
    fx.add_argument("--sfx", action="append", metavar="PATH", help="sound effects for accents: a file or folder (repeat for more; replaces the preset's)")
    fx.add_argument("--no-sfx", action="store_true", help="drop the sound effects (files, folders and Studio collections)")
    s.add_argument("--sfx-from", action="append", metavar="NAME", help="sound effects from a Venture Studio collection or kit by name (repeat for more; replaces the preset's)")
    mf = s.add_mutually_exclusive_group()
    mf.add_argument(
        "--music-from", metavar="NAME",
        help="a Venture Studio collection or kit whose songs builds offer the planner (as build --music-from; if Studio isn't there, builds go on without)",
    )
    mf.add_argument("--no-music-from", action="store_true", help="no Studio songs")
    rm = actions.add_parser("remove", help="delete a saved preset (a built-in one comes back as it was)")
    rm.add_argument("name")


def _print(preset: Preset) -> None:
    print(f"{preset.name} ({preset.source})" + (f": {preset.description}" if preset.description else ""))
    print(f"  target    {preset.target or 'none'}")
    print(f"  pacing    {preset.style or 'auto (from the brief)'}")
    print(f"  format    {preset.format or 'from the footage'}")
    print(f"  graphics  {'on' if preset.graphics else 'off'}")
    print(f"  look      {preset.graphics_style or '(the default look)'}")
    print(f"  logo      {preset.logo or 'none'}")
    print(f"  approved  {', '.join(Path(p).name for p in preset.look) or 'none'}")
    print(f"  sound fx  {', '.join([*preset.sfx, *(f'Venture Studio: {n}' for n in preset.sfx_from)]) or 'none'}")
    print(f"  music     {'from Venture Studio ' + repr(preset.music_from) if preset.music_from else 'none from Venture Studio'}")
    print("  brief")
    for line in preset.brief.splitlines() or ["(none)"]:
        print(f"    {line}")
    if preset.source == "built in" and preset.graphics:
        print(
            f"\nTo add your logo (the look stays as it is):\n"
            f"  roughcut preset save {preset.name} --logo LOGO_FILE\n"
            f"To match graphics you approved in an earlier run:\n"
            f"  roughcut preset save {preset.name} --look-from OUTPUT_FOLDER"
        )


def command(args: argparse.Namespace) -> int:
    try:
        return _command(args)
    except PresetError as e:
        print(f"roughcut: {e}", file=sys.stderr)
        return 1


def _command(args: argparse.Namespace) -> int:
    if args.preset_command == "list":
        found = all_presets()
        if args.json:
            print(json.dumps([{**p.to_json(), "source": p.source} for p in found]))
            return 0
        width = max((len(p.name) for p in found), default=10)
        for p in found:
            print(f"{p.name:<{width}}  {p.summary()}  [{p.source}]")
            if p.description:
                print(f"{'':<{width}}  {p.description}")
        print(f"\nUse one with: roughcut build FOLDER --preset NAME. Yours are saved in {presets_dir()}")
        return 0
    if args.preset_command == "show":
        preset = load(args.name)
        if args.json:
            print(json.dumps({**preset.to_json(), "source": preset.source}))
        else:
            _print(preset)
        return 0
    if args.preset_command == "remove":
        removed = remove(args.name)
        name = _clean_name(args.name)
        if removed is None:
            if name in BUILT_IN:
                raise PresetError(f"{name} is built in and can't be removed, and you haven't saved a version of it.")
            raise PresetError(f"there's no saved preset called {name!r}.")
        print(f"Removed {removed}." + (f" The built-in {name} preset is back." if name in BUILT_IN else ""))
        return 0

    # save
    name = _clean_name(args.name)
    if args.base:
        preset = replace(load(args.base), name=name)
    elif _file(name).is_file() or name in BUILT_IN:
        preset = load(name)  # a broken saved file stops here; it's never overwritten with blanks
    else:
        preset = Preset(name=name)
    if args.description is not None:
        preset.description = args.description
    if args.brief_file:
        try:
            preset.brief = read_user_text(args.brief_file).strip()
        except OSError as e:
            raise PresetError(f"can't read {args.brief_file}: {e}") from e
    elif args.brief is not None:
        preset.brief = args.brief.strip()
    if args.target is not None:
        preset.target = args.target.strip() or None
    if args.style is not None:
        preset.style = args.style
    if args.format is not None:
        preset.format = args.format.strip() or None
    if args.graphics is not None:
        preset.graphics = args.graphics
    if args.graphics_style is not None:
        preset.graphics_style = args.graphics_style.strip()
    if args.no_look:
        preset.look = ()
    elif args.look_from:
        from .paths import PathInputError, leading_folder

        try:  # the output folder, then graphic IDs: a folder path with spaces typed without quotes arrives in pieces
            folder, ids = leading_folder(args.look_from, wanted="graphics.json", notify=lambda line: print(line, file=sys.stderr))
        except PathInputError as e:
            raise PresetError(str(e)) from None
        if not Path(folder).expanduser().is_dir():
            raise PresetError(f"can't find the folder {folder} (put a path with spaces in quotes, or drag the folder into Terminal).")
        preset.look = approved_from_run(Path(folder), ids)
    if args.no_music_from:
        preset.music_from = None
    elif args.music_from is not None:
        preset.music_from = args.music_from.strip() or None
    if args.no_logo:
        preset.logo = None
    elif args.logo:
        preset.logo = str(Path(args.logo).expanduser().resolve())
    if args.no_sfx:
        preset.sfx, preset.sfx_from = (), ()
    if args.sfx:
        # By spelling only: a sound-effects folder may be on a share that isn't connected right now.
        preset.sfx = tuple(dict.fromkeys(os.path.normpath(str(Path(p).expanduser().absolute())) for p in args.sfx if p.strip()))
    if args.sfx_from:
        preset.sfx_from = tuple(dict.fromkeys(n.strip() for n in args.sfx_from if n.strip()))
    path = save(preset)
    print(f"Saved the {name} preset to {path}: {preset.summary()}.")
    print(f"Use it with: roughcut build FOLDER --preset {name}")
    return 0

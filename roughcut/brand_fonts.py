"""Brand fonts for motion graphics.

Graphics render offline, so a design that names a font the Mac doesn't have
silently falls back to a system face. roughcut ships the Venture OC fonts
(Jost, Inter, Fraunces; SIL Open Font License) and reads any .ttf/.otf/.woff2
you drop into the fonts folder (~/Library/Application Support/roughcut/fonts on
a Mac; $ROUGHCUT_FONTS overrides it). Every
family a design names is embedded into the page as @font-face data, so the
render never depends on what's installed.
"""

from __future__ import annotations

import base64
import os
import re
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BUNDLED_DIR = Path(__file__).parent / "assets" / "fonts"
_EXTENSIONS = {".ttf": "truetype", ".otf": "opentype", ".woff2": "woff2", ".woff": "woff"}
_MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".woff2": "font/woff2", ".woff": "font/woff"}


def user_fonts_dir() -> Path:
    override = os.environ.get("ROUGHCUT_FONTS")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "roughcut" / "fonts"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "roughcut" / "fonts"


@dataclass(frozen=True)
class FontFile:
    family: str
    path: Path
    italic: bool
    weight: str  # "400", or "100 900" for a variable font

    @property
    def format(self) -> str:
        return _EXTENSIONS[self.path.suffix.lower()]


def _name_table(data: bytes) -> dict[int, str]:
    """nameID -> string from an sfnt (TTF/OTF) file; empty for anything else."""
    names: dict[int, str] = {}
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return names
    num_tables = struct.unpack(">H", data[4:6])[0]
    for i in range(num_tables):
        rec = data[12 + 16 * i : 28 + 16 * i]
        if len(rec) < 16:
            break
        tag, _, offset, _ = struct.unpack(">4sIII", rec)
        if tag != b"name":
            continue
        _, count, string_offset = struct.unpack(">HHH", data[offset : offset + 6])
        base = offset + string_offset
        for j in range(count):
            platform, encoding, _, name_id, length, str_off = struct.unpack(">HHHHHH", data[offset + 6 + 12 * j : offset + 18 + 12 * j])
            raw = data[base + str_off : base + str_off + length]
            if platform == 3 or (platform == 0):
                text = raw.decode("utf-16-be", errors="ignore")
            elif platform == 1 and encoding == 0:
                text = raw.decode("mac_roman", errors="ignore")
            else:
                continue
            if text and (name_id not in names or platform == 3):
                names[name_id] = text
        break
    return names


def _has_table(data: bytes, wanted: bytes) -> bool:
    if len(data) < 12:
        return False
    num_tables = struct.unpack(">H", data[4:6])[0]
    return any(data[12 + 16 * i : 16 + 16 * i] == wanted for i in range(num_tables))


def _describe(path: Path) -> FontFile | None:
    if path.suffix.lower() not in _EXTENSIONS:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    names = _name_table(data)
    # Typographic family (16) groups all weights; legacy family (1) splits them.
    family = names.get(16) or names.get(1) or re.split(r"[\[\-_ ]", path.stem)[0]
    style = (names.get(17) or names.get(2) or path.stem).lower()
    weight = "100 900" if _has_table(data, b"fvar") else _weight_from_style(style)
    return FontFile(family=family.strip(), path=path, italic="italic" in style or "oblique" in style, weight=weight)


def _weight_from_style(style: str) -> str:
    for word, weight in (
        ("thin", 100), ("extralight", 200), ("ultralight", 200), ("light", 300), ("medium", 500),
        ("semibold", 600), ("demibold", 600), ("extrabold", 800), ("ultrabold", 800), ("black", 900), ("heavy", 900), ("bold", 700),
    ):
        if word in style.replace(" ", "").replace("-", ""):
            return str(weight)
    return "400"


@lru_cache(maxsize=4)
def _scan(dirs: tuple[Path, ...]) -> tuple[FontFile, ...]:
    found: list[FontFile] = []
    for folder in dirs:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            info = _describe(path) if path.is_file() else None
            if info:
                found.append(info)
    return tuple(found)


def font_dirs() -> tuple[Path, ...]:
    return (user_fonts_dir(), BUNDLED_DIR)


def available() -> dict[str, list[FontFile]]:
    """Family name -> files. Your fonts folder wins over the bundled copy of a family."""
    families: dict[str, list[FontFile]] = {}
    for f in _scan(font_dirs()):
        key = f.family
        if key in families and families[key][0].path.parent != f.path.parent:
            continue  # an earlier folder already supplied this family
        families.setdefault(key, []).append(f)
    return families


def families() -> list[str]:
    return sorted(available(), key=str.lower)


def _mentioned(html: str, family: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(family) + r"(?![\w-])", html, re.IGNORECASE) is not None


def font_face_css(html: str) -> str:
    """@font-face rules (data URIs) for every available family the design names."""
    rules = []
    for family, files in available().items():
        if not _mentioned(html, family):
            continue
        for f in files:
            data = base64.b64encode(f.path.read_bytes()).decode("ascii")
            rules.append(
                "@font-face{font-family:%s;src:url(data:%s;base64,%s) format('%s');font-weight:%s;font-style:%s;font-display:block}"
                % (_css_string(family), _MIME[f.path.suffix.lower()], data, f.format, f.weight, "italic" if f.italic else "normal")
            )
    return "\n".join(rules)


def embed(html: str) -> str:
    """The design with its brand fonts embedded, ready to render offline."""
    css = font_face_css(html)
    if not css:
        return html
    tag = "<style data-roughcut-fonts>\n" + css + "\n</style>"
    m = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if m:
        return html[: m.end()] + tag + html[m.end() :]
    return tag + html


def _css_string(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

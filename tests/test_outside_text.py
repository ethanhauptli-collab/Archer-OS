"""Bytes that aren't UTF-8 from outside roughcut never stop a run.

On an SD card or any non-Mac drive, macOS writes a binary "._name" companion
beside every file roughcut creates; cameras store metadata such as "25°C" in
Latin-1 (byte 0xB0), and ffmpeg/ffprobe echo it as stored; a brief may be
saved as Windows-1252.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from roughcut import ffmpeg_ops, media, pauses, textio
from roughcut.cli import main

# What macOS writes as "._<name>" on exFAT/FAT drives: an AppleDouble header (magic, version,
# "Mac OS X" filler, 2 entries), whose first entry is the Finder info at offset 0x32 with
# length 3760 = 0x00000EB0, so byte 37 is always 0xB0: "can't decode byte 0xb0 in position 37".
APPLEDOUBLE = bytes.fromhex("00051607" "00020000" "4d6163204f532058" "2020202020202020" "0002" "00000009" "00000032" "00000eb0") + bytes(3760)


def _junk_beside_every_file(folder: Path) -> list[Path]:
    made = []
    for p in list(folder.rglob("*")):
        if p.is_file() and not p.name.startswith("._"):
            junk = p.with_name("._" + p.name)
            junk.write_bytes(APPLEDOUBLE)
            made.append(junk)
    return made


def test_the_appledouble_sample_really_is_undecodable():
    assert APPLEDOUBLE[37] == 0xB0
    with pytest.raises(UnicodeDecodeError):
        APPLEDOUBLE.decode("utf-8")


def test_a_recut_on_an_sd_card_ignores_macos_companion_files(footage, tmp_path):
    out = tmp_path / "Camera01_roughcut"
    args = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache"), "--no-menu"]
    assert main(["build", str(footage), "-o", str(out), *args]) == 0
    # A run with motion graphics has a graphics/ folder, so writing the next version checks which
    # clips every FCPXML here still uses, and on an SD card that includes macOS's "._" companions.
    (out / "graphics").mkdir(exist_ok=True)
    (out / "graphics" / "G01-title-card-abc123.mov").write_bytes(b"clip")
    made = _junk_beside_every_file(out)
    assert any(p.name.endswith(".fcpxml") for p in made)
    assert main(["render", str(out), "--style", "tight"]) == 0  # crashed with 'utf-8' codec can't decode byte 0xb0
    _junk_beside_every_file(out)
    assert main(["render", str(out), "--format", "vertical"]) == 0
    from roughcut import versions

    assert [v["n"] for v in versions._read(out)] == [1, 2, 3]  # no "._" file counted as a cut
    assert [p.name for p in textio.real_files(out.glob("*.fcpxml"))] == [next(out.glob("* v3.fcpxml")).name]


def test_companion_files_are_never_fonts_or_presets(tmp_path, monkeypatch):
    from roughcut import brand_fonts, presets

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    shutil.copy(brand_fonts.BUNDLED_DIR / "Jost.ttf", fonts / "Jost.ttf")
    (fonts / "._Jost.ttf").write_bytes(APPLEDOUBLE)
    monkeypatch.setenv("ROUGHCUT_FONTS", str(fonts))
    brand_fonts._scan.cache_clear()
    assert all(not f.path.name.startswith("._") for files in brand_fonts.available().values() for f in files)
    brand_fonts._scan.cache_clear()

    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "cfg"))
    presets.presets_dir().mkdir(parents=True, exist_ok=True)
    (presets.presets_dir() / "._venture-oc.json").write_bytes(APPLEDOUBLE)
    assert all(not p.name.startswith("._") for p in presets.all_presets())
    assert main(["preset", "list"]) == 0


def _tagged_clip(tmp_path: Path) -> Path:
    """A clip whose metadata holds raw Latin-1 bytes, as some cameras write them."""
    clip = tmp_path / "VID_0001.mp4"
    cmd = [
        b"ffmpeg", b"-hide_banner", b"-loglevel", b"error", b"-y",
        b"-f", b"lavfi", b"-i", b"testsrc=size=320x240:rate=24:duration=4",
        b"-f", b"lavfi", b"-i", b"aevalsrc=exprs='0.4*sin(2*PI*300*t)*between(t,0.5,1.5)':s=48000:d=4",
        b"-c:v", b"libx264", b"-preset", b"ultrafast", b"-pix_fmt", b"yuv420p", b"-c:a", b"aac", b"-shortest",
        b"-metadata", b"comment=Temp 25\xb0C, GPS 33\xb047'N",
        b"-metadata", b"title=Caf\xe9 ride",
        str(clip).encode(),
    ]
    subprocess.run(cmd, check=True)
    return clip


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_clip_with_latin1_tags_analyzes(tmp_path):
    clip = _tagged_clip(tmp_path)
    info = media.probe(clip)
    assert info.has_audio and info.has_video
    assert ffmpeg_ops.detect_silences(clip, float(info.duration)) is not None
    assert ffmpeg_ops.window_peaks(clip) and ffmpeg_ops.loudness_blocks(clip) is not None and pauses.window_levels(clip)


def _fake_tool(bin_dir: Path, name: str, stdout: bytes, stderr: bytes = b"") -> None:
    """A stand-in ffmpeg/ffprobe that prints exactly these bytes, the way they echo a camera's Latin-1 tags."""
    tool = bin_dir / name
    tool.write_text(
        "#!" + sys.executable + "\nimport sys\n"
        f"sys.stdout.buffer.write({stdout!r})\nsys.stderr.buffer.write({stderr!r})\n"
    )
    tool.chmod(0o755)


def test_tool_output_holding_latin1_bytes_is_read_not_fatal(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = {
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 1,
                     "duration": "4.0", "tags": {"comment": "PLACEHOLDER"}}],
        "format": {"duration": "4.0", "tags": {"comment": "PLACEHOLDER"}},
    }
    raw = json.dumps(probe).encode().replace(b"PLACEHOLDER", b"Temp 25\xb0C")
    _fake_tool(bin_dir, "ffprobe", raw)
    _fake_tool(bin_dir, "ffmpeg", b"", b"  Metadata:\n    comment : Temp 25\xb0C\n[silencedetect @ 0x1] silence_start: 1.0\n"
                                       b"[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 1.5\n")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + __import__("os").environ["PATH"])
    clip = tmp_path / "VID_0001.m4a"
    clip.write_bytes(b"not really audio")
    data = media._ffprobe(clip)  # raised 'utf-8' codec can't decode byte 0xb0 before
    assert data["format"]["tags"]["comment"] == "Temp 25\ufffdC"
    assert ffmpeg_ops.detect_silences(clip, 4.0) == [(1.0, 2.5)]


def test_run_text_replaces_bytes_that_are_not_utf8():
    proc = textio.run_text([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'25\\xb0C ok')"])
    assert proc.stdout == "25�C ok"


def test_briefs_in_older_encodings_read_as_written(tmp_path):
    text = "Feature Chef Rémi Lauvand and Alberto Bañuelos – 25°C on the pier"
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "utf-16"):
        f = tmp_path / f"brief-{encoding}.txt"
        f.write_bytes(text.encode(encoding))
        assert textio.read_user_text(f) == text, encoding
    mac = tmp_path / "brief-mac.txt"
    mac.write_bytes("Café ° résumé".encode("mac_roman"))
    assert "Caf" in textio.read_user_text(mac)  # never raises, whatever the bytes


def test_a_brief_file_in_windows_1252_reaches_the_plan(footage, tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_bytes("Open on the café, 25°C and sunny.".encode("cp1252"))
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none",
                 "--cache-dir", str(tmp_path / "cache"), "--context-file", str(brief), "--no-menu"]) == 0

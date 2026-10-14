"""Reference images for the motion graphics: stored once per run, shown to the designer only."""

from __future__ import annotations

import base64
import json
import shutil
import struct
import subprocess
import time
import zlib
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, SPEECH, FakeProvider, ffmpeg, make_clip

from roughcut import graphics, pipeline, references
from roughcut.analyze import Analysis
from roughcut.cli import main, summary_lines
from roughcut.graphics import PlacedGraphic
from roughcut.pipeline import Options
from roughcut.plan import GraphicSpec, normalize
from roughcut.providers import ImagePart, ProviderAuthError, ProviderError, ProviderLimitError, TextPart
from roughcut.timeline import STYLES, build_timeline

GRAPHIC = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}
STAT = {"kind": "stat", "segment": "V01.S005", "words": "The kids have been asking", "text": "Weeks", "subtext": "", "seconds": 3, "direction": ""}


# ------------------------------------------------------------------ pictures made here, so no real image is ever in the repo


def png_bytes(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """A solid-colour RGBA PNG, written by hand."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def make_image(path: Path, size: str = "96x64", *, src: str = "testsrc") -> Path:
    ffmpeg("-f", "lavfi", "-i", f"{src}=size={size}:duration=1", "-frames:v", "1", str(path))
    return path


def probe(path: Path) -> tuple[str, int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()
    codec, w, h = out.split(",")
    return codec, int(w), int(h)


def pixel(path: Path, x: int = 4, y: int = 4) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True
    ).stdout
    return raw[0], raw[1], raw[2]


def has_encoder(name: str) -> bool:
    return name in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@pytest.fixture
def pictures(tmp_path) -> Path:
    folder = tmp_path / "pictures"
    folder.mkdir()
    return folder


# ------------------------------------------------------------------ ingestion


@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "webp", "gif", "tiff", "bmp"])
def test_each_format_is_stored_as_a_jpeg_named_by_its_content(ext, pictures, tmp_path):
    if ext == "webp" and not has_encoder("libwebp"):
        pytest.skip("this ffmpeg can't write WebP")
    path = pictures / f"look.{ext}"
    if ext == "png":
        path.write_bytes(png_bytes(64, 48, (200, 30, 30, 255)))
    else:
        make_image(path, "64x48")
    out = tmp_path / "out"
    warnings: list[str] = []
    (name,) = references.ingest([path], out, warnings)
    assert warnings == []
    assert references.STORED.fullmatch(name) and name.startswith("graphics/references/R01-")
    stored = out / name
    assert probe(stored) == ("mjpeg", 64, 48)
    # The name carries the first six hex digits of the stored bytes.
    import hashlib

    assert name.endswith(f"-{hashlib.sha256(stored.read_bytes()).hexdigest()[:6]}.jpg")


def test_transparency_is_flattened_onto_white_and_a_gifs_first_frame_is_used(pictures, tmp_path):
    clear = pictures / "clear.png"
    clear.write_bytes(png_bytes(40, 40, (255, 0, 0, 0)))  # fully transparent red
    gif = pictures / "two.gif"
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:s=48x32:d=0.4:r=5", "-f", "lavfi", "-i", "color=c=blue:s=48x32:d=0.4:r=5",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", str(gif),
    )
    out = tmp_path / "out"
    names = references.ingest([clear, gif], out, [])
    assert len(names) == 2
    r, g, b = pixel(out / names[0])
    assert min(r, g, b) >= 250  # white, not black
    r, g, b = pixel(out / names[1])
    assert r > 200 and b < 60  # the first frame (red), not the last (blue)


def test_a_large_picture_is_scaled_down_to_1600_and_a_small_one_is_never_scaled_up(pictures, tmp_path):
    big = make_image(pictures / "big.png", "3200x1000")
    tall = make_image(pictures / "tall.png", "900x2400")
    small = make_image(pictures / "small.png", "120x80")
    out = tmp_path / "out"
    a, b, c = references.ingest([big, tall, small], out, [])
    assert probe(out / a)[1:] == (1600, 500)
    assert probe(out / b)[1:] == (600, 1600)
    assert probe(out / c)[1:] == (120, 80)


def test_at_most_eight_pictures_are_used_and_the_extras_are_named_in_one_warning(pictures, tmp_path):
    paths = []
    for n in range(10):
        path = pictures / f"look{n:02d}.png"
        path.write_bytes(png_bytes(8 + n, 8, (n * 20, 10, 10, 255)))
        paths.append(path)
    warnings: list[str] = []
    names = references.ingest(paths, tmp_path / "out", warnings)
    assert [n.split("/")[-1][:3] for n in names] == [f"R{k:02d}" for k in range(1, 9)]
    assert warnings == ["Only 8 reference images are used; the rest were left out (look08.png, look09.png)."]
    assert references.MAX_REFERENCES == 8


def test_junk_corrupt_empty_unknown_and_missing_files_are_warnings_never_failures(pictures, tmp_path):
    good = pictures / "good.png"
    good.write_bytes(png_bytes(16, 16, (1, 2, 3, 255)))
    (pictures / "._good.png").write_bytes(b"resource fork junk")
    (pictures / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    corrupt = pictures / "corrupt.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really")
    empty = pictures / "empty.jpg"
    empty.write_bytes(b"")
    notes = pictures / "notes.txt"
    notes.write_text("hello")
    folder = pictures / "a folder"
    folder.mkdir()
    sources = [pictures / "._good.png", pictures / ".DS_Store", corrupt, empty, notes, folder, pictures / "gone.png", good]
    warnings: list[str] = []
    names = references.ingest(sources, tmp_path / "out", warnings)
    assert len(names) == 1 and names[0].startswith("graphics/references/R01-")  # a bad picture doesn't take a number
    text = "\n".join(warnings)
    for named in ("corrupt.png", "empty.jpg", "notes.txt", "a folder", "gone.png"):
        assert f"The reference image {named} " in text and "was left out" in text
    assert "good.png" not in text and "._good" not in text and ".DS_Store" not in text  # junk is skipped without a word
    assert str(pictures) not in text  # a file is named, never its folder
    assert len(warnings) == 5


def test_a_picture_over_the_size_limit_is_left_out(pictures, tmp_path):
    assert references.MAX_SOURCE_BYTES == 40 * 1024 * 1024
    big = pictures / "huge.png"
    big.write_bytes(png_bytes(64, 64, (9, 9, 9, 255)))
    with open(big, "r+b") as f:
        f.truncate(41 * 1024 * 1024)  # sparse: nothing is written
    warnings: list[str] = []
    assert references.ingest([big], tmp_path / "out", warnings) == []
    assert warnings == ["The reference image huge.png is 41 MB (the limit is 40 MB), so it was left out."]


def test_a_heic_this_computer_cannot_read_is_a_warning_and_sips_is_the_fallback(pictures, tmp_path, monkeypatch):
    heic = pictures / "snap.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 64)
    out = tmp_path / "out"
    monkeypatch.setattr(references.shutil, "which", lambda name: None)  # no sips (not a Mac)
    warnings: list[str] = []
    assert references.ingest([heic], out, warnings) == []
    assert warnings == ["The reference image snap.heic is a HEIC picture this computer can't read (save it as a JPEG or PNG), so it was left out."]

    # On a Mac, sips turns it into a JPEG and the same scaling and flattening follow.
    real = references._ffmpeg_jpeg
    monkeypatch.setattr(references.shutil, "which", lambda name: "/usr/bin/sips")
    monkeypatch.setattr(references, "_sips_jpeg", lambda src, dest: shutil.copyfile(make_image(pictures / "via-sips.jpg", "200x100"), dest) and None)
    monkeypatch.setattr(references, "_ffmpeg_jpeg", lambda src, dest: "failed" if src.suffix == ".heic" else real(src, dest))
    warnings = []
    (name,) = references.ingest([heic], out, warnings)
    assert warnings == [] and probe(out / name)[1:] == (200, 100)


def test_identical_content_is_stored_once_and_its_file_is_reused(pictures, tmp_path):
    a = pictures / "a.png"
    a.write_bytes(png_bytes(32, 32, (5, 6, 7, 255)))
    twin = pictures / "twin.png"
    shutil.copyfile(a, twin)
    out = tmp_path / "out"
    names = references.ingest([a, twin], out, [])
    assert len(names) == 1  # the same picture twice is one reference
    stored = out / names[0]
    stamp = stored.stat().st_mtime_ns
    time.sleep(0.02)
    assert references.ingest([a], out, []) == names  # a later run: the same name...
    assert stored.stat().st_mtime_ns == stamp  # ...and the file is not written again
    assert sorted(p.name for p in stored.parent.iterdir()) == [stored.name]  # no temp files left behind


def test_a_name_with_a_percent_sign_is_not_read_as_a_numbered_sequence(pictures, tmp_path):
    path = pictures / "look%03d.png"
    path.write_bytes(png_bytes(24, 24, (1, 1, 1, 255)))
    warnings: list[str] = []
    assert len(references.ingest([path], tmp_path / "out", warnings)) == 1 and warnings == []


def test_a_disk_that_stops_answering_is_a_warning_not_a_hang(pictures, tmp_path, monkeypatch):
    path = pictures / "look.png"
    path.write_bytes(png_bytes(16, 16, (1, 1, 1, 255)))
    monkeypatch.setattr(references, "STAT_SECONDS", 0.2)
    monkeypatch.setattr(references, "_head", lambda p: time.sleep(5))
    t0 = time.monotonic()
    warnings: list[str] = []
    assert references.ingest([path], tmp_path / "out", warnings) == []
    assert time.monotonic() - t0 < 3 and "look.png" in warnings[0] and "didn't open" in warnings[0]


def test_a_converter_that_hangs_is_given_up_on(pictures, tmp_path, monkeypatch):
    path = pictures / "look.png"
    path.write_bytes(png_bytes(16, 16, (1, 1, 1, 255)))
    monkeypatch.setattr(references, "CONVERT_SECONDS", 0.2)
    monkeypatch.setattr(references, "KILL_GRACE", 0.2)
    monkeypatch.setattr(references, "run_text", lambda cmd, **kw: time.sleep(30))  # ignores its own timeout, as a hung read on a share can
    t0 = time.monotonic()
    warnings: list[str] = []
    assert references.ingest([path], tmp_path / "out", warnings) == []
    assert time.monotonic() - t0 < 5 and warnings == ["The reference image look.png took longer than 0.2 s to convert, so it was left out."]


def test_saved_names_are_checked_before_they_are_read(tmp_path):
    good = "graphics/references/R01-abcdef.jpg"
    assert references.stored_names([good, good, "../etc/passwd", "/abs/R01-abcdef.jpg", 5, None, "graphics/references/R1-abc.jpg"]) == [good]
    assert references.stored_names("graphics/references/R01-abcdef.jpg") == [] and references.stored_names(None) == []
    folder = tmp_path / "graphics" / "references"
    folder.mkdir(parents=True)
    (folder / "R01-abcdef.jpg").write_bytes(b"\xff\xd8\xff jpeg")
    warnings: list[str] = []
    refs = references.load(tmp_path, [good, "graphics/references/R02-123456.jpg"], warnings)
    assert refs.names == [good] and len(refs) == 1 and bool(refs)
    assert warnings == ["The reference image R02-123456.jpg is missing from graphics/references, so the designer didn't see it."]
    assert not references.load(tmp_path, [], [])


# ------------------------------------------------------------------ the design request


def six_graphics():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S003", "V01.S004", "V01.S005"], "broll": []}], "graphics": []}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", format_override="640x360")
    placed = [
        PlacedGraphic(id=f"G{n + 1:02d}", spec=GraphicSpec(kind="stat", segment="V01.S003", words="", text=f"Graphic {n + 1}"), index=n, start=n * 40, frames=30)
        for n in range(6)
    ]
    return a, tl, placed


def reference_set(tmp_path, n: int = 2) -> references.ReferenceSet:
    folder = tmp_path / "pics"
    folder.mkdir(parents=True, exist_ok=True)
    for k in range(n):
        (folder / f"p{k}.png").write_bytes(png_bytes(20 + k, 20, (k * 60, 20, 20, 255)))
    out = tmp_path / "ref-out"
    return references.load(out, references.ingest(sorted(folder.glob("p*.png")), out, []), [])


def test_every_design_request_carries_the_pictures_before_its_text(tmp_path):
    a, tl, placed = six_graphics()
    refs = reference_set(tmp_path)
    provider = FakeProvider()
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[], references=refs)
    calls = [c for c in provider.calls if c["schema_name"] == "graphics"]
    assert len(calls) == 2  # two batches: the one that sets the look, and the rest
    for call in calls:
        parts = call["parts"]
        assert isinstance(parts[0], TextPart) and parts[0].text == refs.intro()
        assert [type(p) for p in parts[1:3]] == [ImagePart, ImagePart]
        assert [p.data for p in parts[1:3]] == refs.images and all(p.media_type == "image/jpeg" for p in parts[1:3])
        assert isinstance(parts[-1], TextPart) and parts[-1].text.startswith("<canvas>")  # the request itself comes last
    assert refs.sent is True
    intro = refs.intro()
    for phrase in (
        "The editor attached 2 reference images showing the look they want for the graphics.",
        "Match their typography, colour, layering, spacing, shape language and the energy of motion they suggest.",
        "Do not copy their words, logos, brand marks or photographs.",
        "This video's own words, name spellings and brand (the preset's palette, fonts and logo) win where they conflict.",
    ):
        assert phrase in intro
    assert "1 reference image " in reference_set(tmp_path / "one", 1).intro()


def test_without_references_a_design_request_is_what_it_always_was(tmp_path):
    a, tl, placed = six_graphics()
    provider = FakeProvider()
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[])
    for call in provider.calls:
        assert not any("reference image" in p.text for p in call["parts"] if isinstance(p, TextPart))
        assert "<editor_direction>" not in call["parts"][-1].text
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=[], references=references.ReferenceSet())
    assert not any("reference image" in p.text for c in provider.calls for p in c["parts"] if isinstance(p, TextPart))


class CannotSee(FakeProvider):
    supports_vision = False


def test_a_model_that_cannot_look_at_images_is_told_once_and_sent_none(tmp_path):
    a, tl, placed = six_graphics()
    refs = reference_set(tmp_path)
    provider = CannotSee()
    warnings: list[str] = []
    graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings, references=refs)
    assert warnings == ["fake-model can't look at images, so the reference images were left out"]  # once, though there were two batches
    assert len([c for c in provider.calls if c["schema_name"] == "graphics"]) == 2
    assert not any(isinstance(p, ImagePart) for c in provider.calls for p in c["parts"])
    assert refs.sent is False and refs.warned is True


class TooOldToSee(CannotSee):
    """A Claude Code older than the picture boundary needs (see providers.claude_code.MIN_PICTURE_VERSION)."""

    def picture_problem(self):
        return "Claude Code 2.1.100 is older than 2.1.259. Run `claude update`."


def test_an_old_claude_code_says_why_the_reference_images_were_left_out(tmp_path):
    a, tl, placed = six_graphics()
    refs = reference_set(tmp_path)
    warnings: list[str] = []
    graphics.design(TooOldToSee(), placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings, references=refs)
    assert warnings == ["Claude Code 2.1.100 is older than 2.1.259. Run `claude update`. The reference images were left out."]


class FailsWithPictures(FakeProvider):
    """A model whose design requests fail with `error` while they carry the reference pictures: the first `times` of them
    (None: every one); a request without them is answered."""

    def __init__(self, error: Exception, times: int | None = None):
        super().__init__()
        self.error, self.left = error, times

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        pictured = any(getattr(p, "text", "").startswith("The editor attached") for p in parts)
        if schema_name == "graphics" and pictured and self.left != 0:
            self.calls.append({"system": system, "parts": parts, "schema_name": schema_name, "purpose": kw.get("purpose")})
            if self.left is not None:
                self.left -= 1
            raise self.error
        return super().complete_json(system, parts, schema, schema_name=schema_name, **kw)


def pictured(provider) -> list[bool]:
    """For each design request the provider got: did it carry the reference pictures?"""
    return [any(getattr(p, "text", "").startswith("The editor attached") for p in c["parts"]) for c in provider.calls if c["schema_name"] == "graphics"]


def design_with(provider, refs, tmp_path) -> tuple[dict, list[str]]:
    a, tl, placed = six_graphics()
    warnings: list[str] = []
    html = graphics.design(provider, placed, a, tl.frame_duration, brief="", style="", width=640, height=360, warnings=warnings, references=refs)
    return html, warnings


@pytest.mark.parametrize("error", ["overloaded", "Anthropic API error 529: overloaded_error", "Could not reach the Anthropic API: timed out", "Anthropic API error 500: internal"])
def test_a_request_that_fails_for_a_reason_other_than_the_pictures_is_sent_again_with_them(error, tmp_path):
    refs = reference_set(tmp_path)
    provider = FailsWithPictures(ProviderError(error), times=1)
    html, warnings = design_with(provider, refs, tmp_path)
    assert len(html) == 6  # every graphic was designed
    assert pictured(provider) == [True, True, True]  # the first batch twice, then the other batch: never without them
    assert refs.sent is True and refs.dropped is False
    assert warnings == []  # nothing was lost, so nothing to say


def test_a_request_that_keeps_failing_with_the_pictures_is_sent_without_them_and_says_the_request_failed(tmp_path):
    refs = reference_set(tmp_path)
    provider = FailsWithPictures(ProviderError("Anthropic API error 529: overloaded_error"))
    html, warnings = design_with(provider, refs, tmp_path)
    assert len(html) == 6
    assert pictured(provider) == [True, True, False, True, True, False]  # each batch: with, with again, then without
    assert refs.dropped is True and refs.sent is False
    (warning,) = warnings
    assert warning.startswith("The design request for G01-G04 (Anthropic API error 529: overloaded_error); G05-G06 (Anthropic API error 529: overloaded_error) failed twice with the reference images")
    assert "turned down" not in warning  # it isn't said to be the pictures' fault
    assert "sent again without them: those graphics were designed without seeing the look you attached" in warning


@pytest.mark.parametrize("error", ["the image could not be processed", "messages.0.content.1.image.source.base64: Image does not match the provided media type", "Unsupported picture format"])
def test_a_request_the_model_turns_down_for_the_pictures_is_not_sent_with_them_again(error, tmp_path):
    refs = reference_set(tmp_path)
    provider = FailsWithPictures(ProviderError(error))
    html, warnings = design_with(provider, refs, tmp_path)
    assert len(html) == 6
    assert pictured(provider) == [True, False, True, False]  # an answer about the pictures is not asked for twice
    assert refs.dropped is True
    (warning,) = warnings
    assert warning.startswith("The model turned down the design request for G01-G04 (") and "with the reference images" in warning


@pytest.mark.parametrize("error", [ProviderAuthError("The API key was rejected."), ProviderLimitError("You've hit your usage limit.")])
def test_a_rejected_key_or_a_used_up_plan_is_never_sent_again(error, tmp_path):
    refs = reference_set(tmp_path)
    provider = FailsWithPictures(error)
    if isinstance(error, ProviderAuthError):
        with pytest.raises(ProviderAuthError):
            design_with(provider, refs, tmp_path)
        assert pictured(provider) == [True]
    else:
        html, warnings = design_with(pipeline.StopAtLimit(provider), refs, tmp_path)  # as a run asks: after the limit, nothing more is sent
        assert html == {} and pictured(provider) == [True]
        assert any("graphics design failed" in w and "usage limit" in w for w in warnings)
    assert refs.dropped is False  # and no request went without the pictures


def test_a_revisions_notes_reach_the_designer_as_the_editors_direction():
    a, tl, placed = six_graphics()
    request = graphics._design_request(placed[:1], a, tl.frame_duration, brief="", style="", width=640, height=360, editor_notes="  Make the lower thirds bolder and orange.\nAnd cut the intro.  ")
    assert "<editor_direction>" in request and "Make the lower thirds bolder and orange.\nAnd cut the intro." in request
    assert "ignore the rest" in request
    assert request.index("<style_direction>") < request.index("<editor_direction>") < request.index("<graphics>")
    assert "<editor_direction>" not in graphics._design_request(placed[:1], a, tl.frame_duration, brief="", style="", width=640, height=360)


# ------------------------------------------------------------------ in a run


@pytest.fixture(scope="module")
def shared_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("refs-analysis-cache")


@pytest.fixture(scope="module")
def clear_mov(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("graphic") / "clear.mov"
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30000/1001:d=10", "-vf", "format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", str(path))
    return path


def fake_renders(monkeypatch, mov: Path) -> None:
    def render_one(job):
        shutil.copy(mov, job["out"])
        return {"id": job["id"], "error": None, "console": []}

    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "_render_one", render_one)


def graphics_plan(*specs) -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan["graphics"] = list(specs or (GRAPHIC, STAT))
    return plan


def opts_for(footage, out, cache, **kw) -> Options:
    return Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw)


def design_calls(provider) -> list[dict]:
    return [c for c in provider.calls if c["schema_name"] == "graphics"]


def make_pictures(folder: Path, n: int = 2) -> list[Path]:
    folder.mkdir(exist_ok=True)
    out = []
    for k in range(n):
        path = folder / f"mood{k}.png"
        path.write_bytes(png_bytes(64, 36, (30 + 90 * k, 80, 200 - 90 * k, 255)))
        out.append(path)
    return out


def test_a_build_stores_the_pictures_and_shows_them_to_the_designer_only(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    provider = FakeProvider(graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    fake_renders(monkeypatch, clear_mov)
    pics = make_pictures(tmp_path / "moodboard")
    out = tmp_path / "out"
    result = pipeline.build(opts_for(footage, out, shared_cache, graphics=True, graphics_refs=tuple(str(p) for p in pics)))

    stored = sorted((out / "graphics" / "references").glob("*.jpg"))
    assert [p.name[:3] for p in stored] == ["R01", "R02"]
    names = [f"graphics/references/{p.name}" for p in stored]
    plan_json = json.loads((out / "plan.json").read_text())
    assert plan_json["settings"]["graphics_refs"] == names  # the stored names, relative to the output folder

    # The designer saw both, before the request text.
    (design,) = design_calls(provider)
    assert design["parts"][0].text.startswith("The editor attached 2 reference images")
    assert [p.data for p in design["parts"][1:3]] == [p.read_bytes() for p in stored]

    # Nothing else did: not the planner, the saved files, the report beyond the names, or the log.
    for call in provider.calls:
        if call["schema_name"] != "graphics":
            assert not any(isinstance(p, ImagePart) and p.data in {s.read_bytes() for s in stored} for p in call["parts"])
            assert not any("reference image" in p.text for p in call["parts"] if isinstance(p, TextPart))
    saved_text = {name: (out / name).read_text() for name in ("plan.json", "graphics.json", "prompt.md", "analysis.json", "roughcut.log", "edit_report.md", "edit_report.html")}
    for name, text in saved_text.items():
        for pic in pics:
            assert str(pic) not in text and str(pic.parent) not in text, name
            assert pic.name not in text, name
        for s in stored:
            assert base64.b64encode(s.read_bytes()[:80]).decode()[:40] not in text, name
    assert "attached" not in saved_text["prompt.md"]
    report = saved_text["edit_report.md"]
    assert all(p.name in report for p in stored) and "graphics/references/" in report  # listed by stored file name
    assert json.loads((out / "graphics.json").read_text())["style"] == ""  # the style holds no picture text either

    assert result.summary["references"] == {"stored": 2, "used": 2, "unseen": False}
    assert "2 reference images shown to the graphics designer" in " ".join(summary_lines(result.summary))

    # The originals can go: a re-cut and the settings don't need them.
    for pic in pics:
        pic.unlink()
    again = pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    assert json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"] == names
    assert [c.kind for c in again.timeline.connected if c.kind == "graphic"] == ["graphic", "graphic"]


def test_a_build_without_pictures_asks_for_nothing_new(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    provider = FakeProvider(graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    fake_renders(monkeypatch, clear_mov)
    out = tmp_path / "out"
    result = pipeline.build(opts_for(footage, out, shared_cache, graphics=True))
    assert not (out / "graphics" / "references").exists()
    assert json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"] == []
    (design,) = design_calls(provider)
    texts = [p.text for p in design["parts"] if isinstance(p, TextPart)]
    assert not any("reference image" in t for t in texts) and "<editor_direction>" not in texts[-1]
    # Only the frames of the shots under the graphics, each introduced by what it sits over.
    assert [type(p) for p in design["parts"]] == [TextPart, ImagePart, TextPart, ImagePart, TextPart]
    assert result.summary["references"] is None and "reference image" not in " ".join(summary_lines(result.summary))
    report = (out / "edit_report.md").read_text()
    assert "Reference images" not in report and "graphics/references" not in report


def test_pictures_for_a_cut_without_graphics_are_left_out_with_a_warning(footage, tmp_path, monkeypatch, shared_cache):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    pic = make_pictures(tmp_path / "moodboard", 1)[0]
    out = tmp_path / "out"
    result = pipeline.build(opts_for(footage, out, shared_cache, graphics_refs=(str(pic),)))
    assert "Reference images are only used by motion graphics, which are off for this cut, so they were left out." in result.warnings
    assert not (out / "graphics" / "references").exists()
    assert json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"] == []


def test_a_bad_picture_is_a_warning_and_the_build_finishes(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    provider = FakeProvider(graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    fake_renders(monkeypatch, clear_mov)
    good = make_pictures(tmp_path / "moodboard", 1)[0]
    bad = tmp_path / "moodboard" / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff garbage")
    out = tmp_path / "out"
    result = pipeline.build(opts_for(footage, out, shared_cache, graphics=True, graphics_refs=(str(bad), str(good))))
    assert any(w.startswith("The reference image broken.jpg ") for w in result.warnings)
    assert len(json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"]) == 1
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2


def test_a_picture_left_out_is_said_at_the_end_of_the_run_and_at_the_top_of_the_paper_edit(footage, tmp_path, monkeypatch, shared_cache, clear_mov, capsys):
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider(graphics_plan()))
    fake_renders(monkeypatch, clear_mov)
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n nope")
    out = tmp_path / "out"
    argv = ["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(shared_cache), "--graphics", "--graphics-ref", str(bad)]
    assert main(argv) == 0
    assert "! The reference image broken.png couldn't be read as a picture, so it was left out." in capsys.readouterr().out
    assert "> The reference image broken.png couldn't be read as a picture, so it was left out." in (out / "edit_report.md").read_text()


def test_a_model_that_cannot_look_builds_without_the_pictures_and_says_so(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    provider = CannotSee(graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    fake_renders(monkeypatch, clear_mov)
    pic = make_pictures(tmp_path / "moodboard", 1)[0]
    result = pipeline.build(opts_for(footage, tmp_path / "out", shared_cache, graphics=True, graphics_refs=(str(pic),), vision=False))
    assert result.warnings.count("fake-model can't look at images, so the reference images were left out") == 1
    assert not any(isinstance(p, ImagePart) for c in provider.calls for p in c["parts"])
    assert result.summary["references"] == {"stored": 1, "used": 0, "unseen": True}
    assert any("can't look at images" in line for line in summary_lines(result.summary))


class LeavesOutG01(FakeProvider):
    """A design reply without G01: a batch that failed, or a plan that ran out part-way."""

    def complete_json(self, system, parts, schema, *, schema_name, **kw):
        reply = super().complete_json(system, parts, schema, schema_name=schema_name, **kw)
        if schema_name == "graphics":
            reply = {**reply, "graphics": [g for g in reply["graphics"] if g["id"] != "G01"]}
        return reply


def test_design_missing_and_a_later_revision_use_the_saved_pictures(footage, tmp_path, monkeypatch, shared_cache, clear_mov):
    plan = graphics_plan()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: LeavesOutG01(plan))
    fake_renders(monkeypatch, clear_mov)
    pics = make_pictures(tmp_path / "moodboard")
    out = tmp_path / "out"
    pipeline.build(opts_for(footage, out, shared_cache, graphics=True, graphics_refs=tuple(str(p) for p in pics)))
    for pic in pics:
        pic.unlink()  # the originals are gone
    stored = [(out / n).read_bytes() for n in json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"]]
    assert len(stored) == 2

    designer = FakeProvider(plan)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: designer)
    pipeline.rerender(out, Options(inputs=[]), design_missing=True)
    (call,) = design_calls(designer)
    assert call["parts"][0].text.startswith("The editor attached 2 reference images") and [p.data for p in call["parts"][1:3]] == stored
    designed = call["parts"][-1].text.split("<graphics>")[-1]
    assert "Weeks" in designed and "Heading Out" not in designed  # only the graphic that had no design (G01, the cold open's)

    # A revision that changes one graphic's text designs that one, in the look of the others, with the pictures again.
    changed = json.loads(json.dumps(plan))
    changed["graphics"][1]["text"] = "Three Weeks"
    designer.plan = changed
    before = len(design_calls(designer))
    result = pipeline.revise(out, "Say three weeks.", Options(inputs=[]))
    (call,) = design_calls(designer)[before:]
    assert [p.data for p in call["parts"][1:3]] == stored
    assert "Three Weeks" in call["parts"][-1].text and "Heading Out" not in call["parts"][-1].text.split("<graphics>")[-1]
    assert len(json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"]) == 2  # still the same two
    assert sum(1 for c in result.timeline.connected if c.kind == "graphic") == 2


def test_the_command_line_takes_graphics_ref_for_build_and_revise(footage, tmp_path, monkeypatch, shared_cache, clear_mov, capsys):
    provider = FakeProvider(graphics_plan())
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    fake_renders(monkeypatch, clear_mov)
    pics = make_pictures(tmp_path / "moodboard")
    out = tmp_path / "out"
    argv = ["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(shared_cache), "--graphics"]
    assert main(argv + ["--graphics-ref", str(pics[0]), "--graphics-ref", str(pics[1])]) == 0
    printed = capsys.readouterr().out
    assert "2 reference images shown to the graphics designer" in printed
    assert len(json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"]) == 2

    # Revise with one new picture: it replaces the run's pictures, and designs every graphic again.
    new = make_pictures(tmp_path / "newboard", 3)[2]
    calls = len(design_calls(provider))
    assert main(["revise", str(out), "Like this.", "--graphics-ref", str(new)]) == 0
    refs = json.loads((out / "plan.json").read_text())["settings"]["graphics_refs"]
    assert len(refs) == 1
    (call,) = design_calls(provider)[calls:]
    assert call["parts"][0].text.startswith("The editor attached 1 reference image ")

    # Pictures alone are enough to revise.
    new2 = make_pictures(tmp_path / "board3", 1)[0]
    assert main(["revise", str(out), "--graphics-ref", str(new2)]) == 0

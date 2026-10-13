"""Music from Venture Studio: `roughcut studio music`, `--music studio:ID` and `--music-from`.

The catalog is a fake snapshot (test_studio's helpers); the songs are short generated files inside a fake
library folder. A network share that stops answering is played by a stat or probe that blocks.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from conftest import ffmpeg, make_clip
from test_studio import (  # noqa: F401 - fixtures
    NOW,
    _asset,
    _reset_progress,
    fake,
    library,
    only_the_cache_is_opened,
    untouched,
)

from roughcut import analyze, cli, pipeline, presets, studio
from roughcut.analyze import Analysis
from roughcut.cli import main
from roughcut.pipeline import Options
from roughcut.plan import normalize
from roughcut.prompts import library_line, render_media
from roughcut.revise import describe_changes
from roughcut.timeline import STYLES

FIXTURE = Path(__file__).parents[1] / "mac" / "Tests" / "RoughcutKitTests" / "Fixtures" / "studio-music-sample.json"


@pytest.fixture(scope="session")
def audio(tmp_path_factory, footage) -> dict[str, Path]:
    """A 50 s song and a 3 s sting, generated once."""
    root = tmp_path_factory.mktemp("audio")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=50:sample_rate=48000", "-c:a", "aac", str(root / "song.m4a"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=3:sample_rate=48000", "-c:a", "aac", str(root / "sting.m4a"))
    return {"song": root / "song.m4a", "sting": root / "sting.m4a"}


LISTED = [
    _asset(1, "Music/Sunrise.m4a", kind="audio", duration=184.0, favorite=1, tags="upbeat, warm", provider="Vendor A", license="Standard"),
    _asset(2, "Titles/Glitch/Glitch Hit Music Loop.aiff", kind="audio", package_id=9, template_resource=1),  # part of a Motion template
    _asset(3, "How to use/Readme Tone.wav", kind="audio", browse_visible=0),  # setup files Studio hides
    _asset(4, "Music/Gone.wav", kind="audio", missing=1),
    _asset(5, "Music/Lossless.flac", kind="audio"),  # Studio's audio, but not a type Roughcut reads
    _asset(6, "Sound Effects/whoosh_soft_01.wav", kind="audio", duration=1.2),
    _asset(7, "Music/Big Impact Anthem.wav", kind="audio", duration=184.0),  # a known length beats the name's words
    _asset(8, "Library/Calm Piano Music.wav", kind="audio"),
    _asset(9, "Footage/clip.mov", kind="video"),
    _asset(10, "Library/Impact Hit.wav", kind="audio"),
    _asset(11, "Library/Café Ambience.wav", kind="audio", tags="room tone"),
    _asset(12, "Library/Twin.wav", kind="audio", display_name="Twin, 50% off"),
]
FTS = "CREATE VIRTUAL TABLE assets_fts USING fts5(name, tags); INSERT INTO assets_fts(rowid, name, tags) SELECT id, name, tags FROM assets;"


def test_the_listing_is_studios_standalone_audio_with_a_guess(fake, library):
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=LISTED, collections={"Promo Music": [1, 2, 7, 8], "Calm": [8]})
    with untouched(fake.dir, library):
        listing = studio.music_listing()
        promo = studio.music_listing(collection="promo music")
        favs = studio.music_listing(favorites=True)
    rows = {r["id"]: r for r in listing.rows}
    assert set(rows) == {1, 5, 6, 7, 8, 10, 11, 12}  # never template parts, hidden setup files, missing files or video
    assert [r["id"] for r in promo.rows] == [7, 8, 1] and [r["id"] for r in favs.rows] == [1]
    assert rows[1]["length"] == "3:04" and rows[8]["length"] == "?" and rows[1]["tags"] == ["upbeat", "warm"] and rows[1]["favorite"]
    assert (rows[1]["provider"], rows[1]["license"], rows[1]["path"]) == ("Vendor A", "Standard", f"{library}/Music/Sunrise.m4a")
    assert {i: rows[i]["guess"] for i in (1, 6, 7, 8, 10, 11)} == {1: "music", 6: "sfx", 7: "music", 8: "music", 10: "sfx", 11: "unknown"}
    assert not rows[5]["usable"] and ".flac" in rows[5]["why_not"] and all(rows[i]["usable"] for i in rows if i != 5)
    assert rows[8]["collections"] == ["Calm", "Promo Music"] and rows[6]["collections"] == []
    assert "catalog snapshot from" in listing.source and "1 h old" in listing.source


def test_a_known_length_beats_the_name():
    assert studio.music_guess("Big Impact Anthem", 184) == "music"
    assert studio.music_guess("Big Impact Anthem") == "unknown"  # both kinds of word, no length
    assert studio.music_guess("Riser Song", 3) == "sfx"
    assert studio.music_guess("whoosh") == "sfx" and studio.music_guess("Calm theme") == "music" and studio.music_guess("Track 01", 20) == "music"


def test_search_uses_studios_index_and_otherwise_escapes_wildcards(fake, library):
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=LISTED, after=FTS)
    with untouched(fake.dir):
        assert [r["id"] for r in studio.music_listing(["cafe"]).rows] == [11]  # the index folds accents, as Studio's does
        assert [r["id"] for r in studio.music_listing(["sun", "warm"]).rows] == [1]  # every word, each as a word start
        assert studio.music_listing(["%"]).rows == [] and studio.music_listing(["_"]).rows == []
    fake.snapshot("1700000900-1-1", NOW - 60, assets=LISTED)  # no index: a substring match, wildcards escaped
    assert [r["id"] for r in studio.music_listing(["piano"]).rows] == [8]
    assert [r["id"] for r in studio.music_listing(["50"]).rows] == [12]
    assert studio.music_listing(["%"]).rows == []
    assert studio._like("50%_off\\") == "%50\\%\\_off\\\\%"


def test_the_json_listing_is_an_array_and_the_text_says_how_old(fake, library, capsys):
    fake.snapshot("1700000500-1-1", NOW - 3 * 3600, assets=LISTED)
    assert main(["studio", "music", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and rows and all(isinstance(r, dict) and "event" not in r for r in rows)
    assert set(rows[0]) == {"id", "name", "file", "path", "library", "seconds", "length", "provider", "license", "favorite", "tags", "notes",
                            "collections", "guess", "usable", "why_not", "as_of"}
    assert main(["studio", "music", "piano"]) == 0
    text = capsys.readouterr().out
    assert "catalog snapshot from" in text and "3 h old" in text and "Back up catalog now" in text
    assert "Calm Piano Music" in text and "--music studio:8" in text


def test_the_listing_without_studio_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    assert main(["studio", "music", "--json"]) == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "not installed" in printed.err


def test_a_kit_file_lists_its_audio(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    kit = tmp_path / "venture-project-promo.json"
    kit.write_text(json.dumps({"format_version": 1, "kind": studio.KIT_KIND, "exported_at": NOW, "project": {"name": "Promo kit", "assets": [
        {"asset_id": 1, "name": "Sunrise", "kind": "audio", "extension": "m4a", "original_reference": "/Volumes/Media/Studio Library/Music/Sunrise.m4a"},
        {"asset_id": 2, "name": "Logo sting", "kind": "video", "extension": "mov", "original_reference": "/Volumes/Media/Studio Library/Logo.mov"},
    ]}}))
    assert main(["studio", "music", "--kit", str(kit), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["id"], r["length"], r["usable"]) for r in rows] == [(1, "?", True)] and "kit 'Promo kit'" in rows[0]["as_of"]


# ---------------------------------------------------------------- the app's decoder fixture


def _sample_rows(fake) -> list[dict]:
    fake.snapshot("1700000500-1-1", NOW - 3600, libraries=[("/Volumes/Media/Studio Library", "Studio Library")], assets=LISTED[:2] + LISTED[4:8],
                  collections={"Promo Music": [1, 7]})
    return [{**r, "as_of": "the catalog snapshot from 12 Oct 21:04 (14 h old)"} for r in studio.music_listing().rows]


def test_swift_fixture_matches_the_listing(fake, monkeypatch):
    """mac/Tests/.../studio-music-sample.json is what `roughcut studio music --json` prints; the app's tests decode it.
    ROUGHCUT_WRITE_FIXTURES=1 rewrites it from here when the listing changes on purpose."""
    live = _sample_rows(fake)
    if os.environ.get("ROUGHCUT_WRITE_FIXTURES"):
        FIXTURE.write_text(json.dumps(live, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    sample = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(sample, list) and sample
    assert [set(r) for r in sample] == [set(r) for r in live]
    assert [{k: type(v).__name__ for k, v in r.items()} for r in sample] == [{k: type(v).__name__ for k, v in r.items()} for r in live]
    assert any(r["seconds"] is None for r in sample) and any(not r["usable"] for r in sample) and any(r["favorite"] for r in sample)
    assert any(r["collections"] for r in sample)


def test_the_apps_music_bed_takes_the_audio_types_roughcut_reads():
    """MusicFiles.extensions in the app is media.AUDIO_EXTS."""
    import re

    from roughcut.media import AUDIO_EXTS

    swift = (Path(__file__).parents[1] / "mac" / "Sources" / "RoughcutKit" / "StudioMusic.swift").read_text()
    listed = re.search(r"static let extensions: \[String\] = \[([^\]]*)\]", swift).group(1)
    assert {"." + e for e in re.findall(r'"(\w+)"', listed)} == AUDIO_EXTS


# ---------------------------------------------------------------- --music studio:ID


@pytest.fixture
def songs(library, fake, audio) -> dict[str, Path]:
    music = library / "Music"
    music.mkdir()
    files = {
        "sunrise": music / "Sunrise.m4a", "ballad": music / "Old Ballad.m4a", "sting": music / "Short Sting.m4a",
        "whoosh": library / "Sound Effects" / "whoosh_soft_01.m4a",
    }
    for key, path in files.items():
        shutil.copy2(audio["sting" if key in ("sting", "whoosh") else "song"], path)
    old = time.time() - 30 * 86400
    os.utime(files["ballad"], (old, old))  # the oldest file: it must not win just for being older
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[
        _asset(1, "Music/Sunrise.m4a", kind="audio", duration=50.0, favorite=1, provider="Vendor A", license="Standard", tags="warm"),
        _asset(2, "Music/Old Ballad.m4a", kind="audio", tags="upbeat, energetic"),  # length unknown: measured
        _asset(3, "Music/Short Sting.m4a", kind="audio"),  # measured: too short for a song
        _asset(4, "Sound Effects/whoosh_soft_01.m4a", kind="audio"),  # sounds like an effect: never measured
        _asset(5, "Titles/Glitch/Loop.m4a", kind="audio", package_id=9, template_resource=1),
        _asset(6, "Music/Not Here.m4a", kind="audio"),
    ], collections={"Promo Music": [1, 2, 3, 4, 5, 6], "Twin": [1], "Effects": [3, 4]}, kits={"Twin": [2]})
    return files


def _build(footage, out, *more, cache=None) -> list[str]:
    return ["build", str(footage / "talk.mov"), "-o", str(out), "--provider", "none", "--transcriber", "none",
            "--cache-dir", str(cache or out.parent / "cache"), *more]


def test_a_studio_song_by_id(footage, songs, fake, tmp_path, capsys):
    out = tmp_path / "out"
    with untouched(fake.dir):
        assert main(_build(footage, out, "--music", "studio:1")) == 0
    analysis = json.loads((out / "analysis.json").read_text())
    song = next(c for c in analysis["clips"] if c.get("music_bed"))
    assert song["media"]["path"] == str(songs["sunrise"]) and song["library"]["studio_id"] == 1 and song["library"]["provider"] == "Vendor A"
    assert song["music_order"] == 0
    assert "Venture Studio song: a favorite; tags: warm; from Vendor A." in (out / "prompt.md").read_text()
    fcpxml = next(out.glob("*.fcpxml")).read_text()
    assert songs["sunrise"].as_uri() in fcpxml
    report = (out / "edit_report.md").read_text()
    assert "**Vendor A**, Standard license: Sunrise.m4a" in report  # the paper edit credits it


@pytest.mark.parametrize("ref, said", [
    ("studio:99", "no item 99"),
    ("studio:5", "part of a Motion template"),
    ("studio:6", "isn't there"),
])
def test_a_studio_song_that_cant_be_used_stops_before_the_output_folder(footage, songs, tmp_path, capsys, ref, said):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music", ref)) == 1
    err = capsys.readouterr().err
    assert said in err and f"--music {ref}" in err and not out.exists()


def test_a_studio_song_given_by_its_path_is_treated_as_one(footage, songs, fake, tmp_path):
    """The app adds Studio's songs as plain paths: the run still knows them (tags for the planner, deadlines)."""
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music", str(songs["ballad"]), "--music", str(footage / "music" / "music.m4a"))) == 0
    clips = {Path(c["media"]["path"]).name: c for c in json.loads((out / "analysis.json").read_text())["clips"]}
    assert clips["Old Ballad.m4a"]["library"]["studio_id"] == 2 and clips["Old Ballad.m4a"]["music_order"] == 0
    assert "library" not in clips["music.m4a"] and clips["music.m4a"]["music_order"] == 1
    assert "Venture Studio song: tags: upbeat, energetic." in (out / "prompt.md").read_text()


def test_a_song_on_a_share_that_stopped_answering_stops_before_the_output_folder(footage, tmp_path, monkeypatch, capsys):
    song = footage / "music" / "music.m4a"
    release = threading.Event()
    real = Path.exists

    def hung(self, *a, **k):
        if self == song:
            release.wait(30)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "exists", hung)
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = main(_build(footage, out, "--music", str(song)))
    release.set()
    assert code == 1 and time.monotonic() - t0 < 5 and "didn't answer in 0.3 s" in capsys.readouterr().err and not out.exists()


def test_a_studio_song_without_studio_stops_too(footage, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music", "studio:1")) == 1
    assert "isn't installed" in capsys.readouterr().err and not out.exists()


def test_a_studio_song_on_a_share_that_stopped_answering_stops_within_the_deadline(footage, songs, tmp_path, monkeypatch, capsys):
    release = threading.Event()
    real = os.path.realpath

    def hung(path, *a, **k):
        if str(path).startswith(str(songs["sunrise"].parent)):
            release.wait(30)
        return real(path, *a, **k)

    monkeypatch.setattr(studio.os.path, "realpath", hung)
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = main(_build(footage, out, "--music", "studio:1"))
    release.set()
    assert code == 1 and time.monotonic() - t0 < 5 and "didn't answer in 0.3 s" in capsys.readouterr().err and not out.exists()


# ---------------------------------------------------------------- --music-from


def _music(out: Path) -> list[tuple[str, int | None]]:
    clips = json.loads((out / "analysis.json").read_text())["clips"]
    return sorted(((Path(c["media"]["path"]).name, c.get("music_order")) for c in clips if c.get("music_bed")), key=lambda x: x[1])


def test_music_from_ranks_and_typed_music_stays_first(footage, songs, tmp_path):
    out = tmp_path / "out"
    with untouched(songs["sunrise"].parent):
        assert main(_build(footage, out, "--music-from", "Promo Music", "-c", "An upbeat, energetic promo",
                           "--music", str(footage / "music" / "music.m4a"))) == 0
    # Typed first; then the favorite, then the song whose tags the brief uses. The sting is too short, the whoosh
    # sounds like an effect, the template part is never offered, and the missing file is left out.
    assert _music(out) == [("music.m4a", 0), ("Sunrise.m4a", 1), ("Old Ballad.m4a", 2)]
    report = (out / "edit_report.md").read_text()
    assert "Music: music.m4a (--music) runs under the whole cut" in report  # no cue: the typed song plays


def test_without_typed_music_the_top_song_plays_not_the_oldest(footage, songs, tmp_path):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music-from", "collection:Promo Music", "--music-max", "2")) == 0
    assert _music(out) == [("Sunrise.m4a", 0), ("Old Ballad.m4a", 1)]
    assert "Music: Sunrise.m4a (--music) runs under the whole cut" in (out / "edit_report.md").read_text()
    out1 = tmp_path / "out1"
    assert main(_build(footage, out1, "--music-from", "Promo Music", "--music-max", "1")) == 0
    assert _music(out1) == [("Sunrise.m4a", 0)]


@pytest.mark.parametrize("source, said", [
    ("Nope", "no collection or kit named 'Nope'"),
    ("Twin", "say collection:Twin or kit:Twin"),
])
def test_music_from_never_stops_a_build(footage, songs, tmp_path, source, said):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music-from", source)) == 0
    assert _music(out) == [] and said in (out / "edit_report.md").read_text()


def test_music_from_without_studio_or_its_files(footage, songs, tmp_path, monkeypatch):
    for path in songs.values():
        path.unlink()  # the share isn't connected
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music-from", "Promo Music")) == 0
    report = (out / "edit_report.md").read_text()
    assert _music(out) == [] and "no songs found" not in report  # the folder isn't there; the collection isn't empty
    assert "--music-from Promo Music: 3 of its songs couldn't be reached" in report and "network folder isn't connected" in report
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    out2 = tmp_path / "out2"
    assert main(_build(footage, out2, "--music-from", "Promo Music")) == 0
    assert "Venture Studio isn't installed on this Mac" in (out2 / "edit_report.md").read_text()


def test_music_from_a_stalled_share_gives_up_within_the_deadline(footage, songs, tmp_path, monkeypatch):
    release = threading.Event()
    real = os.path.realpath

    def hung(path, *a, **k):
        if str(path).startswith(str(songs["sunrise"].parent.parent)):
            release.wait(30)
        return real(path, *a, **k)

    monkeypatch.setattr(studio.os.path, "realpath", hung)
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = main(_build(footage, out, "--music-from", "Promo Music"))
    release.set()
    assert code == 0 and time.monotonic() - t0 < 20
    assert _music(out) == [] and "stopped adding Studio songs: its network folder didn't answer in 0.3 s" in (out / "edit_report.md").read_text()


def test_a_share_that_stalls_while_a_song_is_read_leaves_the_song_out(footage, songs, tmp_path, monkeypatch):
    release = threading.Event()
    real = analyze.ffmpeg_ops.window_peaks

    def hung(path, *a, **k):
        if Path(path) == songs["sunrise"]:
            release.wait(30)
        return real(path, *a, **k)

    monkeypatch.setattr(analyze.ffmpeg_ops, "window_peaks", hung)
    monkeypatch.setattr(analyze, "SHARE_MEASURE_SECONDS", 0.5)
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = main(_build(footage, out, "--music", "studio:1", cache=tmp_path / "fresh-cache"))
    release.set()
    assert code == 0 and time.monotonic() - t0 < 30
    assert _music(out) == [] and "Sunrise.m4a: left out: its network folder stopped answering" in (out / "edit_report.md").read_text()


def test_render_checks_a_studio_song_with_a_deadline(footage, songs, tmp_path, monkeypatch):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music", "studio:1")) == 0
    release = threading.Event()
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    real = Path.exists

    def hung(self, *a, **k):
        if self == songs["sunrise"]:
            release.wait(30)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "exists", hung)
    t0 = time.monotonic()
    result = pipeline.rerender(out, Options(inputs=[], cache_dir=tmp_path / "cache"))
    release.set()
    assert time.monotonic() - t0 < 20 and any("Sunrise.m4a (a Venture Studio song) couldn't be checked" in w for w in result.warnings)


# ---------------------------------------------------------------- the planner, revise and presets


def test_the_media_block_shows_what_studio_says_about_a_song():
    song = make_clip("A01", kind="audio", seconds=120)
    song.music_bed, song.music_order = True, 0
    song.library = {"studio_id": 1, "title": "Sunrise", "favorite": True, "tags": [f"tag{i}" for i in range(20)], "notes": "x" * 500,
                    "provider": "Vendor A", "license": "Standard"}
    line = library_line(song.library)
    assert line.startswith("Venture Studio song: a favorite; tags: tag0, ") and "tag11" in line and "tag12" not in line
    assert "x" * 159 + "…" in line and "from Vendor A" in line
    block = render_media(Analysis(clips=[song]), STYLES["medium"])
    assert line in block and render_media(Analysis(clips=[song]), STYLES["medium"]) == block


def test_revise_names_the_song_when_a_cue_moves_to_another():
    talk = make_clip("V01", sentences=[(1.0, 3.0, "Today we head up the mountain.")])
    a, b = make_clip("A01", kind="audio", seconds=120), make_clip("A02", kind="audio", seconds=120)
    a.music_bed, a.music_order, b.music_bed, b.music_order = True, 1, True, 0
    analysis = Analysis(clips=[talk, a, b])

    def plan(cue: str | None):
        raw = {"title": "t", "sections": [{"name": "One", "purpose": "", "items": ["V01.S001"], "broll": []}],
               "music": [{"clip": cue, "first_section": 0, "last_section": 0, "source_in": 0}] if cue else []}
        return normalize(raw, analysis, source="fake")

    changes = "\n".join(describe_changes(plan("A01"), plan("A02"), analysis, before_seconds=None, after_seconds=2.0, previous_version=1))
    assert "- Music: A01.wav → A02.wav" in changes and "No lines added or removed" not in changes
    # No cue: the first --music plays, so dropping the cue on A01 swaps the song too.
    assert "- Music: A01.wav → A02.wav" in "\n".join(describe_changes(plan("A01"), plan(None), analysis, before_seconds=None, after_seconds=2.0, previous_version=1))


def test_only_your_presets_name_a_studio_collection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    assert all(p.music_from is None for p in presets.BUILT_IN.values())  # CI builds --preset venture-oc without Studio
    assert not any(f.startswith("--music-from") for p in presets.BUILT_IN.values() for f in presets.flags(p, "build"))
    assert main(["preset", "save", "mine", "--music-from", "Promo Music"]) == 0
    mine = presets.load("mine")
    assert mine.music_from == "Promo Music" and "--music-from=Promo Music" in presets.flags(mine, "build")
    assert "--music-from=Promo Music" not in presets.flags(mine, "render")
    args, _ = cli._apply_preset(cli.build_parser().parse_args(["build", "x", "--preset", "mine", "--no-music-from"]),
                                ["build", "x", "--preset", "mine", "--no-music-from"])
    assert args.music_from == ""
    assert main(["preset", "save", "mine", "--no-music-from"]) == 0 and presets.load("mine").music_from is None


# ---------------------------------------------------------------- found by review

KIT = {"format_version": 1, "kind": studio.KIT_KIND, "exported_at": NOW, "project": {"name": "Promo kit", "assets": [
    {"asset_id": 1, "name": "Sunrise", "kind": "audio", "extension": "m4a", "original_reference": "/Volumes/Media/Studio Library/Music/Sunrise.m4a"},
]}}


def test_a_song_given_more_than_once_is_one_clip(footage, songs, tmp_path):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music", "studio:1", "--music", "studio:1", "--music", str(songs["sunrise"]),
                       "--music-from", "Promo Music", "--music-max", "2")) == 0
    assert [name for name, _ in _music(out)] == ["Sunrise.m4a", "Old Ballad.m4a"]  # --music-from skips the one already given
    fcpxml = next(out.glob("*.fcpxml")).read_text()
    assert fcpxml.count(songs["sunrise"].as_uri()) == 1
    assert "found 3 media files" in (out / "roughcut.log").read_text()  # the talk, and two songs
    out2 = tmp_path / "out2"  # an ordinary song given twice has always been one clip
    typed = footage / "music" / "music.m4a"
    assert main(_build(footage, out2, "--music", str(typed), "--music", str(typed))) == 0
    assert [name for name, _ in _music(out2)] == ["music.m4a"]


def test_music_from_with_nothing_to_offer_still_says_no_songs(footage, songs, tmp_path):
    out = tmp_path / "out"
    assert main(_build(footage, out, "--music-from", "collection:Effects")) == 0  # a sting too short, a sound effect by name
    assert _music(out) == [] and "no songs found" in (out / "edit_report.md").read_text()


def test_a_kit_file_on_a_stalled_share_does_not_hang_music_from(footage, songs, tmp_path, monkeypatch):
    kit = tmp_path / "promo-kit.json"
    kit.write_text(json.dumps(KIT))
    release = threading.Event()
    real_is_file, real_open = Path.is_file, os.open

    def hung_is_file(self, *a, **k):
        if self == kit:
            release.wait(30)
        return real_is_file(self, *a, **k)

    def hung_open(path, *a, **k):
        if str(path) == str(kit):
            release.wait(30)
        return real_open(path, *a, **k)

    monkeypatch.setattr(Path, "is_file", hung_is_file)
    monkeypatch.setattr(studio.os, "open", hung_open)
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    out = tmp_path / "out"
    t0 = time.monotonic()
    code = main(_build(footage, out, "--music-from", str(kit)))
    elapsed = time.monotonic() - t0
    assert code == 0 and elapsed < 20
    assert _music(out) == [] and "didn't answer in 0.3 s" in (out / "edit_report.md").read_text()
    t0 = time.monotonic()
    assert main(["studio", "music", "--kit", str(kit)]) == 1  # the listing gives up too
    assert time.monotonic() - t0 < 20
    release.set()


def test_a_kit_file_whose_open_stalls_is_given_up_on(tmp_path, monkeypatch):
    kit = tmp_path / "promo-kit.json"
    kit.write_text(json.dumps(KIT))
    release = threading.Event()
    real_open = os.open

    def hung_open(path, *a, **k):
        if str(path) == str(kit):
            release.wait(30)
        return real_open(path, *a, **k)

    monkeypatch.setattr(studio.os, "open", hung_open)
    monkeypatch.setattr(studio, "FILE_SECONDS", 0.3)
    with pytest.raises(studio.StudioUnavailable, match="promo-kit.json.*didn't answer in 0.3 s"):
        studio.load_kit(kit)
    release.set()
    assert studio.load_kit(kit).name == "Promo kit"  # the same file on a healthy share reads fine


def test_the_kit_listing_points_at_the_song_not_at_a_studio_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    kit = tmp_path / "venture-project-promo.json"
    kit.write_text(json.dumps(KIT))
    assert main(["studio", "music", "--kit", str(kit)]) == 0
    text = capsys.readouterr().out
    assert "studio:" not in text  # studio:ID needs the catalog snapshot, which isn't here
    assert "--music '/Volumes/Media/Studio Library/Music/Sunrise.m4a'" in text and f"--music-from {kit}" in text


def test_a_search_that_finds_many_items_binds_none_of_them(fake, library):
    if not hasattr(sqlite3.Connection, "setlimit"):
        pytest.skip("this Python can't lower SQLite's variable limit")
    many = [_asset(i, f"Library/file {i}.wav", kind="audio") for i in range(1, 301)]
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=many, after=FTS)
    copy = studio.newest_copy()
    with studio.catalog(copy) as con:
        con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
        found = studio.standalone_audio(con, words=["file"])
        assert len(found) == 300 and studio.standalone_audio(con, words=["file", "nothing"]) == []

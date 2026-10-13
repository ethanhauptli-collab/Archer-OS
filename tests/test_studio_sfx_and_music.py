"""Sound effects and music from the same Venture Studio, together: one build with `--sfx-from` and `--music-from`
from a fake Studio, its re-cut and revision, and what "what changed" says about sound effects.

The catalog is a fake snapshot (test_studio's helpers) with one collection of sounds and one of songs; the files
are short generated audio in a fake library folder. Studio's folder and the library are checked untouched.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg
from test_end_to_end import check_invariants, validate_dtd
from test_studio import (  # noqa: F401 - fixtures
    NOW,
    _asset,
    _reset_progress,
    fake,
    library,
    only_the_cache_is_opened,
    untouched,
)

from roughcut import pipeline, sfx, studio
from roughcut.cli import main
from roughcut.revise import accent_item, accents_before, recut_items, setting_deltas, sound_state

PLAN = dict(CANNED_PLAN, music=[], sfx="soft")  # no music cue: the first song given plays under the whole cut


@pytest.fixture
def catalog(fake, library) -> Path:
    """Real audio in the library where the snapshot says it is: sounds and a song, in two collections of the same Studio."""
    folder = library / "Sound Effects" / "Accents"
    folder.mkdir(parents=True)
    swell = "volume='exp(-((t-0.5)*(t-0.5))/0.02)':eval=frame"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=1:r=48000", "-af", swell, str(folder / "whoosh_soft_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=0.8:r=48000", "-af", swell, str(folder / "swoosh_fast_02.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.9:d=1.2:r=48000", "-af", "volume='exp(-4*t)':eval=frame", str(folder / "impact_hit_01.wav"))
    music = library / "Music"
    music.mkdir()
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=50:sample_rate=48000", "-c:a", "aac", str(music / "Sunrise.m4a"))
    rel = "Sound Effects/Accents/"
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[
        _asset(1, rel + "whoosh_soft_01.wav", kind="audio", category="audio", provider="Vendor A", license="Standard", duration=1.0),
        _asset(2, rel + "swoosh_fast_02.wav", kind="audio", category="audio", provider="Vendor B", license="Extended", duration=0.8),
        _asset(3, rel + "impact_hit_01.wav", kind="audio", category="audio", provider="Vendor B", license="Extended", duration=1.2),
        _asset(4, "Music/Sunrise.m4a", kind="audio", category="audio", provider="Vendor A", license="Standard", duration=50.0, favorite=1),
    ], collections={"Promo Sounds": [1, 2, 3], "Promo Music": [4]})
    return library


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider(PLAN)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: p)
    return p


def _args(footage, out, cache) -> list[str]:
    return ["--transcriber", "none", "--cache-dir", str(cache)]


def _project(root: ET.Element, label: str) -> ET.Element:
    return next(p for p in root.iter("project") if label in p.get("name"))


def _accents(root: ET.Element) -> list[ET.Element]:
    return [el for el in _project(root, "Rough Cut").iter("asset-clip") if el.get("audioRole") == "effects.SFX"]


def _src(root: ET.Element, el: ET.Element) -> str:
    asset = next(a for a in root.iter("asset") if a.get("id") == el.get("ref"))
    return asset.find("media-rep").get("src")


def _path(url: str) -> str:
    return unquote(urlparse(url).path)


def _studio_part(report: str) -> str:
    return report.split("## Venture Studio")[1].split("\n## ")[0]


def test_a_build_with_sounds_and_music_from_the_same_studio(footage, catalog, fake, provider, tmp_path, only_the_cache_is_opened):
    out, cache = tmp_path / "out", tmp_path / "cache"
    with untouched(fake.dir, catalog):
        assert main([
            "build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache),
            "--sfx-from", "Promo Sounds", "--music-from", "Promo Music", "--music-max", "1",
        ]) == 0
    assert all(p.parent.name == studio.CACHE_FOLDER for p in only_the_cache_is_opened)
    fcpxml = next(out.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    check_invariants(fcpxml)
    root = ET.parse(fcpxml).getroot()
    base = (catalog).as_uri() + "/"

    # The accents: on the effects role, Studio's own files in place, nothing resolved or copied.
    accent_clips = _accents(root)
    assert accent_clips and all(_src(root, el).startswith(base + "Sound%20Effects/Accents/") for el in accent_clips)
    assert not [el for el in _project(root, "Stringout").iter("asset-clip") if el.get("audioRole") == "effects.SFX"]
    # The music bed: the Studio song, under the whole cut, with the planner told what Studio says about it.
    songs = [el for el in _project(root, "Rough Cut").iter("asset-clip") if str(el.get("audioRole", "")).startswith("music")]
    assert songs and {_src(root, el) for el in songs} == {base + "Music/Sunrise.m4a"}
    assert "Venture Studio song: a favorite; from Vendor A." in (out / "prompt.md").read_text()
    analysis = json.loads((out / "analysis.json").read_text())
    assert not [c for c in analysis["clips"] if c["id"].startswith("X")], "sound clips are an in-memory copy only"
    assert next(c for c in analysis["clips"] if c.get("music_bed") and c.get("library"))["library"]["studio_id"] == 4

    # plan.json keeps what both features need to rebuild the cut without Studio.
    saved = json.loads((out / "plan.json").read_text())
    pool = saved["settings"]["sfx_pool"]
    assert sorted(x.name for x in sfx.Pool.from_json(pool).sounds) == ["impact_hit_01.wav", "swoosh_fast_02.wav", "whoosh_soft_01.wav"]
    assert saved["settings"]["sfx_from"] == ["Promo Sounds"] and saved["settings"]["sfx_amount"] == "soft" and saved["plan"]["sfx"] == "soft"
    sources = saved["studio_sources"]
    used = sorted({_path(_src(root, el)) for el in accent_clips + songs})
    assert all(sources["files"][u]["status"] == "matched" for u in used)

    # The paper edit's Venture Studio section counts and credits the sounds and the song alike.
    report = (out / "edit_report.md").read_text()
    section = _studio_part(report)
    project = _project(root, "Rough Cut").get("name")
    assert f'{len(used)} Studio items in "{project}"' in section and f"{len(used)} of its" in section
    credits = " ".join(line for line in section.splitlines() if "license:" in line)
    for u in used:
        assert Path(u).name in credits, u
    assert "**Vendor A**, Standard license: " in section and "Sunrise.m4a" in section
    sound = report.split("## Sound effects")[1].split("\n## ")[0]
    assert "Venture Studio" in sound and "Provider and license" in sound

    # A re-cut on a Mac without Studio: the same sounds from the saved pool, the Studio section from the saved check.
    gone = tmp_path / "nowhere"
    pre = {a.get("name") for a in accent_clips}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(studio.STUDIO_ENV, str(gone))
        assert main(["render", str(out)]) == 0
    v2 = ET.parse(next(out.glob("*v2.fcpxml"))).getroot()
    assert {a.get("name") for a in _accents(v2)} == pre
    again = (out / "edit_report.md").read_text()
    part = _studio_part(again)
    assert "from the check made earlier" in part and f'{len(used)} Studio items in "' in part and "**Vendor A**, Standard license: " in part
    resaved = json.loads((out / "plan.json").read_text())
    assert resaved["studio_sources"] == sources and resaved["settings"]["sfx_pool"] == pool
    # Credits are not a cut: with every setting the same, the list of changes says so and never mentions Studio.
    changes = json.loads((out / "result.json").read_text())["changes"]
    assert changes["unchanged"] is True and not [i for i in changes["items"] if "Studio" in i or "credit" in i.lower()]


@pytest.fixture
def slashed(catalog, fake) -> Path:
    """The same files, in collections with a slash in their names (Studio allows any characters)."""
    rel = "Sound Effects/Accents/"
    fake.snapshot("1700000600-1-1", NOW - 1800, assets=[
        _asset(1, rel + "whoosh_soft_01.wav", kind="audio", category="audio", provider="Vendor A", license="Standard", duration=1.0),
        _asset(2, rel + "swoosh_fast_02.wav", kind="audio", category="audio", provider="Vendor B", license="Extended", duration=0.8),
        _asset(3, rel + "impact_hit_01.wav", kind="audio", category="audio", provider="Vendor B", license="Extended", duration=1.2),
        _asset(4, "Music/Sunrise.m4a", kind="audio", category="audio", provider="Vendor A", license="Standard", duration=50.0, favorite=1),
    ], collections={"Hits/Impacts": [1, 2, 3], "Songs/Warm": [4]})
    return catalog


def test_a_name_is_a_kit_file_only_when_it_ends_in_json():
    """One rule for --sfx-from and --music-from: a slash in a collection's name doesn't make it a path."""
    for name in ("Hits/Impacts", "collection:Hits/Impacts", "kit:Promo/Kit", "/Volumes/Media/Sound Effects", "kits/promo-kit", "Promo Sounds"):
        assert not sfx.is_kit_file(name), name
    for name in ("promo-kit.json", "kits/promo.JSON", "/Users/you/Kits/promo kit.json", "~/promo.json", " promo.json "):
        assert sfx.is_kit_file(name), name
    assert not sfx.is_kit_file("collection:promo.json")


def test_a_collection_with_a_slash_in_its_name_works_unprefixed_for_sounds_and_songs(footage, slashed, fake, provider, tmp_path, capsys):
    out, cache = tmp_path / "out", tmp_path / "cache"
    base = ["build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache)]
    with untouched(fake.dir, slashed):
        assert main(base + ["--sfx-from", "Hits/Impacts", "--music-from", "Songs/Warm", "--music-max", "1"]) == 0
    saved = json.loads((out / "plan.json").read_text())
    assert saved["settings"]["sfx_from"] == ["Hits/Impacts"], "kept as typed, never turned into a path"
    assert sorted(s.name for s in sfx.Pool.from_json(saved["settings"]["sfx_pool"]).sounds) == ["impact_hit_01.wav", "swoosh_fast_02.wav", "whoosh_soft_01.wav"]
    root = ET.parse(next(out.glob("*.fcpxml"))).getroot()
    assert _accents(root)
    assert {_src(root, el) for el in _project(root, "Rough Cut").iter("asset-clip") if str(el.get("audioRole", "")).startswith("music")} == {
        slashed.as_uri() + "/Music/Sunrise.m4a"}

    # The spelling the Mac app and a preset use for a name that isn't typed in a build: the same name, a warning when it fails.
    again = tmp_path / "again"
    assert main(["build", str(footage), "-o", str(again), "--name", "footage", *_args(footage, again, cache), "--preset-sfx-from=Hits/Impacts"]) == 0
    assert len(json.loads((again / "plan.json").read_text())["settings"]["sfx_pool"]["sounds"]) == 3
    assert json.loads((again / "plan.json").read_text())["settings"]["sfx_from"] == ["Hits/Impacts"]

    # A re-cut and a render with the name keep it too, and the saved pool is replaced by that collection's.
    assert main(["render", str(out), "--sfx-from", "Hits/Impacts"]) == 0
    assert json.loads((out / "plan.json").read_text())["settings"]["sfx_from"] == ["Hits/Impacts"]

    # A saved preset with the name works the same way.
    from roughcut import presets

    assert main(["preset", "save", "slashes", "--brief", "Promo.", "--sfx-from", "Hits/Impacts"]) == 0
    assert presets.flags(presets.load("slashes"), "build").count("--preset-sfx-from=Hits/Impacts") == 1
    viapreset = tmp_path / "viapreset"
    assert main(["build", str(footage), "-o", str(viapreset), "--name", "footage", *_args(footage, viapreset, cache), "--preset", "slashes"]) == 0
    assert len(json.loads((viapreset / "plan.json").read_text())["settings"]["sfx_pool"]["sounds"]) == 3
    capsys.readouterr()


def test_a_name_with_a_slash_that_studio_lacks_says_what_it_can_take(footage, slashed, fake, provider, tmp_path, capsys):
    out, cache = tmp_path / "out", tmp_path / "cache"
    base = ["build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache)]
    for typo in ("Hits/Impact", "kits/promo-kit"):
        assert main(base + ["--sfx-from", typo]) == 1 and not out.exists(), "stopped before its folder was made"
        err = capsys.readouterr().err
        assert f"Venture Studio has no collection or kit named {typo!r}" in err
        assert "path ending in .json" in err, "a kit file is given by a path that says so"
        assert str(Path.cwd()) not in err, "not turned into a path nobody typed"
    # A folder of sounds is still told apart, whether or not Studio is there.
    folder = tmp_path / "Sounds"
    folder.mkdir()
    for studio_dir in (str(fake.dir), str(tmp_path / "nowhere")):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(studio.STUDIO_ENV, studio_dir)
            assert main(base + ["--sfx-from", str(folder)]) == 1 and not out.exists()
            assert "that's a folder" in capsys.readouterr().err
    # Not typed (a preset's): a warning, and the cut goes on.
    assert main(base + ["--preset-sfx-from", "Hits/Impact"]) == 0
    assert any("no collection or kit named 'Hits/Impact'" in w for w in json.loads((out / "result.json").read_text())["warnings"])


def _count(out: Path, version: int) -> int:
    return len(_accents(ET.parse(next(out.glob(f"*v{version}.fcpxml"))).getroot()))


def test_a_revision_keeps_what_both_features_saved_and_says_what_it_did_to_the_sounds(footage, catalog, fake, provider, tmp_path):
    out, cache = tmp_path / "out", tmp_path / "cache"
    assert main([
        "build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache),
        "--sfx-from", "Promo Sounds", "--music-from", "Promo Music", "--music-max", "1",
    ]) == 0
    first = json.loads((out / "plan.json").read_text())
    first_count = _count(out, 1)
    assert first_count >= 1
    provider.plan = dict(PLAN, sfx="full")
    assert main(["revise", str(out), "more sound design, whooshes on the B-roll"]) == 0
    second = json.loads((out / "plan.json").read_text())
    # Everything either feature saved is still there, and the revision's reading of the notes is in the plan.
    assert second["studio_sources"]["files"] and second["settings"]["sfx_pool"] == first["settings"]["sfx_pool"]
    for key in ("sfx", "sfx_from", "sfx_off"):
        assert second["settings"][key] == first["settings"][key]
    assert second["settings"]["sfx_amount"] == "full" and second["plan"]["sfx"] == "full" and second["revision_of"] == 1
    items = json.loads((out / "result.json").read_text())["changes"]["items"]
    second_count = _count(out, 2)
    assert items[0] == "Sound effects: full, as your notes ask (v1 had soft)." and "Sound effects: soft → full" not in items  # said once
    if second_count != first_count:
        assert f"Sound effects: {first_count} → {second_count} accent{'s' if second_count != 1 else ''}" in items
    report = (out / "edit_report.md").read_text()
    assert "Sound effects: full, as your notes ask (v1 had soft)." in report.split("## Revision notes")[1].split("\n## ")[0]
    assert "**Vendor A**, Standard license: " in _studio_part(report)

    # Losing them says how many went, and the song and the Studio section stay.
    provider.plan = dict(PLAN, sfx="none")
    assert main(["revise", str(out), "lose the whooshes"]) == 0
    third = json.loads((out / "plan.json").read_text())
    assert third["settings"]["sfx_amount"] == "none" and third["settings"]["sfx_pool"] == first["settings"]["sfx_pool"]
    assert third["studio_sources"]["files"]
    items = json.loads((out / "result.json").read_text())["changes"]["items"]
    assert items[0] == "Sound effects: none, as your notes ask (v2 had full)."
    assert f"Sound effects: {second_count} → 0 accents" in items and _count(out, 3) == 0
    section = _studio_part((out / "edit_report.md").read_text())
    assert "Sunrise.m4a" in section and "whoosh" not in section  # the song is still credited; no sound plays, none is credited

    # A re-cut after it keeps all of it too, and --sfx-from names the sounds to use from then on.
    assert main(["render", str(out), "--style", "tight", "--sfx-from", "Promo Sounds"]) == 0
    fourth = json.loads((out / "plan.json").read_text())
    assert fourth["studio_sources"]["files"] and fourth["settings"]["style"] == "tight"
    assert sfx.Pool.from_json(fourth["settings"]["sfx_pool"]).sounds
    items = json.loads((out / "result.json").read_text())["changes"]["items"]
    assert "Pacing: medium → tight" in items


def test_without_ai_the_same_studio_still_gives_the_sounds_and_the_song(footage, catalog, fake, tmp_path):
    """--provider none: a stringout-style cut gets at most the soft accents, the first Studio song, and the credits."""
    out, cache = tmp_path / "out", tmp_path / "cache"
    assert main([
        "build", str(footage / "talk.mov"), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(cache),
        "--sfx-from", "Promo Sounds", "--music-from", "Promo Music",
    ]) == 0
    fcpxml = next(out.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    check_invariants(fcpxml)
    report = (out / "edit_report.md").read_text()
    saved = json.loads((out / "plan.json").read_text())
    assert saved["settings"]["sfx_amount"] == "soft" and saved["settings"]["sfx_pool"]["sounds"]
    assert "Sunrise.m4a" in _studio_part(report) and "**Vendor A**, Standard license: " in _studio_part(report)
    assert "Music: Sunrise.m4a (--music) runs under the whole cut" in report


def test_sounds_picked_as_a_folder_in_the_library_are_credited_by_their_path(footage, catalog, fake, provider, tmp_path):
    """Studio's review matches paths, not how Roughcut found a file: a plain --sfx folder inside a library, and a song
    given by --music studio:ID, are counted and credited like the ones a collection named."""
    out, cache = tmp_path / "out", tmp_path / "cache"
    with untouched(fake.dir, catalog):
        assert main([
            "build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache),
            "--sfx", str(catalog / "Sound Effects" / "Accents"), "--music", "studio:4",
        ]) == 0
    root = ET.parse(next(out.glob("*.fcpxml"))).getroot()
    heard = {Path(_path(_src(root, el))).name for el in _accents(root)}
    assert heard
    section = _studio_part((out / "edit_report.md").read_text())
    n = len(heard) + 1
    assert f"{n} Studio items in " in section
    assert "Sunrise.m4a" in section and all(name in section for name in heard)
    assert ("Vendor B" in section) == bool(heard & {"swoosh_fast_02.wav", "impact_hit_01.wav"})  # a credit for each file that played
    # The sounds themselves carry no provider (they weren't named through Studio): the Sound effects table has no credits column.
    assert "Provider and license" not in (out / "edit_report.md").read_text().split("## Sound effects")[1].split("\n## ")[0]


# ---------------------------------------------------------------- what changed, in plain words


def _settings(**kw) -> dict:
    return {"sfx": ["/Volumes/Media/Sound Effects"], "sfx_from": [], "sfx_off": False, "sfx_amount": "soft", **kw}


def test_the_amount_the_sounds_and_the_count_are_said_in_plain_words():
    assert setting_deltas(_settings(), _settings(sfx_amount="full")) == ["Sound effects: soft → full"]
    assert setting_deltas(_settings(), _settings(sfx_amount="none")) == ["Sound effects: soft → none"]
    assert setting_deltas(_settings(), _settings(sfx_off=True)) == ["Sound effects: soft → off"]
    assert setting_deltas(_settings(sfx_off=True), _settings()) == ["Sound effects: off → soft"]
    assert setting_deltas(_settings(), _settings()) == []
    # Other sounds to use (a folder and a Studio collection by name); the amount isn't repeated for a pool that was none.
    other = _settings(sfx=["/Users/you/Downloads/Hits"], sfx_from=["Promo Sounds"])
    assert setting_deltas(_settings(), other) == ["Sound effects from: Sound Effects → Hits, Venture Studio Promo Sounds"]
    assert setting_deltas({"sfx": [], "sfx_from": []}, _settings()) == ["Sound effects from: none → Sound Effects"]
    assert setting_deltas({}, {"style": "tight"}) == []  # a run that never said anything about sounds
    assert setting_deltas({"style": "tight"}, {"style": "tight", **_settings()}) == ["Sound effects from: none → Sound Effects"]
    # A note that already says the amount is not said twice.
    assert setting_deltas(_settings(), _settings(sfx_amount="full"), {"sfx_amount"}) == []
    assert accent_item(4, 7) == "Sound effects: 4 → 7 accents" and accent_item(3, 1) == "Sound effects: 3 → 1 accent"
    assert accent_item(4, 4) is None and accent_item(None, 4) is None and accent_item(4, None) is None


def test_the_version_before_is_asked_how_many_accents_it_had():
    assert accents_before({"accents": 5}, _settings()) == 5
    assert accents_before({}, {"sfx": [], "sfx_from": []}) == 0  # no sounds to use: none
    assert accents_before({}, _settings(sfx_off=True)) == 0 and accents_before({}, _settings(sfx_amount="none")) == 0
    assert accents_before({}, _settings()) is None  # from before they were counted
    assert [sound_state(s) for s in ({}, _settings(), _settings(sfx_off=True), _settings(sfx_amount="full"), _settings(sfx_amount=None))] == [
        "no sounds", "soft", "off", "full", "soft",
    ]


def test_a_recut_says_how_many_accents_it_has_now():
    was, now = _settings(), _settings()
    items, unchanged = recut_items(was, now, 1, before={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 3},
                                   after={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 5})
    assert items == ["Sound effects: 3 → 5 accents"] and not unchanged
    items, unchanged = recut_items(was, _settings(sfx_amount="full"), 1, before={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 3},
                                   after={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 3})
    assert items[0] == "Sound effects: soft → full" and "sound effects only add to how it sounds" in items[-1] and not unchanged
    items, unchanged = recut_items(was, now, 1, before={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 3},
                                   after={"seconds": 30.0, "edits": 8, "broll": 2, "accents": 3})
    assert unchanged and "same cut" in items[0]


def test_accents_are_counted_in_the_version_entry(footage, catalog, fake, provider, tmp_path):
    out, cache = tmp_path / "out", tmp_path / "cache"
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", *_args(footage, out, cache), "--sfx-from", "Promo Sounds"]) == 0
    entry = json.loads((out / "versions.json").read_text())["versions"][-1]
    assert entry["accents"] >= 1 and entry["accents"] == len(_accents(ET.parse(next(out.glob("*.fcpxml"))).getroot()))


# ---------------------------------------------------------------- presets and flags together


def test_a_preset_can_bring_sounds_and_songs_and_neither_touches_the_other(tmp_path, monkeypatch, capsys):
    from roughcut import cli, presets

    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    for p in presets.BUILT_IN.values():  # CI builds --preset venture-oc on a Mac without Studio
        assert p.sfx == () and p.sfx_from == () and p.music_from is None
        assert not [f for f in presets.flags(p, "build") if "sfx" in f or "music-from" in f]
    assert main(["preset", "save", "promos", "--brief", "Promo.", "--sfx", "/Volumes/Media/Sound Effects", "--sfx-from", "Promo Sounds",
                 "--music-from", "Promo Music"]) == 0
    p = presets.load("promos")
    build = presets.flags(p, "build")
    assert "--sfx=/Volumes/Media/Sound Effects" in build and "--preset-sfx-from=Promo Sounds" in build and "--music-from=Promo Music" in build
    assert not [f for f in presets.flags(p, "render") if "sfx" in f or "music-from" in f]
    capsys.readouterr()
    assert main(["preset", "list", "--json"]) == 0
    listed = {x["name"]: x for x in json.loads(capsys.readouterr().out)}
    assert listed["promos"]["sfx"] == ["/Volumes/Media/Sound Effects"] and listed["promos"]["sfx_from"] == ["Promo Sounds"]
    assert listed["promos"]["music_from"] == "Promo Music"
    assert listed["venture-oc"]["sfx"] == [] and listed["venture-oc"]["music_from"] is None
    assert main(["preset", "show", "promos"]) == 0
    shown = capsys.readouterr().out
    assert "Venture Studio: Promo Sounds" in shown and "from Venture Studio 'Promo Music'" in shown
    # Dropping one kind leaves the other.
    assert main(["preset", "save", "promos", "--no-sfx"]) == 0
    p = presets.load("promos")
    assert p.sfx == () and p.sfx_from == () and p.music_from == "Promo Music"
    assert main(["preset", "save", "promos", "--sfx-from", "Promo Sounds", "--no-music-from"]) == 0
    p = presets.load("promos")
    assert p.sfx_from == ("Promo Sounds",) and p.music_from is None
    # Typed flags win over a preset's, kind by kind.
    assert main(["preset", "save", "promos", "--sfx", "/Volumes/Media/Sound Effects", "--music-from", "Promo Music"]) == 0
    argv = ["build", "x", "--preset", "promos", "--sfx", "/Users/you/Hits", "--no-music-from"]
    args, _ = cli._apply_preset(cli.build_parser().parse_args(argv), argv)
    assert args.sfx == ["/Users/you/Hits"] and args.music_from == "" and args.preset_sfx_from == ["Promo Sounds"]
    argv = ["build", "x", "--preset", "promos", "--sfx-from", "Other Sounds"]
    args, _ = cli._apply_preset(cli.build_parser().parse_args(argv), argv)
    assert args.sfx_from == ["Other Sounds"] and args.preset_sfx_from == [] and args.music_from == "Promo Music"

"""The paper edit's Venture Studio section: what Studio's Project history review will match in a cut.

Studio compares paths as plain strings. These tests check Roughcut's prediction against a small, separate
statement of that rule for flat files (below), on FCPXML whose media paths don't exist anywhere: the
prediction never touches a media file.
"""

from __future__ import annotations

import json
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import FakeProvider
from test_studio import (  # noqa: F401 - fixtures
    NOW,
    _asset,
    _reset_progress,
    fake,
    library,
    only_the_cache_is_opened,
    untouched,
)

from roughcut import pipeline, studio
from roughcut.cli import main
from roughcut.pipeline import Options
from roughcut.report import studio_section

ROOT = "/Volumes/Media/Studio Library"
OUT = "/Users/editor/Promo_roughcut"

HISTORY = """
CREATE TABLE project_history_events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL);
CREATE TABLE project_history_items (event_id INTEGER NOT NULL, asset_id INTEGER NOT NULL, output_path TEXT NOT NULL);
"""


def studio_rule(rows: list[tuple[int, str, str]], prepared: list[tuple[int, str]], path: str) -> set[int]:
    """Studio's exact-match rule for a flat file, stated plainly: every catalog row at its root + "/" + path below it,
    and every prepared output, compared with the path as written. Returns the item ids found there."""
    return {aid for aid, root, rel in rows if root.rstrip("/") + "/" + rel == path} | {aid for aid, out in prepared if out == path}


def fcpxml(clips: list[tuple[str, dict]], *, name: str = "Promo - Rough Cut v1 (tight, Opus)", extra: str = "") -> str:
    """A flat project: one spine clip per (path, attributes), each its own asset with a file URL."""
    assets, spine = [], []
    for i, (path, attrs) in enumerate(clips, 1):
        assets.append(f'<asset id="r{i + 1}" name="a{i}" start="0s" duration="10s" hasAudio="1">'
                      f'<media-rep kind="original-media" src="file://{quote(path)}"/></asset>')
        more = "".join(f' {k}="{v}"' for k, v in attrs.items())
        spine.append(f'<asset-clip ref="r{i + 1}" offset="{(i - 1) * 10}s" start="0s" duration="10s"{more}/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10"><resources>'
        '<format id="r1" frameDuration="1001/30000s" width="1920" height="1080"/>' + "".join(assets) + "</resources>"
        f'<library><event name="E"><project name="{name}"><sequence format="r1" duration="{len(clips) * 10}s"><spine>'
        + "".join(spine) + extra + "</spine></sequence></project></event></library></fcpxml>\n"
    )


@pytest.fixture
def catalog_rows(fake):
    """A catalog on a share that doesn't exist here, with a pack Studio prepared and a pack copy it scanned since."""
    rows = [
        (1, ROOT, "Music/Sunrise.m4a"),
        (2, ROOT, "Music/Old Ballad.wav"),
        (3, ROOT, "Sound Effects/whoosh_soft_01.wav"),
        (4, ROOT, "Packs/Promo/whoosh_soft_01.wav"),  # the pack copy, scanned: the same path as asset 3's prepared output
        (5, ROOT, "Music/Café.m4a"),
        (6, ROOT, "Music/Calm Piano.wav"),
        (7, ROOT, "Music/Gone.wav"),
    ]
    prepared = [(2, "/Users/editor/Desktop/Pack/Media/Old Ballad.wav"), (3, f"{ROOT}/Packs/Promo/whoosh_soft_01.wav")]
    assets = [
        _asset(1, "Music/Sunrise.m4a", kind="audio", provider="Vendor A", license="Standard"),
        _asset(2, "Music/Old Ballad.wav", kind="audio", provider="Vendor A", license="Standard"),
        _asset(3, "Sound Effects/whoosh_soft_01.wav", kind="audio", provider="Vendor B", license="Extended"),
        _asset(4, "Packs/Promo/whoosh_soft_01.wav", kind="audio"),
        _asset(5, "Music/Café.m4a", kind="audio"),
        _asset(6, "Music/Calm Piano.wav", kind="audio"),
        _asset(7, "Music/Gone.wav", kind="audio", missing=1, display_name="Gone (missing)"),
    ]
    after = HISTORY + "INSERT INTO project_history_events VALUES (1, 'prepared'), (2, 'confirmed_used');\n" + "".join(
        f"INSERT INTO project_history_items VALUES (1, {aid}, '{out}');\n" for aid, out in prepared
    ) + f"INSERT INTO project_history_items VALUES (2, 6, '{ROOT}/Music/Somewhere else.wav');\n"  # not a prepared event: no mapping
    fake.snapshot("1700000500-1-1", NOW - 3600, libraries=[(ROOT, "Studio Library")], assets=assets, after=after)
    return rows, prepared


CUT = [
    f"{ROOT}/Music/Sunrise.m4a",  # matched
    f"{ROOT}/Music/Old Ballad.wav",  # matched...
    "/Users/editor/Desktop/Pack/Media/Old Ballad.wav",  # ...and its pack copy: the same item, counted once
    f"{ROOT}/Packs/Promo/whoosh_soft_01.wav",  # two items at one path: ambiguous
    unicodedata.normalize("NFD", f"{ROOT}/Music/Café.m4a"),  # another Unicode form: a near miss
    f"{ROOT}/music/calm piano.wav",  # another letter case: a near miss
    "/Volumes/Media-1/Studio Library/Music/Sunrise.m4a",  # the share mounted again under a numbered name: a near miss
    f"{ROOT}/Projects/Shoot/new.mov",  # under the library, scanned after the snapshot
    "/Users/editor/Downloads/song.wav",  # outside every library
    f"{OUT}/graphics/G01-lower-third-abc123.mov",  # Roughcut's own render
    f"{ROOT}/Music/Gone.wav",  # missing in Studio: still matched, as Studio's review does
]


def test_the_prediction_follows_studios_exact_rule(fake, catalog_rows):
    rows, prepared = catalog_rows
    xml = fcpxml([(p, {}) for p in CUT])
    with untouched(fake.dir):
        record = studio.match_media(xml, out_dir=Path(OUT))
    files = record["files"]
    assert list(files) == CUT and record["refused"] is None  # in timeline order and record["snapshot"]["id"] == "1700000500-1-1"
    for path in CUT:
        ids = studio_rule(rows, prepared, path)
        want = "matched" if len(ids) == 1 else "ambiguous" if ids else None
        got = files[path]["status"]
        assert (got if got in ("matched", "ambiguous") else None) == want, path
        assert sorted(a["id"] for a in files[path]["assets"]) == sorted(ids), path
    status = {p: files[p]["status"] for p in CUT}
    assert [status[p] for p in CUT[4:10]] == ["near_miss", "near_miss", "near_miss", "not_in_snapshot", "outside", "render"]
    assert "Unicode form" in files[CUT[4]]["why"] and files[CUT[4]]["studio_path"] == f"{ROOT}/Music/Café.m4a"
    assert "letter case" in files[CUT[5]]["why"]
    assert "/Volumes/Media-1" in files[CUT[6]]["why"] and files[CUT[6]]["studio_path"] == CUT[0]
    assert files[CUT[0]]["assets"] == [{"id": 1, "name": "Sunrise.m4a", "provider": "Vendor A", "license": "Standard"}]


def test_the_section_counts_items_not_files_and_gives_credits(fake, catalog_rows):
    xml = fcpxml([(p, {}) for p in CUT])
    record = studio.match_media(xml, out_dir=Path(OUT))
    projects, refused = studio.cut_projects(xml)
    lines = studio_section(record, projects, refused=refused, out_dir=Path(OUT))
    text = "\n".join(lines)
    assert lines[0] == "## Venture Studio" and "catalog snapshot from" in text and "1 h old" in text
    # Sunrise, Old Ballad (twice: its catalog path and its pack copy) and Gone: 3 items from 4 files.
    assert '3 Studio items in "Promo - Rough Cut v1 (tight, Opus)" (4 of its 11 files)' in text
    assert "**Vendor A**, Standard license: Sunrise.m4a, Old Ballad.wav" in text
    assert "Provider not set in Studio, license not set: Gone (missing)" in text
    assert "**Near misses**" in text and "**Ambiguous**" in text and "whoosh_soft_01.wav (item 3), whoosh_soft_01.wav (item 4)" in text
    assert "**Not in this snapshot yet**" in text and "`new.mov`" in text and "Outside every Studio library: `song.wav`" in text
    assert "Roughcut's own renders" in text and "only once" in text and "Stringout" in text and "Back up catalog now" in text
    assert "Not in Studio" not in text


def test_disabled_clips_and_audition_alternatives_dont_count(fake, catalog_rows):
    audition = (
        '<audition offset="40s" duration="10s"><asset-clip ref="r2" offset="40s" start="0s" duration="10s"/>'
        '<asset-clip ref="r3" offset="40s" start="0s" duration="10s"/></audition>'
    )
    xml = fcpxml([(CUT[0], {}), (CUT[1], {}), (CUT[3], {"enabled": "0"})], extra=audition)
    projects, refused = studio.cut_projects(xml)
    assert refused is None
    assert [p for p, _ in projects[0].paths()] == [CUT[0], CUT[1]]  # the disabled clip and the second audition pick are left out


def test_a_snapshot_without_project_history_has_no_prepared_outputs(fake):
    fake.snapshot("1700000500-1-1", NOW - 3600, libraries=[(ROOT, "Studio Library")], assets=[_asset(2, "Music/Old Ballad.wav", kind="audio")])
    pack = "/Users/editor/Desktop/Pack/Media/Old Ballad.wav"
    record = studio.match_media(fcpxml([(f"{ROOT}/Music/Old Ballad.wav", {}), (pack, {})]))
    assert [f["status"] for f in record["files"].values()] == ["matched", "outside"]


def test_a_catalog_row_studio_refuses_stops_its_review_of_any_file(fake):
    fake.snapshot("1700000500-1-1", NOW - 3600, libraries=[(ROOT, "Studio Library")], assets=[
        _asset(1, "Music/Sunrise.m4a", kind="audio"), _asset(2, "../elsewhere.wav", kind="audio"),
    ])
    xml = fcpxml([(CUT[0], {})])
    record = studio.match_media(xml)
    assert "Unsafe source path" in record["refused"] and record["files"][CUT[0]]["status"] == "matched"
    text = "\n".join(studio_section(record, studio.cut_projects(xml)[0]))
    assert "**Studio's Project history review would refuse this file:**" in text and "Credits" in text


def test_a_file_studio_would_refuse_is_said_so():
    long_name = "x" * 201
    projects, refused = studio.cut_projects(fcpxml([(CUT[0], {})], name=long_name))
    assert projects == [] and "200 characters" in refused
    assert "control character" in studio.cut_projects(fcpxml([(CUT[0], {})], name="Promo\x7f"))[1]
    assert "DTD" in studio.cut_projects(fcpxml([(CUT[0], {})]).replace("<!DOCTYPE fcpxml>", '<!DOCTYPE fcpxml [<!ENTITY a "b">]>'))[1]


def test_kit_only_says_not_in_this_kit(tmp_path, monkeypatch):
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    doc = {"format_version": 1, "kind": studio.KIT_KIND, "exported_at": NOW, "project": {"name": "Promo kit", "assets": [
        {"asset_id": 1, "name": "Sunrise", "kind": "audio", "extension": "m4a", "provider": "Vendor A", "license": "Standard",
         "original_reference": CUT[0], "catalog_missing": False, "readiness": "ready"},
    ]}}
    (tmp_path / "kit.json").write_text(json.dumps(doc))
    kit = studio.load_kit(tmp_path / "kit.json")
    xml = fcpxml([(CUT[0], {}), (CUT[7], {})])
    with pytest.raises(studio.StudioUnavailable):
        studio.match_media(xml)
    record = studio.match_media(xml, kit=kit)
    assert [f["status"] for f in record["files"].values()] == ["matched", "not_in_kit"]
    text = "\n".join(studio_section(record, studio.cut_projects(xml)[0]))
    assert "kit 'Promo kit'" in text and "**Not in this kit**" in text and "Not in Studio" not in text


def test_paths_are_compared_as_written_with_no_file_touched(fake, catalog_rows, monkeypatch):
    """Nothing but Roughcut's copy is opened or looked at: a stalled share can't hold the prediction up."""
    import os

    touched = []
    for name in ("stat", "lstat"):
        real = getattr(os, name)

        def spy(path, *a, _real=real, **k):
            if str(path).startswith(("/Volumes", "/Users/editor")):
                touched.append(str(path))
            return _real(path, *a, **k)

        monkeypatch.setattr(os, name, spy)
    studio.match_media(fcpxml([(p, {}) for p in CUT]), out_dir=Path(OUT))
    assert touched == []


# ---------------------------------------------------------------- in runs: build, render, revise


@pytest.fixture
def shoot_in_library(footage, library, fake) -> Path:
    folder = library / "Projects" / "Promo shoot"
    folder.mkdir(parents=True)
    for name in ("talk.mov", "talk.json", "broll.mp4", "broll.json"):
        shutil.copy2(footage / name, folder / name)
    (library / "Music").mkdir()
    shutil.copy2(footage / "music" / "music.m4a", library / "Music" / "Sunrise.m4a")
    fake.snapshot("1700000500-1-1", NOW - 3600, assets=[
        _asset(1, "Projects/Promo shoot/talk.mov", kind="video", provider="Our crew", license="Owned"),
    ])
    return folder


def _section(report: str) -> str:
    return report.split("## Venture Studio", 1)[1].split("\n## ", 1)[0] if "## Venture Studio" in report else ""


def test_render_and_revise_rebuild_the_section_without_studio(shoot_in_library, library, fake, tmp_path, monkeypatch):
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    song = library / "Music" / "Sunrise.m4a"
    with untouched(fake.dir):
        pipeline.build(Options(inputs=[shoot_in_library], out=out, provider="none", transcriber="none", cache_dir=cache, music=[song]))
    report = (out / "edit_report.md").read_text()
    section = _section(report)
    assert "Checked against Venture Studio's catalog snapshot" in section and '1 Studio item in "Promo shoot - Rough Cut v1' in section
    assert "**Our crew**, Owned license: talk.mov" in section and "`Sunrise.m4a`" in section.split("Not in this snapshot yet")[1]
    html = (out / "edit_report.html").read_text()
    assert 'id="venture-studio"' in html and "Our crew" in html
    saved = json.loads((out / "plan.json").read_text())["studio_sources"]
    assert {Path(p).name: f["status"] for p, f in saved["files"].items()} == {"talk.mov": "matched", "Sunrise.m4a": "not_in_snapshot"}

    # Studio gone (another Mac): render rebuilds the section from plan.json, and a file the saved map lacks reads "not checked".
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    record = json.loads((out / "plan.json").read_text())
    record["studio_sources"]["files"] = {p: f for p, f in record["studio_sources"]["files"].items() if not p.endswith("Sunrise.m4a")}
    (out / "plan.json").write_text(json.dumps(record))
    pipeline.rerender(out, Options(inputs=[], cache_dir=cache))
    section = _section((out / "edit_report.md").read_text())
    assert "from the check made earlier against Venture Studio's catalog snapshot" in section
    assert "**Our crew**, Owned license: talk.mov" in section and "**Not checked**" in section and "`Sunrise.m4a`" in section
    assert "(1 of its 2 files, 1 not checked)" in section
    assert "studio_sources" in json.loads((out / "plan.json").read_text())

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    pipeline.revise(out, "Tighter.", Options(inputs=[], cache_dir=cache))
    section = _section((out / "edit_report.md").read_text())
    assert "from the check made earlier" in section and "**Our crew**, Owned license: talk.mov" in section
    assert json.loads((out / "plan.json").read_text())["studio_sources"]["files"]


def test_installed_but_unavailable_is_one_line(footage, fake, tmp_path):
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, provider="none", transcriber="none", cache_dir=tmp_path / "cache"))
    section = _section((out / "edit_report.md").read_text())
    assert section.strip().startswith("Couldn't check this cut against Venture Studio:") and "Back up catalog now" in section
    assert len([ln for ln in section.splitlines() if ln.strip()]) == 1
    assert "studio_sources" not in json.loads((out / "plan.json").read_text())


def test_no_section_without_studio_or_without_studio_files(footage, fake, tmp_path, monkeypatch):
    fake.snapshot("1700000500-1-1", NOW - 3600, libraries=[("/Volumes/Media/Studio Library", "Studio Library")])
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, provider="none", transcriber="none", cache_dir=tmp_path / "cache"))
    assert "Venture Studio" not in (out / "edit_report.md").read_text()  # nothing in the cut is Studio's
    monkeypatch.setenv(studio.STUDIO_ENV, str(tmp_path / "nowhere"))
    out2 = tmp_path / "out2"
    main(["build", str(footage), "-o", str(out2), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")])
    assert "Venture Studio" not in (out2 / "edit_report.md").read_text()

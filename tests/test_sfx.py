"""Sound-effect accents: the pool (sfx.py: classing, probing under a deadline, what's skipped) and the
accents on the cut's own frames (accents.py: events, precedence, spacing, turns, levels, lanes), end to end
through build, render and revise, with every FCPXML checked against the DTD and check_invariants."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import CANNED_PLAN, FakeProvider, ffmpeg, make_clip
from test_end_to_end import RATE_UNDEFINED, check_invariants, validate_dtd

from roughcut import accents, pipeline, presets, report, sfx, studio
from roughcut.analyze import Analysis, Cache
from roughcut.cli import main
from roughcut.media import MediaInfo
from roughcut.pipeline import Options
from roughcut.plan import normalize
from roughcut.timecode import parse_fcpx_time as T
from roughcut.timeline import PEAK_CEILING, STYLES, ConnectedClip, build_timeline

# ------------------------------------------------------------------ a folder of sounds


@pytest.fixture(scope="session")
def sfx_folder(tmp_path_factory) -> Path:
    """Sounds as a vendor folder holds them: classed by name or folder, plus what the pool must leave out."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("sfx") / "Sound Effects"
    for sub in ("Whooshes", "UI", "Incoming"):
        (root / sub).mkdir(parents=True)
    swell = "volume='exp(-((t-{at})*(t-{at}))/{w})':eval=frame"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=1.2:r=48000", "-af", swell.format(at=0.7, w=0.02), str(root / "Whooshes" / "whoosh_soft_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.5:d=0.9:r=48000", "-af", swell.format(at=0.4, w=0.01), str(root / "Whooshes" / "swoosh_fast_02.wav"))
    # 150 ms, peaking just under 0 dBFS: too short for a plain loudness reading.
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=1200:duration=0.15:sample_rate=48000", "-af", "volume=7.5", str(root / "UI" / "pop_ui_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.9:d=1.5:r=48000", "-af", "volume='exp(-4*t)':eval=frame", str(root / "impact_hit_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:duration=3:sample_rate=48000", "-af", "volume='t/3':eval=frame", str(root / "riser_tension_01.wav"))
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.05:d=2:r=48000", str(root / "room_tone.wav"))  # no class word
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=a=0.3:d=10:r=48000", str(root / "long_whoosh_bed.wav"))  # over 8 s
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=0.5:sample_rate=48000", str(root / "whoosh_lossless.flac"))  # not read
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500:duration=0.5:sample_rate=48000", str(root / "Incoming" / "whoosh_partial.wav"))
    (root / "Incoming" / sfx.IMPORT_MARKER).write_text("{}")  # Studio is still copying into it
    (root / "._whoosh_soft_01.wav").write_bytes(b"resource fork junk")
    (root / ".DS_Store").write_bytes(b"junk")
    return root


@pytest.fixture(scope="session")
def folder_pool(sfx_folder, tmp_path_factory) -> sfx.Pool:
    return sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path_factory.mktemp("sfx-cache")))


@pytest.mark.parametrize(
    ("name", "tags", "folders", "kind"),
    [
        ("whoosh_soft_01.wav", (), (), "whoosh"),
        ("WhooshSoft01.wav", (), (), "whoosh"),
        ("swoosh-pass-by.wav", (), (), "whoosh"),
        ("UIClick_03.wav", (), (), "pop"),
        ("Big Impacts 2.wav", (), (), "hit"),
        ("Cinematic Riser 4.aif", (), (), "riser"),
        ("take_07.wav", (), ("Transitions", "SFX"), "whoosh"),  # by its folder
        ("take_07.wav", ("braam",), ("Transitions",), "hit"),  # Studio's tags before folders
        ("hit then whoosh.wav", (), (), "hit"),  # the first word in the name
        ("room_tone.wav", (), ("Ambience",), None),
        ("tapestry_building_popcorn.wav", (), (), None),  # whole words only
    ],
)
def test_sounds_are_classed_by_the_words_in_their_names(name, tags, folders, kind):
    assert sfx.classify(name, tags, folders) == kind


def test_a_folder_pool_classes_measures_and_lists_what_it_left_out(folder_pool, sfx_folder):
    pool = folder_pool
    by_name = {s.name: s for s in pool.sounds}
    assert sorted(by_name) == ["impact_hit_01.wav", "pop_ui_01.wav", "riser_tension_01.wav", "swoosh_fast_02.wav", "whoosh_soft_01.wav"]
    assert {n: s.kind for n, s in by_name.items()} == {
        "impact_hit_01.wav": "hit", "pop_ui_01.wav": "pop", "riser_tension_01.wav": "riser", "swoosh_fast_02.wav": "whoosh", "whoosh_soft_01.wav": "whoosh",
    }
    # The hit is the loudest 50 ms; a riser lands with its end.
    assert 0.6 <= by_name["whoosh_soft_01.wav"].hit <= 0.75
    assert 0.3 <= by_name["swoosh_fast_02.wav"].hit <= 0.45
    assert by_name["impact_hit_01.wav"].hit == 0.0
    assert by_name["riser_tension_01.wav"].hit == pytest.approx(3.0)
    # A 150 ms pop still gets a loudness reading (measured with silence around it), and its peak.
    pop = by_name["pop_ui_01.wav"]
    assert float(pop.media.duration) == pytest.approx(0.15) and pop.lufs is not None and pop.peak > -3
    assert all(s.media.kind == "audio" and s.path.startswith(str(sfx_folder)) for s in pool.sounds)
    assert [s.id for s in pool.sounds] == ["X01", "X02", "X03", "X04", "X05"] and [s.path for s in pool.sounds] == sorted(s.path for s in pool.sounds)
    # Left out, and said why: no class word, a format Roughcut doesn't read, too long, mid-import; junk never counts.
    assert pool.skipped["unclassified"]["names"] == ["room_tone.wav"]
    assert pool.skipped["unsupported"]["names"] == ["whoosh_lossless.flac"]
    assert pool.skipped["long"]["names"] == ["long_whoosh_bed.wav"]
    assert pool.skipped["importing"]["names"] == ["Incoming/"]
    assert not any("._" in n or "DS_Store" in n for e in pool.skipped.values() for n in e["names"])
    # Saved in plan.json and read back the same.
    again = sfx.Pool.from_json(json.loads(json.dumps(pool.to_json())))
    assert [s.to_json() for s in again.sounds] == [s.to_json() for s in pool.sounds] and again.skipped == pool.skipped


def test_each_sound_is_measured_once(sfx_folder, tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache")
    first = sfx.build_pool(sfx.sources([str(sfx_folder)], []), cache)

    def no(*a, **k):
        raise AssertionError("probed again")

    monkeypatch.setattr(sfx, "probe", no)
    monkeypatch.setattr(sfx.ffmpeg_ops, "loudness_blocks", no)
    second = sfx.build_pool(sfx.sources([str(sfx_folder)], []), cache)
    assert [s.to_json() for s in second.sounds] == [s.to_json() for s in first.sounds]


def test_a_missing_folder_is_a_warning_not_a_stop(tmp_path):
    pool = sfx.build_pool(sfx.sources([str(tmp_path / "Not Connected" / "SFX")], []), Cache(tmp_path / "c"))
    assert pool.sounds == [] and "can't find it" in pool.warnings[0]


def test_a_folder_macos_wont_let_us_read_says_how_to_allow_it(sfx_folder, tmp_path, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(sfx, "_measure", denied)
    pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c"))
    assert pool.sounds == [] and "Privacy & Security" in pool.warnings[-1] and pool.skipped["unreadable"]["count"] == 6


def test_the_pool_is_capped_with_every_class_taking_turns():
    def c(name):
        return sfx.Candidate(f"/Volumes/Media/{name}", name)

    found = [c(f"whoosh_{i:03d}.wav") for i in range(50)] + [c("pop_1.wav"), c("hit_1.wav"), c("zz_riser.wav")]
    kept, over = sfx._take_turns(found, 10)
    assert {k.kind for k in kept} == {"whoosh", "pop", "hit", "riser"} and len(kept) == 10 and len(over) == 43


# ------------------------------------------------------------------ accents on a cut (no ffmpeg)


def _sound(sid: str, kind: str, seconds: float = 1.0, *, hit: float = 0.5, lufs: float | None = -20.0, peak: float | None = -6.0, name: str = "") -> sfx.Sound:
    name = name or f"{kind}_{sid.lower()}.wav"
    media = MediaInfo(
        path=f"/Volumes/Media/SFX/{name}", name=name, kind="audio", duration=Fraction(round(seconds * 48000), 48000), start=Fraction(0),
        has_video=False, has_audio=True, audio_channels=2, audio_rate=48000, fingerprint=sid.lower(),
    )
    return sfx.Sound(sid, kind, media, seconds if kind == "riser" else hit, lufs, peak)


def _pool(*sounds: sfx.Sound) -> sfx.Pool:
    return sfx.Pool(sounds=list(sounds))


SENTENCES = [(1 + 4.5 * i, 4.6 + 4.5 * i, f"Sentence number {i + 1} is here.") for i in range(8)]


def _cut(*, montage: bool = False, music: bool = False, dialogue_level: float | None = -16.0):
    """Four sections of two 3.6 s lines each (about 8 s a section), so the last starts past 20 s."""
    talk = make_clip("V01", seconds=40, sentences=SENTENCES)
    broll = make_clip("V02", seconds=10)
    song = make_clip("A01", kind="audio", seconds=60)
    analysis = Analysis(clips=[talk, broll, song])
    seg = [s.id for s in talk.segments]
    sections = [
        {"name": "Open", "purpose": "", "items": seg[0:2], "broll": []},
        {"name": "Middle", "purpose": "", "items": seg[2:4], "broll": []},
        {"name": "Build", "purpose": "", "items": seg[4:6] + (["V02@1-5"] if montage else []), "broll": []},
        {"name": "Wrap", "purpose": "", "items": seg[6:8], "broll": []},
    ]
    raw = {"title": "t", "sections": sections, "music": [{"clip": "A01", "first_section": 0, "last_section": 3, "source_in": 0}] if music else []}
    plan = normalize(raw, analysis, source="fake:fake-model")
    tl = build_timeline(plan, analysis, style=STYLES["medium"], name="t", dialogue_level=dialogue_level, fill=False)
    return tl, analysis, plan


def _graphic(tl, gid: str, frame: int, text: str = "Maria Lopez") -> None:
    tl.connected.append(ConnectedClip(gid, Fraction(0), 90, frame, 3, kind="graphic", note=text))


def _placed(tl):
    return [(a["event"], a["kind"], a["frame"]) for a in tl.accents]


def test_soft_is_section_whooshes_and_graphic_accents_full_adds_montage_and_riser():
    tl, analysis, _ = _cut(montage=True)
    starts = [f for f, _ in tl.section_starts]
    assert len(starts) == 4 and starts[-1] * tl.frame_duration >= accents.RISER_FROM
    _graphic(tl, "G01", 30)  # a lower third in the open
    pool = _pool(_sound("X01", "whoosh", name="a_whoosh.wav"), _sound("X02", "whoosh", name="b_whoosh.wav"), _sound("X03", "whoosh", name="c_whoosh.wav"),
                 _sound("X04", "pop", 0.15, hit=0.0), _sound("X05", "riser", 3.0), _sound("X06", "hit", 1.5, hit=0.0))
    accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third"})
    assert _placed(tl) == [("graphic", "pop", 30)] + [("section", "whoosh", f) for f in starts[1:]]
    accents.place(tl, analysis, pool, "full", graphic_kinds={"G01": "lower_third"})
    whys = [a["event"] for a in tl.accents]
    assert "montage" in whys and "riser" in whys
    riser = next(c for c in tl.connected if c.kind == "sfx" and pool.sound(c.clip_id).kind == "riser")
    assert riser.end == starts[-1], "a riser ends on its event: the last section's first frame"
    assert ("section", "whoosh", starts[-1]) in _placed(tl), "the whoosh into the last section plays with the riser into it"
    accents.place(tl, analysis, pool, "none")
    assert tl.accents == [] and not [c for c in tl.connected if c.kind == "sfx"]
    # A cut made without AI never gets more than soft.
    accents.place(tl, analysis, pool, "full", heuristic=True)
    assert {a["event"] for a in tl.accents} == {"section"}


def test_accents_keep_their_distance_and_the_higher_one_wins():
    tl, analysis, _ = _cut()
    fd = tl.frame_duration
    second, third = (f for f, _ in tl.section_starts[1:3])
    _graphic(tl, "G01", second + 20)  # a lower third under a second after a section change: the pop wins
    _graphic(tl, "G02", third, "OC Food Fest")  # a title card on a section change: the hit wins
    pool = _pool(_sound("X01", "whoosh"), _sound("X02", "pop", 0.15, hit=0.0), _sound("X03", "hit", 1.5, hit=0.0))
    accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third", "G02": "title_card"})
    placed = _placed(tl)
    assert ("graphic", "pop", second + 20) in placed and ("section", "whoosh", second) not in placed
    assert ("graphic", "hit", third) in placed and ("section", "whoosh", third) not in placed
    frames = sorted(a["frame"] for a in tl.accents)
    assert all(b - a >= round(accents.SPACING / fd) for a, b in zip(frames, frames[1:]))


def test_a_graphic_that_never_rendered_gets_no_accent():
    """graphics.make's placed list holds graphics that never rendered: only the ones attached to the cut count."""
    tl, analysis, plan = _cut()
    _graphic(tl, "G01", 30)

    class G:
        def __init__(self, gid):
            self.id, self.spec = gid, type("S", (), {"kind": "lower_third"})()

    pool = _pool(_sound("X01", "pop", 0.15, hit=0.0))
    pipeline.place_accents(tl, analysis, pool, Options(inputs=[], sfx_amount="soft"), plan, [G("G01"), G("G02")])
    assert _placed(tl) == [("graphic", "pop", 30)]


def test_the_hit_lands_on_its_frame_and_never_reads_past_the_file():
    tl, analysis, _ = _cut()
    fd = tl.frame_duration
    pool = _pool(_sound("X01", "whoosh", 1.234, hit=0.71), _sound("X02", "whoosh", 0.9, hit=0.4, name="b.wav"))
    accents.place(tl, analysis, pool, "soft")
    assert tl.accents
    for a in tl.accents:
        cc = next(c for c in tl.connected if c.kind == "sfx" and c.start == a["start"])
        s = pool.sound(cc.clip_id)
        lands = cc.start * fd - cc.src_in + Fraction(s.hit).limit_denominator(1000000)
        assert a["frame"] * fd <= lands < (a["frame"] + 1) * fd
        assert cc.src_in + cc.frames * fd <= s.media.duration and (cc.src_in / fd).denominator == 1
    # At the very start the sound is trimmed at frame 0, with a whole-sample de-click where it was cut.
    early = _sound("X09", "whoosh", 1.5, hit=1.2)
    start, head, frames, cut_head, cut_tail = accents._span(early, 10, tl)
    assert start == 0 and head > 0 and cut_head and not cut_tail
    tl.section_starts.insert(1, (10, "Early"))
    accents.place(tl, analysis, _pool(early), "soft")
    cc = next(c for c in tl.connected if c.kind == "sfx" and c.start == 0)
    assert cc.fade_in > 0 and (cc.fade_in * 48000).denominator == 1 and cc.fade_out == 0


def test_each_class_takes_its_sounds_in_turn_and_none_replays_within_ten_seconds():
    tl, analysis, _ = _cut()
    starts = [f for f, _ in tl.section_starts[1:]]  # about 8 s apart
    three = _pool(*(_sound(f"X0{i}", "whoosh", name=f"{c}_whoosh.wav") for i, c in enumerate("abc", 1)))
    accents.place(tl, analysis, three, "soft")
    assert [a["file"] for a in tl.accents] == ["a_whoosh.wav", "b_whoosh.wav", "c_whoosh.wav"]
    accents.place(tl, analysis, three, "soft")
    assert [a["file"] for a in tl.accents] == ["a_whoosh.wav", "b_whoosh.wav", "c_whoosh.wav"], "the same cut gets the same sounds"
    one = _pool(_sound("X01", "whoosh"))
    missed = accents.place(tl, analysis, one, "soft")
    played = [a["frame"] for a in tl.accents]
    assert played == [starts[0], starts[2]] and missed["replayed"] == 1, "the second change is under 10 s after the first"
    missed = accents.place(tl, analysis, _pool(_sound("X01", "pop")), "soft")
    assert tl.accents == [] and missed["no_sound"] == {"whoosh": 3}


def test_a_short_pop_is_levelled_under_the_dialogue_never_at_its_own_level():
    loud = _sound("X01", "pop", 0.15, lufs=None, peak=-0.5)  # too short to measure, peaking near full scale
    volume, why = accents.level(loud, -16.0, "soft", False)
    assert volume < 0 and volume + loud.peak <= PEAK_CEILING and "peak" in why
    measured = _sound("X02", "pop", 0.15, lufs=-5.3, peak=-0.6)
    volume, _ = accents.level(measured, -16.0, "soft", False)
    assert volume == pytest.approx(-16.0 - 12.0 - (-5.3), abs=0.5)
    under, why = accents.level(measured, -16.0, "soft", True)
    assert under == volume - 6 and "word" in why
    full, _ = accents.level(measured, -16.0, "full", False)
    assert full == volume + 3
    # A quiet sound comes up, but never past -1 dBFS peaks.
    quiet = _sound("X03", "hit", lufs=-40.0, peak=-3.0)
    volume, why = accents.level(quiet, -16.0, "full", False)
    assert volume + quiet.peak <= PEAK_CEILING and "peaks" in why
    # Levels kept as recorded: a fixed gain under the sound's own level.
    fixed, why = accents.level(measured, None, "soft", False)
    assert fixed == accents.SFX_DB["soft"] and "fixed" in why


def test_accents_sit_below_every_music_lane_with_level_rows():
    tl, analysis, _ = _cut(music=True)
    music = [c for c in tl.connected if c.kind == "music"]
    assert music and min(c.lane for c in music) == -1
    # A long whoosh and a pop as a lower third lands inside it take two lanes.
    first = tl.section_starts[1][0]
    _graphic(tl, "G01", first + 90)
    pool = _pool(_sound("X01", "whoosh", 7.0, hit=0.2, name="a.wav"), _sound("X02", "pop", 0.15, hit=0.0, name="b.wav"))
    accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third"})
    lanes = sorted({c.lane for c in tl.connected if c.kind == "sfx"})
    assert lanes and max(lanes) <= -2 and len(lanes) >= 2
    rows = [r for r in tl.levels if r["use"].startswith("sound effect")]
    assert rows and all(r["volume"] is not None for r in rows)
    accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third"})  # placing again replaces, never doubles
    assert len([r for r in tl.levels if r["use"].startswith("sound effect")]) == len(rows)


# ------------------------------------------------------------------ builds, re-cuts and revisions


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


@pytest.fixture(scope="module")
def cache(tmp_path_factory):
    return tmp_path_factory.mktemp("analysis-cache")


def _sfx_clips(root: ET.Element, project: str) -> list[ET.Element]:
    p = next(x for x in root.iter("project") if project in x.get("name"))
    return [el for el in p.iter("asset-clip") if el.get("audioRole") == "effects.SFX"]


def test_a_build_with_sound_effects(footage, sfx_folder, tmp_path, fake, cache):
    out = tmp_path / "out"
    fake.plan = dict(CANNED_PLAN, sfx="full")
    assert main(["build", str(footage), "-o", str(out), "--name", "footage", "--transcriber", "none", "--cache-dir", str(cache), "--sfx", str(sfx_folder)]) == 0
    fcpxml = next(out.glob("*.fcpxml"))
    validate_dtd(fcpxml)
    check_invariants(fcpxml)
    root = ET.parse(fcpxml).getroot()
    formats = {f.get("id"): f for f in root.iter("format")}
    rough = _sfx_clips(root, "Rough Cut")
    assert rough, "accents in the rough cut"
    for el in rough:
        assert formats[el.get("format")].get("name") == RATE_UNDEFINED and int(el.get("lane")) <= -2
        assert el.find("adjust-volume") is not None, "never at its recorded level"
    assert _sfx_clips(root, "Stringout") == [], "the stringout is for reading the speech"
    # Only the sounds the cut uses reach the browser, never the whole pool.
    browser = [el for el in root.find(".//event") if el.tag == "asset-clip" and el.get("audioRole") == "effects.SFX"]
    assert sorted(el.get("name") for el in browser) == sorted({el.get("name") for el in rough})
    assert all("Sound Effects" in el.find("keyword").get("value") for el in browser)
    # Saved outside analysis.json (its digest stays) and inside plan.json, for re-cuts.
    analysis = json.loads((out / "analysis.json").read_text())
    assert not [c for c in analysis["clips"] if c["id"].startswith("X")]
    saved = json.loads((out / "plan.json").read_text())
    assert saved["settings"]["sfx_amount"] == "full" and saved["settings"]["sfx"] == [str(sfx_folder)]
    assert len(saved["settings"]["sfx_pool"]["sounds"]) == 5 and saved["plan"]["sfx"] == "full"
    report = (out / "edit_report.md").read_text()
    assert "## Sound effects" in report and "room_tone.wav" in report and "whoosh_lossless.flac" in report
    levels = report.split("## Levels")[1].split("## Media")[0]
    assert "sound effect (whoosh)" in levels
    media = report.split("## Media")[1].split("##")[0]
    assert "whoosh" not in media, "the pool isn't footage"


@pytest.mark.parametrize(("asked", "whys"), [("none", set()), ("soft", {"section"}), ("unspecified", {"section"}), ("full", {"section", "montage"})])
def test_the_planner_reads_how_many_accents_the_brief_asks_for(footage, sfx_folder, tmp_path, fake, cache, asked, whys):
    fake.plan = dict(CANNED_PLAN, sfx=asked)
    result = pipeline.build(Options(inputs=[footage], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=cache, sfx=(str(sfx_folder),)))
    assert {a["event"] for a in result.timeline.accents} == whys
    assert json.loads((tmp_path / "out" / "plan.json").read_text())["settings"]["sfx_amount"] == ("soft" if asked == "unspecified" else asked)
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)


def test_graphics_get_their_accents_once_they_are_attached(footage, sfx_folder, tmp_path, monkeypatch, cache):
    """The cut is written once before graphics (with the section whooshes) and again after: a title card that rendered
    gets a hit and wins over the whoosh on its section change; a lower third that never rendered gets nothing."""
    from roughcut import graphics
    from roughcut.analyze import Clip
    from roughcut.plan import GraphicSpec

    title = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}
    lower = dict(title, kind="lower_third", segment="V01.S006", words="The kids", text="Maria Lopez")
    provider = FakeProvider(dict(CANNED_PLAN, graphics=[title, lower], sfx="soft"))
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    seen = {}

    def fake_make(designer, specs, tl, analysis, out_dir, **k):
        seen["accents before"] = [a["event"] for a in tl.accents]
        fd = tl.frame_duration
        placed = [
            graphics.PlacedGraphic(id=f"G0{i + 1}", spec=GraphicSpec(**{f: g[f] for f in ("kind", "segment", "words", "text")}), index=i,
                                   start=tl.seg_ranges[g["segment"]][0], frames=75)
            for i, g in enumerate((title, lower))
        ]
        g = placed[0]  # only the title card rendered
        media = MediaInfo(path=str(out_dir / "graphics" / "G01-title-card.mov"), name="G01-title-card.mov", kind="video", duration=75 * fd,
                          start=Fraction(0), has_video=True, has_audio=False, width=tl.width, height=tl.height, frame_duration=fd, codec="prores")
        tl.connected.append(ConnectedClip(g.id, Fraction(0), g.frames, g.start, 2, kind="graphic", note=g.spec.text))
        return [Clip(id=g.id, media=media, role="graphic", transcript_source="graphic")], placed, []

    monkeypatch.setattr(graphics, "make", fake_make)
    result = pipeline.build(Options(inputs=[footage], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=cache,
                                    sfx=(str(sfx_folder),), graphics=True))
    assert seen["accents before"] == ["section", "section"]
    title_at = next(c.start for c in result.timeline.connected if c.kind == "graphic")
    placed = [(a["event"], a["kind"], a["frame"]) for a in result.timeline.accents]
    assert ("graphic", "hit", title_at) in placed
    assert not any(a["event"] == "section" and abs(a["frame"] - title_at) < 60 for a in result.timeline.accents), "the hit wins"
    assert not any(a["kind"] == "pop" for a in result.timeline.accents), "the lower third never rendered"
    validate_dtd(result.fcpxml)
    check_invariants(result.fcpxml)
    assert "title card 'Heading Out'" in result.report.read_text()


def test_render_places_the_same_accents_without_the_folder_and_a_new_style_moves_them(footage, sfx_folder, tmp_path, fake, cache):
    folder = tmp_path / "SFX"
    shutil.copytree(sfx_folder, folder)
    out = tmp_path / "out"
    fake.plan = dict(CANNED_PLAN, sfx="full")
    built = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, sfx=(str(folder),)))
    before = [(a["file"], a["frame"], a["volume"]) for a in built.timeline.accents]
    assert before
    shutil.rmtree(folder)  # unplugged: the saved pool places them, nothing reads the share
    calls = len(fake.calls)
    again = pipeline.rerender(out, Options(inputs=[]), overrides=set())
    assert [(a["file"], a["frame"], a["volume"]) for a in again.timeline.accents] == before and len(fake.calls) == calls
    assert any("no longer where" in w and "a sound effect" in w for w in again.warnings), "missing_media is told about them"
    validate_dtd(again.fcpxml)
    check_invariants(again.fcpxml)
    tight = pipeline.rerender(out, Options(inputs=[], style="tight"), overrides={"style"})
    sections = {f for f, _ in tight.timeline.section_starts[1:]}
    assert [a["frame"] for a in tight.timeline.accents] != [b[1] for b in before]
    assert {a["frame"] for a in tight.timeline.accents if a["event"] == "section"} <= sections, "on the new cut's own frames"
    check_invariants(tight.fcpxml)
    # --no-sfx on a re-cut turns them off; the pool stays saved.
    off = pipeline.rerender(out, Options(inputs=[], sfx_off=True), overrides={"sfx_off"})
    assert off.timeline.accents == [] and "Off for this cut" in off.report.read_text()
    assert json.loads((out / "plan.json").read_text())["settings"]["sfx_pool"]["sounds"]


def test_revise_reads_sound_effects_from_the_notes(footage, sfx_folder, tmp_path, fake, cache):
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, sfx=(str(sfx_folder),)))
    fake.plan = dict(CANNED_PLAN, sfx="none")
    result = pipeline.revise(out, "Lose the whooshes.", Options(inputs=[]))
    request = fake.calls[-1]["parts"][1].text
    assert "sound-effect accents soft" in request.split("<cut_settings>")[1] and "lose the whooshes" in request.split("</editor_notes>")[1]
    assert result.timeline.accents == []
    assert json.loads((out / "plan.json").read_text())["settings"]["sfx_amount"] == "none"
    assert "Sound effects: none, as your notes ask (v1 had soft)." in result.report.read_text()
    fake.plan = dict(CANNED_PLAN, sfx="unspecified")
    result = pipeline.revise(out, "Tighter hook.", Options(inputs=[]))
    assert result.timeline.accents == [], "notes about the lines leave them as they are"
    fake.plan = dict(CANNED_PLAN, sfx="soft")
    result = pipeline.revise(out, "Bring back a few subtle whooshes.", Options(inputs=[]))
    assert result.timeline.accents and json.loads((out / "plan.json").read_text())["settings"]["sfx_amount"] == "soft"


def test_notes_asking_for_sound_effects_without_any_say_how(footage, tmp_path, fake, cache):
    out = tmp_path / "out"
    pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache))
    fake.plan = dict(CANNED_PLAN, sfx="full")
    result = pipeline.revise(out, "Add whooshes on the transitions.", Options(inputs=[]))
    note = next(w for w in result.warnings if w.startswith("Your notes ask for sound effects"))
    assert "--sfx" in note and result.timeline.accents == []


def test_no_sfx_reads_nothing(footage, sfx_folder, tmp_path, fake, cache, monkeypatch):
    def no(*a, **k):
        raise AssertionError("the pool was read")

    monkeypatch.setattr(sfx, "build_pool", no)
    monkeypatch.setattr(sfx, "sources", no)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(cache), "--sfx", str(sfx_folder), "--no-sfx"]) == 0
    assert "effects.SFX" not in next(out.glob("*.fcpxml")).read_text()
    assert json.loads((out / "plan.json").read_text())["settings"]["sfx_off"] is True


def test_without_a_pool_the_cut_is_unchanged(footage, sfx_folder, tmp_path, fake, cache):
    """No sounds given (or --no-sfx): the plan's sfx field changes nothing, byte for byte."""

    def cut(name: str, plan: dict, **kw) -> str:
        fake.plan = plan
        out = tmp_path / name
        pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw))
        return next(out.glob("*.fcpxml")).read_text()

    plain = cut("plain", CANNED_PLAN)
    assert cut("asked", dict(CANNED_PLAN, sfx="full")) == plain
    assert cut("off", dict(CANNED_PLAN, sfx="full"), sfx=(str(sfx_folder),), sfx_off=True) == plain
    assert "effects.SFX" not in plain and "## Sound effects" not in (tmp_path / "plain" / "edit_report.md").read_text()


def test_sound_effects_in_a_footage_folder_are_never_footage(footage, sfx_folder, tmp_path, fake, cache):
    shoot = tmp_path / "shoot"
    shutil.copytree(footage, shoot)
    shutil.copytree(sfx_folder, shoot / "SFX")
    result = pipeline.build(Options(inputs=[shoot], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=cache, sfx=(str(shoot / "SFX"),)))
    analysis = json.loads((tmp_path / "out" / "analysis.json").read_text())
    assert not [c for c in analysis["clips"] if "SFX" in c["media"]["path"]]
    assert result.timeline.accents


def test_a_share_that_stops_answering_drops_its_sounds_in_time(footage, sfx_folder, tmp_path, fake, cache, monkeypatch):
    """A mounted share that stops answering blocks in the kernel: each file gets a deadline, and the first stall
    drops the rest of that share's sounds, so the run finishes without them."""
    stuck = threading.Event()

    def hang(*a, **k):
        stuck.wait(30)
        raise OSError("gave up")

    monkeypatch.setattr(sfx, "FILE_SECONDS", 0.3)
    monkeypatch.setattr(sfx, "_measure", hang)
    t = time.monotonic()
    try:
        result = pipeline.build(Options(inputs=[footage], out=tmp_path / "out", name="footage", transcriber="none", cache_dir=cache, sfx=(str(sfx_folder),)))
    finally:
        stuck.set()
    assert time.monotonic() - t < 20
    assert result.timeline.accents == []
    assert any("stopped answering" in w for w in result.warnings)
    validate_dtd(result.fcpxml)

    # A folder listing that never comes back is dropped the same way.
    stuck.clear()
    monkeypatch.setattr(sfx, "SCAN_IDLE_SECONDS", 0.3)
    monkeypatch.setattr(sfx, "_scandir", hang)
    try:
        pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c2"))
    finally:
        stuck.set()
    assert pool.sounds == [] and "stopped answering" in pool.warnings[0]


def test_a_render_checks_sound_effects_under_a_deadline(monkeypatch):
    """missing_media on a share that doesn't answer: said once, never waited on per file."""
    clips = [sfx.Sound(f"X0{i}", "whoosh", _sound("X", "whoosh").media, 0.5, -20, -6).clip() for i in (1, 2)]
    analysis = Analysis(clips=clips)
    tl, _, _ = _cut()
    tl.connected += [ConnectedClip("X01", Fraction(0), 10, 0, -2, kind="sfx"), ConnectedClip("X02", Fraction(0), 10, 40, -2, kind="sfx")]
    calls = []

    def stalled(fn, *a, seconds, what=""):
        calls.append(what)
        raise pipeline.Stalled(what)

    monkeypatch.setattr(pipeline, "bounded", stalled)
    warnings = pipeline.missing_media(analysis, tl)
    assert len(calls) == 1 and len(warnings) == 1 and "didn't answer" in warnings[0] and "2 sound effects" in warnings[0]


# ------------------------------------------------------------------ shares that are slow, asleep or in the way


class _Slow:
    """os.scandir's iterator, answering one entry at a time as a loaded network share does."""

    def __init__(self, it, delay: float):
        self.it, self.delay = it, delay

    def __enter__(self):
        self.it.__enter__()
        return self

    def __exit__(self, *exc):
        return self.it.__exit__(*exc)

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(self.delay)
        return next(self.it)


class _Entry:
    """A directory entry that costs nothing to make."""

    def __init__(self, name: str, directory: str, *, folder: bool = False):
        self.name, self.path, self.folder = name, f"{directory}/{name}", folder

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self.folder

    def is_symlink(self) -> bool:
        return False


class _Listed:
    """What os.scandir returns for a folder: its entries, one at a time, then `after` (a sleep or a stall)."""

    def __init__(self, entries, after=None, delay: float = 0.0):
        self.entries, self.after, self.delay = entries, after, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for e in self.entries:
            if self.delay:
                time.sleep(self.delay)
            yield e
        if self.after:
            self.after()


def test_a_kit_file_on_a_stalled_share_is_read_under_a_deadline(footage, tmp_path, fake, monkeypatch, capsys):
    """studio.load_kit opens the file with plain os calls, which a stalled mount blocks in the kernel."""
    stuck = threading.Event()

    def hang(*a, **k):
        stuck.wait(30)
        raise OSError("gave up")

    monkeypatch.setattr(studio, "load_kit", hang)
    monkeypatch.setattr(sfx, "KIT_SECONDS", 0.3, raising=False)
    kit = str(tmp_path / "Volumes-Media" / "promo-kit.json")
    t = time.monotonic()
    try:
        with pytest.raises(sfx.SourceError, match="didn't answer"):
            sfx.sources([], [kit], strict=[kit])
        loose = sfx.sources([], [kit])
        assert loose.items == [] and "didn't answer" in loose.warnings[0] and "no sound effects from it" in loose.warnings[0]
        # The build stops before its folder is made (a kit file you named is an explicit request); a preset's never stops it.
        out = tmp_path / "out"
        base = ["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "cache")]
        assert main(base + ["--sfx-from", kit]) == 1 and not out.exists()
        assert "didn't answer" in capsys.readouterr().err
        assert main(base + ["--preset-sfx-from", kit]) == 0
        assert any("didn't answer" in w for w in json.loads((out / "result.json").read_text())["warnings"])
    finally:
        stuck.set()
    assert time.monotonic() - t < 30


def test_a_folder_given_to_sfx_from_says_to_use_sfx(tmp_path):
    folder = tmp_path / "Sound Effects"
    folder.mkdir()
    with pytest.raises(sfx.SourceError, match="folder.*--sfx"):
        sfx.sources([], [str(folder)], strict=[str(folder)])


def test_a_slow_share_that_keeps_answering_loses_no_sounds(sfx_folder, tmp_path, monkeypatch):
    """The deadline is about silence: a listing that takes minutes because the share is slow, one entry after another, finishes."""
    real = os.scandir
    monkeypatch.setattr(sfx, "SCAN_SECONDS", 0.5, raising=False)  # the old whole-listing ceiling
    monkeypatch.setattr(sfx, "SCAN_IDLE_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(sfx, "_scandir", lambda p: _Slow(real(p), 0.12), raising=False)
    t = time.monotonic()
    pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c"))
    assert time.monotonic() - t > 1.0, "slower than the silence it allows, in all"
    assert len(pool.sounds) == 5
    assert not [w for w in pool.warnings if "answer" in w or "stopped" in w or "slow" in w]


def test_a_listing_that_stops_answering_keeps_the_folders_it_finished(tmp_path):
    folder = str(tmp_path)
    stuck = threading.Event()

    def asleep():
        stuck.wait(30)
        raise OSError("gave up")

    def scandir(p):
        if str(p) == folder:
            return _Listed([_Entry("whoosh_a.wav", folder), _Entry("whoosh_b.wav", folder), _Entry("More", folder, folder=True)])
        return _Listed([_Entry("whoosh_c.wav", str(p))], after=asleep)  # the subfolder goes to sleep before it is done

    t = time.monotonic()
    try:
        listing = sfx.list_folder(folder, idle=0.3, total=60, scandir=scandir)
    finally:
        stuck.set()
    assert time.monotonic() - t < 10
    assert listing.status == "stalled" and sorted(c.name for c in listing.found) == ["whoosh_a.wav", "whoosh_b.wav"]
    assert listing.seen == 2, "a half-listed folder counts for nothing: Studio's import marker may be in the half not seen"


def test_a_listing_that_runs_past_its_total_time_keeps_what_it_listed(tmp_path):
    folder = str(tmp_path)

    def scandir(p):
        if str(p) == folder:
            return _Listed([_Entry(f"whoosh_{i}.wav", folder) for i in range(20)] + [_Entry("More", folder, folder=True)])
        return _Listed([_Entry(f"pop_{i}.wav", str(p)) for i in range(100_000)], delay=0.01)  # steady, so never silent

    listing = sfx.list_folder(folder, idle=5, total=0.5, scandir=scandir)
    assert listing.status == "slow" and len(listing.found) == 20


def test_a_listing_reads_a_tree_the_way_os_walk_did(sfx_folder):
    listing = sfx.list_folder(str(sfx_folder))
    assert listing.status == "done" and listing.denied == []
    assert sorted(c.name for c in listing.found) == sorted(
        ["impact_hit_01.wav", "long_whoosh_bed.wav", "pop_ui_01.wav", "riser_tension_01.wav", "room_tone.wav", "swoosh_fast_02.wav", "whoosh_soft_01.wav"])
    assert listing.other == ["whoosh_lossless.flac"] and listing.importing == ["Incoming/"]
    by = {c.name: c for c in listing.found}
    assert by["pop_ui_01.wav"].folders == ("UI", "Sound Effects") and by["impact_hit_01.wav"].folders == ("Sound Effects",)
    assert by["pop_ui_01.wav"].path == str(sfx_folder / "UI" / "pop_ui_01.wav")
    single = sfx.list_folder(str(sfx_folder / "UI" / "pop_ui_01.wav"))
    assert [c.name for c in single.found] == ["pop_ui_01.wav"] and single.found[0].folders == ("UI",)
    with pytest.raises(FileNotFoundError):
        sfx.list_folder(str(sfx_folder / "nope"))


def test_a_stalled_folder_listing_is_worded_apart_from_a_slow_one(sfx_folder, tmp_path, monkeypatch):
    stuck = threading.Event()
    real = os.scandir

    def scandir(p):
        if str(p).endswith("UI"):
            stuck.wait(30)
            raise OSError("gave up")
        return real(p)

    monkeypatch.setattr(sfx, "SCAN_IDLE_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(sfx, "_scandir", scandir, raising=False)
    try:
        pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c"))
    finally:
        stuck.set()
    assert any("stopped answering" in w and "listed" in w for w in pool.warnings)
    assert {s.name for s in pool.sounds} >= {"impact_hit_01.wav", "riser_tension_01.wav"}, "what was listed before it stopped"
    assert "whoosh_soft_01.wav" not in {s.name for s in pool.sounds}

    monkeypatch.setattr(sfx, "SCAN_IDLE_SECONDS", 5, raising=False)
    monkeypatch.setattr(sfx, "SCAN_TOTAL_SECONDS", 0.3, raising=False)
    monkeypatch.setattr(sfx, "_scandir", lambda p: _Slow(real(p), 0.1), raising=False)
    pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c2"))
    assert any("slow" in w and "listed" in w for w in pool.warnings) and not any("stopped answering" in w for w in pool.warnings)


def test_a_folder_macos_wont_list_says_how_to_allow_it(sfx_folder, tmp_path, monkeypatch):
    real = os.scandir

    def denied(p):
        if str(p) == str(sfx_folder):
            raise PermissionError(1, "Operation not permitted")
        return real(p)

    monkeypatch.setattr(sfx, "_scandir", denied, raising=False)
    pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c"))
    assert pool.sounds == [] and "Privacy & Security" in pool.warnings[0] and str(sfx_folder) in pool.warnings[0]
    # One subfolder it can't open is said, and the rest of the folder still counts.

    def sub_denied(p):
        if str(p).endswith("UI"):
            raise PermissionError(1, "Operation not permitted")
        return real(p)

    monkeypatch.setattr(sfx, "_scandir", sub_denied, raising=False)
    pool = sfx.build_pool(sfx.sources([str(sfx_folder)], []), Cache(tmp_path / "c2"))
    assert any("UI" in w and "Privacy & Security" in w for w in pool.warnings)
    assert "whoosh_soft_01.wav" in {s.name for s in pool.sounds} and "pop_ui_01.wav" not in {s.name for s in pool.sounds}


def test_each_file_is_classed_once(monkeypatch):
    calls = []
    real = sfx.classify
    monkeypatch.setattr(sfx, "classify", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    found = [sfx.Candidate(f"/Volumes/Media/{k}_{i:03d}.wav", f"{k}_{i:03d}.wav") for k in ("whoosh", "pop", "hit", "riser", "noise") for i in range(40)]
    kept, over = sfx._take_turns(found, 60)
    assert len(kept) == 60 and len(over) == 100 and len(calls) == len(found)


def test_classing_a_big_folder_is_quick():
    found = [sfx.Candidate(f"/Volumes/Media/Library/Pack {i % 90}/{('whoosh', 'pop', 'hit', 'riser', 'take')[i % 5]}_{i:05d}.wav", f"{('whoosh', 'pop', 'hit', 'riser', 'take')[i % 5]}_{i:05d}.wav",
                           folders=(f"Pack {i % 90}", "Library")) for i in range(20_000)]
    t = time.monotonic()
    kept, _ = sfx._take_turns(found, sfx.MAX_FILES)
    assert time.monotonic() - t < 2 and len(kept) == sfx.MAX_FILES


def test_the_class_words_still_mean_what_they_did():
    for text, want in [("Whoosh Soft", ("whoosh", 0)), ("big pass by", ("whoosh", 1)), ("fly-by 3", ("whoosh", 0)), ("UI Notification", ("pop", 0)),
                       ("then a build up", ("riser", 2)), ("Hit hit", ("hit", 0)), ("slam_dunk_impacts", ("hit", 0)), ("popcorn", None),
                       ("Cinematic Riser 4", ("riser", 1)), ("Big Impacts 2", ("hit", 1)), ("hit then whoosh", ("hit", 0)), ("WhooshSoft01", ("whoosh", 0))]:
        assert sfx.class_of_text(text) == want, text


def test_a_cut_with_no_transcript_has_no_words_for_an_accent_to_sit_under():
    tl, analysis, _ = _cut()
    for seg in analysis.clip("V01").segments:
        seg.words, seg.text = [], ""  # a build with no transcriber: one empty segment per clip
    assert accents._word_spans(tl, analysis) == []
    accents.place(tl, analysis, _pool(_sound("X01", "whoosh")), "soft")
    assert tl.accents and not any(a["under_words"] for a in tl.accents)
    for seg in analysis.clip("V01").segments:
        seg.text = "A line the transcript has, without its words timed."
    assert accents._word_spans(tl, analysis), "a line with text but no word times still sounds over its span"


def test_a_sound_shorter_than_a_frame_is_said_so_not_blamed_on_the_replay_rule():
    tl, analysis, _ = _cut()
    _graphic(tl, "G01", 30)
    tiny = _sound("X01", "pop", 0.02, hit=0.0)  # 20 ms: under one frame at any rate the cut runs at
    pool = _pool(tiny, *(_sound(f"X0{i}", "whoosh", name=f"{c}_whoosh.wav") for i, c in enumerate("abc", 2)))
    missed = accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third"})
    assert missed["replayed"] == 0 and missed["too_short"] == {"pop": 1} and missed["no_sound"] == {}
    text = "\n".join(report.sound_effects_section(tl, {"pool": pool, "amount": "soft", "missed": missed}))
    assert "shorter than a frame" in text and "10 s" not in text
    # A pop a frame long or more is used, with the tiny one left to it.
    pool = _pool(tiny, _sound("X03", "pop", 0.15, hit=0.0, name="pop_b.wav"))
    missed = accents.place(tl, analysis, pool, "soft", graphic_kinds={"G01": "lower_third"})
    assert missed["too_short"] == {} and [a["file"] for a in tl.accents if a["kind"] == "pop"] == ["pop_b.wav"]


def test_a_cut_with_no_event_for_the_pool_says_no_accents_not_zero_accents():
    tl, analysis, _ = _cut()
    tl.section_starts = tl.section_starts[:1]  # a single section, no graphics: nothing calls for one
    pool = _pool(_sound("X01", "whoosh"))
    missed = accents.place(tl, analysis, pool, "soft")
    text = "\n".join(report.sound_effects_section(tl, {"pool": pool, "amount": "soft", "missed": missed}))
    assert tl.accents == [] and "No accents" in text and "0 accents" not in text


def _two_graphics_attach(monkeypatch):
    """graphics.make that attaches a title card and a lower third, the way a render that worked does."""
    from roughcut import graphics
    from roughcut.analyze import Clip
    from roughcut.plan import GraphicSpec

    title = {"kind": "title_card", "segment": "V01.S003", "words": "heading up the mountain", "text": "Heading Out", "subtext": "", "seconds": 2.5, "direction": ""}
    lower = dict(title, kind="lower_third", segment="V01.S006", words="The kids", text="Maria Lopez")
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)

    def fake_make(designer, specs, tl, analysis, out_dir, **k):
        fd = tl.frame_duration
        placed, clips = [], []
        for i, g in enumerate((title, lower)):
            p = graphics.PlacedGraphic(id=f"G0{i + 1}", spec=GraphicSpec(**{f: g[f] for f in ("kind", "segment", "words", "text")}), index=i,
                                       start=tl.seg_ranges[g["segment"]][0], frames=75)
            media = MediaInfo(path=str(out_dir / "graphics" / f"{p.id}.mov"), name=f"{p.id}.mov", kind="video", duration=75 * fd, start=Fraction(0),
                              has_video=True, has_audio=False, width=tl.width, height=tl.height, frame_duration=fd, codec="prores")
            tl.connected.append(ConnectedClip(p.id, Fraction(0), p.frames, p.start, 2, kind="graphic", note=p.spec.text))
            placed.append(p)
            clips.append(Clip(id=p.id, media=media, role="graphic", transcript_source="graphic"))
        return clips, placed, []

    monkeypatch.setattr(graphics, "make", fake_make)
    return [title, lower]


def _every_sound_reads_as_missing(monkeypatch):
    """pipeline.missing_media's existence checks for sounds, all answering 'gone'; returns the paths it was asked about."""
    checked = []
    real_bounded = pipeline.bounded

    def gone(fn, *a, seconds, what=""):
        if fn is not os.path.exists:
            return real_bounded(fn, *a, seconds=seconds, what=what)
        checked.append(a[0])
        return False

    monkeypatch.setattr(pipeline, "bounded", gone)
    return checked


def test_the_report_and_missing_media_describe_the_cut_after_its_graphics(footage, sfx_folder, tmp_path, monkeypatch, cache):
    """Graphics attach after the first placement and change it (a hit beats a section whoosh; a lower third with no pop in
    the pool gets none): the paper edit's notes and the missing-media warning are about the cut that is written."""
    pool = tmp_path / "SFX"
    pool.mkdir()
    for name in ("Whooshes/whoosh_soft_01.wav", "impact_hit_01.wav"):
        shutil.copy(sfx_folder / name, pool / Path(name).name)
    graphics_asked = _two_graphics_attach(monkeypatch)
    provider = FakeProvider(dict(CANNED_PLAN, graphics=graphics_asked, sfx="soft"))
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    checked = _every_sound_reads_as_missing(monkeypatch)
    out = tmp_path / "out"
    result = pipeline.build(Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, sfx=(str(pool),), graphics=True))
    final = {a["file"] for a in result.timeline.accents}
    assert final == {"impact_hit_01.wav", "whoosh_soft_01.wav"} and not any(a["kind"] == "pop" for a in result.timeline.accents)
    assert "No pop in the pool" in result.report.read_text(), "the lower third that got none is said"
    missing = next(w for w in result.warnings if "are no longer where the footage was read" in w)
    assert missing.startswith("2 files") and "impact_hit_01.wav" in missing and "whoosh_soft_01.wav" in missing
    assert {Path(p).name for p in checked[-2:]} == final, "the sounds in the cut that is written are the ones checked last"
    # A re-cut does the same: the pool is saved with the plan, the folder can be gone, the notes follow the final placement.
    shutil.rmtree(pool)
    checked.clear()
    again = pipeline.rerender(out, Options(inputs=[]), overrides=set())
    assert {a["file"] for a in again.timeline.accents} == final
    assert "No pop in the pool" in again.report.read_text()
    assert {Path(p).name for p in checked[-2:]} == final
    assert next(w for w in again.warnings if "are no longer where the footage was read" in w).startswith("2 files")


def test_a_sounds_folder_reached_through_a_link_is_never_footage(tmp_path):
    real = tmp_path / "real"
    (real / "Trip" / "SFX").mkdir(parents=True)
    (real / "Trip" / "talk.mov").write_bytes(b"x")
    (real / "Trip" / "SFX" / "whoosh_soft_01.wav").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real)
    out = tmp_path / "out"
    spelled_apart = Options(inputs=[link / "Trip"], sfx=(str(real / "Trip" / "SFX"),))
    assert [f.name for f in pipeline.footage_files(spelled_apart, out)] == ["talk.mov"]
    as_a_file = Options(inputs=[link / "Trip"], sfx=(str(real / "Trip" / "SFX" / "whoosh_soft_01.wav"),))
    assert [f.name for f in pipeline.footage_files(as_a_file, out)] == ["talk.mov"]
    same = Options(inputs=[real / "Trip"], sfx=(str(real / "Trip" / "SFX"),))
    assert [f.name for f in pipeline.footage_files(same, out)] == ["talk.mov"]
    nothing = Options(inputs=[link / "Trip"])
    assert sorted(f.name for f in pipeline.footage_files(nothing, out)) == ["talk.mov", "whoosh_soft_01.wav"]


def test_finding_where_sounds_really_are_never_waits_on_a_stalled_share(tmp_path, monkeypatch):
    (tmp_path / "Trip" / "SFX").mkdir(parents=True)
    (tmp_path / "Trip" / "talk.mov").write_bytes(b"x")
    (tmp_path / "Trip" / "SFX" / "whoosh_soft_01.wav").write_bytes(b"x")

    def stalled(fn, *a, seconds, what=""):
        raise pipeline.Stalled(what)

    monkeypatch.setattr(pipeline, "bounded", stalled)
    files = pipeline.footage_files(Options(inputs=[tmp_path / "Trip"], sfx=(str(tmp_path / "Trip" / "SFX"),)), tmp_path / "out")
    assert [f.name for f in files] == ["talk.mov"], "by their spelling when the real location can't be found in time"


# ------------------------------------------------------------------ presets


def test_built_in_presets_have_no_sound_effects():
    for p in presets.BUILT_IN.values():
        assert p.sfx == () and p.sfx_from == ()
        assert not [f for f in presets.flags(p, "build") if "sfx" in f]


def test_your_preset_can_bring_sound_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    assert main(["preset", "save", "promos", "--brief", "Promo.", "--sfx", "/Volumes/Media/Sound Effects", "--sfx-from", "Whooshes"]) == 0
    p = presets.load("promos")
    assert p.sfx == ("/Volumes/Media/Sound Effects",) and p.sfx_from == ("Whooshes",)
    assert "--sfx=/Volumes/Media/Sound Effects" in presets.flags(p, "build") and "--preset-sfx-from=Whooshes" in presets.flags(p, "build")
    assert presets.flags(p, "render") == [] or not [f for f in presets.flags(p, "render") if "sfx" in f]
    capsys.readouterr()
    assert main(["preset", "list", "--json"]) == 0
    listed = {x["name"]: x for x in json.loads(capsys.readouterr().out)}
    assert listed["promos"]["sfx"] == ["/Volumes/Media/Sound Effects"] and listed["promos"]["sfx_from"] == ["Whooshes"]
    assert listed["venture-oc"]["sfx"] == [] and listed["venture-oc"]["sfx_from"] == []
    assert main(["preset", "save", "promos", "--no-sfx"]) == 0
    assert presets.load("promos").sfx == () and presets.load("promos").sfx_from == ()

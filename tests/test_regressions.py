"""Regression tests for bugs found in code review (one test per finding)."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction

import pytest
from conftest import NTSC, SPEECH, FakeProvider, make_clip

from roughcut.analyze import Analysis
from roughcut.plan import normalize
from roughcut.segments import Segment
from roughcut.timeline import STYLES, build_timeline, speech_pieces
from roughcut.transcribe import Word


def _clip_with_words(words, silences, seconds=20.0):
    clip = make_clip("V01", seconds=seconds, silences=silences)
    clip.segments = [Segment(id="V01.S001", clip_id="V01", start=words[0].start, end=words[-1].end, text=" ".join(w.text for w in words), words=words)]
    clip.role = "aroll"
    return clip


def test_silence_clamp_keeps_stretched_last_word():
    # Whisper stretched "park." across a pause; its real audio is 2.6-3.0.
    words = [Word(1.0, 1.3, "We"), Word(1.35, 1.6, "went"), Word(1.65, 1.8, "to"), Word(1.85, 2.0, "the"), Word(2.02, 3.0, "park.")]
    clip = _clip_with_words(words, [(2.05, 2.55), (3.0, 5.0)])
    piece = speech_pieces(clip.segments[0], clip, STYLES["medium"], 0)[-1]
    assert piece.b >= 3.0


def test_silence_clamp_keeps_stretched_first_word():
    words = [Word(1.0, 2.8, "So"), Word(2.85, 3.2, "anyway.")]
    clip = _clip_with_words(words, [(1.3, 2.6)])
    piece = speech_pieces(clip.segments[0], clip, STYLES["medium"], 0)[0]
    assert piece.a <= 1.0


def test_silence_clamp_still_trims_true_tails():
    words = [Word(1.0, 1.5, "Hello"), Word(1.55, 2.9, "there.")]  # real audio ends at 2.1
    clip = _clip_with_words(words, [(2.1, 6.0)])
    piece = speech_pieces(clip.segments[0], clip, STYLES["medium"], 0)[-1]
    assert piece.wb == 2.1


def test_one_word_sentence_with_inner_pause_survives():
    clip = _clip_with_words([Word(10.0, 12.0, "Yeah.")], [(10.4, 11.6)])
    assert speech_pieces(clip.segments[0], clip, STYLES["medium"], 0)


def test_overlapping_pick_does_not_cut_previous_words():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    last_word = a.segment("V01.S003").words[-1]  # "mountain." 7.75-7.98
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S003", "V01@7-9"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    first, second = tl.spine[0], tl.spine[1]
    first_end = first.src_in + first.frames * NTSC
    assert float(first_end) >= last_word.end - float(NTSC)
    assert second.src_in >= first_end


def test_no_frame_is_replayed_across_a_section_break():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize(
        {"sections": [{"name": "A", "items": ["V01.S003"], "broll": []}, {"name": "B", "items": ["V01.S004"], "broll": []}]}, a
    )
    for style in STYLES.values():
        tl = build_timeline(plan, a, style=style, name="t")
        first, second = tl.spine[0], tl.spine[1]
        assert second.src_in >= first.src_in + first.frames * NTSC


def test_music_maps_to_kept_sections():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("A01", kind="audio", seconds=60)])
    raw = {
        "sections": [
            {"name": "Intro", "items": ["V01.S001"], "broll": []},
            {"name": "Bogus", "items": ["V99.S001"], "broll": []},
            {"name": "Middle", "items": ["V01.S003"], "broll": []},
            {"name": "Finale", "items": ["V01.S005"], "broll": []},
        ],
        "music": [{"clip": "A01", "first_section": 2, "last_section": 2, "source_in": 0}],
    }
    plan = normalize(raw, a)
    assert [s.name for s in plan.sections] == ["Intro", "Middle", "Finale"]
    assert (plan.music[0].first_section, plan.music[0].last_section) == (1, 1)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    music = next(c for c in tl.connected if c.kind == "music")
    assert music.start == tl.section_frames[1]
    assert music.end == tl.section_frames[2]


def test_music_skips_sections_that_placed_nothing():
    clip = make_clip("V01", seconds=12, sentences=[(1, 2, "Hello there."), (3, 3.5, "Um."), (5, 6, "Third one."), (7, 8, "Fourth one.")])
    a = Analysis(clips=[clip, make_clip("A01", kind="audio", seconds=60)])
    raw = {
        "sections": [{"name": n, "items": [f"V01.S00{i + 1}"], "broll": []} for i, n in enumerate(["One", "Two", "Three", "Four"])],
        "music": [{"clip": "A01", "first_section": 2, "last_section": 2, "source_in": 0}],
    }
    plan = normalize(raw, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert 1 not in tl.section_frames  # "Um." is all filler
    music = next(c for c in tl.connected if c.kind == "music")
    assert (music.start, music.end) == (tl.section_frames[2], tl.section_frames[3])


@pytest.mark.parametrize("ref", ["V02@12-16.0.", "V02@12.5.-16", "V02@1:-3"])
def test_malformed_ranges_are_skipped_not_fatal(ref):
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=30)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", ref], "broll": []}]}, a)
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001"]
    assert any(ref in w for w in plan.warnings)


def test_late_broll_in_point_backs_off():
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=10)])
    raw = {"sections": [{"name": "x", "items": ["V01.S005"], "broll": [{"clip": "V02", "over": ["V01.S005"], "source_in": 9.5, "reason": ""}]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    cut = next(c for c in tl.connected if c.kind == "broll")
    f0, f1 = tl.seg_ranges["V01.S005"]
    assert cut.start <= f0 and cut.end >= f1 and cut.frames == tl.total_frames  # the whole line, snapped to the timeline's ends
    assert cut.src_in + cut.frames * NTSC <= a.clip("V02").media.duration


def test_control_characters_are_scrubbed_from_xml(tmp_path):
    lxml = pytest.importorskip("lxml.etree")
    from roughcut.fcpxml import build_fcpxml

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    plan = normalize({"sections": [{"name": "Open\x0bing", "items": ["V01.S001"], "broll": []}], "flags": [{"ref": "V01.S001", "note": "check\x0bthis"}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t\x01")
    path = tmp_path / "x.fcpxml"
    path.write_text(build_fcpxml([tl], a, event_name="e"))
    lxml.parse(str(path))  # raises on illegal characters


def test_bwf_timecode_sets_asset_start(tmp_path):
    from roughcut.media import probe

    wav = tmp_path / "field.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2:sample_rate=48000",
         "-write_bext", "1", "-metadata", "time_reference=1728000000", str(wav)],
        check=True,
    )
    info = probe(wav)
    assert info.start == 36000  # 10:00:00:00


def test_slightly_slow_phone_clip_keeps_its_real_length(monkeypatch, tmp_path):
    from roughcut import media

    fake = tmp_path / "IMG_0001.MOV"
    fake.write_bytes(b"")
    data = {
        "format": {"duration": "59.97"},
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "30000/1001", "avg_frame_rate": "1788/60",
             "nb_frames": "1788", "duration": "59.97"},
        ],
    }
    monkeypatch.setattr(media, "_ffprobe", lambda p: data)
    info = media.probe(fake)
    assert abs(float(info.duration) - 59.97) < 0.05


def test_rerender_after_fallback_keeps_rough_cut(footage, tmp_path, monkeypatch):
    from roughcut import pipeline
    from roughcut.pipeline import Options

    provider = FakeProvider({"title": "x", "sections": [{"name": "bad", "items": ["V42.S001"], "broll": []}]})
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    built = pipeline.build(Options(inputs=[footage], out=out, name="f", transcriber="none", cache_dir=tmp_path / "c"))
    assert built.fell_back and built.timeline.spine
    again = pipeline.rerender(out, Options(inputs=[]))
    assert again.timeline.spine
    assert len(list(out.glob("*.fcpxml"))) == 1  # same name as the build, not a second file


def test_telemetry_srt_is_not_a_transcript(tmp_path):
    from roughcut.transcribe import TranscriptionError, load_sidecar

    cues = []
    for i in range(40):
        t0, t1 = i * 0.033, (i + 1) * 0.033
        cues.append(f"{i + 1}\n00:00:{t0:06.3f} --> 00:00:{t1:06.3f}\nFrameCnt: {i}, DiffTime: 33ms\n[iso: 100] [shutter: 1/500.0] [fnum: 2.8]\n")
    srt = tmp_path / "DJI_0001.SRT"
    srt.write_text("\n".join(cues))
    with pytest.raises(TranscriptionError, match="telemetry"):
        load_sidecar(srt)


def test_rejected_key_stops_before_analysis(footage, tmp_path, monkeypatch, capsys):
    """The first real run lost 19 minutes and then silently cut a stringout."""
    import json

    from roughcut import pipeline
    from roughcut.cli import main
    from roughcut.providers import ProviderAuthError

    class RejectedKey(FakeProvider):
        def verify(self):
            raise ProviderAuthError("Anthropic rejected the key sk-ant-api03…abcd.")

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: RejectedKey())
    out = tmp_path / "out"
    code = main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--progress-json"])
    assert code == 1
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert events[-1] == {"event": "error", "message": "Anthropic rejected the key sk-ant-api03…abcd."}
    assert not any(e.get("stage") == "probe" for e in events)  # stopped before any analysis
    assert not (out / "analysis.json").exists()


def test_auth_failure_mid_run_is_fatal_and_stops_vision(footage, tmp_path, monkeypatch):
    from roughcut import pipeline
    from roughcut.pipeline import Options
    from roughcut.providers import ProviderAuthError

    class KeyRevokedMidRun(FakeProvider):
        def complete_json(self, *a, **k):
            self.calls.append(k.get("purpose"))
            raise ProviderAuthError("rejected")

    provider = KeyRevokedMidRun()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    with pytest.raises(ProviderAuthError):
        pipeline.build(Options(inputs=[footage], out=tmp_path / "o", transcriber="none", cache_dir=tmp_path / "c", workers=1))
    assert provider.calls == ["visual log"]  # no further batches, no planning call, no fallback cut
    assert not list((tmp_path / "o").glob("*.fcpxml"))


def test_repeated_warnings_collapse():
    from roughcut.report import collapse_warnings

    ws = [f"visual log failed for V{i:02d}, I{i:03d}: Anthropic rejected the key." for i in range(1, 47)] + ["something else"]
    out = collapse_warnings(ws)
    assert len(out) == 2
    assert "(x46: V01, I001" in out[0] and "and 84 more" in out[0]



# Typing exact model ids in Terminal was "a nightmare": menu + forgiving names.


def test_model_menu_defaults_and_choices(capsys):
    from roughcut.cli import choose_claude_models

    assert choose_claude_models(vision=True, ask=lambda _: "") == ("claude-opus-5-5", None)
    assert choose_claude_models(vision=True, ask=lambda _: "2") == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert choose_claude_models(vision=True, ask=lambda _: "3") == ("claude-sonnet-5-5", None)
    assert choose_claude_models(vision=False, ask=lambda _: "2") == ("claude-sonnet-5-5", None)
    answers = iter(["sonnet", "9", "3"])
    assert choose_claude_models(vision=True, ask=lambda _: next(answers)) == ("claude-sonnet-5-5", None)
    out = capsys.readouterr().out
    assert "Type a number from 1 to 3" in out and "[default]" in out

    def eof(_):
        raise EOFError

    assert choose_claude_models(vision=True, ask=eof) == ("claude-opus-5-5", None)


def _captured_options(monkeypatch, argv, interactive):
    import roughcut.cli as cli
    import roughcut.pipeline as pipeline

    seen = {}

    def fake_build(opts):
        seen["opts"] = opts
        raise RuntimeError("stop here")

    monkeypatch.setattr(pipeline, "build", fake_build)
    monkeypatch.setattr(cli, "_interactive", lambda: interactive)
    monkeypatch.setattr(cli, "choose_claude_models", lambda vision: seen.setdefault("menu", ("claude-sonnet-5-5", None)))
    assert cli.main(argv) == 1
    return seen


def test_build_asks_for_a_model_in_terminal(monkeypatch, tmp_path):
    seen = _captured_options(monkeypatch, ["build", str(tmp_path)], interactive=True)
    assert "menu" in seen and seen["opts"].model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "extra,interactive",
    [(["--model", "Claude-Sonnet.5.5"], True), (["--no-menu"], True), ([], False), (["--progress-json"], True), (["--provider", "none"], True)],
)
def test_build_skips_the_menu_when_it_should(monkeypatch, tmp_path, extra, interactive):
    seen = _captured_options(monkeypatch, ["build", str(tmp_path), *extra], interactive=interactive)
    assert "menu" not in seen
    if "--model" in extra:
        assert seen["opts"].model == "claude-sonnet-5-5"


# ---------------------------------------------------------------- names Venture Studio's Project history accepts


def _studio_accepts(name: str) -> bool:
    """Venture Studio's rule for a project name in an FCPXML it reviews: 1-200 characters once trimmed, no control characters."""
    import unicodedata

    v = name.strip()
    return 0 < len(v) <= 200 and not any(unicodedata.category(c) == "Cc" for c in v)


def _old_slug(text: str) -> str:
    """slug() as every earlier release wrote it: file names a folder already has."""
    import re

    s = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"\s+", " ", s) or "Roughcut"


def test_project_names_lose_control_characters_and_stay_under_200():
    from roughcut.pipeline import event_name, name_timelines
    from roughcut.timeline import Timeline
    from roughcut.versions import final_cut_name, fit_name, safe_name, short_slug, slug

    def timelines():
        rough = Timeline(name="", width=1920, height=1080, frame_duration=NTSC)
        stringout = Timeline(name="", width=1920, height=1080, frame_duration=NTSC)
        return rough, stringout

    rough, stringout = timelines()
    name_timelines(rough, stringout, "Launch\tnight\nOC\x7f\x85", 1, "medium", "heuristic")
    assert rough.name == "Launch night OC - Rough Cut v1 (medium, no AI)" and stringout.name == "Launch night OC - Stringout v1"
    long = "Venture OC member spotlight " * 12  # 336 characters
    rough, stringout = timelines()
    name_timelines(rough, stringout, long, 12, "tight", "anthropic:claude-opus-5-5")
    assert len(rough.name) == 200 and rough.name.endswith("… - Rough Cut v12 (tight, Opus)")
    assert len(stringout.name) <= 200 and stringout.name.endswith(" - Stringout v12")
    assert len(event_name(long)) <= 200 and all(_studio_accepts(n) for n in (rough.name, stringout.name, event_name(long)))
    # An odd model name from another provider can't push it past 200 either.
    rough, stringout = timelines()
    name_timelines(rough, stringout, "Promo", 2, "medium", "openai:" + "m" * 300)
    assert _studio_accepts(rough.name) and rough.name.startswith("Promo - Rough Cut v2 (medium, mmm")
    # Names that already fit are exactly what they were, in Final Cut and on disk.
    rough, stringout = timelines()
    name_timelines(rough, stringout, "OCVIBE: Café 🎉 / night", 3, "tight", "anthropic:claude-opus-5-5")
    assert rough.name == "OCVIBE - Café 🎉 - night - Rough Cut v3 (tight, Opus)"
    assert safe_name("A: B/C") == "A - B - C" and final_cut_name("x" * 200) == "x" * 200 and final_cut_name("Tab\there") == "Tab here"
    assert len(final_cut_name("y" * 250)) == 200 and fit_name("Promo", " v1") == "Promo v1"
    for name in ("OCVIBE", "Café 🎉 night", "  spaced\tout  ", "a" * 300, "é" * 200, "x: y/z?", "", "!!!"):
        assert slug(name) == _old_slug(name)
        assert short_slug(name) == slug(name) or len(short_slug(name).encode()) <= 255 - len(" v99999.fcpxml") - len("..12345678.tmp")


def test_every_project_name_in_the_fcpxml_is_one_studio_accepts(footage, tmp_path):
    import xml.etree.ElementTree as ET

    from roughcut.cli import main

    name = "Member\tspotlight\r\n" + "a very long name " * 15
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--name", name]) == 0
    (fcpxml,) = out.glob("*.fcpxml")
    root = ET.parse(fcpxml).getroot()
    projects = [p.get("name") for p in root.iter("project")]
    assert len(projects) == 2 and all(_studio_accepts(p) for p in projects), projects
    assert all(_studio_accepts(e.get("name")) for e in root.iter("event"))
    assert projects[0].endswith(" - Rough Cut v1 (medium, no AI)") and projects[1].endswith(" - Stringout v1")
    # A name too long for a file name used to stop the run when the cut was written; now only it is shortened...
    assert len(fcpxml.name.encode()) <= 255 and fcpxml.name.endswith(" v1.fcpxml")
    # ...and versions.json names the same project Final Cut shows.
    entry = json.loads((out / "versions.json").read_text())["versions"][-1]
    assert entry["project"] == projects[0]
    # The next version is shortened the same way: the folder's files keep one name.
    assert main(["render", str(out), "--style", "tight"]) == 0
    assert (out / fcpxml.name.replace(" v1.fcpxml", " v2.fcpxml")).is_file()


def test_a_name_that_fits_keeps_its_file_name(footage, tmp_path):
    from roughcut.cli import main

    # The longest name a disk takes once atomic_write's temp-name parts are added: written exactly as before.
    name = "a" * 231
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--name", name]) == 0
    assert (out / f"{name} v1.fcpxml").is_file()
    assert main(["render", str(out)]) == 0
    assert (out / f"{name} v2.fcpxml").is_file()
    assert [v["fcpxml"] for v in json.loads((out / "versions.json").read_text())["versions"]] == [f"{name} v1.fcpxml", f"{name} v2.fcpxml"]


# ---------------------------------------------------------------- the first --music song plays


def _two_songs(first_order, second_order):
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=[(1.0, 3.0, "Hello there.")], role="aroll")])
    for cid, order in (("A01", first_order), ("A02", second_order)):
        song = make_clip(cid, kind="audio", seconds=90, role="music")
        song.music_bed, song.music_order = True, order
        a.clips.append(song)
    return a


def test_the_first_music_file_given_plays_when_the_plan_cues_none():
    from roughcut.timeline import _music_cues

    # A01 is the older file (clips are numbered by date); A02 was given first.
    a = _two_songs(1, 0)
    plan = normalize({"title": "t", "sections": [{"name": "A", "items": ["V01.S001"]}]}, a)
    notes: list[str] = []
    (cue,) = _music_cues(plan, a, True, notes)
    assert cue.clip_id == "A02"
    assert notes == ["Music: A02.wav (--music) runs under the whole cut; A01.wav not used."]
    # An analysis saved before the order was kept plays what it played then: the earliest-dated.
    old = _two_songs(None, None)
    assert [c.clip_id for c in _music_cues(normalize({"title": "t", "sections": [{"name": "A", "items": ["V01.S001"]}]}, old), old, True, [])] == ["A01"]
    restored = Analysis.from_json(json.loads(json.dumps(a.to_json())))
    assert [c.music_order for c in restored.clips] == [None, 1, 0]


def _song_in_cut(fcpxml) -> str:
    import xml.etree.ElementTree as ET

    rough = next(p for p in ET.parse(fcpxml).getroot().iter("project") if "Rough Cut" in p.get("name"))
    (music,) = [el for el in rough.iter("asset-clip") if el.get("lane") == "-1"]
    return music.get("name")


def test_the_first_music_file_given_plays_end_to_end(footage, tmp_path):
    from conftest import ffmpeg

    from roughcut.cli import main
    from roughcut.pipeline import analysis_digest

    songs = tmp_path / "Downloads"
    songs.mkdir()
    for name, created in (("Older Song.m4a", "2026-01-01T09:00:00Z"), ("Favourite.m4a", "2026-06-01T09:00:00Z")):
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=30:sample_rate=48000", "-c:a", "aac", "-metadata", f"creation_time={created}", str(songs / name))
    out = tmp_path / "out"
    code = main([
        "build", str(footage), "-o", str(out), "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"),
        "--music", str(songs / "Favourite.m4a"), "--music", str(songs / "Older Song.m4a"), "--name", "footage",
    ])
    assert code == 0
    assert _song_in_cut(out / "footage v1.fcpxml") == "Favourite"
    assert "Favourite.m4a (--music) runs under the whole cut; Older Song.m4a not used." in (out / "edit_report.md").read_text()

    # A folder an earlier release built: its analysis.json has no music_order. Its plan still matches
    # (the digest is clip IDs and files only), render re-cuts it, and it plays the song it played then.
    saved = json.loads((out / "analysis.json").read_text())
    digest = json.loads((out / "plan.json").read_text())["analysis_digest"]
    for clip in saved["clips"]:
        clip.pop("music_order", None)
    (out / "analysis.json").write_text(json.dumps(saved, indent=1))
    assert analysis_digest(Analysis.from_json(saved)) == digest
    assert main(["render", str(out)]) == 0
    assert _song_in_cut(out / "footage v2.fcpxml") == "Older Song"

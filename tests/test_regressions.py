"""Regression tests for bugs found in code review (one test per finding)."""

from __future__ import annotations

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
    assert cut.frames == f1 - f0
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

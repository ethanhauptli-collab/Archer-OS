"""Editorial: B-roll on its words, one use per line, filler and pause rules, length checks."""

from __future__ import annotations

from fractions import Fraction

import pytest
from conftest import make_clip
from test_narration import narration_project

from roughcut.analyze import Analysis
from roughcut.phrases import frame_at
from roughcut.plan import PLAN_SCHEMA, normalize
from roughcut.timeline import STYLES, build_timeline

# ------------------------------------------------------------ N3: B-roll on its words


def _word_frame(tl, analysis, seg_id, text):
    seg = analysis.segment(seg_id)
    w = next(w for w in seg.words if w.text.strip(".,").lower() == text.lower())
    return frame_at(tl, seg.clip_id, w.start)


def _honda_plan(words=("", "Honda Center", "two hotels", "parks")):
    clips = ["I01", "I02", "V01", "I04"]
    return {
        "sections": [
            {
                "name": "Plan",
                "items": ["A01.S003"],
                "broll": [{"clip": c, "over": ["A01.S003"], "words": w, "source_in": -1, "reason": ""} for c, w in zip(clips, words)],
            }
        ]
    }


def test_broll_comes_in_on_the_words_it_illustrates():
    a = narration_project()
    plan = normalize(_honda_plan(), a)
    assert [b.words for b in plan.sections[0].broll] == ["", "Honda Center", "two hotels", "parks"]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t", fill=False)
    shots = {c.clip_id: c for c in tl.connected}
    line_start, line_end = tl.seg_ranges["A01.S003"]
    assert shots["I01"].start == line_start  # the first shot starts with the line
    assert abs(shots["I02"].start - _word_frame(tl, a, "A01.S003", "Honda")) <= 2
    assert abs(shots["V01"].start - _word_frame(tl, a, "A01.S003", "two")) <= 2
    assert abs(shots["I04"].start - _word_frame(tl, a, "A01.S003", "parks")) <= 2
    assert shots["I01"].end == shots["I02"].start  # an anchored shot ends the one before it
    assert shots["V01"].end == shots["I04"].start
    assert max(c.end for c in tl.connected) <= line_end
    assert not [w for w in tl.warnings if "B-roll" in w]


def test_unfound_words_fall_back_to_the_even_split():
    a = narration_project()
    even = build_timeline(normalize(_honda_plan(("", "", "", "")), a), a, style=STYLES["medium"], name="t", fill=False)
    tl = build_timeline(normalize(_honda_plan(("", "Disneyland", "", "")), a), a, style=STYLES["medium"], name="t", fill=False)
    assert [(c.clip_id, c.start, c.frames) for c in tl.connected] == [(c.clip_id, c.start, c.frames) for c in even.connected]
    assert any('"Disneyland" isn\'t heard' in w for w in tl.warnings)


def test_unanchored_shots_share_the_time_up_to_the_next_anchor():
    a = narration_project()
    tl = build_timeline(normalize(_honda_plan(("", "", "", "parks")), a), a, style=STYLES["medium"], name="t", fill=False)
    shots = {c.clip_id: c for c in tl.connected}
    parks = _word_frame(tl, a, "A01.S003", "parks")
    assert abs(shots["I04"].start - parks) <= 2
    # I01, I02 and V01 split the time before "parks"; the stills are capped, the video fills its share.
    assert shots["I01"].start == tl.seg_ranges["A01.S003"][0]
    assert shots["V01"].end == shots["I04"].start


def test_a_plan_saved_before_words_existed_still_renders(tmp_path):
    from test_end_to_end import check_invariants, validate_dtd

    from roughcut.fcpxml import build_fcpxml

    assert "words" in PLAN_SCHEMA["properties"]["sections"]["items"]["properties"]["broll"]["items"]["required"]
    a = narration_project()
    old = _honda_plan()
    for b in old["sections"][0]["broll"]:
        del b["words"]
    plan = normalize(old, a)
    assert all(b.words == "" for b in plan.sections[0].broll)
    assert plan.to_json()["sections"][0]["broll"][0]["words"] == ""
    for raw in (old, _honda_plan()):
        tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
        path = tmp_path / "x.fcpxml"
        path.write_text(build_fcpxml([tl], a, event_name="e"))
        validate_dtd(path)
        check_invariants(path)


def test_words_said_twice_anchor_on_the_occurrence_after_the_previous_shot():
    sentences = [(1.0, 13.0, "Parks matter here because the city plans more parks near the river.")]
    a = Analysis(clips=[make_clip("A01", kind="audio", seconds=15, sentences=sentences, role="voiceover"), make_clip("V01", seconds=60), make_clip("V02", seconds=60)])
    raw = {"sections": [{"name": "x", "items": ["A01.S001"], "broll": [
        {"clip": "V01", "over": ["A01.S001"], "words": "", "source_in": -1, "reason": ""},
        {"clip": "V02", "over": ["A01.S001"], "words": "parks", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    seg = a.segment("A01.S001")
    second = [w for w in seg.words if w.text.strip(".,").lower() == "parks"][1]
    assert abs(v02.start - frame_at(tl, "A01", second.start)) <= 2


def test_lead_is_about_two_frames():
    a = narration_project()
    tl = build_timeline(normalize(_honda_plan(), a), a, style=STYLES["medium"], name="t", fill=False)
    i04 = next(c for c in tl.connected if c.clip_id == "I04")
    lead = _word_frame(tl, a, "A01.S003", "parks") - i04.start
    assert 1 <= lead <= 3
    assert Fraction(lead) * tl.frame_duration < Fraction(1, 8)


def _no_flashes_or_repeated_stills(tl, a):
    fd = tl.frame_duration
    tiny = round(Fraction(1, 2) / fd)
    assert all(c.frames >= tiny for c in tl.connected), [(c.clip_id, c.start, c.frames) for c in tl.connected]
    stills = [c.clip_id for c in tl.connected if a.clip(c.clip_id).media.kind == "image"]
    assert len(stills) == len(set(stills)), stills
    shots = sorted(tl.connected, key=lambda c: c.start)
    assert not [(p.clip_id, q.start) for p, q in zip(shots, shots[1:]) if p.clip_id == q.clip_id and p.end == q.start and q.note == "auto-fill"]


def test_fill_never_flashes_a_few_frames_or_repeats_a_still():
    a = narration_project()
    for words in (("", "Honda Center", "two hotels", "parks"), ("", "", "", "")):
        tl = build_timeline(normalize(_honda_plan(words), a), a, style=STYLES["medium"], name="t")
        _no_flashes_or_repeated_stills(tl, a)
        assert min(c.start for c in tl.connected) == 0  # the first shot starts with the narration's padding
    # A capped still at the start moves up rather than leaving a sliver for a flash shot.
    raw = {"sections": [{"name": "Plan", "items": ["A01.S003"], "broll": [{"clip": "I02", "over": ["A01.S003"], "words": "", "source_in": -1, "reason": ""}]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t")
    _no_flashes_or_repeated_stills(tl, a)
    first = min(tl.connected, key=lambda c: c.start)
    assert first.clip_id == "I02" and first.start == 0


# ------------------------------------------------------------ T9: one use per line


def _reads():
    from test_narration import VO

    a01 = make_clip("A01", kind="audio", seconds=44, sentences=VO, role="voiceover")
    # The second read says the long line a little differently, with an "um".
    second = VO[:2] + [(12.5, 36.0, "Around Honda Center the plan adds homes, um, offices, two hotels, restaurants and twenty acres of parks.")] + VO[3:]
    a02 = make_clip("A02", kind="audio", seconds=44, sentences=second, role="voiceover")
    a02.take_of, a02.same_words = "A01", 0.97
    return a01, a02


def test_the_same_line_from_another_read_plays_once():
    a01, a02 = _reads()
    a = Analysis(clips=[a01, a02, make_clip("V01", seconds=60)])
    raw = {
        "sections": [
            {"name": "Open", "items": ["A01.S003", "A01.S004"], "broll": []},
            {"name": "Again", "items": ["A02.S003", "A02.S001"], "broll": [
                {"clip": "V01", "over": ["A02.S003", "A02.S001"], "words": "", "source_in": -1, "reason": ""},
            ]},
        ],
        "alternates": [{"chosen": "A01.S004", "others": ["A02.S004"], "note": "cleaner"}],
        "flags": [{"ref": "A02.S003", "note": "check the read"}],
    }
    plan = normalize(raw, a)
    refs = [i.ref for s in plan.sections for i in s.items]
    assert refs == ["A01.S003", "A01.S004", "A02.S001"]
    assert any("A02.S003 says the same line as A01.S003" in w for w in plan.warnings)
    assert {"chosen": "A01.S003", "others": ["A02.S003"], "note": "the same line from another read or export"} in plan.alternates
    assert plan.flags[0]["ref"] == "A01.S003"  # follows the line to the use that was kept
    assert plan.sections[1].broll[0].over == ["A02.S001"]
    # A short line in two reads of one script is the same line too.
    assert [i.ref for i in normalize({"sections": [{"name": "x", "items": ["A01.S004", "A02.S004"]}]}, a).sections[0].items] == ["A01.S004"]


def test_a_shared_line_across_unrelated_clips_is_dropped_only_when_long():
    thanks = [(1.0, 2.0, "Thank you."), (3.0, 8.0, "It opens this summer in Anaheim near the arena.")]
    v01 = make_clip("V01", seconds=10, sentences=thanks)
    v02 = make_clip("V02", seconds=10, sentences=thanks)  # a different cutdown of the same promo
    a = Analysis(clips=[v01, v02])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V02.S001", "V01.S002", "V02.S002"]}]}, a)
    assert [i.ref for i in plan.sections[0].items] == ["V01.S001", "V02.S001", "V01.S002"]
    assert plan.alternates == [{"chosen": "V01.S002", "others": ["V02.S002"], "note": "the same line from another read or export"}]
    # Retakes inside one recording stay the planner's call.
    one = Analysis(clips=[make_clip("V01", seconds=20, sentences=thanks + [(10.0, 15.0, "It opens this summer in Anaheim near the arena.")])])
    assert len(normalize({"sections": [{"name": "x", "items": ["V01.S002", "V01.S003"]}]}, one).sections[0].items) == 2


# ------------------------------------------------------------ T10: fillers and pacing


@pytest.mark.parametrize("name", ["tight", "medium", "loose"])
def test_keep_fillers_keeps_the_rest_of_the_style(name):
    from dataclasses import asdict

    from roughcut.pipeline import Options, style_for

    base = STYLES[name]
    kept = style_for(Options(inputs=[], style=name, keep_fillers=True))
    assert kept.remove_fillers is False
    assert {k: v for k, v in asdict(kept).items() if k != "remove_fillers"} == {k: v for k, v in asdict(base).items() if k != "remove_fillers"}
    assert style_for(Options(inputs=[], style=name)) == base


def _short_uh(kind):
    from roughcut.segments import build_segments
    from roughcut.transcribe import Word

    clip = make_clip("A01" if kind == "audio" else "V01", kind=kind, seconds=6, role="voiceover" if kind == "audio" else "aroll")
    words = [Word(1.0, 1.4, "We"), Word(1.4, 1.8, "went"), Word(1.8, 2.0, "uh,"), Word(2.0, 2.4, "to"), Word(2.4, 2.9, "the"), Word(2.9, 3.4, "park.")]
    clip.segments = build_segments(clip.id, words)
    return clip


def test_a_short_uh_is_cut_from_narration_and_left_on_camera():
    from roughcut.plan import heuristic_plan
    from roughcut.timeline import speech_pieces

    vo = _short_uh("audio")
    pieces = speech_pieces(vo.segments[0], vo, STYLES["medium"], 0)
    assert len(pieces) == 2 and pieces[0].b <= 1.8 + 1e-9 and pieces[1].a >= 2.0 - 1e-9
    a = Analysis(clips=[vo, make_clip("V09", seconds=30)])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    kept = [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]
    assert not [k for k in kept if k[0] < 1.95 and k[1] > 1.85], kept  # no part of the uh plays

    cam = _short_uh("video")
    assert len(speech_pieces(cam.segments[0], cam, STYLES["medium"], 0)) == 1  # a 0.2 s cut would jump the picture
    a = Analysis(clips=[cam])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert any(n.startswith("Left 1 short um/uh on camera") for n in tl.notes)
    keep = build_timeline(heuristic_plan(a), a, style=STYLES["medium"].__class__(0.1, 0.2, 0.6, remove_fillers=False), name="t")
    assert not any("um/uh" in n for n in keep.notes)


# ------------------------------------------------------------ T14: length and review


@pytest.mark.parametrize("style", ["tight", "medium", "loose"])
def test_the_kept_length_shown_to_the_planner_is_what_gets_built(style):
    from conftest import SPEECH

    from roughcut.prompts import render_media
    from roughcut.timeline import kept_length

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH)])
    clip = a.clip("V01")
    for seg in clip.segments:
        kept, cuts = kept_length(seg, clip, a, STYLES[style])
        tl = build_timeline(normalize({"sections": [{"name": "x", "items": [seg.id]}]}, a), a, style=STYLES[style], name="t")
        assert abs(tl.seconds - kept) <= 2 * len(tl.spine) * float(tl.frame_duration), seg.id
        assert cuts == len(tl.spine) - 1, seg.id
    media = render_media(a, STYLES[style])
    uh = clip.segments[5]  # "And honestly, uh, I needed it too.": the uh is cut
    kept, _ = kept_length(uh, clip, a, STYLES[style])
    assert f"{uh.id} [0:15.4-0:18.0 | 2.6s → {kept:.1f}s, 1 edit] And honestly" in media
    assert render_media(a, STYLES[style]) == media  # deterministic, so the prompt cache still hits


def test_review_finds_an_uncovered_jump_cut_and_marks_it():
    from conftest import SPEECH

    from roughcut.plan import heuristic_plan

    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), make_clip("V02", seconds=10)])
    raw = {"sections": [{"name": "Open", "items": ["V01.S003", "V01.S005"]}, {"name": "End", "items": ["V01.S006"]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t", target_seconds=10)
    rv = tl.review
    assert rv.target == 10 and "target 0:10" in rv.length_line() and "under" in rv.length_line()
    assert [s for s, _ in rv.sections] == ["Open", "End"] and sum(sec for _, sec in rv.sections) == pytest.approx(tl.seconds)
    lines = [(j.before, j.after) for j in rv.jump_cuts]
    assert (["V01.S003"], ["V01.S005"]) in lines and (["V01.S005"], ["V01.S006"]) in lines
    jump = next(j for j in rv.jump_cuts if j.before == ["V01.S003"])
    todo = [m for m in tl.markers if m.kind == "todo" and m.value.startswith("Jump cut")]
    assert jump.frame in [m.frame for m in todo] and "V01.S003" in todo[0].note
    assert any("Jump cuts on camera with no B-roll over them: " in x for x in rv.lines(tl.frame_duration))

    # A cutaway over the next line hides the jump: it starts on the edit, not after the padding.
    raw["sections"][0]["broll"] = [{"clip": "V02", "over": ["V01.S005"], "words": "", "source_in": -1, "reason": ""}]
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    assert (["V01.S003"], ["V01.S005"]) not in [(j.before, j.after) for j in tl.review.jump_cuts]
    v02 = next(c for c in tl.connected if c.clip_id == "V02")
    edit = next(sc.offset for sc in tl.spine if "V01.S005" in sc.seg_ids)
    assert v02.start == edit

    # The stringout is all jump cuts by design: reviewed, not marked.
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    assert tl.review.jump_cuts and not [m for m in tl.markers if m.value.startswith("Jump cut")]


def test_review_counts_fill_and_repeated_stills():
    a = narration_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S002", "I02@0-3", "A01.S003"], "broll": [
        {"clip": "I02", "over": ["A01.S002"], "words": "", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    assert tl.review.repeated_stills == {"I02": 2}
    assert tl.review.filled_shots > 0 and tl.review.filled_seconds > 10


def test_report_and_render_say_how_long_against_the_target(footage, tmp_path, monkeypatch, capsys):
    from conftest import FakeProvider

    from roughcut import pipeline
    from roughcut.cli import main

    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "-t", "1m", "--no-menu", "--name", "f"]) == 0
    printed = capsys.readouterr().out
    assert "Rough cut: 0:" in printed and ", target 1:00 (" in printed
    report = (out / "edit_report.md").read_text()
    assert "target 1:00" in report and "## Cut check" in report
    assert main(["render", str(out), "--style", "tight"]) == 0
    assert ", target 1:00 (" in capsys.readouterr().out
    assert "target 1:00" in (out / "edit_report.md").read_text()


# ------------------------------------------------------------ T6: finished promos over a music bed

# One sentence with a 0.8 s breath in the middle (longer than medium's 0.6 s), then another.
PROMO = [(1.0, 3.0, "So this is the new arena district"), (3.8, 5.6, "coming to Anaheim next year."), (6.4, 9.0, "It opens with restaurants and music.")]
BEDS = {
    "music": "aevalsrc=exprs='0.02*(sin(2*PI*262*t)+sin(2*PI*330*t)+sin(2*PI*392*t)+0.5*sin(2*PI*(523+262*floor(mod(t*2,2)))*t))':s=48000:d=11",
    "noise": "anoisesrc=c=pink:a=0.06:d=11:r=48000",
}


@pytest.fixture(scope="module")
def promos(tmp_path_factory):
    import json
    import shutil
    import subprocess

    from conftest import spread_words

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("promos")
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in PROMO)
    speech = f"0.5*sin(2*PI*180*t)*(0.6+0.4*sin(2*PI*5*t))*({gates})"
    words = [w for s, e, text in PROMO for w in spread_words(s, e, text)]
    for name, bed in BEDS.items():
        folder = root / name
        folder.mkdir()
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=4", "-f", "lavfi", "-i", "smptebars=size=320x180:rate=25:duration=4",
             "-f", "lavfi", "-i", "color=c=navy:size=320x180:rate=25:duration=3",
             "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d=11", "-f", "lavfi", "-i", bed,
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a][4:a]amix=inputs=2:normalize=0[a]",
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(folder / "promo.mov")],
            check=True,
        )
        (folder / "promo.json").write_text(json.dumps({"words": words}))
    return root


def _analyse(folder, cache_dir):
    from roughcut.analyze import Cache, analyze

    return analyze([folder / "promo.mov"], transcriber=None, cache=Cache(cache_dir))


def _spans(tl):
    return [(float(s.src_in), float(s.src_in + s.frames * tl.frame_duration)) for s in tl.spine]


def test_a_promo_over_music_keeps_its_breaths_and_noise_does_not(promos, tmp_path):
    from roughcut.plan import heuristic_plan
    from roughcut.prompts import render_media

    music = _analyse(promos / "music", tmp_path / "cache")
    clip = music.clips[0]
    assert clip.silence_db > -35 and clip.bed == "music" and clip.cuts_per_min >= 4  # two picture cuts in 11 s
    assert any("speech over a music bed in a finished edit" in n for n in music.notes)
    assert "a finished edit, speech over a music bed: take consecutive lines together" in render_media(music)
    tl = build_timeline(heuristic_plan(music), music, style=STYLES["medium"], name="t")
    assert any(a < 3.0 and b > 3.8 for a, b in _spans(tl)), _spans(tl)  # the 0.8 s breath plays

    noise = _analyse(promos / "noise", tmp_path / "cache")
    assert noise.clips[0].silence_db > -35 and noise.clips[0].bed == "noise"
    assert "music bed" not in render_media(noise)
    tl = build_timeline(heuristic_plan(noise), noise, style=STYLES["medium"], name="t")
    assert not any(a < 3.2 and b > 3.6 for a, b in _spans(tl)), _spans(tl)  # dead air over room noise goes

    # Cached: a second analysis doesn't measure again, and the result survives analysis.json.
    again = Analysis.from_json(_analyse(promos / "music", tmp_path / "cache").to_json())
    assert (again.clips[0].bed, again.clips[0].cuts_per_min) == ("music", clip.cuts_per_min)


def test_jumps_in_a_music_bed_get_markers_and_a_clashing_cue_a_warning():
    from conftest import SPEECH

    v01 = make_clip("V01", seconds=20, sentences=SPEECH)
    v01.bed, v01.cuts_per_min = "music", 12.0
    a = Analysis(clips=[v01, make_clip("A01", kind="audio", seconds=60)])
    raw = {"sections": [{"name": "x", "items": ["V01.S005", "V01.S003", "V01.S004"]}], "music": [{"clip": "A01", "first_section": 0, "last_section": 0, "source_in": 0}]}
    tl = build_timeline(normalize(raw, a, source="fake"), a, style=STYLES["medium"], name="t")
    todo = [m for m in tl.markers if m.kind == "todo"]
    assert [m.value for m in todo] == ["Music jumps: smooth this edit"]  # S005 -> S003; S003+S004 play straight through
    assert len({m.frame for m in todo}) == len(todo)  # not also marked as a jump cut
    assert any("1 edit jumps the music" in n for n in tl.notes)
    assert any("music A01 plays over V01" in w for w in tl.warnings)


# ------------------------------------------------------------ T21: moments in long B-roll

MOMENTS = [
    {"at": 6.0, "note": "wide shot of the boardwalk, people walking", "usable": True},
    {"at": 30.0, "note": "close-up of a coffee cup on a cafe table", "usable": True},
    {"at": 45.0, "note": "shaky pan, lens flare", "usable": False},
    {"at": 54.0, "note": "kids riding bikes past the pier", "usable": True},
]


def _walk_project():
    sentences = [(0.5, 5.0, "We started the morning down by the pier."), (5.5, 10.0, "Then we stopped for coffee at a little cafe.")]
    walk = make_clip("V02", seconds=60)
    walk.visual = {"description": "Walking tour of the beach town", "tags": ["walk", "beach"], "shot": "pov", "quality": "good", "moments": [dict(m) for m in MOMENTS]}
    return Analysis(clips=[make_clip("A01", kind="audio", seconds=11, sentences=sentences, role="voiceover"), walk])


def test_a_cutaway_left_to_the_software_starts_on_the_moment_that_fits():
    a = _walk_project()
    raw = {"sections": [{"name": "x", "items": ["A01.S001", "A01.S002"], "broll": [
        {"clip": "V02", "over": ["A01.S002"], "words": "", "source_in": -1, "reason": ""},
    ]}]}
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    shot = next(c for c in tl.connected if c.clip_id == "V02")
    assert float(shot.src_in) == pytest.approx(30.0, abs=0.05)  # "coffee ... cafe", not 15% in
    raw["sections"][0]["broll"][0]["source_in"] = 12
    tl = build_timeline(normalize(raw, a), a, style=STYLES["medium"], name="t", fill=False)
    assert float(next(c for c in tl.connected if c.clip_id == "V02").src_in) == pytest.approx(12, abs=0.05)  # the planner's pick stands


def test_fill_starts_long_clips_on_their_moments_and_never_on_an_unusable_one():
    from roughcut.plan import heuristic_plan

    a = _walk_project()
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    starts = sorted(round(float(c.src_in)) for c in tl.connected if c.clip_id == "V02")
    assert starts and set(starts) <= {6, 30, 54}, starts
    assert 30 in starts  # the coffee line gets the cafe


def test_moments_are_logged_once_with_labelled_frames_and_stills_stay_cached(tmp_path):
    import shutil
    import subprocess

    from conftest import FakeProvider

    from roughcut.analyze import Cache, Clip
    from roughcut.media import probe
    from roughcut.prompts import render_media
    from roughcut.vision import describe_clips, moment_times

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")

    class Logger(FakeProvider):
        def complete_json(self, system, parts, schema, *, schema_name, purpose, effort=None, max_tokens=64000):
            if schema_name != "clip_moments":
                return super().complete_json(system, parts, schema, schema_name=schema_name, purpose=purpose)
            self.calls.append({"schema_name": schema_name, "parts": parts})
            labels = [p.text for p in parts if hasattr(p, "text") and " @" in p.text]
            clip_id = labels[0].split()[0]
            times = [float(lbl.rsplit("(", 1)[1].rstrip("s)")) for lbl in labels]
            return {"clips": [{"id": clip_id, "moments": [{"at": t + 0.4, "note": f"frame at {t:g}", "usable": i != 1} for i, t in enumerate(times)]}]}

    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp_path / "walk.mp4"))
    ffmpeg("-f", "lavfi", "-i", "smptebars=size=160x90:rate=25:duration=6", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp_path / "short.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x90:duration=1", "-frames:v", "1", str(tmp_path / "photo.png"))

    def project():
        clips = [Clip(id="V01", media=probe(tmp_path / "walk.mp4"), role="broll"), Clip(id="V02", media=probe(tmp_path / "short.mp4"), role="broll"),
                 Clip(id="I01", media=probe(tmp_path / "photo.png"), role="still")]
        return Analysis(clips=clips)

    cache = Cache(tmp_path / "cache")
    a, provider = project(), Logger()
    assert describe_clips(a, provider, cache, workers=1) == []
    assert [c["schema_name"] for c in provider.calls] == ["clip_log", "clip_moments"]
    sampled = moment_times(a.clip("V01"))
    assert len(sampled) == 3 and not moment_times(a.clip("V02")) and not moment_times(a.clip("I01"))
    parts = provider.calls[1]["parts"]
    kinds = ["label" if getattr(p, "text", "").startswith("V01 @") else "image" if hasattr(p, "data") else "text" for p in parts]
    assert kinds == ["text"] + ["label", "image"] * 3 + ["text"]  # each frame says when it is
    moments = a.clip("V01").visual["moments"]
    assert [m["at"] for m in moments] == sampled  # snapped to the frames it was shown
    assert [m["usable"] for m in moments] == [True, False, True]
    assert a.clip("V01").visual["description"]  # the description is kept alongside
    assert f"Moments: {sampled[0]:g}s frame at" in render_media(a) and "(not usable)" in render_media(a)

    again, provider = project(), Logger()
    describe_clips(again, provider, cache, workers=1)
    assert provider.calls == []  # stills, descriptions and moments all come from the cache
    assert again.clip("V01").visual["moments"] == moments and again.clip("I01").visual["description"]


HOME = [(1.0, 4.0, "We drove all the way back home from the coast."), (5.2, 7.5, "And then we unpacked the car and the bikes."), (8.65, 10.5, "It was late by then for everyone.")]
HOME_SILENCES = [(0.0, 1.0), (4.0, 4.45), (4.75, 5.2), (7.5, 7.95), (8.2, 8.65), (10.5, 12.0)]
HOME_BREATH = (4.45, 4.75)  # quiet; the sound between 7.95 and 8.2 is loud, so it's not a breath


def _home_clip(kind, breaths):
    from conftest import spread_words

    from roughcut.segments import build_segments
    from roughcut.transcribe import Word

    words = [Word(**w) for s, e, text in HOME for w in spread_words(s, e, text)]
    for x, y in zip(words, words[1:]):  # Whisper: each word runs up to the next
        x.end = y.start
    clip = make_clip("V01" if kind == "video" else "A01", kind=kind, seconds=12, silences=list(HOME_SILENCES), role="aroll" if kind == "video" else "voiceover")
    clip.segments = build_segments(clip.id, words, gap_split=5.0)  # one long segment, like run-on speech
    clip.breaths = list(breaths)
    return clip


def _home_spans(kind, breaths, style):
    from roughcut.plan import heuristic_plan

    a = Analysis(clips=[_home_clip(kind, breaths)])
    return _spans(build_timeline(heuristic_plan(a), a, style=STYLES[style], name="t"))


def _plays(spans, t):
    return any(a <= t < b for a, b in spans)


def test_a_pause_split_by_a_breath_is_one_pause_on_camera():
    # Each half (0.45 s) is under medium's 0.6 s, so without the breath marked the whole 1.2 s pause stays.
    assert _plays(_home_spans("video", [], "medium"), 4.6)
    spans = _home_spans("video", [HOME_BREATH], "medium")
    assert not any(_plays(spans, t) for t in (4.3, 4.6, 4.9))  # one edit, breath and all
    assert _plays(spans, 3.9) and _plays(spans, 5.25)
    assert _plays(spans, 8.0)  # the loud sound between two pauses is never cut
    # Tight cut each half and left the breath as a half-second flash; now it's one edit.
    flashes = [(a, b) for a, b in _home_spans("video", [], "tight") if b - a < 0.6 and a < 4.6 < b]
    assert flashes and not _plays(_home_spans("video", [HOME_BREATH], "tight"), 4.6)


def test_narration_keeps_the_breath_and_loses_the_silence_around_it():
    spans = _home_spans("audio", [HOME_BREATH], "medium")
    assert _plays(spans, 4.6)  # sound is never removed from narration
    assert not _plays(spans, 4.3) and not _plays(spans, 4.95)
    assert _plays(spans, 3.9) and _plays(spans, 5.25)


def test_a_silence_holding_a_word_is_never_joined_across():
    from roughcut.timeline import _pause_clusters

    clip = _home_clip("video", [HOME_BREATH])
    assert [(s, e) for s, e, _ in _pause_clusters(clip)][1] == (4.0, 5.2)
    clip.silences[2] = (4.75, 5.6)  # now holds a whole word (quiet speech under a raised level)
    assert (4.0, 5.6) not in [(s, e) for s, e, _ in _pause_clusters(clip)]


@pytest.fixture(scope="module")
def breath_footage(tmp_path_factory):
    import json
    import shutil
    import subprocess

    from conftest import spread_words

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("breath")
    speech = "+".join(f"0.5*sin(2*PI*200*t)*between(t,{s},{e})" for s, e, _ in HOME)
    expr = f"{speech}+0.06*sin(2*PI*400*t)*between(t,4.45,4.75)+0.3*sin(2*PI*300*t)*between(t,7.95,8.2)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30000/1001:duration=12",
                    "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=48000:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(root / "cam.mov")], check=True)
    words = [w for s, e, text in HOME for w in spread_words(s, e, text)]
    for x, y in zip(words, words[1:]):
        x["end"] = y["start"]
    (root / "cam.json").write_text(json.dumps({"words": words}))
    return root


def test_breaths_are_found_by_level_and_cut_on_camera(breath_footage, tmp_path):
    from roughcut.analyze import Cache, analyze
    from roughcut.plan import heuristic_plan

    a = analyze([breath_footage / "cam.mov"], transcriber=None, cache=Cache(tmp_path / "cache"))
    clip = a.clips[0]
    assert len(clip.breaths) == 1 and clip.breaths[0] == pytest.approx(HOME_BREATH, abs=0.03)  # the loud one isn't
    spans = _spans(build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t"))
    assert not _plays(spans, 4.6) and _plays(spans, 8.0)
    assert Analysis.from_json(a.to_json()).clips[0].breaths == clip.breaths


@pytest.mark.parametrize("bed,pieces", [("music", 1), ("noise", 2), (None, 2)])
def test_pauses_over_a_music_bed_stay_up_to_a_second(bed, pieces):
    from roughcut.timeline import kept_length

    clip = make_clip("V01", seconds=12, sentences=PROMO, silences=[(3.0, 3.8), (5.6, 6.4)])
    clip.bed = bed
    a = Analysis(clips=[clip])
    seg = clip.segments[0]  # "... district [0.8 s] coming to Anaheim next year."
    assert kept_length(seg, clip, a, STYLES["medium"])[1] == pieces - 1
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V01.S002"]}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert len(tl.spine) == pieces + (0 if bed == "music" else 1)  # S001 and S002 join over music too
    assert kept_length(seg, clip, a, STYLES["loose"])[1] == 0  # loose keeps 1.2 s anyway

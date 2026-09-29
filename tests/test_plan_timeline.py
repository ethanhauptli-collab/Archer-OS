from fractions import Fraction

import pytest
from conftest import NTSC, SPEECH, make_clip

from roughcut.analyze import Analysis
from roughcut.plan import heuristic_plan, normalize
from roughcut.timeline import STYLES, Style, build_timeline, parse_format, speech_pieces


@pytest.fixture
def analysis():
    return Analysis(
        clips=[
            make_clip("V01", seconds=20, sentences=SPEECH),
            make_clip("V02", seconds=8),
            make_clip("V03", seconds=2),
            make_clip("A01", kind="audio", seconds=30),
            make_clip("I01", kind="image"),
        ]
    )


def test_normalize_drops_bad_references(analysis):
    raw = {
        "title": "T",
        "sections": [
            {"name": "One", "items": ["V01.S003", "v01.s004", "V09.S001", "V01.S003", "garbage", "V02@1-3", "I01", "V02@7-99"], "broll": [
                {"clip": "V02", "over": ["V01.S003", "V01.S099"], "source_in": -1, "reason": "r"},
                {"clip": "A01", "over": ["V01.S003"], "source_in": 0, "reason": "audio can't be b-roll"},
            ]},
            {"name": "Empty", "items": ["V07.S001"], "broll": []},
        ],
        "alternates": [{"chosen": "V01.S003", "others": ["V01.S001", "V01.S404"], "note": ""}],
        "flags": [{"ref": "V01.S002", "note": "check"}],
        "music": [{"clip": "A01", "first_section": 0, "last_section": 5, "source_in": 0}, {"clip": "V02", "first_section": 0, "last_section": 0, "source_in": 0}],
    }
    plan = normalize(raw, analysis)
    assert [s.name for s in plan.sections] == ["One"]
    refs = [i.ref for i in plan.sections[0].items]
    assert refs == ["V01.S003", "V01.S004", "V02@1-3", "I01@0-4", "V02@7-8.008"]
    assert len(plan.sections[0].broll) == 1 and plan.sections[0].broll[0].over == ["V01.S003"]
    assert plan.alternates[0]["others"] == ["V01.S001"]
    assert plan.music[0].last_section == 0
    assert len(plan.music) == 1
    joined = " ".join(plan.warnings)
    for fragment in ("V09.S001", "used twice", "garbage", "A01", "Empty"):
        assert fragment in joined


def test_normalize_tolerates_missing_keys(analysis):
    plan = normalize({"sections": [{"items": ["V01.S001"]}]}, analysis)
    assert plan.title == "Rough Cut"
    assert plan.sections[0].name == "Section 1"


def test_heuristic_plan_is_all_speech_in_order(analysis):
    plan = heuristic_plan(analysis)
    assert [i.seg_id for i in plan.sections[0].items] == [s.id for s in analysis.clip("V01").segments]


def test_heuristic_plan_montage_when_no_speech():
    a = Analysis(clips=[make_clip("V01", seconds=10), make_clip("I01", kind="image")])
    plan = heuristic_plan(a)
    assert plan.sections[0].name == "Montage"
    assert [i.clip_id for i in plan.sections[0].items] == ["V01", "I01"]


def test_filler_is_cut_out(analysis):
    clip = analysis.clip("V01")
    seg = clip.segments[5]  # "And honestly, uh, I needed it too."
    uh = next(w for w in seg.words if w.text == "uh,")
    pieces = speech_pieces(seg, clip, STYLES["medium"], 0)
    assert len(pieces) == 2
    for p in pieces:
        assert p.b <= uh.start or p.a >= uh.end

    kept = speech_pieces(seg, clip, Style(0.1, 0.2, 0.6, remove_fillers=False), 0)
    assert len(kept) == 1


def test_pause_inside_sentence_depends_on_style():
    from roughcut.segments import build_segments

    # "Wait for ... it." with a ~1s pause inside one sentence.
    clip = make_clip("V01", seconds=10, sentences=[(1.0, 2.0, "Wait for"), (3.0, 3.5, "it.")])
    words = [w for s in clip.segments for w in s.words]
    clip.segments = build_segments("V01", words, gap_split=5.0)
    assert len(clip.segments) == 1
    seg = clip.segments[0]
    assert len(speech_pieces(seg, clip, STYLES["tight"], 0)) == 2
    assert len(speech_pieces(seg, clip, STYLES["medium"], 0)) == 2
    assert len(speech_pieces(seg, clip, STYLES["loose"], 0)) == 1


def test_padding_never_reaches_neighbouring_sentences(analysis):
    clip = analysis.clip("V01")
    s3, s4 = clip.segments[2], clip.segments[3]  # 0.2s apart
    p3 = speech_pieces(s3, clip, STYLES["loose"], 0)[-1]
    p4 = speech_pieces(s4, clip, STYLES["loose"], 0)[0]
    assert p3.b <= s4.words[0].start
    assert p4.a >= s3.words[-1].end


def test_timeline_is_frame_accurate_and_contiguous(analysis):
    plan = normalize(
        {
            "title": "T",
            "sections": [
                {"name": "Open", "items": ["V01.S005"], "broll": []},
                {"name": "Body", "items": ["V01.S003", "V01.S004", "I01@0-2", "V02@1-3"], "broll": [
                    {"clip": "V02", "over": ["V01.S003", "V01.S004"], "source_in": -1, "reason": ""},
                    {"clip": "V03", "over": ["V01.S003"], "source_in": 0, "reason": "short clip"},
                ]},
            ],
            "alternates": [{"chosen": "V01.S003", "others": ["V01.S001"], "note": "better take"}],
            "flags": [{"ref": "V01.S004", "note": "check"}],
            "music": [{"clip": "A01", "first_section": 0, "last_section": 1, "source_in": 0}],
        },
        analysis,
    )
    tl = build_timeline(plan, analysis, style=STYLES["medium"], name="t")
    assert tl.frame_duration == NTSC
    pos = 0
    for sc in tl.spine:
        assert sc.offset == pos and sc.frames >= 1
        clip = analysis.clip(sc.clip_id)
        if clip.media.kind == "video":
            assert (sc.src_in / clip.media.frame_duration).denominator == 1
            assert sc.src_in + sc.frames * NTSC <= clip.media.duration
        pos += sc.frames
    # S003 and S004 are 0.2s apart in the same take, so they play as one clip.
    body = [sc for sc in tl.spine if sc.section == 1]
    assert body[0].seg_ids == ["V01.S003", "V01.S004"]
    assert [sc.clip_id for sc in body] == ["V01", "I01", "V02"]

    broll = [c for c in tl.connected if c.kind == "broll"]
    assert {c.clip_id for c in broll} == {"V02", "V03"}
    lanes = {c.clip_id: c.lane for c in broll}
    assert lanes["V02"] != lanes["V03"]  # overlapping cutaways stack on separate lanes
    v03 = next(c for c in broll if c.clip_id == "V03")
    assert v03.src_in + v03.frames * NTSC <= analysis.clip("V03").media.duration

    music = [c for c in tl.connected if c.kind == "music"]
    assert music and music[0].lane == -1 and music[0].frames == tl.total_frames

    kinds = [(m.kind, m.value) for m in tl.markers]
    assert ("chapter", "Open") in kinds and ("chapter", "Body") in kinds
    assert any(k == "todo" for k, _ in kinds)
    assert any(v.startswith("ALT takes") for _, v in kinds)


def test_timecode_start_is_respected():
    clip = make_clip("V01", seconds=20, sentences=SPEECH, start=108000 * NTSC)
    a = Analysis(clips=[clip])
    tl = build_timeline(heuristic_plan(a), a, style=STYLES["medium"], name="t")
    # src_in is clip-local; the FCPXML writer adds the timecode origin.
    assert all(sc.src_in < 20 for sc in tl.spine)


@pytest.mark.parametrize(
    "spec,expected",
    [("1080p", (1920, 1080, None)), ("vertical", (1080, 1920, None)), ("3840x2160@23.976", (3840, 2160, Fraction(1001, 24000))), ("4k@25", (3840, 2160, Fraction(1, 25)))],
)
def test_parse_format(spec, expected):
    assert parse_format(spec) == expected


def test_format_override(analysis):
    tl = build_timeline(heuristic_plan(analysis), analysis, style=STYLES["tight"], name="t", format_override="vertical@25")
    assert (tl.width, tl.height, tl.frame_duration) == (1080, 1920, Fraction(1, 25))

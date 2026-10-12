"""Captions (.srt) for each cut: the kept words, timed through the cut, names spelled as the brief spells them."""

from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path

from conftest import SPEECH, make_clip

from roughcut import captions
from roughcut.analyze import Analysis
from roughcut.plan import normalize
from roughcut.timeline import STYLES, build_timeline

CUE = re.compile(r"(\d+)\n(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)\n((?:.+\n)+)")


def cut(items=("V01.S001", "V01.S003", "V01.S004", "V01.S005", "V01.S006"), *, extra=(), spellings=()):
    a = Analysis(clips=[make_clip("V01", seconds=20, sentences=SPEECH), *extra])
    plan = normalize({"sections": [{"name": "x", "items": list(items), "broll": []}], "spellings": list(spellings)}, a)
    return a, plan, build_timeline(plan, a, style=STYLES["medium"], name="t")


def cues(text: str) -> list[tuple[int, Fraction, Fraction, list[str]]]:
    out = []
    for m in CUE.finditer(text):
        g = [int(x) for x in m.groups()[1:9]]
        a = Fraction(g[0] * 3600 + g[1] * 60 + g[2]) + Fraction(g[3], 1000)
        b = Fraction(g[4] * 3600 + g[5] * 60 + g[6]) + Fraction(g[7], 1000)
        out.append((int(m.group(1)), a, b, m.group(10).rstrip("\n").split("\n")))
    return out


def test_captions_follow_the_cut():
    a, plan, tl = cut()
    text = captions.srt(tl, a)
    found = cues(text)
    assert found and [n for n, *_ in found] == list(range(1, len(found) + 1))
    assert text.count("-->") == len(found)  # every cue parsed
    words = " ".join(" ".join(lines) for *_, lines in found)
    # The kept lines, in cut order; the flubbed start ("Um, wait, let me start over.") was cut, and fillers never show.
    assert "So today we're heading up the mountain." in words and "It's going to be a long day." in words
    assert "start over" not in words and not re.search(r"\b(um|uh)\b", words, re.I)
    assert "And honestly, I needed it too." in words  # "uh" dropped from the caption, the rest kept
    fd = tl.frame_duration
    end = tl.total_frames * fd
    for (_, a1, b1, lines), nxt in zip(found, found[1:] + [None]):
        assert 0 <= a1 < b1 <= end + Fraction(1, 1000)
        assert nxt is None or b1 <= nxt[1], "captions never overlap"
        assert len(lines) <= 2 and all(len(line) <= captions.LINE_CHARS for line in lines)
        assert abs(a1 / fd - round(a1 / fd)) * fd < Fraction(1, 1000), "on a frame"


def test_each_caption_starts_on_its_first_word():
    a, plan, tl = cut(items=("V01.S003", "V01.S005"))
    fd = tl.frame_duration
    first = cues(captions.srt(tl, a))
    seg = a.segment("V01.S005")
    piece = next(p for p in tl.spine if p.seg_ids == ["V01.S005"] or "V01.S005" in p.seg_ids)
    want = (piece.offset + round(Fraction(seg.words[0].start - float(piece.src_in)) / fd)) * fd
    cue = next(c for c in first if c[3][0].startswith("The kids"))
    assert abs(cue[1] - want) < Fraction(1, 1000)


def test_b_roll_sound_is_not_captioned():
    broll = make_clip("V02", seconds=10, sentences=[(1.0, 3.0, "Somebody talking in the background.")], role="broll")
    a, plan, tl = cut(items=("V01.S003", "V02@0-5"), extra=(broll,))
    assert any(p.clip_id == "V02" for p in tl.spine)
    assert "background" not in captions.srt(tl, a)


def test_names_are_spelled_as_the_brief_spells_them():
    speech = [(1.0, 4.0, "First up, Kogi from Roy Choy, the truck."), (5.0, 8.0, "Nonna's Table is Gina Rousseau's pasta bar.")]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}],
        "spellings": [
            {"heard": "Roy Choy", "correct": "Roy Choi"},
            {"heard": "Gina Rousseau", "correct": "Gina Russo"},
            {"heard": "Damon Choe", "correct": "Damon Cho"},  # never said: dropped
            {"heard": "Kogi", "correct": "Kogi"},  # no change: dropped
        ],
    }
    plan = normalize(raw, a)
    assert plan.spellings == raw["spellings"][:2]
    assert json.loads(json.dumps(plan.to_json()))["spellings"] == plan.spellings
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    words = " ".join(" ".join(c[3]) for c in cues(captions.srt(tl, a, plan.spellings)))
    assert "Roy Choi, the truck." in words and "Gina Russo's pasta bar." in words and "Choy" not in words


def test_a_line_never_breaks_inside_a_name():
    assert captions._lines("Chef Remy Lovan runs Golden Hour Bar up on the mezzanine.".split()) == [
        "Chef Remy Lovan runs", "Golden Hour Bar up on the mezzanine.",
    ]
    assert captions._lines("And he has a second bar too, The Copper Still.".split()) == ["And he has a second bar too,", "The Copper Still."]
    assert captions._lines("Short line.".split()) == ["Short line."]


def test_every_version_has_its_captions_and_earlier_ones_go_to_history(footage, tmp_path):
    from roughcut.cli import main

    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", str(footage), "-o", str(out), *common]) == 0
    first = json.loads((out / "result.json").read_text())
    v1 = out / Path(first["fcpxml"]).with_suffix(".srt").name  # "<name> v1.srt" beside "<name> v1.fcpxml"
    assert first["captions"] == str(v1) and v1.is_file() and v1.name.endswith(" v1.srt")
    assert main(["render", str(out), "--style", "tight"]) == 0
    v2 = Path(json.loads((out / "result.json").read_text())["captions"])
    assert v2.name == v1.name.replace(" v1.srt", " v2.srt") and v2.is_file() and not v1.exists()
    assert list((out / "history").glob(f"v1 */{v1.name}")), "the earlier captions are kept with their cut"
    assert json.loads((out / "versions.json").read_text())["versions"][-1]["captions"] == v2.name


def test_words_whisper_stretched_over_a_cut_pause_are_still_captioned(tmp_path):
    """mlx-whisper runs each word up to the next one, so a sentence's last word covers the pause after it,
    which the cut removes. Captioned by where it sounds, the word (often a name) stays."""
    import shutil
    import subprocess

    import pytest
    from test_dead_air import SECONDS, SENTENCES, whisper_words

    from roughcut.cli import main

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    foot = tmp_path / "talk"
    foot.mkdir()
    gates = "+".join(f"between(t,{s},{e})" for s, e, _ in SENTENCES)
    speech = f"0.5*sin(2*PI*220*t)*(0.55+0.45*sin(2*PI*4*t))*({gates})"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:size=320x180:rate=30:duration={SECONDS}",
         "-f", "lavfi", "-i", f"aevalsrc=exprs='{speech}':s=48000:d={SECONDS}", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(foot / "talk.mov")],
        check=True,
    )
    (foot / "talk.json").write_text(json.dumps({"words": whisper_words(SENTENCES)}))
    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--no-stringout"]  # the .json sidecar's words
    assert main(["build", str(foot), "-o", str(out), *common]) == 0
    text = next(out.glob("*.srt")).read_text()
    said = " ".join(t for _, _, t in SENTENCES).split()
    assert [w for w in said if w not in text.split()] == []
    assert "coming to Anaheim.\n" in text, "the sentence ends its caption"


def test_a_planned_line_from_a_b_roll_clip_is_captioned():
    vox = make_clip("V02", seconds=10, sentences=[(1.0, 3.0, "Best tacos in the county.")], role="broll")
    a, plan, tl = cut(items=("V01.S003", "V02.S001"), extra=(vox,))
    text = captions.srt(tl, a)
    assert "Best tacos in the county." in text and "Today we're heading up the mountain." in text


def _fixed(text: str, spellings: list[dict]) -> str:
    said = [captions._Word(w, n, n + 1) for n, w in enumerate(text.split())]
    return " ".join(w.text for w in captions.fix_names(said, spellings))


def test_a_spelling_copied_with_its_possessive_or_punctuation_is_written_once():
    """The planner copies "heard" as the transcript has it ("Lin Nguyen's", "Marco Belini."): the caption
    keeps the transcript's own possessive and punctuation, once, and adds no possessive nobody said."""
    possessive = [{"heard": "Gina Rousseau's", "correct": "Gina Russo's"}]
    assert _fixed("Nonna's Table is Gina Rousseau's pasta bar.", possessive) == "Nonna's Table is Gina Russo's pasta bar."
    assert _fixed("Gina Rousseau runs it.", possessive) == "Gina Russo runs it."
    assert _fixed("Gina Rousseau's pasta bar.", [{"heard": "Gina Rousseau's", "correct": "Gina Russo"}]) == "Gina Russo's pasta bar."
    assert _fixed("Lunch at Wahoo's today.", [{"heard": "Wahoo's", "correct": "Wahoos"}]) == "Lunch at Wahoos today."
    curly = [{"heard": "Gina Rousseau\u2019s", "correct": "Gina Russo\u2019s"}]
    assert _fixed("It is Gina Rousseau\u2019s pasta bar.", curly) == "It is Gina Russo\u2019s pasta bar."
    assert _fixed("It is Gina Rousseau's pasta bar.", curly) == "It is Gina Russo's pasta bar."
    assert _fixed("It was Lin Nguyen's first restaurant.", [{"heard": "Lin Nguyen's", "correct": "Linh Nguyen's"}]) == "It was Linh Nguyen's first restaurant."
    assert _fixed("It was Lin Nguyen's first restaurant.", [{"heard": "Lin Nguyen", "correct": "Linh Nguyen"}]) == "It was Linh Nguyen's first restaurant."
    assert _fixed("Chef Marco Belini. He cooks.", [{"heard": "Marco Belini.", "correct": "Marco Bellini."}]) == "Chef Marco Bellini. He cooks."
    assert _fixed("Roy Choy, chef.", [{"heard": "Roy Choy,", "correct": "Roy Choi,"}]) == "Roy Choi, chef."
    assert _fixed("Meet Roy Choy junior.", [{"heard": "Roy Choy junior", "correct": "Roy Choi Jr."}]) == "Meet Roy Choi Jr."
    # A possessive that is part of the name.
    assert _fixed("Nona's Table is great.", [{"heard": "Nona's Table", "correct": "Nonna's Table"}]) == "Nonna's Table is great."
    # plan.json keeps the pairs clean.
    speech = [(1.0, 4.0, "Roy Choy, chef."), (5.0, 9.0, "Nonna's Table is Gina Rousseau's pasta bar.")]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}],
        "spellings": [{"heard": "Roy Choy,", "correct": "Roy Choi,"}, {"heard": "Gina Rousseau\u2019s", "correct": "Gina Russo\u2019s"}],
    }
    plan = normalize(raw, a)
    assert plan.spellings == [{"heard": "Roy Choy", "correct": "Roy Choi"}, {"heard": "Gina Rousseau", "correct": "Gina Russo"}]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    words = " ".join(" ".join(c[3]) for c in cues(captions.srt(tl, a, raw["spellings"])))  # an older plan.json's pairs, as they were
    assert "Roy Choi, chef." in words and "Gina Russo's pasta bar." in words


def test_a_name_with_a_filler_inside_it_is_still_fixed():
    speech = [(1.0, 5.0, "Dough Boy Pizza comes from Marco, um, Belini.")]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    raw = {"sections": [{"name": "x", "items": ["V01.S001"], "broll": []}], "spellings": [{"heard": "Marco Belini", "correct": "Marco Bellini"}]}
    plan = normalize(raw, a)
    assert plan.spellings == raw["spellings"]
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert "Dough Boy Pizza comes from Marco Bellini." in " ".join(" ".join(c[3]) for c in cues(captions.srt(tl, a, plan.spellings)))
    assert _fixed("from Marco, Belini.", [{"heard": "Marco, um, Belini", "correct": "Marco Bellini"}]) == "from Marco Bellini."


def test_no_caption_line_is_longer_than_42_characters():
    """An 80-84 character line that can't split into two lines of 42 starts a new caption instead."""
    speech = [
        (1.0, 5.6, "The neighborhood got a brand new Mediterranean-inspired rooftop restaurant this year."),
        (6.0, 10.4, "We partnered with the Anaheim Convention Center to bring entrepreneurs together this spring."),
    ]
    a = Analysis(clips=[make_clip("V01", seconds=12, sentences=speech)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    found = cues(captions.srt(tl, a))
    lines = [line for *_, ls in found for line in ls]
    assert " ".join(lines).split() == " ".join(t for *_, t in speech).split()
    assert all(len(line) <= captions.LINE_CHARS for line in lines), lines
    # A single word too long for a line still gets a caption, on its own.
    long = "Supercalifragilisticexpialidocious-and-then-some"
    a = Analysis(clips=[make_clip("V01", seconds=8, sentences=[(1.0, 4.0, f"It was {long} today.")])])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert [ls for *_, ls in cues(captions.srt(tl, a))] == [["It was"], [long], ["today."]]


def test_a_cut_word_with_a_few_milliseconds_of_sound_kept_is_not_captioned():
    """The retake after a kept line starts a few milliseconds before the piece's last frame ends: its first
    word is cut, so it isn't prepended to the next caption."""
    speech = [
        (0.5, 3.03, "Dough Boy Pizza comes from Marco Belini."),
        (3.235, 5.0, "Wait, let me redo that."),
        (6.0, 9.0, "Chef Kenji Tanaka is doing omakase."),
    ]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S001", "V01.S003"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    edge = tl.spine[0].src_in + tl.spine[0].frames * tl.frame_duration
    assert 3.235 < edge, "the first piece plays a few milliseconds of 'Wait,'"
    assert [" ".join(ls) for *_, ls in cues(captions.srt(tl, a))] == [
        "Dough Boy Pizza comes from Marco Belini.", "Chef Kenji Tanaka is doing omakase.",
    ]
    # The same at the start of a piece: the end of the line before it.
    speech = [(0.5, 5.06, "Wait, hold on, let me redo that."), (5.17, 9.0, "Dough Boy Pizza comes from Marco Belini.")]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    plan = normalize({"sections": [{"name": "x", "items": ["V01.S002"], "broll": []}]}, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    assert tl.spine[0].src_in < a.clips[0].segments[0].words[-1].end, "the piece plays a few milliseconds of 'that.'"
    assert [" ".join(ls) for *_, ls in cues(captions.srt(tl, a))] == ["Dough Boy Pizza comes from Marco Belini."]


def test_graphics_spell_names_as_the_brief_does():
    """A spelling fixes a graphic's on-screen text too; its words stay as transcribed, to find them in the speech."""
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=[(1.0, 4.0, "First up, Kogi from Roy Choy, the truck."), (5.0, 8.0, "Then Golden Hour.")])])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}],
        "spellings": [{"heard": "Roy Choy", "correct": "Roy Choi"}, {"heard": "Golden Hour", "correct": "Golden Hour Bar"}],
        "graphics": [
            {"kind": "lower_third", "segment": "V01.S001", "words": "Kogi from Roy Choy", "text": "Kogi", "subtext": "Chef Roy Choy's truck", "seconds": 3, "direction": ""},
            {"kind": "lower_third", "segment": "V01.S002", "words": "Golden Hour", "text": "Golden Hour Bar", "subtext": "Golden Hour", "seconds": 3, "direction": ""},
        ],
    }
    plan = normalize(raw, a)
    assert [(g.text, g.subtext, g.words) for g in plan.graphics] == [
        ("Kogi", "Chef Roy Choi's truck", "Kogi from Roy Choy"),
        ("Golden Hour Bar", "Golden Hour Bar", "Golden Hour"),  # already spelled out: not "Golden Hour Bar Bar"
    ]
    assert captions.respell("Golden Hour Bar", plan.spellings) == "Golden Hour Bar" and captions.respell("Roy Choi", plan.spellings) == "Roy Choi"

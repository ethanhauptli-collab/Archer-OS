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
    # Only "correct" has the possessive or the punctuation: still only the transcript's own, once.
    for correct in ("Roy Choi's", "Roy Choi\u2019s", "Roy Choi.", "Roy Choi!", "Roy Choi's."):
        fix = [{"heard": "Roy Choy", "correct": correct}]
        assert captions.clean_spelling("Roy Choy", correct) == ("Roy Choy", "Roy Choi")
        assert _fixed("Roy Choy runs it.", fix) == "Roy Choi runs it."
        assert _fixed("Kogi is Roy Choy's truck.", fix) == "Kogi is Roy Choi's truck."
    # A name's own 's and an abbreviation's period stay, from either side.
    assert captions.clean_spelling("Trader Joes", "Trader Joe's") == ("Trader Joes", "Trader Joe's")
    assert _fixed("Lunch at Trader Joes today.", [{"heard": "Trader Joes", "correct": "Trader Joe's"}]) == "Lunch at Trader Joe's today."
    assert captions.clean_spelling("Roy Choy junior.", "Roy Choi Jr.") == ("Roy Choy junior", "Roy Choi Jr.")
    assert _fixed("Meet Roy Choy junior, the chef.", [{"heard": "Roy Choy junior.", "correct": "Roy Choi Jr."}]) == "Meet Roy Choi Jr., the chef."
    assert _fixed("Acme Ink makes it.", [{"heard": "Acme Ink", "correct": "Acme Inc."}]) == "Acme Inc. makes it."
    assert _fixed("We met on Mane St today.", [{"heard": "Mane St", "correct": "Main St."}]) == "We met on Main St. today."
    assert _fixed("Then the US Grant.", [{"heard": "US Grant", "correct": "U.S. Grant."}]) == "Then the U.S. Grant."
    assert captions.clean_spelling("Acme Inc.", "Acme Inc.") == ("Acme Inc.", "Acme Inc.")  # the same: no fix
    # Only an initialism's or a known short form's period is the name's own: a web address copied with the
    # sentence's period loses it, and short forms beyond the first few keep theirs.
    assert _fixed("Go to venture OC dot com today and see.", [{"heard": "venture OC dot com.", "correct": "ventureoc.com."}]) == "Go to ventureoc.com today and see."
    assert _fixed("Order at Kogi BBQ dot com tonight.", [{"heard": "Kogi BBQ dot com", "correct": "KogiBBQ.com."}]) == "Order at KogiBBQ.com tonight."
    assert _fixed("Acme corporation makes it.", [{"heard": "Acme corporation", "correct": "Acme Corp."}]) == "Acme Corp. makes it."
    assert _fixed("Drive down Harbor boulevard tonight.", [{"heard": "Harbor boulevard", "correct": "Harbor Blvd."}]) == "Drive down Harbor Blvd. tonight."
    assert _fixed("Warner brothers made it.", [{"heard": "Warner brothers", "correct": "Warner Bros."}]) == "Warner Bros. made it."
    assert _fixed("Take Beach avenue home.", [{"heard": "Beach avenue", "correct": "Beach Ave."}]) == "Take Beach Ave. home."
    assert _fixed("She got a PhD today.", [{"heard": "PhD", "correct": "Ph.D."}]) == "She got a Ph.D. today."
    assert _fixed("Then the US Grant.", [{"heard": "US Grant", "correct": "U.S. Grant"}]) == "Then the U.S. Grant."


def test_undoing_a_spelling_gives_the_name_back_as_heard_without_the_right_form_s_own_period():
    """A revision that undoes a pair respells graphics with it reversed: the period of "Jr." or "Inc." is the
    right form's own and goes with it, while a sentence's period after the name stays."""
    for heard, correct, said in [
        ("Roy Choy junior", "Roy Choi Jr.", "Meet Roy Choy junior tonight"),
        ("Roy Choy junior", "Roy Choi Jr.", "Meet Roy Choy junior. He cooks."),
        ("Roy Choy junior", "Roy Choi Jr.", "Roy Choy junior"),
        ("Acme Ink", "Acme Inc.", "Acme Ink"),
        ("Acme Ink", "Acme Inc.", "Acme Ink, owner"),
        ("Mane St", "Main St.", "Owner (Mane St)"),
        ("kogi", "Kogi BBQ Co.", "kogi"),
        ("US Grant", "U.S. Grant", "Then the US Grant."),
        ("Roy Choy", "Roy Choi", "Roy Choy's truck."),
        ("the mountain", "Mount Baldy", "Up the mountain"),
        ("Mount Baldy", "Mt. Baldy", "Mount Baldy"),
    ]:
        h, c = captions.clean_spelling(heard, correct)
        shown = captions.respell(said, [{"heard": h, "correct": c}])
        assert shown != said
        assert captions.respell(shown, [{"heard": c, "correct": h}]) == said, (heard, correct, said)
    # In what's said too: a misheard short form that ends a sentence keeps the sentence's period.
    assert _fixed("Acme Inc. makes it. We love Acme Inc. So.", [{"heard": "Acme Inc.", "correct": "Acme Incorporated"}]) == (
        "Acme Incorporated makes it. We love Acme Incorporated. So."
    )


def test_undoing_a_spelling_leaves_a_longer_name_that_is_still_spelled_alone():
    """A pair undone inside the right form of a pair still in force: that name stays as the pair in force spells it."""
    keep = [{"heard": "Chef Remy Lovan", "correct": "Chef Rémy Lovain"}]
    assert captions.respell("Chef Rémy Lovain cooks", [{"heard": "Rémy", "correct": "Remy"}], keep=keep) == "Chef Rémy Lovain cooks"
    assert captions.respell("Rémy cooks", [{"heard": "Rémy", "correct": "Remy"}], keep=keep) == "Remy cooks"
    keep = [{"heard": "Kogi", "correct": "Kogi by Roy Choi"}]
    shown = "Kogi by Roy Choi rules, says Roy Choi."
    undone = captions.respell(shown, [{"heard": "Choi", "correct": "Choy"}], keep=keep)
    assert undone == "Kogi by Roy Choi rules, says Roy Choy."
    assert captions.respell(undone, keep) == undone  # not "Kogi by Roy Choi by Roy Choy"
    # A shorter name in force where a longer undone one starts doesn't stop the undo: "Kogi" stays spelled, by its pair.
    keep = [{"heard": "kogi", "correct": "Kogi"}]
    undone = captions.respell("Kogi BBQ Co. rules", [{"heard": "Kogi BBQ Co.", "correct": "kogi bbq"}], keep=keep)
    assert undone == "kogi bbq rules" and captions.respell(undone, keep) == "Kogi bbq rules"
    # Through the plan's graphics, as a revision applies them.
    from roughcut.plan import GraphicSpec, respell_graphics

    specs = [GraphicSpec(kind="lower_third", segment="V01.S001", words="Remy", text="Chef Rémy Lovain", subtext="Rémy's kitchen")]
    respell_graphics(specs, [{"heard": "Rémy", "correct": "Remy"}], keep=[{"heard": "Chef Remy Lovan", "correct": "Chef Rémy Lovain"}])
    assert (specs[0].text, specs[0].subtext) == ("Chef Rémy Lovain", "Remy's kitchen")


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


def test_a_name_s_spelling_only_on_correct_never_adds_a_possessive_or_ends_a_sentence():
    """{"heard": "Roy Choy", "correct": "Roy Choi's"}: the captions and the lower third say "Roy Choi", and
    "Roy Choi." doesn't break a caption mid-sentence."""
    speech = [(1.0, 4.0, "First up, Kogi is Roy Choy's truck."), (5.0, 8.0, "Roy Choy runs it every night.")]
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=speech)])
    for correct in ("Roy Choi's", "Roy Choi\u2019s", "Roy Choi."):
        raw = {
            "sections": [{"name": "x", "items": ["V01.S001", "V01.S002"], "broll": []}],
            "spellings": [{"heard": "Roy Choy", "correct": correct}],
            "graphics": [{"kind": "lower_third", "segment": "V01.S002", "words": "Roy Choy runs it", "text": "Roy Choy", "subtext": "Kogi", "seconds": 3, "direction": ""}],
        }
        plan = normalize(raw, a)
        assert plan.spellings == [{"heard": "Roy Choy", "correct": "Roy Choi"}]
        assert [g.text for g in plan.graphics] == ["Roy Choi"]
        tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
        for spellings in (plan.spellings, raw["spellings"]):  # and an older plan.json's pair, as it was
            found = [" ".join(ls) for *_, ls in cues(captions.srt(tl, a, spellings))]
            assert "Roy Choi runs it every night." in found, found
            assert "First up, Kogi is Roy Choi's truck." in " ".join(found)


def test_a_respelled_name_keeps_the_quotes_and_brackets_around_it():
    fix = [{"heard": "Roy Choy", "correct": "Roy Choi"}]
    for said, right in [
        ("\u201cRoy Choy changed LA.\u201d", "\u201cRoy Choi changed LA.\u201d"),
        ("Owner (Roy Choy)", "Owner (Roy Choi)"),
        ('"Roy Choy,"', '"Roy Choi,"'),
        ("\u201cRoy Choy\u201d is here", "\u201cRoy Choi\u201d is here"),
        ("(Roy Choy's truck)", "(Roy Choi's truck)"),
        ("'Roy Choy.'", "'Roy Choi.'"),
        ("[Roy Choy\u2019s]", "[Roy Choi\u2019s]"),
    ]:
        assert captions.respell(said, fix) == right
        assert _fixed(said, fix) == right
    assert _fixed("Meet (Roy Choy junior).", [{"heard": "Roy Choy junior", "correct": "Roy Choi Jr."}]) == "Meet (Roy Choi Jr.)."
    assert captions.respell("Kogi\nRoy Choy  and friends", fix) == "Kogi\nRoy Choi  and friends"  # its line breaks too
    # On a graphic, through the plan.
    a = Analysis(clips=[make_clip("V01", seconds=10, sentences=[(1.0, 4.0, "Roy Choy changed how this city eats.")])])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S001"], "broll": []}],
        "spellings": fix,
        "graphics": [{"kind": "quote", "segment": "V01.S001", "words": "Roy Choy changed how this city eats",
                      "text": "\u201cRoy Choy changed how this city eats.\u201d", "subtext": "Owner (Roy Choy)", "seconds": 4, "direction": ""}],
    }
    assert [(g.text, g.subtext) for g in normalize(raw, a).graphics] == [("\u201cRoy Choi changed how this city eats.\u201d", "Owner (Roy Choi)")]


def test_a_kinetic_caption_is_designed_with_the_name_as_the_brief_spells_it():
    """The designer reveals a kinetic caption's words at their times: they show the name as spelled, one entry
    for the whole name at its first word's time; the timing the design is noted with keeps the transcript's words."""
    from roughcut import graphics

    speech = [(1.0, 5.0, "Kogi is Roy Choy's truck, and it rules.")]
    a = Analysis(clips=[make_clip("V01", seconds=8, sentences=speech)])
    raw = {
        "sections": [{"name": "x", "items": ["V01.S001"], "broll": []}],
        "spellings": [{"heard": "Roy Choy", "correct": "Roy Choi"}],
        "graphics": [{"kind": "kinetic_caption", "segment": "V01.S001", "words": "Kogi is Roy Choy's truck", "text": "Kogi is Roy Choy's truck", "subtext": "", "seconds": 0, "direction": ""}],
    }
    plan = normalize(raw, a)
    tl = build_timeline(plan, a, style=STYLES["medium"], name="t")
    placed = graphics.place(plan.graphics, tl, a, [])
    times = dict(placed[0].word_times)
    request = graphics._design_request(placed, a, tl.frame_duration, brief="", style="", width=1920, height=1080, spellings=plan.spellings)
    item = json.loads(request.split("<graphics>")[1].split("</graphics>")[0])[0]
    assert item["text"] == "Kogi is Roy Choi's truck" and item["said"] == "Kogi is Roy Choi's truck, and it rules."
    assert [(w["word"], w["at"]) for w in item["words"]] == [("Kogi", times["Kogi"]), ("is", times["is"]), ("Roy Choi's", times["Roy"]), ("truck,", times["truck,"])]
    assert [w for w, _ in placed[0].word_times] == ["Kogi", "is", "Roy", "Choy's", "truck,"]  # the timing note's words
    assert "Choy" not in request

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

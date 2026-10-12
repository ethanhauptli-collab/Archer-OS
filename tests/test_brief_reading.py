"""What a brief says about pacing, punch-ins and graphics.

The planner reads the whole brief and says what it asks for (the plan's pacing, punch_ins and
graphics_asked); Auto pacing and Auto punch-ins follow that. Without a model (--provider none, or
the stringout when the model fails) a few plain wording rules stand in: tight or loose only for
clear cues not turned down in their own clause, punch-ins only when asked for at the cuts.
"""

from __future__ import annotations

import json

import pytest
from conftest import CANNED_PLAN, FakeProvider

from roughcut import pipeline, presets
from roughcut.pipeline import graphics_from_brief, graphics_off_note, punch_in_from_brief, read_punch_in, style_from_brief
from roughcut.plan import PLAN_SCHEMA, normalize
from roughcut.timeline import STYLES

# ------------------------------------------------------------ the planner's reading


def _plan(**fields) -> dict:
    plan = json.loads(json.dumps(CANNED_PLAN))
    plan.update(fields)
    return plan


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: provider)
    return provider


def _opts(footage, out, cache, **kw) -> pipeline.Options:
    return pipeline.Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=cache, **kw)


def test_the_plan_schema_asks_the_planner_what_the_brief_says():
    props = PLAN_SCHEMA["properties"]
    assert props["pacing"]["enum"] == ["tight", "medium", "loose", "unspecified"]
    assert props["punch_ins"]["enum"] == ["on", "off", "unspecified"]
    assert props["graphics_asked"]["type"] == "boolean"
    assert {"pacing", "punch_ins", "graphics_asked"} <= set(PLAN_SCHEMA["required"])
    for name in ("pacing", "punch_ins", "graphics_asked"):
        assert len(props[name]["description"]) > 60, name
    assert "one-off" in props["punch_ins"]["description"] or "one reframe" in props["punch_ins"]["description"]


def test_normalize_keeps_the_reading_and_tolerates_a_plan_without_it(footage, tmp_path, fake):
    from roughcut.analyze import Analysis

    pipeline.build(_opts(footage, tmp_path / "out", tmp_path / "c", style="medium"))
    analysis = Analysis.load(tmp_path / "out" / "analysis.json")
    plan = normalize(_plan(pacing="loose", punch_ins="on", graphics_asked=True), analysis, source="fake:fake-model")
    assert (plan.pacing, plan.punch_ins, plan.graphics_asked) == ("loose", "on", True)
    assert plan.to_json()["pacing"] == "loose" and plan.to_json()["punch_ins"] == "on" and plan.to_json()["graphics_asked"] is True
    older = normalize(CANNED_PLAN, analysis, source="fake:fake-model")  # an older plan.json, or a model that left them out
    assert (older.pacing, older.punch_ins, older.graphics_asked) == ("unspecified", "unspecified", False)
    odd = normalize(_plan(pacing="Very tight", punch_ins=True, graphics_asked="yes"), analysis, source="fake:fake-model")
    assert (odd.pacing, odd.punch_ins, odd.graphics_asked) == ("unspecified", "on", False)


NO_PUNCH_INS_SAID = "not punching in (the brief doesn't ask for them at the jump cuts)"


@pytest.mark.parametrize(
    "pacing, punch_ins, style, punch_in, said",
    [
        ("tight", "on", "tight", True, ["pacing: tight (the brief asks for it)", "punching in on jump cuts (the brief asks for it)"]),
        ("loose", "off", "loose", False, ["pacing: loose (the brief asks for it)", "not punching in (the brief turns them down)"]),
        ("medium", "unspecified", "medium", False, ["pacing: medium (the brief asks for it)", NO_PUNCH_INS_SAID]),
        ("unspecified", "unspecified", "medium", False, ["pacing: medium (the brief doesn't say)", NO_PUNCH_INS_SAID]),
    ],
)
def test_auto_pacing_and_punch_ins_follow_the_planners_reading(footage, tmp_path, fake, capsys, pacing, punch_ins, style, punch_in, said):
    fake.plan = _plan(pacing=pacing, punch_ins=punch_ins, graphics_asked=False)
    out = tmp_path / "out"
    # The wording rules would read this brief the other way: the planner's reading decides.
    result = pipeline.build(_opts(footage, out, tmp_path / "c", context="Keep it loose, no punch-ins", style="auto"))
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == (style, punch_in), "saved, so re-cuts keep them"
    assert result.project == f"footage - Rough Cut v1 ({style}, fake-model)"
    log = capsys.readouterr().err
    for line in said:
        assert line in log
    if punch_ins == "unspecified":  # said all the same, so a one-off reframe read as punch-ins (or not) shows
        assert "punching in on" not in log


def test_an_auto_build_shows_the_planner_kept_lengths_at_medium(footage, tmp_path, fake):
    from roughcut.analyze import Analysis

    fake.plan = _plan(pacing="tight", punch_ins="unspecified", graphics_asked=False)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", style="auto"))
    sent = next(c for c in fake.calls if c["purpose"] == "edit plan")["parts"][0].text
    analysis = Analysis.load(out / "analysis.json")
    assert sent == pipeline.media_part(analysis, STYLES["medium"]).text, "before the planner has read the brief, at medium"
    assert pipeline.style_for(pipeline.Options(inputs=[], style="auto")) == STYLES["medium"]


def test_an_auto_build_tells_the_planner_what_tight_and_loose_make_of_the_kept_lengths(footage, tmp_path, fake):
    """The kept lengths are shown at medium before the planner picks the pacing: it's told how much shorter tight and
    how much longer loose play them for this footage, so it plans the target at the pacing it picks."""
    from roughcut.analyze import Analysis
    from roughcut.timeline import kept_length

    fake.plan = _plan(pacing="loose", punch_ins="unspecified", graphics_asked=False)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", style="auto", target_seconds=12, context="Let it breathe."))
    call = next(c for c in fake.calls if c["purpose"] == "edit plan")
    analysis = Analysis.load(out / "analysis.json")
    total = {n: sum(kept_length(seg, c, analysis, STYLES[n])[0] for c in analysis.clips if not c.hidden and c.has_transcript for seg in c.segments)
             for n in STYLES}
    brief = call["parts"][1].text
    note = brief.split("<pacing>")[1].split("</pacing>")[0]
    assert "kept lengths in the media are at medium pacing" in note and "at the pacing you pick" in note
    assert f"about {round(100 * (1 - total['tight'] / total['medium']))}% shorter" in note
    assert f"about {round(100 * (total['loose'] / total['medium'] - 1))}% longer" in note
    assert brief.index("<target_length>") < brief.index("<pacing>") < brief.index("Build the rough cut plan.")
    assert call["parts"][0].text == pipeline.media_part(analysis, STYLES["medium"]).text, "the cached media block is unchanged"
    assert note in (out / "prompt.md").read_text()
    # A pacing you pick: the kept lengths are already at it.
    pipeline.build(_opts(footage, tmp_path / "o2", tmp_path / "c", style="loose", target_seconds=12))
    assert "<pacing>" not in [c for c in fake.calls if c["purpose"] == "edit plan"][-1]["parts"][1].text


def test_a_plan_without_the_reading_cuts_at_medium_without_punch_ins(footage, tmp_path, fake, capsys):
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", context="Tight cuts, punch in on the jump cuts.", style="auto"))
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == ("medium", False), "a model planned it: no guessing from the wording"
    assert "pacing: medium (the brief doesn't say)" in capsys.readouterr().err


def test_a_pacing_or_punch_in_you_pick_wins_over_the_planner(footage, tmp_path, fake, capsys):
    fake.plan = _plan(pacing="tight", punch_ins="on", graphics_asked=False)
    out = tmp_path / "out"
    pipeline.build(_opts(footage, out, tmp_path / "c", style="loose", punch_in=False))
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == ("loose", False)
    assert "pacing:" not in capsys.readouterr().err
    # From the command line, and a preset's pacing over the app's --style auto.
    from roughcut.cli import main

    common = ["-o", str(out), "--transcriber", "none", "--cache-dir", str(tmp_path / "c"), "--no-menu"]
    assert main(["build", str(footage), *common, "--style", "medium", "--no-punch-in"]) == 0
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == ("medium", False)
    assert main(["build", str(footage), *common, "--preset", "save-the-kid", "--no-graphics", "--style", "auto"]) == 0
    assert json.loads((out / "plan.json").read_text())["settings"]["style"] == "loose", "the preset's own pacing"
    assert main(["build", str(footage), *common]) == 0
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == ("tight", True), "Auto: the planner's reading"
    # A re-cut keeps them.
    assert main(["render", str(out), "--format", "vertical"]) == 0
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert (settings["style"], settings["punch_in"]) == ("tight", True)


def test_the_planner_sees_which_part_of_the_brief_is_the_presets(footage, tmp_path, fake):
    stk = presets.SAVE_THE_KID_BRIEF
    pipeline.build(_opts(footage, tmp_path / "out", tmp_path / "c", context=stk + "\n\nThe hike with Myles. Keep it tight.", preset_brief=stk, style="auto"))
    brief = next(c for c in fake.calls if c["purpose"] == "edit plan")["parts"][1].text
    assert brief.index("<preset_brief>") < brief.index(stk) < brief.index("</preset_brief>") < brief.index("<brief>\nThe hike with Myles. Keep it tight.\n</brief>")
    assert brief.count(stk) == 1
    assert "pacing" in pipeline.PLANNER_SYSTEM and "punch_ins" in pipeline.PLANNER_SYSTEM and "graphics_asked" in pipeline.PLANNER_SYSTEM


# ------------------------------------------------------------ a brief that asks for graphics while they're off


def test_the_planner_says_the_brief_asks_for_graphics_while_theyre_off(footage, tmp_path, fake, capsys):
    from roughcut.cli import main

    fake.plan = _plan(pacing="unspecified", punch_ins="unspecified", graphics_asked=True)
    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "-c", "Name each chef on screen as they come up.", "--transcriber", "none",
                 "--cache-dir", str(tmp_path / "c"), "--no-menu"]) == 0
    printed = capsys.readouterr()
    result = json.loads((out / "result.json").read_text())
    note = next(w for w in result["warnings"] if w.startswith("The brief asks for graphics"))
    assert "motion graphics are off" in note and "roughcut revise" in note and "--graphics" in note
    top = (out / "edit_report.md").read_text().split("- **Rough cut:**")[0]
    assert note in top, "at the top of the report, not only in its Warnings"
    assert note in printed.out, "at the end of the CLI's output"
    assert note in (out / "roughcut.log").read_text()


def test_with_graphics_off_the_planner_is_told_this_cut_has_none(footage, tmp_path, fake):
    """A brief that asks for lower thirds while graphics are off: the planner plans none and describes none in the
    logline, flags or cut notes, and still says the brief asks for them (for the graphics-off note)."""
    from roughcut.prompts import render_brief
    from roughcut.revise import recover_brief

    fake.plan = _plan(pacing="unspecified", punch_ins="unspecified", graphics_asked=True)
    out = tmp_path / "out"
    result = pipeline.build(_opts(footage, out, tmp_path / "c", context="List the restaurants and chefs with lower thirds."))
    brief = next(c for c in fake.calls if c["purpose"] == "edit plan")["parts"][1].text
    note = brief.split("<motion_graphics_off>")[1].split("</motion_graphics_off>")[0]
    assert "no motion graphics" in note and "plan none" in note
    assert "logline" in note and "flags" in note and "cut_notes" in note and "graphics_asked" in note
    assert "<motion_graphics>" not in brief
    assert any(w.startswith("The brief asks for graphics") for w in result.warnings), "graphics_asked still read from the brief"
    # The reviser is told the same while they're off.
    pipeline.revise(out, "Swap the hook.", pipeline.Options(inputs=[]))
    assert "<motion_graphics_off>" in fake.calls[-1]["parts"][1].text
    # On: the graphics brief, and no such note. An older folder's prompt reads as graphics off either way.
    assert "<motion_graphics_off>" not in render_brief("b", None, None, graphics=True)
    assert recover_brief("# Brief\n" + render_brief("b", None, None))["graphics"] is False
    assert recover_brief("# Brief\n" + render_brief("b", None, None, graphics=True))["graphics"] is True


def test_no_graphics_note_unless_the_planner_says_so_or_when_theyre_on(footage, tmp_path, fake, monkeypatch):
    from roughcut import graphics

    # The wording rules would see graphics in this brief; the planner says it doesn't ask for them.
    fake.plan = _plan(pacing="unspecified", punch_ins="unspecified", graphics_asked=False)
    result = pipeline.build(_opts(footage, tmp_path / "o1", tmp_path / "c", context="The graphics designer is in the video."))
    assert not any("asks for graphics" in w for w in result.warnings)
    # Graphics on: nothing to warn about.
    monkeypatch.setattr(graphics, "renderer_problem", lambda: None)
    monkeypatch.setattr(graphics, "make", lambda *a, **k: ([], [], []))
    fake.plan = _plan(pacing="unspecified", punch_ins="unspecified", graphics_asked=True, graphics=[])
    result = pipeline.build(_opts(footage, tmp_path / "o2", tmp_path / "c", context="Lower thirds for each chef.", graphics=True))
    assert not any("asks for graphics" in w for w in result.warnings)


def test_a_cut_without_ai_reads_the_graphics_request_from_the_wording(footage, tmp_path, capsys):
    from roughcut.cli import main

    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", str(footage), "-o", str(out), "-c", "Add lower thirds for each chef.", *common]) == 0
    captured = capsys.readouterr()
    assert 'The brief asks for graphics ("lower thirds"), but this cut was made without AI' in captured.out, "in the CLI's summary, not only the log"
    assert "--provider claude-code" in captured.out
    assert main(["build", str(footage), "-o", str(out), "-c", "The graphics designer is in the video.", *common]) == 0
    assert "asks for graphics" not in capsys.readouterr().out


def test_the_presets_own_graphics_line_is_not_a_request(tmp_path):
    stk = presets.SAVE_THE_KID_BRIEF
    opts = pipeline.Options(inputs=[], context=stk + "\n\nThe hike.", preset_brief=stk)
    assert graphics_off_note(opts, tmp_path, asked=None, made_without_ai=True) is None
    opts = pipeline.Options(inputs=[], context=stk + "\n\nLower thirds for each person.", preset_brief=stk)
    assert '("Lower thirds")' in graphics_off_note(opts, tmp_path, asked=None, made_without_ai=True)
    assert graphics_off_note(opts, tmp_path, asked=False, made_without_ai=False) is None, "a model planned it: its reading decides"
    assert graphics_off_note(pipeline.Options(inputs=[], graphics=True), tmp_path, asked=True, made_without_ai=False) is None


# ------------------------------------------------------------ without AI: the plain wording rules

TIGHT = [
    "Keep it tight.",
    "Keep it tight",
    "Short and tight.",
    "Make it short and tight.",
    "Keep it short and tight",
    "I want it tight",
    "I want this tight.",
    "It should be tight.",
    "It needs to be tight",
    "Needs to feel tight.",
    "Tight and energetic",
    "Tight and fun, 60 seconds",
    "Fun, tight and upbeat",
    "Keep the interviews tight",
    "Edit tight",
    "Cut tight",
    "Make it tighter and faster",
    "Cut it tighter than v1.",
    "Promo\n\n• Tight cuts\n• Punch-ins on jump cuts\n• Lower thirds for each chef",
    "1) Tight cuts\n2) Punch-ins on jump cuts\n3) Lower thirds for each chef",
    "(Tight cuts please)",
    '"Tight cuts"',
    "Venture OC promo, Tight cuts, lower thirds for each chef",
    "Venture OC Promo: Tight Cuts, Lower Thirds",
    "Relaxed beach cafe promo. Keep it tight.",
    "Promo for a relaxed, family-friendly brewery, tight cuts",
    "Use the slow and dreamy drone shots for B-roll, tight cuts on the interviews",
    "Slow and steady build, tight cuts",
    "The pace is slow in the original, make it snappier",
    "Keep it punchy, under 60 seconds.",
    "Make it punchier",
    "Fast-paced and energetic, end on the CTA.",
    "Snappy edit, no dead air.",
    "No dead air.",
    "no dead air, keep it tight",
    "no filler and tight cuts",
    "without music, punchy",
    "A no-nonsense punchy promo.",
    "Keep it tight but not rushed.",
    "Keep it tight, not too loose",
    "Instagram reel, vertical, super tight, hook in the first 2 seconds.",
    "Tight 30s cutdown.",
    "Pacing: tight.",
    "do tight cuts of dead space and list the restaurants and chef names with motion graphics in the right center side of frame",
    "Tight cuts with no dead air and punch-ins on the jump cuts",
]
LOOSE = [
    "Let it breathe.",
    "Let the moments breathe, this is about my son.",
    "Let the story breathe; don't rush it.",
    "Let the chefs' stories breathe.",
    "Loose pacing, documentary feel.",
    "Slow-paced and reflective.",
    "a looser cut",
    "Looser than the last one.",
    "Keep it loose and conversational.",
    "keep it loose, not tight",
    "Pacing: loose.",
    "Pacing - loose",
    "Episode 14: the hike. Hold shots longer on the views, and let it breathe.",
]
MEDIUM = [
    "",
    "The OC Vibe food hall.",
    # Turned down in its own clause.
    "Don't cut it too tight, it's a reflective piece",
    "Don’t cut it too tight",  # the apostrophe a Mac text field types
    "Don't make it too tight, let the chef talk.",
    "Don't make it tight and snappy, it's a eulogy",
    "Not too tight - Ethan talks slowly and it should feel natural.",
    "No tight cuts, it's a eulogy for my dad.",
    "Avoid tight cuts on the emotional parts.",
    "no need to be tight",
    "nothing snappy or punchy, this is a memorial",
    "never snappy",
    "the last one was too tight",
    # "tight" that isn't about the cut.
    "We're on a tight deadline, just need a first pass by Friday.",
    "Shot on a tight budget so there isn't much B-roll.",
    "We shot this on a tight schedule so some takes are rough; pick the cleanest reads.",
    "Tight shots of the food, wide shots of the dining room.",
    "Use the tight close-ups of the plating as B-roll.",
    "Tight framing on the chef.",
    "A tight-knit community of chefs and owners.",
    "Tightly framed interview, so cover jump cuts with B-roll.",
    # Words near pacing that don't ask for it.
    "Tie up any loose ends in the story by the final line.",
    "End on the punch line about the tacos.",
    "Lower third style: loose, handwritten",
    "Graphics style: loose hand-drawn feel",
    "Keep every pause he takes in the eulogy part",
    "Quick edit for Instagram",
    "Relaxed, family-friendly brewery promo.",
    # Both, or said outright.
    "Punchy open but let the emotional moments breathe.",
    "tighter, but let the moments breathe",
    "Pacing: medium.",
]

PUNCH_IN = [
    "Punch in on the jump cuts.",
    "Punching in on the jump cuts is fine.",
    "Use punch-ins to hide the jump cuts.",
    "Add punch-ins",
    "Add Punch-Ins",
    "Punch-Ins On Jump Cuts",
    "• Punch-ins on jump cuts",
    "Zoom in on jump cuts.",
    "Use zoom-ins on jump cuts.",
    "Punch in on every other jump cut so it doesn't look choppy.",
    "punch in every other cut",
    "Punch in at each cut.",
    # Another clause's "no" doesn't reach the request.
    "There's no B-roll so punch in on the jump cuts.",
    "We don't have B-roll so punch in on jump cuts.",
    "Without B-roll we need punch-ins on the jump cuts.",
    "Tight cuts with no dead air and punch-ins on the jump cuts",
    "no B-roll and punch in on jump cuts",
    "Avoid long pauses and punch in on jump cuts",
    "no ums and punch-ins",
    "Punch in on the jump cuts - no B-roll was shot.",
    "Punch-ins: no question, on every jump cut",
    # Negative words that still ask for it.
    "Don't forget to punch in on the jump cuts.",
    "I don't mind punch-ins on the talking parts.",
]
NO_PUNCH_IN = [
    "",
    "No punch-ins please.",
    "No punch ins or zooms, keep it clean.",
    "no B-roll or punch-ins",
    "I don't want any punch-ins, it's a doc.",
    "don't add punch-ins",
    "Don't punch in on jump cuts",
    "Don’t punch in on jump cuts",
    "never use punch-ins",
    "without any punch-ins",
    "avoid using punch ins",
    "Skip the punch-ins, just cut it straight",
    "We really don't want you to punch in on the cuts",
    "Do not under any circumstances punch in on jump cuts",
    "Please don't do any of those zoom-in jump cuts, the client hates them.",
    "Punch-ins: none please",
    "punch ins? no.",
    "Punch-ins - no",
    "Punch-ins – no thanks",
    # One reframe on one moment, not a punch-in at every jump cut.
    "Punch in on the burger when he takes the first bite.",
    "Punch in on Roy's face for the last line.",
    "Punch in at the reveal",
    "Zoom in on the chef when he talks about the menu.",
    # Not about framing at all.
    "Dad punches in at the shop at 6am, show the morning routine.",
    "Me punching in at work at 6am, then the drive home.",
    "a punchy promo",
    "the punch line",
]


@pytest.mark.parametrize("brief", TIGHT)
def test_without_ai_briefs_that_ask_for_a_tight_cut(brief):
    assert style_from_brief(brief)[0] == "tight"


@pytest.mark.parametrize("brief", LOOSE)
def test_without_ai_briefs_that_ask_for_a_loose_cut(brief):
    assert style_from_brief(brief)[0] == "loose"


@pytest.mark.parametrize("brief", MEDIUM)
def test_without_ai_briefs_that_dont_ask_for_tight_or_loose(brief):
    assert style_from_brief(brief)[0] == "medium"


def test_without_ai_the_log_quotes_the_words_it_read():
    assert style_from_brief("Short and tight.") == ("tight", 'the brief says "tight"')
    assert style_from_brief("Don't cut it too tight, it's a reflective piece") == ("medium", 'the brief says "Don\'t cut it too tight"')
    assert style_from_brief("keep it loose, not tight") == ("loose", 'the brief says "keep it loose"')
    assert style_from_brief("") == ("medium", "the brief doesn't ask for tight or loose pacing")


@pytest.mark.parametrize("brief", PUNCH_IN)
def test_without_ai_briefs_that_ask_for_punch_ins(brief):
    assert punch_in_from_brief(brief)
    assert read_punch_in(brief)[0] is True


@pytest.mark.parametrize("brief", NO_PUNCH_IN)
def test_without_ai_briefs_that_dont_ask_for_punch_ins(brief):
    assert not punch_in_from_brief(brief)
    assert read_punch_in(brief)[0] is not True


def test_without_ai_a_turned_down_punch_in_says_so():
    assert read_punch_in("No punch-ins please.") == (False, "No punch-ins")
    assert read_punch_in("Punch-ins - no")[0] is False
    assert read_punch_in("Punch in on Roy's face for the last line.") == (None, "")


def test_the_built_in_preset_briefs_read_as_their_own_pacing():
    for preset in presets.BUILT_IN.values():
        assert style_from_brief(preset.brief)[0] == preset.style, preset.name
        assert read_punch_in(preset.brief) == (None, ""), preset.name


@pytest.mark.parametrize("preset", sorted(presets.BUILT_IN))
@pytest.mark.parametrize("own", [*TIGHT, *LOOSE, *MEDIUM, *PUNCH_IN, *NO_PUNCH_IN])
def test_without_ai_your_own_words_beat_the_presets_brief(preset, own):
    # The preset's brief comes first, then yours (`--preset` in Terminal, a preset picked in the app).
    preset_brief = presets.BUILT_IN[preset].brief
    whole = preset_brief + "\n\n" + own if own else preset_brief
    mine = style_from_brief(own)
    said_nothing = mine[1] == "the brief doesn't ask for tight or loose pacing"
    assert style_from_brief(whole, preset_brief)[0] == (presets.BUILT_IN[preset].style if said_nothing else mine[0])
    assert read_punch_in(whole, preset_brief) == read_punch_in(own)


def test_a_preset_brief_says_what_it_reads_when_yours_says_nothing():
    whole = presets.SAVE_THE_KID_BRIEF + "\n\nThe hike with Myles."
    assert style_from_brief(whole, presets.SAVE_THE_KID_BRIEF) == ("loose", 'the preset\'s brief says "Let moments breathe"')


GRAPHICS = [
    "do tight cuts of dead space and list the restaurants and chef names based on this script with motion graphics in the right center side of frame",
    "Add lower thirds for each chef.",
    "Add Lower Thirds for each chef",
    "• Lower thirds for each speaker",
    "A lower third with each speaker's name and role.",
    "Put their names on screen as they're introduced.",
    "Show the names on screen.",
    "Title card at the open, then callouts for the big numbers.",
    "Kinetic captions on the hook.",
    "Graphics: an episode title card and a name lower third for new people.",
    "There's no B-roll, so add on-screen text for each dish.",
]
NO_GRAPHICS = [
    "",
    "No graphics, tight cuts.",
    "no lower thirds or titles, keep it raw",
    "Without graphics this time.",
    "Graphics: none.",
    "Keep it tight and let the chef talk.",
    "The chef's name is Roy Choi.",
    "She runs a graphic design studio downstairs.",
    "The graphics designer is in the video.",
    "Our graphics department made the logo.",
    "List the best moments first.",
]


@pytest.mark.parametrize("brief", GRAPHICS)
def test_without_ai_briefs_that_ask_for_graphics(brief):
    assert graphics_from_brief(brief)


@pytest.mark.parametrize("brief", NO_GRAPHICS)
def test_without_ai_briefs_that_dont_ask_for_graphics(brief):
    assert not graphics_from_brief(brief)


# ------------------------------------------------------------ the CLI keeps the preset's part apart


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


def test_a_build_without_ai_reads_your_words_before_its_presets(footage, config, tmp_path, capsys):
    from roughcut.cli import main

    # A preset of your own with no pacing of its own: Auto reads the briefs, yours first.
    assert main(["preset", "save", "walk", "--brief", "Walk-and-talk rundown. Short and punchy.", "--no-graphics"]) == 0
    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    argv = ["build", str(footage), "-o", str(out), "--preset", "walk", "-c", "Not too tight, let it breathe.", "--style", "auto", *common]
    assert main(argv) == 0
    assert 'pacing: loose (the brief says "let it breathe")' in capsys.readouterr().err
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert settings["style"] == "loose"
    assert settings["context"] == "Walk-and-talk rundown. Short and punchy.\n\nNot too tight, let it breathe.", "the planner still reads both"
    assert settings["preset_brief"] == "Walk-and-talk rundown. Short and punchy."
    # Saying nothing about pace: the preset's brief decides.
    assert main(["build", str(footage), "-o", str(out), "--preset", "walk", "-c", "The OC Vibe food hall.", "--style", "auto", *common]) == 0
    assert "pacing: tight (the preset's brief says \"punchy\")" in capsys.readouterr().err


def test_a_build_without_ai_says_how_it_read_punch_ins(footage, tmp_path, capsys):
    from roughcut.cli import main

    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", str(footage), "-o", str(out), "-c", "No punch-ins please, it's a doc.", *common]) == 0
    assert 'not punching in (the brief says "No punch-ins")' in capsys.readouterr().err
    assert main(["build", str(footage), "-o", str(out), "-c", "There's no B-roll so punch in on the jump cuts.", *common]) == 0
    assert 'punching in on jump cuts (the brief says "punch in on the jump cuts")' in capsys.readouterr().err
    assert json.loads((out / "plan.json").read_text())["settings"]["punch_in"] is True


def test_the_apps_preset_brief_is_sent_apart_and_not_twice(footage, tmp_path, capsys):
    from roughcut.cli import main

    # The app's brief starts with the preset's; it sends that part again so the CLI knows which words are yours.
    out = tmp_path / "out"
    preset_brief = presets.VENTURE_OC_BRIEF
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    argv = ["build", str(footage), "-o", str(out), f"--context={preset_brief}\n\nLet the chefs' stories breathe.", f"--preset-brief={preset_brief}"]
    assert main([*argv, "--style", "auto", *common]) == 0
    assert "pacing: loose (the brief says \"Let the chefs' stories breathe\")" in capsys.readouterr().err
    settings = json.loads((out / "plan.json").read_text())["settings"]
    assert settings["context"] == f"{preset_brief}\n\nLet the chefs' stories breathe.", "the preset's brief once"
    assert settings["preset_brief"] == preset_brief

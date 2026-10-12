"""What a brief says about pacing, punch-ins and graphics, read the way the owner writes briefs.

A "no", "don't" or "avoid" counts only in its own clause, "tight" only when it's about the cut,
your own words before a preset's, and "punch in on Roy's face" is one reframe, not a setting.
"""

from __future__ import annotations

import json

import pytest

from roughcut import presets
from roughcut.pipeline import graphics_from_brief, punch_in_from_brief, read_punch_in, style_from_brief

TIGHT = [
    "Keep it punchy, under 60 seconds.",
    "Make it snappier than the last one.",
    "Make it punchier",
    "Fast cuts, high energy, music driven.",
    "Quick pace, get to the food fast.",
    "High energy promo with quick cuts between the chefs.",
    "Rapid-fire rundown of every vendor.",
    "Keep it tight.",
    "Venture OC promo for the new food hall. Keep it tight, 60 seconds, end on the opening date.",
    "Instagram reel, vertical, super tight, hook in the first 2 seconds.",
    "Super tight edit for reels.",
    "Tight pacing throughout.",
    "Tight: under 90s.",
    "Tight 30s cutdown.",
    "A tight 60 for Instagram.",
    "Tighten up the pauses, he rambles.",
    "He tends to ramble; tighten it up a lot.",
    "Cut it tighter than v1.",
    "Make it tighter.",
    "Fast-paced and energetic, end on the CTA.",
    "Snappy edit, no dead air.",
    "Trim the dead air hard, every pause out.",
    "Trim pauses tight but keep the breaths before the punchline.",
    "do tight cuts of dead space and list the restaurants and chef names with motion graphics in the right center side of frame",
    "no filler and tight cuts",
    "no dead air, keep it tight",
    "without music, punchy",
    "A no-nonsense punchy promo.",
    "Keep it tight but not rushed.",
    "Keep it tight, not too loose",
    "Fast-paced, but don't cut off his laughs.",
    "Don't be afraid to cut it tight.",
    "Pacing: tight.",
    "Pace: fast. Energy: high.",
]
LOOSE = [
    "Let it breathe.",
    "Let the moments breathe, this is about my son.",
    "Let the story breathe; don't rush it.",
    "Let the chefs' stories breathe.",
    "Keep it loose and conversational.",
    "Keep it loose.",
    "Loose, documentary feel.",
    "Loosely cut, documentary feel.",
    "a looser cut",
    "Slow-paced and reflective.",
    "Slow and reflective, hold on the kids.",
    "Slow it down this time.",
    "Slower than the last episode.",
    "Unhurried, give the moments room to breathe.",
    "Don't rush the emotional moments.",
    "Relaxed pace, Sunday vibe.",
    "Keep the pacing relaxed, it's a Sunday vlog.",
    "Let the laughter play out.",
    "Episode 14: the hike. Hold shots longer on the views, and let it breathe.",
    "keep it loose, not tight",
    "Pacing: loose.",
    "Pacing - loose",
    "Style: loose",
]
MEDIUM = [
    "",
    # Turned down: the opposite of a request for tight.
    "Don't cut it too tight, it's a reflective piece",
    "Don’t cut it too tight",  # the apostrophe a Mac text field types
    "Don't make it too tight, let the chef talk.",
    "Don't make it tight and snappy, it's a eulogy",
    "Not too tight - Ethan talks slowly and it should feel natural.",
    "No tight cuts, it's a eulogy for my dad.",
    "Avoid tight cuts on the emotional parts.",
    "avoid tight cuts",
    "no need to be tight",
    "nothing snappy or punchy, this is a memorial",
    "never snappy",
    "not snappy, reflective",
    "Don't cut it too snappy",
    "no fast-paced edits",
    "Not a fast-paced edit. Let the chef explain each dish.",
    "No slow pacing, this is a hype video.",
    "the last one was too tight",
    # "tight" that isn't about the cut.
    "We're on a tight deadline, just need a first pass by Friday.",
    "Shot on a tight budget so there isn't much B-roll.",
    "Tight turnaround on this one, client review Monday.",
    "We shot this on a tight schedule so some takes are rough; pick the cleanest reads.",
    "Tight on time, just get the story in order.",
    "Tight shots of the food, wide shots of the dining room.",
    "Use the tight close-ups of the plating as B-roll.",
    "Tight CU of the burger for the open.",
    "Tightly framed interview, so cover jump cuts with B-roll.",
    "Tightly cropped verticals.",
    "A tight-knit community of chefs and owners.",
    "These chefs are really tight with each other.",
    "Keep the lower thirds tight and minimal.",
    "The owners have a tight relationship with local farms.",
    "It's a tight squeeze in the kitchen, show that.",
    "The space is tight, show the crowd energy.",
    "Hold on tight, the drive is bumpy.",
    "Tighten the ending.",
    "tighter hook, drop the second interview line, more drone over the intro",
    "Punch Bowl Social, Tight Lines Fishing Co, Snappy Salads, The Loose Goose",
    "Graphics style: loose hand-drawn feel",
    "Tie up any loose ends in the story by the final line.",
    "End on the punch line about the tacos.",
    # Both, or said outright.
    "Punchy open but let the emotional moments breathe.",
    "tighter, but let the moments breathe",
    "Pacing: medium.",
    "Pacing should be medium, nothing rushed.",
]

PUNCH_IN = [
    "Punch in on the jump cuts.",
    "Punching in on the jump cuts is fine.",
    "Use punch-ins to hide the jump cuts.",
    "Add punch-ins",
    "Zoom in on jump cuts.",
    "Zoom-ins on the cuts.",
    "Use zoom-ins on jump cuts.",
    "Add some zoom cuts on the talking head.",
    "Do a punch in at each jump cut.",
    "Scale up 110% on alternating jump cuts.",
    "Zoom in 10% on every other cut.",
    "Use digital zooms to hide the jump cuts.",
    "Hide the jump cuts with a punch in.",
    "Interview with the owner, single cam. Punch in on every other jump cut so it doesn't look choppy.",
    # Another clause's "no" doesn't reach the request.
    "There's no B-roll so punch in on the jump cuts.",
    "We don't have B-roll so punch in on jump cuts.",
    "Skip the B-roll and punch in on the cuts instead.",
    "Without B-roll we need punch-ins on the jump cuts.",
    "Single camera, no b-roll, so punch in on the jump cuts",
    "Tight cuts with no dead air and punch-ins on the jump cuts",
    "no B-roll and punch in on jump cuts",
    "Avoid long pauses and punch in on jump cuts",
    "cut out the pauses without B-roll and punch in on the jump cuts",
    "no filler and punch in on jump cuts",
    "no ums and punch-ins",
    "no ums but punch in on the cuts",
    "Interview with the owner, single cam. No B-roll was shot, so use punch-ins to cover the edits.",
    # Negative words that still ask for it.
    "Don't forget to punch in on the jump cuts.",
    "Don't be afraid to punch in on jump cuts.",
    "I don't mind punch-ins on the talking parts.",
    "Never leave a raw jump cut: punch in.",
    "Keep it punchy and punch in on the jump cuts.",
    "A rundown of 12 vendors, quick cuts between each, punch-ins on the jump cuts.",
]
NO_PUNCH_IN = [
    "",
    "No punch-ins please.",
    "No punch ins or zooms, keep it clean.",
    "No zooms.",
    "no B-roll or punch-ins",
    "no zooms or punch-ins",
    "I don't want any punch-ins, it's a doc.",
    "don't add punch-ins",
    "Don't punch in on jump cuts",
    "Don’t punch in on jump cuts",
    "never use punch-ins",
    "Never punch in.",
    "without any punch-ins",
    "without zoom cuts",
    "Avoid zoom cuts.",
    "avoid using punch ins",
    "Skip the punch-ins, just cut it straight",
    "We really don't want you to punch in on the cuts",
    "Do not under any circumstances punch in on jump cuts",
    "Please don't do any of those zoom-in jump cuts, the client hates them.",
    "Punch-ins: none please",
    "punch ins? no.",
    # One reframe on one moment, not a punch-in at every jump cut.
    "Punch in on the burger when he takes the first bite.",
    "Punch in on Roy's face for the last line.",
    "List the restaurants and chefs. Punch in on Roy's face when he says the truck started it all.",
    "Punch in at the reveal",
    "Can you punch in on the speaker during the Q&A answers?",
    "Zoom in on the chef when he talks about the menu.",
    "Zoom in on the menu board when he mentions prices.",
    "Add zoom-ins on the food",
    "Scale up the logo",
    "the punch-in on V03 looks soft, lose it",
    # Not about framing at all.
    "Dad punches in at the shop at 6am, show the morning routine.",
    "Me punching in at work at 6am, then the drive home.",
    "a punchy promo",
    "the punch line",
    "Punch Bowl Social, Tight Lines Fishing Co",
]


@pytest.mark.parametrize("brief", TIGHT)
def test_briefs_that_ask_for_a_tight_cut(brief):
    assert style_from_brief(brief)[0] == "tight"


@pytest.mark.parametrize("brief", LOOSE)
def test_briefs_that_ask_for_a_loose_cut(brief):
    assert style_from_brief(brief)[0] == "loose"


@pytest.mark.parametrize("brief", MEDIUM)
def test_briefs_that_dont_ask_for_tight_or_loose(brief):
    assert style_from_brief(brief)[0] == "medium"


def test_a_turned_down_tight_says_why():
    assert style_from_brief("Don't cut it too tight, it's a reflective piece") == ("medium", 'the brief says "Don\'t cut it too tight"')
    assert style_from_brief("keep it loose, not tight") == ("loose", 'the brief says "keep it loose"')


@pytest.mark.parametrize("brief", PUNCH_IN)
def test_briefs_that_ask_for_punch_ins(brief):
    assert punch_in_from_brief(brief)
    assert read_punch_in(brief)[0] is True


@pytest.mark.parametrize("brief", NO_PUNCH_IN)
def test_briefs_that_dont_ask_for_punch_ins(brief):
    assert not punch_in_from_brief(brief)


def test_a_turned_down_punch_in_says_so():
    assert read_punch_in("No punch-ins please.") == (False, "No punch-ins")
    assert read_punch_in("Punch in on Roy's face for the last line.") == (None, "")


def test_the_built_in_preset_briefs_read_as_their_own_pacing():
    assert style_from_brief(presets.VENTURE_OC_BRIEF)[0] == "tight"
    assert style_from_brief(presets.SAVE_THE_KID_BRIEF)[0] == "loose"
    for preset in presets.BUILT_IN.values():
        assert style_from_brief(preset.brief)[0] == preset.style, preset.name
        assert read_punch_in(preset.brief) == (None, ""), preset.name


@pytest.mark.parametrize("preset", sorted(presets.BUILT_IN))
@pytest.mark.parametrize("own", [*TIGHT, *LOOSE, *MEDIUM, *PUNCH_IN, *NO_PUNCH_IN])
def test_your_own_words_beat_the_presets_brief(preset, own):
    # The preset's brief comes first, then yours (`--preset` in Terminal, a preset picked in the app).
    preset_brief = presets.BUILT_IN[preset].brief
    whole = preset_brief + "\n\n" + own if own else preset_brief
    mine = style_from_brief(own)
    said_nothing = mine[1].startswith("the default")
    assert style_from_brief(whole, preset_brief)[0] == (presets.BUILT_IN[preset].style if said_nothing else mine[0])
    assert read_punch_in(whole, preset_brief) == read_punch_in(own)


def test_a_preset_brief_says_what_it_reads_when_yours_says_nothing():
    whole = presets.SAVE_THE_KID_BRIEF + "\n\nThe hike with Myles."
    assert style_from_brief(whole, presets.SAVE_THE_KID_BRIEF) == ("loose", 'the preset\'s brief says "Let moments breathe"')


GRAPHICS = [
    "do tight cuts of dead space and list the restaurants and chef names based on this script with motion graphics in the right center side of frame",
    "Add lower thirds for each chef.",
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
    "List the best moments first.",
]


@pytest.mark.parametrize("brief", GRAPHICS)
def test_briefs_that_ask_for_graphics(brief):
    assert graphics_from_brief(brief)


@pytest.mark.parametrize("brief", NO_GRAPHICS)
def test_briefs_that_dont_ask_for_graphics(brief):
    assert not graphics_from_brief(brief)


# ------------------------------------------------------------ the CLI keeps the preset's part apart


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUGHCUT_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


def test_a_build_reads_your_words_before_its_presets(footage, config, tmp_path, capsys):
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


def test_a_build_says_when_it_reads_no_punch_ins(footage, tmp_path, capsys):
    from roughcut.cli import main

    out = tmp_path / "out"
    common = ["--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]
    assert main(["build", str(footage), "-o", str(out), "-c", "No punch-ins please, it's a doc.", *common]) == 0
    assert 'not punching in (the brief says "No punch-ins")' in capsys.readouterr().err
    assert main(["build", str(footage), "-o", str(out), "-c", "There's no B-roll so punch in on the jump cuts.", *common]) == 0
    assert 'punching in on jump cuts (the brief says "punch in")' in capsys.readouterr().err
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


# ------------------------------------------------------------ a brief that asks for graphics while they're off

SCALE_BRIEF = (
    "do tight cuts of dead space and list the restaurants and chef names based on this script "
    "with motion graphics in the right center side of frame:\nKogi - Roy Choi\nOx Burger - Damon Cho"
)


def test_a_brief_asking_for_graphics_while_theyre_off_says_how_to_add_them(footage, tmp_path, monkeypatch):
    from conftest import FakeProvider

    from roughcut import pipeline

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    out = tmp_path / "out"
    opts = pipeline.Options(inputs=[footage], out=out, name="footage", transcriber="none", cache_dir=tmp_path / "c", context=SCALE_BRIEF, style="auto")
    result = pipeline.build(opts)
    note = next(w for w in result.warnings if w.startswith("The brief asks for graphics"))
    assert '"list the restaurants and chef names"' in note and "motion graphics are off" in note
    assert "roughcut revise" in note and "--graphics" in note and "--graphics in Terminal" in note
    report = (out / "edit_report.md").read_text()
    top = report.split("- **Rough cut:**")[0]
    assert note in top, "at the top of the report, not only in its Warnings"
    assert note in json.loads((out / "result.json").read_text())["warnings"]
    assert note in (out / "roughcut.log").read_text()


def test_no_graphics_note_when_the_brief_turns_them_down_or_theyre_on(footage, tmp_path, monkeypatch):
    from conftest import FakeProvider

    from roughcut import pipeline

    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: FakeProvider())
    opts = pipeline.Options(inputs=[footage], out=tmp_path / "o1", name="footage", transcriber="none", cache_dir=tmp_path / "c", context="No graphics, tight cuts.")
    assert not any("asks for graphics" in w for w in pipeline.build(opts).warnings)
    # A preset's brief that describes its graphics, with graphics turned off on purpose: not a request of yours.
    stk = presets.SAVE_THE_KID_BRIEF
    opts = pipeline.Options(inputs=[footage], out=tmp_path / "o2", name="footage", transcriber="none", cache_dir=tmp_path / "c", context=stk + "\n\nThe hike.", preset_brief=stk)
    assert not any("asks for graphics" in w for w in pipeline.build(opts).warnings)


def test_a_cut_without_ai_says_the_graphics_need_it(footage, tmp_path, capsys):
    from roughcut.cli import main

    out = tmp_path / "out"
    assert main(["build", str(footage), "-o", str(out), "-c", "Add lower thirds for each chef.", "--provider", "none", "--transcriber", "none", "--cache-dir", str(tmp_path / "c")]) == 0
    captured = capsys.readouterr()
    assert 'The brief asks for graphics ("lower thirds"), but this cut was made without AI' in captured.out, "in the CLI's summary, not only the log"
    assert "--provider claude-code" in captured.out

# roughcut

Dump a folder of footage in and get a Final Cut Pro rough cut out.

`roughcut` transcribes every clip on your Mac, removes dead air and "um"s, asks Claude to build the story (hook, sections, best takes, B-roll on the words it illustrates, music), levels the audio, can design animated graphics, and writes an FCPXML file. Import it into Final Cut and you get a real, fully editable project that references your original media, so every cut keeps its handles. It also adds a **Stringout** project (all dialogue in recording order, dead air removed) and organizes the event browser with keywords (A-Roll, B-Roll, Stills, Music, alternate takes, visual tags).

There's a Mac app ([`mac/`](mac/README.md)) and a command-line tool. They do the same thing; the app runs the command-line tool for you.

## Setup (Mac, Apple Silicon)

```bash
brew install ffmpeg python@3.12
git clone https://github.com/ethanhauptli-collab/Archer-OS.git ~/Archer-OS
cd ~/Archer-OS
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[mac,graphics,dev]'
ln -sf "$PWD/.venv/bin/roughcut" /opt/homebrew/bin/roughcut
mac/scripts/build-app.sh --install
```

That installs `roughcut` for every Terminal window (no `source .venv/bin/activate` needed) and puts **Roughcut.app** in Applications. The `mac` extra is mlx-whisper (fast on-device transcription; the first run downloads its model, about 1.6 GB, once) and `graphics` is the renderer for motion graphics. Graphics render with Google Chrome if it's installed; if not, run this once:

```bash
~/Archer-OS/.venv/bin/python -m playwright install chromium
```

Then choose how the AI steps are paid for:

- **Your Claude Pro/Max plan** (no API key): install Claude Code (`npm install -g @anthropic-ai/claude-code`), run `claude` once and type `/login`. Pick "Claude (your Pro/Max plan)" in the app, or add `--provider claude-code` in Terminal.
- **An API key** from [console.anthropic.com](https://console.anthropic.com) → API Keys: `roughcut key set` (or the app's Settings → API Keys) saves it in the macOS Keychain.

`roughcut doctor` (or the app's banner) checks ffmpeg, transcription, the graphics renderer, Claude Code and your key.

**Updating:** `roughcut update` from any folder, or **Roughcut → Update Roughcut…** in the app. It pulls the latest code, reinstalls only when the dependencies changed, and lists what's new. If the Mac app changed, it prints the one command that rebuilds it. A copy too old to have `roughcut update` (it says "invalid choice: 'update'") updates once with:

```bash
cd ~/Archer-OS && git pull && .venv/bin/python -m pip install -e '.[mac,graphics,dev]'
```

## Using the app

1. Drop your footage folder (or clips) on the window.
2. Pick a **Preset** (Venture OC or Save the Kid fill in the brief, length, pacing and graphics look), then add to the brief what's special about this video: who's in it, the must-keep moments, the angle.
3. Optional: a **Music bed** song, a different **Dialogue level**, **Motion graphics** and a logo.
4. **Build Rough Cut**. The checklist shows each step; you can switch to another app, and the Mac stays awake and tells you when it's done.
5. **Open in Final Cut Pro**, choose a library, and open the project the result screen names (for example "OC Food Fest - Rough Cut v1 (tight, Opus)").

After a cut you can:
- **Re-cut** it at another pacing or format, or with punch-ins on or off, with no AI call.
- **Revise with notes**: type what to change ("tighter hook, drop the second interview line, more drone over the intro") and Claude re-plans it as a new version.
- Open the **Paper Edit**.

**Recent rough cuts** on the main screen reopen earlier results.

## Using Terminal

A Venture OC promo, using the preset and adding this video's specifics:

```bash
roughcut build ~/Movies/OCFoodFest --preset venture-oc -c "OC Food Fest at Huntington Beach. Feature Maria, the festival director. End on the ticket price."
```

A Save the Kid episode with a song from your Downloads under it:

```bash
roughcut build ~/Movies/SurfDay --preset save-the-kid --music ~/Downloads/song.m4a -c "Phoenix's first surf lesson. Keep the wipeout and the high five."
```

Then revise it from your notes, as version 2 (no re-transcription):

```bash
roughcut revise ~/Movies/SurfDay_roughcut "open on the wipeout, cut the parking lot part, end on the high five"
```

Re-cut the latest version tighter, or vertical for Reels and Shorts, with no AI call:

```bash
roughcut render ~/Movies/SurfDay_roughcut --style tight --format vertical
```

No AI at all, just cut the silence (like Recut or TimeBolt):

```bash
roughcut build ~/Movies/Talk --provider none
```

Paths with spaces need quotes, or drag the folder from Finder into the Terminal window and it's typed for you. If you forget the quotes, roughcut works out which folder you meant when it can and shows the quoted form for next time; otherwise it stops before doing anything and prints the command with the quotes in place. Add `--open` to any build, render or revise to open the new cut in Final Cut Pro when it's done.

## What you get

Everything goes in `<footage folder>_roughcut/`, next to your footage (or wherever `-o` says).

| File | What it is |
|---|---|
| `<name> vN.fcpxml` | The newest cut: an event with keyworded clips, the **Rough Cut vN** project and the **Stringout vN** project. Import it with File → Import → XML… |
| `<name> vN.srt` | Captions for the cut: what's said on camera and in narration, timed to the cut on whole frames, without ums, at most two lines of 42 characters (a comma starts the next caption only when the rest of the sentence won't fit in this one, a sentence's last word or two never stand alone, and "Jr." or "Dr." inside a sentence doesn't end the caption). When Claude plans the cut, names the transcript misspells are written as your brief spells them, here and in the graphics; a cut made without AI (or the stringout fallback) keeps the transcript's spellings. In Final Cut, with the project open: File > Import > Captions. Each version has its own |
| `edit_report.html` | The paper edit as a page to read or print: sections, every line used, B-roll and why, alternate takes, YouTube chapters (with a Copy button), what was cut and why, levels, a cut check against your target length, and warnings. `edit_report.md` is the same in Markdown |
| `history/` | The last five earlier versions, each with its own FCPXML, plan and paper edit. Earlier projects you imported keep working |
| `graphics/` | Each graphic as an editable `.html` design and a rendered `.mov`, plus `preview.html` showing every graphic over the shot it sits on |
| `plan.json`, `analysis.json`, `prompt.md` | The plan with the run's settings, the transcripts and clip data, and the exact prompt Claude saw |
| `result.json`, `versions.json` | The run summary and the list of versions (the app reads these) |
| `roughcut.log` | Every step of every run with times, ending in FINISHED or STOPPED and why |

When a run ends, Terminal and the app say what made it in and what was left out (graphics, clips described, unreadable files) and roughly what the AI steps cost, per step.

### In Final Cut Pro

- **Markers**: chapter markers for each section; to-do markers for decisions to check (an uncovered jump cut, a music jump inside a finished promo, a line you asked to keep that didn't fit); standard markers name the alternate takes of a line.
- **Roles**: dialogue (narration as its own subrole), B-Roll (planned shots and auto-fill separately), nat sound as effects, the music bed as music, graphics as titles. In the Timeline Index you can mute the music or hide the auto-fill in one click.
- **Notes**: each planned B-roll shot carries Claude's reason for it in the inspector.
- **Keywords**: the other reads of each line you used are marked "Alt take" in the browser, so swapping a read is select, then Shift-R. Duplicate exports are marked too.
- **Levels**: every clip's speech is matched to the dialogue level, B-roll sound sits under it, and the music ducks under speech and comes up between lines. Volume and fades are ordinary FCP volume settings and fade handles you can drag.

## Presets

A preset is the brief, target length, pacing, format and graphics look you use for one kind of video. Two are built in:

- `venture-oc`: Venture Orange County promos. Energetic, 1:30, tight, graphics on, in the ventureoc.com look: navy, orange and gold, Jost, Inter and Fraunces.
- `save-the-kid`: the personal channel. Intimate and reflective, 10 minutes, loose pacing, minimal graphics.

Anything you type wins over the preset (`-t 2m`, `--style medium`, `--no-graphics`), and your `-c` brief is added to the preset's. `roughcut preset list` shows them all and `roughcut preset show venture-oc` shows everything one sets.

Give a preset your logo once, and every run with it can put the logo on title cards and lower thirds:

```bash
roughcut preset save venture-oc --logo ~/Brand/venture-logo.png
```

Once a run's graphics look right (edit their HTML in `graphics/` and re-render until they do), make them the preset's approved look:

```bash
roughcut preset save venture-oc --look-from ~/Movies/OCVIBE_roughcut G01 G03
```

Every later run with that preset shows those designs to the designer, which matches their type, colors, plates and motion. Name up to three graphics by the number in their file name, or leave the numbers off to take the first two of different kinds. The preset keeps its own copies, so the run folder can go; `--no-look` drops them.

`roughcut preset save` also makes new presets (`--from save-the-kid` starts from an existing one; `--brief`, `--brief-file`, `-t`, `--style`, `--format`, `--graphics` or `--no-graphics`, `--graphics-style` set the rest). Yours are saved as JSON in `~/Library/Application Support/roughcut/presets/`, and `roughcut preset remove NAME` deletes one (a built-in preset comes back as it was).

## Revise, re-cut and finish

- **`roughcut revise FOLDER "notes"`** re-plans the latest cut from your notes as the next version. Claude sees the footage, the current cut and every earlier note, and changes only what you asked. `--keep V01.S003,V02.S010` and `--avoid V04` (or `V04@12-30` for seconds 12 to 30 of a clip) pin lines and shots; what you avoid stays out of every later revision and re-cut until a later `--keep` covers it (a line by its ID; a range by its clip or a line that overlaps it; a clip by the clip or any line of it, which brings the whole clip back), and a line you asked to keep that the revision left out stays a to-do marker in re-cuts too. `-t 60s` sets a new length. Graphics that didn't change are reused as they are, hand edits included. Name spellings carry over too; a note like "Choy is right, don't change it" undoes one, in the captions and in the graphics. The paper edit opens with what changed. Notes can change the pacing and punch-ins too: Claude reads them against the cut's settings, so "make it looser" or "too tight" on a medium cut gives a loose version, "it drags" a tight one, and "remove the punch-ins" or "add punch-ins" turns them off or on. Only these notes count, not the brief or earlier notes, so a pacing you picked or a re-cut you made since stays unless these notes ask to change it. The log and the paper edit's revision block say what changed; `--style` or `--punch-in`/`--no-punch-in` given to revise win over the notes. A note asking for graphics when they're off is answered at the top of the paper edit and at the end of the run with the `--graphics` command that adds them.
- **`roughcut render FOLDER`** rebuilds the latest plan with no AI call. It keeps the run's own pacing, format, fillers, punch-ins, levels and fades unless you give new ones (`--style tight`, `--format vertical`, `--format auto` for the footage's own, `--no-keep-fillers`, `--punch-in` or `--no-punch-in`, `--dialogue-level -14`); the app's Re-cut works the same way.
- **If a run stops** after Claude planned the cut (a crash, a closed laptop, a graphic that wouldn't render), nothing paid for is lost: the plan was saved first, and roughcut prints the `roughcut render FOLDER` command that finishes the cut without AI. The app shows a **Finish without AI** button.
- **If your Claude plan's usage limit or your API credit runs out**, the run stops and says so; it never delivers a silence-only cut that looks finished. Transcripts and clip descriptions are cached, so the same command after the reset carries on from there. If it ran out while designing graphics, the cut is written with the graphics that were done, and `roughcut render FOLDER --design-missing` designs the rest after the reset without re-planning anything. Plain `render` also tells you when a cut has graphics with no design yet.

## Audio

| Option | Default | |
|---|---|---|
| `--dialogue-level LUFS` | -16 | Every clip's speech is matched to this loudness (-14 is louder, for phones). `off` keeps clips as recorded |
| `--broll-db`, `--music-db` | -20, -14 | B-roll nat sound and music, in dB under the dialogue |
| `--music FILE` | | A song to lay under the cut (repeatable). It ducks under speech, comes up between lines, fades in, and is back-timed so its real ending lands on the last frame |
| `--fades on/off` | on | Short de-click fades at audio edits and soft fades on B-roll sound (never over a word) |

A boost stops before the peaks clip, and the paper edit's Levels table says when a clip couldn't reach the dialogue level or brings a noisy background up with it (Voice Isolation or a limiter in Final Cut finishes the job).

## Motion graphics

```bash
roughcut build ~/Movies/OCVIBE --preset venture-oc
```

With graphics on (`--graphics`, or a preset that has them on), Claude also plans animated graphics while it plans the cut: lower thirds (names and roles only when the footage or brief states them, names spelled as the brief spells them), stat callouts, title cards, word-by-word kinetic captions, pulled quotes and short lists. It picks the moments and the exact words; roughcut times each graphic to the frame from the transcript, never two on screen at once. A second request designs every graphic as an HTML animation in one look (the preset's, your `--graphics-style`, or a clean default), seeing a frame of the shot each one sits over, and on vertical cuts keeping clear of the Reels/TikTok buttons. A browser renders them frame by frame to transparent ProRes 4444 at the sequence size, and they land on their own lane above the B-roll.

Graphics are never turned on by the brief alone, since they add model calls and need the renderer. When your brief asks for them ("lower thirds", "list the chef names", "motion graphics") and they're off, the top of the paper edit and the end of the run say so, with the `roughcut revise FOLDER --graphics` command that adds them without re-transcribing. Claude decides whether your brief asks for them while it plans (a preset's own graphics line doesn't count), and is told the cut has none, so the paper edit doesn't describe lower thirds that aren't there; a cut made without AI goes by the brief's wording.

- **Fonts**: graphics render offline, so a design can only use fonts roughcut can embed. Jost, Inter and Fraunces (with its italic) ship with roughcut; drop any other `.ttf`, `.otf` or `.woff2` into `~/Library/Application Support/roughcut/fonts/` and designs can use it by name.
- **Checking them**: open `graphics/preview.html` to watch every graphic over its real shot before importing. The paper edit lists anything a graphic did wrong: a script error, a missing font, blank, still on screen at the end, outside the safe area.
- **Editing them**: each design is `graphics/G01-lower-third.html`. Edit the text, colors or timing and run `roughcut render FOLDER` to re-render just that graphic; keep the `<!-- roughcut-timing ... -->` line at the end, which records the timing it was made for. A re-cut at another pacing re-times each graphic inside its design, so captions stay on the spoken words and exits land at the end, and only graphics whose timing changed re-render. A graphic one pacing can't fit (it would cover the one before) keeps its design in `graphics/left-out/` and comes back, without AI, in the next re-cut it fits; a graphic that has no design yet never pushes a designed one off the timeline, and `--design-missing` designs only the ones that fit beside the designed ones (a designed one next to it may come in a moment later or leave sooner to make room; a re-cut says which would push one out). Rendered clips are never deleted while any version uses them, because Final Cut reads them in place.
- **A design that hangs** is stopped after a time limit and left out with a note naming the file to fix; the rest of the cut is delivered.

Rendering took about 9 seconds per 4-second 1080p graphic on the 4-core test machine (an M-series Mac is usually quicker), and designing takes a minute or two for a promo's worth of graphics.

## Useful options

| Option | Default | |
|---|---|---|
| `--preset NAME` | | A brand's or channel's usual brief, length, pacing and graphics look |
| `-t, --target` | none | Target length: `8m`, `90s`, `8:30` |
| `-c, --context` / `--context-file` | | The brief: what it is, audience, tone, must-keep moments |
| `--script FILE` | | A script, outline or transcript to follow |
| `--style` | `auto` | `tight`, `medium`, `loose`: how much pause is kept. `auto`: Claude reads the brief while it plans and says which pacing it asks for (tight for tight cuts, snappy or no dead air; loose for let it breathe or slow and reflective; medium when it doesn't say), and the log says what it read. Your own words win over a preset's brief; a preset's pacing wins over `auto`. Claude sees each line's kept length at medium and how much shorter tight and longer loose play this footage, so it plans your target length at the pacing it picks. Without AI (`--provider none`, or the stringout when the model fails) a few plain wording rules stand in: tight for "tight", "tighter", "snappy", "punchy", "fast-paced" or "no dead air", loose for "let it breathe", "keep it loose", "looser", "loose pacing" or "slow-paced", not when turned down in the same sentence ("don't cut it too tight") or about something else ("a tight deadline", "tight shots"); otherwise medium |
| `--keep-fillers` | off | Don't cut um/uh (narration still loses the short ones) |
| `--punch-in` / `--no-punch-in` | when the brief asks for punch-ins | At each jump cut no B-roll covers, alternate the shot as framed with a 112% punch-in (Transform > Scale; select the clips and reset Transform to undo). A clip that doesn't fill the frame (16:9 in a vertical cut) isn't punched, since its black bars would jump in size; in a cut Claude planned, those cuts get to-do markers like other jump cuts. Re-cuts keep it. Without the flag, Claude reads the brief while it plans: punch-ins asked for at the jump cuts turn them on, a single reframe like "punch in on Roy's face" doesn't, and the log says which way it read the brief. Without AI, the wording rules turn them on only for "punch-ins", "zoom-ins" or "punch in on the (jump) cuts", not turned down ("no punch-ins", "Punch-ins: none") |
| `--format` | from footage | `1080p`, `4k`, `vertical`, `square`, or `WxH@fps` |
| `--provider` | `anthropic` | `anthropic` (API key), `claude-code` (your Claude Pro/Max plan), `openai`, `ollama`, `openai-compatible`, `none` |
| `--model` | menu in Terminal, else Opus | `opus` or `sonnet` (Claude Sonnet 5.5: faster, half the price). Any spelling works: `Sonnet 5.5`, `claude-sonnet-5-5`. Leave it off and `build` shows a menu; `--no-menu` skips it |
| `--effort` | `high` | Claude reasoning effort: `low` … `max` |
| `--no-vision` | | Skip visual descriptions of clips (cheaper, worse B-roll picks) |
| `--vision-model` | same as `--model` | A different model for describing clips, e.g. `sonnet` while Opus plans |
| `--graphics` / `--no-graphics`, `--graphics-style`, `--graphics-logo`, `--graphics-look` | off | Motion graphics, their look, a logo, and approved designs to match |
| `--transcriber` | `auto` | `mlx` (Mac), `faster-whisper`, or `none` (silence only) |
| `--language` | detect | e.g. `en` |
| `--transcripts DIR` | | Folder of `.srt`/`.vtt`/`.json` transcripts named like the clips |
| `--no-stringout`, `--no-fill` | | Leave out the Stringout project (the run still says how much dead air the cut removed); leave narration uncovered where the plan has no B-roll |
| `--open` | | Open the new cut in Final Cut Pro when it's done |

### Use your Claude subscription instead of an API key

`--provider claude-code` sends the AI steps through [Claude Code](https://claude.com/claude-code) signed in with your own Pro or Max plan, so runs count against your plan's usage limits instead of API credit. Anthropic's terms allow running the Claude Code program with your own subscription from your own scripts, but not calling the API directly with subscription credentials, so roughcut never touches your sign-in; it just runs `claude -p`. Claude Code would bill an API key instead of your plan whenever one is set, so roughcut hides `ANTHROPIC_API_KEY` from it. Big shoots use a lot of your allowance on clip descriptions; `--vision-model sonnet` or `--no-vision` stretches it.

**About API keys:** a key from console.anthropic.com starts with `sk-ant-` (`sk-ant-usr-` for new keys, `sk-ant-api03-` for older ones). If the key is missing or rejected, `roughcut build` stops right away and says why. `roughcut key status` shows where the key comes from, and `roughcut key remove` deletes it. An `ANTHROPIC_API_KEY` exported in your shell still works and takes priority. The first time Terminal reads a key the app saved (or the other way round), macOS asks for permission; click **Always Allow**.

### Other models

In these, replace `MODEL_NAME` with the model's name.

OpenAI (first `.venv/bin/python -m pip install -e '.[openai]'`, and `roughcut key set --openai` or export `OPENAI_API_KEY`):

```bash
roughcut build ~/Movies/Trip --provider openai --model MODEL_NAME
```

Local, private and free, with Ollama:

```bash
roughcut build ~/Movies/Trip --provider ollama --model MODEL_NAME --no-vision
```

Anything OpenAI-compatible (LM Studio, OpenRouter, Groq...):

```bash
roughcut build ~/Movies/Trip --provider openai-compatible --base-url http://localhost:1234/v1 --model MODEL_NAME
```

Claude requests opt into Anthropic's server-side refusal fallbacks (`fallbacks: "default"`). If a request is ever declined by a safety classifier, the API reruns it on the recommended fallback model instead of failing.

### Transcripts you already have

A transcript next to a clip wins over transcribing it again: `IMG_1234.srt`, `.vtt`, or Whisper-style `.json` beside `IMG_1234.MOV` (or in a `--transcripts` folder). SRT and VTT only have line-level timing, so word timing inside a line is estimated and cuts are less precise than with a fresh transcript.

## How it works

1. **Analyze** (cached, so re-runs are fast): `ffprobe` reads rate, size, rotation and timecode; `ffmpeg` measures each clip's background noise and loudness and maps the pauses above the noise (so a loud room or a very quiet recording still gets its dead air found); Whisper transcribes with word timestamps. A clip that can't be decoded is cut on its pauses with a warning instead of stopping the run. Words over silence are dropped as hallucinations. Words are grouped into sentence segments with IDs like `V03.S012`, and clips are classified as A-roll, B-roll, voiceover, music or stills. Footage exported more than once (the same promo in 16:9, 9:16 and 4:3) is used once, several reads of a narration script become takes of each line, and finished promos over a music bed are recognized so their pauses aren't chopped.
2. **Look** (optional): frames of each clip go to a vision model, which writes a description and keywords, and logs the good moments inside long B-roll so cutaways start on them.
3. **Plan**: Claude gets the transcripts (with how long each line runs at your pacing), clip descriptions and your brief, and returns a structured plan that picks segments by ID and names the words each B-roll shot and graphic belongs on. It never writes timestamps. The footage block is prompt-cached, so revising or re-planning is cheaper.
4. **Cut**: deterministic code turns the plan into frame-accurate edits: it trims pauses (a pause split by a breath counts as one), cuts fillers, pads each cut, snaps to the source frame grid, starts B-roll on its words (never replaying footage already on screen), covers narration that has no picture, places music, matches levels, adds fades and markers, and checks the result against your target length. Invalid IDs from the model are dropped with a warning, never passed through, and no line plays twice: a retake Claude lists among a line's alternates plays as the take it chose, unless you pick another with `--keep` or `--avoid` or a revision's notes swap them (re-cuts and later revisions keep that pick, in folders made by earlier releases too). Keeping two takes of one line still plays it once, as the chosen take, and the warnings say so. A line listed there that doesn't say that line, like "Wait, let me redo that", is left as it is; a retake in other words still counts.
5. **Write**: FCPXML 1.10 (Final Cut Pro 10.6 and later), checked in tests against Apple's DTD.

## Development

```bash
.venv/bin/python -m pip install -e '.[dev,graphics]'
.venv/bin/python -m pytest
```

The tests generate synthetic footage with ffmpeg and need no API key. CI (`.github/workflows/ci.yml`) runs them on Linux and on an Apple Silicon Mac, builds, tests and launches the Mac app, and cuts real spoken audio with mlx-whisper.

# roughcut

Dump a folder of footage in and get a Final Cut Pro rough cut out.

`roughcut` transcribes every clip on your Mac, removes dead air and "um"s, asks an AI model to build the story (hook, sections, best takes, B-roll, music), and writes an FCPXML file. Import that into Final Cut and you get a real, fully editable project that references your original media, so every cut keeps its handles. It also adds a **Stringout** project (all dialogue in recording order, dead air removed) and organizes the event browser with keywords (A-Roll, B-Roll, Stills, Music, plus visual tags).

There's a command-line tool (below) and a Mac app in [`mac/`](mac/README.md) that wraps it: drop footage in, write a brief, watch the progress, then open the result in Final Cut Pro.

## Setup (Mac, Apple Silicon)

```bash
brew install ffmpeg python@3.12
git clone https://github.com/ethanhauptli-collab/Archer-OS.git && cd Archer-OS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[mac,dev]'          # mlx-whisper for fast on-device transcription
ln -sf "$PWD/.venv/bin/roughcut" /opt/homebrew/bin/roughcut   # `roughcut` in every Terminal window
roughcut key set                     # paste your Claude API key; saved in the macOS Keychain
```

The link means you don't have to `source .venv/bin/activate` in each new window. To update later: `cd ~/Archer-OS && git pull`.

The first transcription downloads the Whisper model (`whisper-large-v3-turbo`, about 1.6 GB) once.

## Use

```bash
# Simplest: everything in the folder, Claude plans the cut
roughcut build ~/Movies/Trip

# With a brief and a target length
roughcut build ~/Movies/Trip -t 8m \
  -c "Vlog: family day trip to Big Bear. Candid, warm. Open on the kids at the lake. Keep the part where they find the giant pinecone."

# Brief from a file, plus a script or outline to follow
roughcut build ~/Movies/Trip --context-file brief.md --script outline.txt

# No AI, just cut the silence (like Recut / TimeBolt)
roughcut build ~/Movies/Talk --provider none

# Re-cut an earlier run tighter, or for vertical, without calling the model again
roughcut render ~/Movies/Trip_roughcut --style tight --format vertical
```

`roughcut doctor` checks that ffmpeg and transcription are installed, and tests your API key against Anthropic.

**About the key:** it has to be an API key from [console.anthropic.com](https://console.anthropic.com) → API Keys (starts with `sk-ant-`: `sk-ant-usr-` for new keys, `sk-ant-api03-` for older ones). A Claude Pro/Max subscription or Claude Code sign-in doesn't give API access. If the key is missing or rejected, `roughcut build` stops right away and says why.

`roughcut key set` checks the key with Anthropic and stores it in your login Keychain, shared with the Mac app (Settings → API Keys writes the same entry). `roughcut key status` shows where the key comes from, and `roughcut key remove` deletes it. An `ANTHROPIC_API_KEY` exported in your shell still works and takes priority. The first time Terminal reads a key the app saved (or the other way round), macOS asks for permission; click **Always Allow**.

Then in Final Cut Pro choose **File → Import → XML…** and pick the `.fcpxml` in the output folder (by default `<footage folder>_roughcut/`, next to your footage).

### What you get

| File | What it is |
|---|---|
| `<name>.fcpxml` | Event with keyworded clips, **Rough Cut** project, **Stringout** project |
| `edit_report.md` | Paper edit: sections, every line used, B-roll choices, alternate takes, YouTube chapters, what was cut and why, warnings |
| `plan.json` | The model's plan (edit it by hand and run `roughcut render` to rebuild) |
| `analysis.json`, `prompt.md` | Transcripts and clip data, and the exact prompt the model saw |
| `roughcut.log` | Every step of every run with times, ending in FINISHED or STOPPED (and why). If the other files are older than your run, this says what happened |

In the Rough Cut, **chapter markers** mark each section, **to-do markers** flag decisions to double-check, and **standard markers** name the alternate takes of a line.

### Useful options

| Option | Default | |
|---|---|---|
| `-t, --target` | none | Target length: `8m`, `90s`, `8:30` |
| `-c, --context` / `--context-file` | | The brief: what it is, audience, tone, must-keep moments |
| `--script FILE` | | A script, outline or transcript to follow |
| `--style` | `medium` | `tight`, `medium`, `loose`: how much pause is kept |
| `--keep-fillers` | off | Don't cut um/uh |
| `--format` | from footage | `1080p`, `4k`, `vertical`, `square`, or `WxH@fps` |
| `--provider` | `anthropic` | `anthropic` (API key), `claude-code` (your Claude Pro/Max plan), `openai`, `ollama`, `openai-compatible`, `none` |
| `--model` | menu in Terminal, else Opus | `opus` or `sonnet` (Claude Sonnet 5.5: faster, half the price). Any spelling works: `Sonnet 5.5`, `claude-sonnet-5-5`. Leave it off and `build` shows a menu; `--no-menu` skips it. Other providers: the model's name |
| `--effort` | `high` | Claude reasoning effort: `low` … `max` |
| `--no-vision` | | Skip visual descriptions of clips (cheaper, worse B-roll picks) |
| `--vision-model` | same as `--model` | A different model for describing clips, e.g. `sonnet` while Opus plans (most tokens on a big shoot go to descriptions) |
| `--transcriber` | `auto` | `mlx` (Mac), `faster-whisper`, or `none` (silence only) |
| `--language` | detect | e.g. `en` |
| `--transcripts DIR` | | Folder of `.srt`/`.vtt`/`.json` transcripts named like the clips |
| `--broll-db`, `--music-db` | -20, -14 | Levels for B-roll nat sound and music |
| `--no-fill` | | Don't auto-cover narration the plan left without picture |
| `--graphics` / `--graphics-style` | off | Claude-designed motion graphics from the transcript (see below) |

### Motion graphics

```bash
roughcut build ~/Movies/OCVIBE --graphics \
  -c "Promo for OC Vibe. Lower third for each speaker, call out the big numbers, kinetic caption on the hook." \
  --graphics-style "navy #0B1F3A and orange #FF6A13, bold geometric sans, premium and energetic"
```

With `--graphics`, Claude also plans animated graphics while it plans the cut: lower thirds (names and roles only when the footage or brief states them), stat callouts, title cards, word-by-word kinetic captions, pulled quotes and short lists. It picks the moments and the exact words; roughcut times each graphic to the frame from the transcript. A second request designs every graphic as an HTML animation in one consistent style (your `--graphics-style`, or a clean default). A browser renders them frame by frame to transparent ProRes 4444 at the sequence size, and they land on their own lane above the B-roll in the Rough Cut.

Each clip's design is saved next to it in `graphics/` as an `.html` file. Edit one (text, colors, timing) and run `roughcut render <output folder>` to re-render just that graphic. A re-cut with a different `--style` re-times the graphics and only re-renders the ones whose length changed.

Setup: `pip install -e '.[graphics]'`. Rendering uses Google Chrome if it's installed; otherwise run `playwright install chromium` once. `roughcut doctor` shows whether graphics are ready. Expect about a minute of design time per eight graphics, and roughly 25 MB per 4-second 1080p graphic.

### Use your Claude subscription instead of an API key

```bash
roughcut build ~/Movies/Trip --provider claude-code
```

This sends the AI steps through [Claude Code](https://claude.com/claude-code) signed in with your own Pro or Max plan, so runs count against your plan's usage limits instead of API credit. Anthropic's terms allow running the Claude Code program with your own subscription from your own scripts, but not calling the API directly with subscription credentials, so roughcut never touches your sign-in. It just runs `claude -p`.

Setup: install Claude Code (`npm install -g @anthropic-ai/claude-code`), run `claude`, and type `/login`. `roughcut doctor` shows whether it's ready. Claude Code would bill an API key instead of your plan whenever one is set, so roughcut hides `ANTHROPIC_API_KEY` from it. Big shoots use a lot of your plan's allowance on clip descriptions; `--vision-model sonnet` or `--no-vision` stretches it. The model menu and `--model` work the same as with an API key.

### Other models

```bash
# OpenAI (pip install -e '.[openai]', export OPENAI_API_KEY)
roughcut build ~/Movies/Trip --provider openai --model <model-name>

# Local, private, free: Ollama
roughcut build ~/Movies/Trip --provider ollama --model <model-name> --no-vision

# Anything OpenAI-compatible (LM Studio, OpenRouter, Groq...)
roughcut build ~/Movies/Trip --provider openai-compatible --base-url http://localhost:1234/v1 --model <model-name>
```

Claude requests opt into Anthropic's server-side refusal fallbacks (`fallbacks: "default"`). If a request is ever declined by a safety classifier, the API reruns it on the recommended fallback model instead of failing.

### Transcripts you already have

A transcript next to a clip wins over transcribing it again: `IMG_1234.srt`, `.vtt`, or Whisper-style `.json` beside `IMG_1234.MOV` (or in a `--transcripts` folder). SRT and VTT only have line-level timing, so word timing inside a line is estimated. Cuts are less precise than with a fresh transcript.

## How it works

1. **Analyze** (cached, so re-runs are fast): `ffprobe` reads rate, size, rotation and timecode; `ffmpeg` measures each clip's background noise and maps the pauses above it (so a loud room or a very quiet recording still gets its dead air found); Whisper transcribes with word timestamps. Words over silence are dropped as hallucinations. Words are grouped into sentence segments with IDs like `V03.S012`, and clips are classified as A-roll, B-roll, voiceover, music or stills.
2. **Look** (optional): three frames per B-roll clip go to a vision model, which writes a description and keywords.
3. **Plan**: the model gets the transcripts, clip descriptions and your brief. It returns a structured plan that picks segments by ID; it never writes timestamps. The transcript block is prompt-cached, so re-planning with a new brief is cheaper.
4. **Cut**: deterministic code turns the plan into frame-accurate edits: it trims pauses (using the silence map too, since Whisper often stretches a word across the pause after it), cuts fillers, pads each cut, snaps to the source frame grid, places B-roll and music on lanes, and adds markers. Invalid IDs from the model are dropped with a warning, never passed through. Narration never plays over black: anything the plan left uncovered is filled with the stills and B-roll whose names and descriptions best match what's being said.

Footage exported more than once (the same promo in 16:9, 9:16 and 4:3, or a clip saved twice) is detected and used once. Several reads of the same narration script are offered to the planner as takes of each line.
5. **Write**: FCPXML 1.10 (Final Cut Pro 10.6 and later), checked in tests against Apple's DTD.

## Development

```bash
pip install -e '.[dev]'
pytest            # 196 tests; generates synthetic footage with ffmpeg, no API key needed
```

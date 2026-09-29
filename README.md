# roughcut

Dump a folder of footage in and get a Final Cut Pro rough cut out.

`roughcut` transcribes every clip on your Mac, removes dead air and "um"s, asks an AI model to build the story (hook, sections, best takes, B-roll, music), and writes an FCPXML file. Import that into Final Cut and you get a real, fully editable project that references your original media, so every cut keeps its handles. It also adds a **Stringout** project (all dialogue in recording order, dead air removed) and organizes the event browser with keywords (A-Roll, B-Roll, Stills, Music, plus visual tags).

This is the command-line engine. A native Mac app will wrap it later (see `CLAUDE.md` → Roadmap).

## Setup (Mac, Apple Silicon)

```bash
brew install ffmpeg python@3.12
git clone https://github.com/ethanhauptli-collab/Archer-OS.git && cd Archer-OS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[mac,dev]'          # mlx-whisper for fast on-device transcription
export ANTHROPIC_API_KEY=sk-ant-...  # add to ~/.zshrc to keep it
```

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

Then in Final Cut Pro choose **File → Import → XML…** and pick the `.fcpxml` in the output folder (by default `<footage folder>_roughcut/`, next to your footage).

### What you get

| File | What it is |
|---|---|
| `<name>.fcpxml` | Event with keyworded clips, **Rough Cut** project, **Stringout** project |
| `edit_report.md` | Paper edit: sections, every line used, B-roll choices, alternate takes, YouTube chapters, what was cut and why, warnings |
| `plan.json` | The model's plan (edit it by hand and run `roughcut render` to rebuild) |
| `analysis.json`, `prompt.md` | Transcripts and clip data, and the exact prompt the model saw |

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
| `--provider` | `anthropic` | `anthropic`, `openai`, `ollama`, `openai-compatible`, `none` |
| `--model` | `claude-opus-5-5` | Any model the provider serves |
| `--effort` | `high` | Claude reasoning effort: `low` … `max` |
| `--no-vision` | | Skip visual descriptions of clips (cheaper, worse B-roll picks) |
| `--vision-model` | same as `--model` | Use a different model for describing clips |
| `--transcriber` | `auto` | `mlx` (Mac), `faster-whisper`, or `none` (silence only) |
| `--language` | detect | e.g. `en` |
| `--transcripts DIR` | | Folder of `.srt`/`.vtt`/`.json` transcripts named like the clips |
| `--broll-db`, `--music-db` | -20, -14 | Levels for B-roll nat sound and music |

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

1. **Analyze** (cached, so re-runs are fast): `ffprobe` reads rate, size, rotation and timecode; `ffmpeg silencedetect` maps silence; Whisper transcribes with word timestamps. Words over silence are dropped as hallucinations. Words are grouped into sentence segments with IDs like `V03.S012`, and clips are classified as A-roll, B-roll, voiceover, music or stills.
2. **Look** (optional): three frames per B-roll clip go to a vision model, which writes a description and keywords.
3. **Plan**: the model gets the transcripts, clip descriptions and your brief. It returns a structured plan that picks segments by ID; it never writes timestamps. The transcript block is prompt-cached, so re-planning with a new brief is cheaper.
4. **Cut**: deterministic code turns the plan into frame-accurate edits: it trims pauses, cuts fillers, pads each cut, snaps to the source frame grid, places B-roll and music on lanes, and adds markers. Invalid IDs from the model are dropped with a warning, never passed through.
5. **Write**: FCPXML 1.10 (Final Cut Pro 10.6 and later), checked in tests against Apple's DTD.

## Development

```bash
pip install -e '.[dev]'
pytest            # 77 tests; generates synthetic footage with ffmpeg, no API key needed
```

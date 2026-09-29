# roughcut: notes for Claude Code

Footage folder → transcripts → LLM edit plan → frame-accurate timeline → FCPXML for Final Cut Pro. Python CLI now; a native Mac app later. The owner edits in Final Cut Pro on an Apple Silicon Mac; this tool produces the first pass they refine there.

## Layout

| Module | Job |
|---|---|
| `roughcut/media.py` | Scan folders, `ffprobe` → `MediaInfo` (exact `Fraction` durations, timecode start, rotation, VFR) |
| `roughcut/ffmpeg_ops.py` | Silence map (`silencedetect`), frame grabs |
| `roughcut/transcribe.py` | mlx-whisper / faster-whisper / sidecar `.srt/.vtt/.json` → `Word`s |
| `roughcut/segments.py` | Words → sentence `Segment`s with IDs `V01.S003` |
| `roughcut/analyze.py` | Orchestrates stage 1, role classification, on-disk cache (`~/Library/Caches/roughcut`) |
| `roughcut/vision.py` | Frame sampling + vision descriptions/tags (batched, cached) |
| `roughcut/prompts.py` | Planner/vision prompts; media block rendered deterministically for prompt caching |
| `roughcut/providers/` | `anthropic_provider.py` (official SDK, streaming, structured outputs, `fallbacks: "default"`), `openai_compat.py` (OpenAI/Ollama/LM Studio, degrades schema → JSON mode → text) |
| `roughcut/plan.py` | `PLAN_SCHEMA`, lenient `normalize()` of model output, `heuristic_plan()` (stringout / no-AI mode) |
| `roughcut/timeline.py` | Plan → `Timeline` in integer sequence frames: padding, filler cuts, merges, B-roll lanes, music, markers |
| `roughcut/fcpxml.py` | `Timeline` → FCPXML 1.10 |
| `roughcut/pipeline.py`, `cli.py` | `roughcut build` / `roughcut render` |

## Invariants: don't break these

- **The model never produces timestamps for speech.** It picks segment IDs; `timeline.py` computes cuts. Anything the model returns goes through `plan.normalize()`, which drops unknown IDs with a warning and never raises.
- **No floats in FCPXML.** All times are `Fraction`s; spine `offset`/`duration` are whole multiples of the sequence `frameDuration`; source in-points snap to the source's own frame grid.
- **FCPXML coordinates:** spine `offset` = timeline time. Spine `start` = asset timecode origin + source in. Connected clips and markers live in the **parent's source time** (`parent.start + (frame − parent.offset) × frameDuration`). Stills use `<video>` with a `3600s` origin; browser stills are `<clip><video duration="0s"/></clip>`. Audio-only clips carry `format` = `FFVideoFormatRateUndefined`. All of this is verified against Apple's DTD and real FCP exports.
- Tests validate every generated FCPXML against `tests/fixtures/FCPXMLv1_10.dtd` (Apple's DTD, as vendored by CommandPost/OpenFCPXMLKit) and check timing invariants (`tests/test_end_to_end.py::check_invariants`). Keep both passing.
- Claude calls use `claude-opus-5-5` by default, adaptive thinking (implicit on this model), `output_config.effort`, streaming, and structured outputs via `output_config.format`. Don't add `thinking: {type: "disabled"}` or `budget_tokens` (400 on this model) or forced `tool_choice`.

## Commands

```bash
source .venv/bin/activate
pytest -q                                   # all tests (needs ffmpeg)
roughcut build <folder> -c "brief" -t 8m    # full run
roughcut build <folder> --provider none     # silence-only, no API
roughcut render <out_dir> --style tight     # rebuild from saved plan
```

## Status: built in a Linux cloud session, not yet run on a Mac

Verified there: 77 tests (including a regression test for each finding from an independent code review), DTD validation, a 4,500-plan fuzz of the timeline/FCPXML math (no DTD errors, off-grid edits, or out-of-media reads), the real Anthropic SDK against a mocked HTTP transport, and the Whisper glue with stubbed modules.
**Not yet verified:** a live Claude call, real mlx-whisper transcription (Hugging Face was blocked in that sandbox), and importing into Final Cut Pro.

### First run on the Mac: checklist

1. `pytest -q` passes.
2. `roughcut build <small folder> --provider none`, then import into FCP. Does it import cleanly? Check that cuts land on words, the timecode-start clips line up, and stills and keywords appear.
3. The same folder with Claude and a brief. Read `edit_report.md` against the FCP timeline, and check B-roll placement, chapter/to-do markers and the music bed.
4. Mixed media: iPhone HDR/VFR clips, vertical clips, a GoPro, a still, a separate audio recording. Watch the report's Warnings section.
5. Field-recorder WAVs: the asset `start` comes from the BWF `time_reference`. Confirm FCP agrees; if not, clips will read "outside the media".
6. Mixed frame rates (e.g. 25fps clip in a 23.98 project): real FCP exports add `<conform-rate>`; we don't. Check how FCP conforms them on import.
7. If FCP rejects the XML, it names the line. Fix `fcpxml.py`, add a regression test, and re-validate against the DTD.

Things most likely to need adjustment after real-world runs: Whisper word-end timing vs `pad_out`; the `silencedetect` threshold (`-35dB`) for noisy footage; the `aroll`/`broll` classification thresholds in `analyze.classify`; whether mlx-whisper with `condition_on_previous_text=True` (verbatim mode, needed so um/uh keep getting transcribed after the first 30s) loops or hallucinates on long clips (fall back with `--no-verbatim`); and the planner prompt.

## Roadmap

1. **Validate on real footage + FCP** (checklist above), then tune prompts and style presets.
2. **Auditions for alternates**: wrap a chosen take and its alternates in `<audition>` (first child = active pick; children carry no offset). Structure is confirmed in the research samples; currently alternates are markers only.
3. **Mac app shell**: SwiftUI window with drag-and-drop folder, brief field, target length, provider picker, and a "Reveal in Finder / Open in Final Cut" button. It shells out to the CLI; add a `--progress-json` flag to the CLI so the app can show stage progress. API keys in Keychain.
4. **Native speed-ups**: Apple SpeechAnalyzer (macOS 26) or WhisperKit instead of mlx-whisper; parallel transcription.
5. **Dual-system audio / multicam sync** (waveform cross-correlation → `sync-clip`/`mc-clip`).
6. **Beat-aware music edits** and a `--vertical-reframe` pass for shorts.

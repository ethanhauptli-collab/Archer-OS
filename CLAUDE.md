# roughcut: notes for Claude Code

Footage folder → transcripts → LLM edit plan → frame-accurate timeline → FCPXML for Final Cut Pro. A Python CLI (the engine) plus a SwiftUI Mac app in `mac/` that drives it. The owner edits in Final Cut Pro on an Apple Silicon Mac; this tool produces the first pass they refine there.

## Layout

| Module | Job |
|---|---|
| `roughcut/media.py` | Scan folders, `ffprobe` → `MediaInfo` (exact `Fraction` durations, timecode start, rotation, VFR) |
| `roughcut/ffmpeg_ops.py` | Per-clip noise floor (`window_peaks` → `noise_threshold`), silence map (`silencedetect`), frame grabs |
| `roughcut/transcribe.py` | mlx-whisper / faster-whisper / sidecar `.srt/.vtt/.json` → `Word`s |
| `roughcut/segments.py` | Words → sentence `Segment`s with IDs `V01.S003` |
| `roughcut/analyze.py` | Orchestrates stage 1, role classification, on-disk cache (`~/Library/Caches/roughcut`) |
| `roughcut/duplicates.py` | Alternate exports (same words / same frames) → `duplicate_of` (hidden); repeated narration reads → `take_of` |
| `roughcut/vision.py` | Frame sampling + vision descriptions/tags (batched, cached) |
| `roughcut/prompts.py` | Planner/vision prompts; media block rendered deterministically for prompt caching |
| `roughcut/providers/` | `anthropic_provider.py` (official SDK, streaming, structured outputs, `fallbacks: "default"`), `claude_code.py` (`--provider claude-code`: runs `claude -p` on the user's Pro/Max plan; images go to temp files read with the Read tool), `openai_compat.py` (OpenAI/Ollama/LM Studio, degrades schema → JSON mode → text) |
| `roughcut/plan.py` | `PLAN_SCHEMA`, lenient `normalize()` of model output, `heuristic_plan()` (stringout / no-AI mode) |
| `roughcut/timeline.py` | Plan → `Timeline` in integer sequence frames: padding, filler cuts, merges, B-roll lanes, music, markers |
| `roughcut/graphics.py` | `--graphics`: place planned graphics on the transcript, one design request (HTML/CSS animations), render with Playwright (seek every animation per frame) to ProRes 4444 alpha, connect above B-roll; `graphics.json` + editable `graphics/*.html` drive re-renders |
| `roughcut/fcpxml.py` | `Timeline` → FCPXML 1.10 |
| `roughcut/pipeline.py`, `cli.py` | `roughcut build` / `render` / `doctor` |
| `roughcut/progress.py` | `--progress-json` JSON-lines events for the Mac app |
| `roughcut/keys.py` | API keys: environment first, then the macOS Keychain entry shared with the app (service `com.roughcut.app`, account = env var name); `roughcut key set/status/remove` |
| `mac/` | SwiftUI app (see `mac/README.md`): `RoughcutKit` (Foundation-only: options→args, event decoding, process runner) + `Roughcut` (views, `AppModel`, Keychain) |

## Invariants: don't break these

- **The model never produces timestamps for speech.** It picks segment IDs; `timeline.py` computes cuts. Anything the model returns goes through `plan.normalize()`, which drops unknown IDs with a warning and never raises.
- **No floats in FCPXML.** All times are `Fraction`s; spine `offset`/`duration` are whole multiples of the sequence `frameDuration`; source in-points snap to the source's own frame grid.
- **FCPXML coordinates:** spine `offset` = timeline time. Spine `start` = asset timecode origin + source in. Connected clips and markers live in the **parent's source time** (`parent.start + (frame − parent.offset) × frameDuration`). Stills use `<video>` with a `3600s` origin; browser stills are `<clip><video duration="0s"/></clip>`. Audio-only clips carry `format` = `FFVideoFormatRateUndefined`. All of this is verified against Apple's DTD and real FCP exports.
- Tests validate every generated FCPXML against `tests/fixtures/FCPXMLv1_10.dtd` (Apple's DTD, as vendored by CommandPost/OpenFCPXMLKit) and check timing invariants (`tests/test_end_to_end.py::check_invariants`). Keep both passing.
- **The app ↔ CLI contract is `--progress-json`** (one JSON object per stdout line: `stage` / `log` / `done` / `error`; stages `scan probe silence transcribe vision plan cut write`; `done.result` = `pipeline.result_summary`) and `roughcut doctor --json`. `mac/Tests/RoughcutKitTests/Fixtures/progress-sample.jsonl` is real output. The Swift tests decode it, and `tests/test_progress.py` fails if the Python side drifts. Change both sides together and regenerate the fixture.
- **A rejected API key stops the run** (`ProviderAuthError`): `build` calls `provider.verify()` before any analysis, and auth errors during vision/planning are fatal. Only non-auth model failures may fall back to the stringout, and that fallback is announced at the top of the report and in the CLI output.
- **Dead air is cut by the silence map, not by word times.** mlx-whisper's word times are contiguous (no gaps), and the word next to a pause is stretched over it on either side. `timeline._cut_silences` runs last and removes every silence longer than the style's `max_gap` that has sound on both sides inside a kept piece (talking-footage ranges too, at a longer threshold). It never removes sound. `tests/test_dead_air.py` covers the 10-14 s gap case.
- **Subscription use goes only through the Claude Code program.** `claude_code.py` runs `claude -p` (signed in by the user) and strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from its environment so it can't bill the API. Never read or forward subscription OAuth credentials to the API ourselves: Anthropic's terms forbid it. `verify()` refuses an `api_key` sign-in.
- **Graphics follow the same timing rule as the cut.** The planner names a segment and the exact words; `graphics.place` finds them in the transcript and the timeline. Designs are self-contained HTML rendered offline (network blocked) by seeking `document.getAnimations()` and `window.seek(t)` per frame, so they must never depend on wall-clock timers. Rendering runs in threads, one Playwright instance per thread (never processes: spawn breaks under embedding).
- **An audio-only storyline (narration) never plays over black**: `timeline._fill_picture` covers any gap the plan left, matching narration words (multi-word names count double) to clip names/visual tags, preferring moving footage, never repeating a still. Several B-roll entries with the same `over` play back to back.
- Claude calls use `claude-opus-5-5` by default (`claude-sonnet-5-5` via the Terminal model menu, `--model sonnet`, or the app's menu; `anthropic_provider.resolve_model` turns any spelling like `Claude-Sonnet.5.5` into the id; same request shape works for both), adaptive thinking (implicit on this model), `output_config.effort`, streaming, and structured outputs via `output_config.format`. Don't add `thinking: {type: "disabled"}` or `budget_tokens` (400 on this model) or forced `tool_choice`.

## Commands

```bash
source .venv/bin/activate
pytest -q                                   # all tests (needs ffmpeg)
roughcut build <folder> -c "brief" -t 8m    # full run
roughcut build <folder> --provider none     # silence-only, no API
roughcut render <out_dir> --style tight     # rebuild from saved plan
roughcut doctor                             # ffmpeg / transcriber / API key check

cd mac && swift test                        # RoughcutKit tests (needs Xcode)
mac/scripts/build-app.sh --install          # build Roughcut.app into /Applications
```

## Status

The CLI was built and tested in a Linux cloud session. The owner has since run it from Terminal on their Mac.
The Mac app (`mac/`) was written in that cloud session with **no Swift toolchain available** (neither the macOS SDK nor a Linux Swift). It had an independent compile-focused review but has never been compiled. On the first `swift build`, fix whatever the compiler reports, keeping the structure, then run `swift test`.

### First real run (OCVIBE, 301 clips: 3 narration reads, 9 interviews/promos, 13 B-roll, 276 stills)

- The Anthropic key was **rejected**; the old code kept going and silently delivered the stringout (all speech back to back, narration over black, the Roy Choi promo three times). Fixed: key check up front, fatal auth errors, clear hints (missing key, Claude sign-in token instead of API key, ANTHROPIC_BASE_URL), `roughcut doctor` tests the key.
- Project shape exposed gaps now fixed: duplicate exports (16:9/9:16/4:3), three takes of one narration script, narration-led structure with 5-25 s sentences needing multiple shots.
- Not yet seen: an actual AI-planned cut. Next run needs a working key (console.anthropic.com API key).

### Single-clip test on the Mac (4K 24p action-cam clip, outdoor cafe)

- The FCPXML imports into Final Cut Pro 11; A-Roll and vision keywords show up on the clip.
- "No cuts" turned out to be the original clip in the browser, not the Rough Cut project. The CLI now names the project to open.
- That hunt still found real bugs: a fixed −35 dB silence level missed pauses under room tone, and the silence clamps failed when Whisper's stretched word overshot the silence by a tenth of a second (fixed by `_cut_silences`).

### CLI verification (cloud)

Verified there: 194 tests (including a regression test for each finding from an independent code review, and the app's progress contract), DTD validation, a 4,500-plan fuzz of the timeline/FCPXML math (no DTD errors, off-grid edits, or out-of-media reads), the real Anthropic SDK against a mocked HTTP transport, and the Whisper glue with stubbed modules.
Not verifiable in the cloud: a live Claude call, real mlx-whisper transcription (Hugging Face was blocked), and importing into Final Cut Pro. Those depend on runs on the Mac; record the results here.

### First run on the Mac: checklist

1. `pytest -q` passes.
2. `roughcut build <small folder> --provider none`, then import into FCP. Does it import cleanly? Check that cuts land on words, the timecode-start clips line up, and stills and keywords appear.
3. The same folder with Claude and a brief. Read `edit_report.md` against the FCP timeline, and check B-roll placement, chapter/to-do markers and the music bed.
4. Mixed media: iPhone HDR/VFR clips, vertical clips, a GoPro, a still, a separate audio recording. Watch the report's Warnings section.
5. Field-recorder WAVs: the asset `start` comes from the BWF `time_reference`. Confirm FCP agrees; if not, clips will read "outside the media".
6. Mixed frame rates (e.g. 25fps clip in a 23.98 project): real FCP exports add `<conform-rate>`; we don't. Check how FCP conforms them on import.
7. If FCP rejects the XML, it names the line. Fix `fcpxml.py`, add a regression test, and re-validate against the DTD.

Things most likely to need adjustment after real-world runs: Whisper word-end timing vs `pad_out`; the per-clip silence level (`ffmpeg_ops.noise_threshold`: default −35 dB, raised above a loud background and lowered under quiet speech; falls back to −35 if it would drop real words); the `aroll`/`broll` classification thresholds in `analyze.classify`; whether mlx-whisper with `condition_on_previous_text=True` (verbatim mode, needed so um/uh keep getting transcribed after the first 30s) loops or hallucinates on long clips (fall back with `--no-verbatim`); and the planner prompt.

## Roadmap

1. **Validate on real footage + FCP** (checklist above), then tune prompts and style presets.
2. **Auditions for alternates**: wrap a chosen take and its alternates in `<audition>` (first child = active pick; children carry no offset). Structure is confirmed in the research samples; currently alternates are markers only.
3. **Mac app shell**: first version done (`mac/`): drop zone, brief, target length, pacing/format/provider, a live stage checklist from `--progress-json`, open in FCP / Finder / paper edit, re-cut without AI, and Keychain-backed API keys. Next: compile and polish on the Mac, remember recent runs, show the paper edit inside the app, and Developer ID signing + notarization so it runs on other Macs.
4. **Native speed-ups**: Apple SpeechAnalyzer (macOS 26) or WhisperKit instead of mlx-whisper; parallel transcription.
5. **Dual-system audio / multicam sync** (waveform cross-correlation → `sync-clip`/`mc-clip`).
6. **Beat-aware music edits** and a `--vertical-reframe` pass for shorts.

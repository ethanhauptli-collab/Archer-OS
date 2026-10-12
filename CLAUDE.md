# roughcut: notes for Claude Code

Footage folder → transcripts → LLM edit plan → frame-accurate timeline → FCPXML for Final Cut Pro. A Python CLI (the engine) plus a SwiftUI Mac app in `mac/` that drives it. The owner edits Venture Orange County promos and a personal YouTube channel (Save the Kid) in Final Cut Pro 11 on an Apple Silicon Mac; this tool produces the first pass they refine there.

## Layout

| Module | Job |
|---|---|
| `roughcut/media.py` | Scan folders, `ffprobe` → `MediaInfo` (exact `Fraction` durations, timecode start, rotation, VFR); `frame_rate_warnings` for clips in the cut at another or variable rate |
| `roughcut/ffmpeg_ops.py` | Per-clip noise floor (`window_peaks` → `noise_threshold`), silence map (`silencedetect`), loudness blocks (ebur128 + peaks, `gated_loudness`, `level_summary`), frame grabs (long edge capped) |
| `roughcut/pauses.py` | What plays in the pauses (music bed vs noise, by spectral flatness; scene cuts per minute) and breath/click islands inside pauses |
| `roughcut/transcribe.py` | mlx-whisper / faster-whisper / sidecar `.srt/.vtt/.json` → `Word`s |
| `roughcut/segments.py` | Words → sentence `Segment`s with IDs `V01.S003` |
| `roughcut/analyze.py` | Orchestrates stage 1, role classification, loudness, on-disk cache (`~/Library/Caches/roughcut`); one undecodable clip is cut on its pauses, a broken transcriber stops the run |
| `roughcut/duplicates.py` | Alternate exports → `duplicate_of` (hidden); repeated narration reads → `take_of`; `same_line` for the one-read-per-line rule; `another_read`: is a take the planner's alternates list really a read of the chosen line |
| `roughcut/vision.py` | Frame sampling + vision descriptions/tags (batched, cached; hidden duplicates share the kept clip's); timed moments in long B-roll |
| `roughcut/prompts.py` | Planner/vision/moments prompts; media block rendered deterministically for prompt caching, showing each line's kept length at the run's style (Auto pacing: at medium, and the brief says what tight and loose make of them, `pacing_choice`); with graphics off the brief tells the planner the cut has none |
| `roughcut/providers/` | `anthropic_provider.py` (official SDK, streaming, structured outputs, `fallbacks: "default"`), `claude_code.py` (`--provider claude-code`: runs `claude -p` on the user's Pro/Max plan), `openai_compat.py`; `cost_breakdown`; `ProviderLimitError` |
| `roughcut/plan.py` | `PLAN_SCHEMA` (B-roll `words`, name `spellings` checked against the transcript, and the planner's reading of the brief or a revision's notes: `pacing`, `punch_ins`, `graphics_asked`), lenient `normalize()` (drops unknown IDs and repeats of a line from another read; a take its own `alternates` list as another, when it reads that line, plays as the chosen one unless the editor's `--keep`/`--avoid` or a revision's swap picks it; `chosen_takes` saves that pick for re-cuts, `as_played` reads it from an older plan.json), `heuristic_plan()` (stringout / no-AI mode) |
| `roughcut/phrases.py` | Find planner-named words in the transcript and the cut (shared by B-roll and graphics placement) |
| `roughcut/timeline.py` | Plan → `Timeline` in integer sequence frames: padding, filler cuts, merges, `_cut_silences`, B-roll on its words, auto-fill, music bed (back-timed, ducked), level matching (`_mix`), fades, markers, `review` (cut check vs target), `dead_air` (set by `pipeline.make_timelines` from the stringout, cut in memory when its project isn't written) |
| `roughcut/graphics.py` | `--graphics`: place planned graphics (one on screen at a time), design (batches; the first sets the look, the rest in parallel; a frame of the shot under each), render with Playwright (async API per thread, every call bounded) to ProRes 4444 alpha, check renders, connect above B-roll; `graphics.json` + editable `graphics/*.html` drive re-renders; a designed graphic a re-cut has no room for keeps its design in `graphics/left-out/` until one has |
| `roughcut/graphics_preview.py` | `graphics/preview.html`: every graphic composited over its shot |
| `roughcut/brand_fonts.py` | Bundled fonts (`roughcut/assets/fonts`: Jost, Inter, Fraunces, OFL) + the user's fonts folder, embedded into designs as @font-face data |
| `roughcut/presets.py` | Built-in (`venture-oc`, `save-the-kid`) and user presets, logo inlining, approved designs |
| `roughcut/fcpxml.py` | `Timeline` → FCPXML 1.10 (roles, notes, volume/fades/keyframes, alt-take keyword ranges) |
| `roughcut/versions.py` | Numbered versions, `history/`, `versions.json`, atomic writes, graphics adoption and pruning |
| `roughcut/revise.py` | `roughcut revise`: re-plan from notes; the diff the report opens with |
| `roughcut/captions.py` | `<name> vN.srt` per version: kept dialogue/narration words mapped through the cut (frames, no fillers), name fixes from the plan's `spellings` (`respell`, which also spells names in graphics' text and in the words a design request shows; a revision that undoes a fix takes it off the graphics too) |
| `roughcut/report.py`, `report_html.py` | Paper edit (Markdown, and the HTML page with copyable chapters) |
| `roughcut/pipeline.py`, `cli.py` | `roughcut build` / `render` / `revise` / `preset` / `update` / `doctor` / `key` |
| `roughcut/paths.py`, `update.py` | Rejoin unquoted paths with spaces; `roughcut update` |
| `roughcut/progress.py` | `--progress-json` JSON-lines events for the Mac app; heartbeat and stall dump |
| `roughcut/keys.py` | API keys: environment first, then the macOS Keychain entry shared with the app (service `com.roughcut.app`, account = env var name) |
| `mac/` | SwiftUI app (see `mac/README.md`): `RoughcutKit` (Foundation-only: options→args, event decoding, presets, revise, process runner) + `Roughcut` (views, `AppModel`, Keychain) |

## Invariants: don't break these

- **The model never produces timestamps for speech.** It picks segment IDs and names words; `timeline.py` computes cuts. Anything the model returns goes through `plan.normalize()`, which drops unknown IDs with a warning and never raises. No line plays twice: a repeat from another read or export is dropped and recorded as an alternate, and a take the plan's own `alternates` list among a line's others is dropped when the chosen take is in the cut, else replaced by it (heuristic/stringout plans are exempt so `render` reproduces them). Only a real retake counts (`duplicates.another_read`: at least half the line's content words, however reworded, the same line, or one inside the other): a flub or another line listed there is left where it is and never named as an alternate. The editor wins: a take `--keep` names is never dropped or replaced (when it names two takes of one line, the chosen one plays and the other stays an alternate, with a warning, and `enforce` doesn't call it missing), an avoided chosen take never goes in place of another (the other take in the cut plays and becomes chosen), and a revision that plays another take where the last version played the chosen one keeps the swap; `revise` saves the reconciled alternates in `plan.json`'s raw plan so re-cuts play the same takes, and `render` and `revise` read an older plan.json's raw alternates as its saved plan played them (`plan.as_played`).
- **No floats in FCPXML.** All times are `Fraction`s; spine `offset`/`duration` are whole multiples of the sequence `frameDuration`; source in-points snap to the source's own frame grid; audio fades are whole samples.
- **FCPXML coordinates:** spine `offset` = timeline time. Spine `start` = asset timecode origin + source in. Connected clips, markers and volume keyframes live in the **parent's (or the clip's own) source time** (`start + (frame − offset) × frameDuration`). Stills use `<video>` with a `3600s` origin; browser stills are `<clip><video duration="0s"/></clip>`. Audio-only clips carry `format` = `FFVideoFormatRateUndefined`. Every clip names its own `format` when it differs from what it would inherit (the sequence's on the spine, the parent's when connected; browser clips always), and a clip at another frame rate gets `<conform-rate scaleEnabled="0" srcFrameRate=…/>` so it plays at real speed, as Final Cut writes them. Never put `adjust-volume` on `<video>`. All of this is checked against Apple's DTD and against real Final Cut 10.7/11 exports in `tests/fixtures/fcp_exports/` (`tests/test_fcp_exports.py`; `check_invariants` enforces the format rule).
- Tests validate every generated FCPXML against `tests/fixtures/FCPXMLv1_10.dtd` (Apple's DTD) and check timing invariants (`tests/test_end_to_end.py::check_invariants`). Keep both passing.
- **The app ↔ CLI contract is `--progress-json`** (one JSON object per stdout line: `stage` / `log` / `done` / `error`; stages `scan probe silence transcribe vision plan cut graphics write`, only ever moving forward; `error` is last and may carry `resume` (after a saved plan, also on SIGTERM/SIGHUP and unexpected errors) and `kind` (`usage_limit`, `cancelled`)), `done.result` = `pipeline.result_summary` (keys may be added, never removed or renamed; `cut` holds the version's style, format, fillers and stringout so the app's Re-cut starts from them), `roughcut doctor --json` and `roughcut preset list --json`. `mac/Tests/RoughcutKitTests/Fixtures/progress-sample.jsonl` is real output; the Swift tests decode it and `tests/test_progress.py` fails if the Python side drifts. Change both sides together and regenerate the fixture.
- **A rejected API key or a used-up plan/credit stops the run** (`ProviderAuthError`, `ProviderLimitError`): `build` calls `provider.verify()` before any analysis, and both are fatal during vision/planning. Only other model failures may fall back to the stringout, and that fallback is announced at the top of the report and in the CLI output.
- **The plan is saved before anything that can still fail** (cutting, graphics): `analysis.json` + `plan.json` (with `analysis_digest` and the run's settings) are written right after planning, so `roughcut render` can always finish without the model. `render` refuses a plan whose digest doesn't match, and takes every cut setting it isn't given from the saved run (in Terminal and from the app alike). `revise` writes `graphics.json` (carried designs, new ones without html) as soon as its plan is saved; `render --design-missing` designs only graphics without html, with the run's own model, never re-planning. Outputs are numbered versions written atomically (temp file + rename); earlier versions are copied to `history/` (last 5). Never move files at run start, and never delete a rendered graphics clip any written version used: Final Cut reads imported media in place.
- **Dead air is cut by the silence map, not by word times.** mlx-whisper's word times are contiguous, and the word next to a pause is stretched over it. `timeline._cut_silences` runs after merging and removes every silence longer than the style's `max_gap` (or `max(1 s, 2×max_gap)` for a finished promo over a music bed, `Clip.bed == "music"`) that has sound on both sides inside a kept piece. It never cuts a "silence" that holds a whole transcribed word. A pause split by a quiet breath or click (≤ 0.4 s, ≥ 12 dB under the speech, no word starting in it and no word whose only sound is in it, `pauses.word_in_pause`) counts as one pause: on camera it goes in one edit, breath included (the one bounded exception to "never removes sound"); in narration the breath stays. Words are dropped as Whisper inventions only by the standard −35 dB map, never by a raised one. `tests/test_dead_air.py` covers the 10-14 s gap case and music-bed promos.
- **Audio levels:** speech is matched by the loudness of its words to the dialogue level, held to ±12 dB and never past −1 dBFS peaks; nat sound and music are set relative to it. Fades never reach a kept word. The music bed ducks under speech with keyframes and is back-timed so the song's own ending lands on the last frame.
- **Subscription use goes only through the Claude Code program.** `claude_code.py` runs `claude -p` (signed in by the user) and strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from its environment so it can't bill the API. Never read or forward subscription OAuth credentials to the API ourselves: Anthropic's terms forbid it. `verify()` refuses an `api_key` sign-in.
- **B-roll and graphics follow the same timing rule as the cut.** The planner names a segment and the exact words; `phrases` finds them in the transcript and the timeline. B-roll starts about 2 frames before its words and never shows the same footage twice within 10 s (`REPLAY_SECONDS`; software-picked in-points go to unseen parts, `_fresh_in`). Graphics never overlap, and a graphic's lead never crosses a cut, so Final Cut connects it to the clip its words are in. Designs are self-contained HTML rendered offline (network blocked) by seeking `document.getAnimations()`, SVG SMIL (`pauseAnimations`/`setCurrentTime`) and `window.seek(t)` per frame (animations `window.seek` starts later are seeked from the frame they start), so they must never depend on wall-clock timers. Each saved design ends with a `roughcut-timing` note (its seconds and word times); a design reused at other timing renders on a warped clock (`time_map`) so captions stay on their words. Every declared font is loaded before frame 0, and brand fonts are embedded, never fetched. Every frame is captured only after the page has drawn what changed (two animation frames, `graphics._DRAWN_JS`): a capture straight after a seek, or after the scale the last capture set and put back, can catch the compositor between two states with an edge a device pixel off.
- **Rendering never hangs and never uses processes.** One Playwright instance per thread, each running the async API under `asyncio.run` so every browser call has a deadline we enforce (`_bounded`/`_evaluate`): a stuck page never answers CDP and `page.evaluate` has no timeout. A design that freezes is left out with a note. Renders land under their final, digest-carrying name (`G01-lower-third-<digest6>.mov`) only when complete; saved designs are matched to graphics by plan `index`, never by ID.
- **Presets never invent brand colors.** Venture OC's palette and fonts are the ones ventureoc.com uses; anything else is the owner's to set. Approved designs and the logo reach only the design request, never the planner or `graphics.json`'s style.
- **An audio-only storyline (narration) never plays over black**: `timeline._fill_picture` covers any gap the plan left, matching narration words to clip names/visual tags, preferring moving footage, never repeating a still while video can cover, never leaving a sub-0.5 s sliver, never replaying the shot on screen; it goes round again (up to 4 rounds) to cover anything a moved still uncovers.
- **Long waits are visible.** Waits on a model or `claude -p` are wrapped in `progress.doing(..., external=True)`; the heartbeat logs "still working" every minute, and only a stall outside those waits gets a stack dump in `roughcut.log`.
- Claude calls use `claude-opus-5-5` by default (`claude-sonnet-5-5` via the Terminal model menu, `--model sonnet`, or the app's menu; `anthropic_provider.resolve_model` turns any spelling like `Claude-Sonnet.5.5` into the id; same request shape works for both), adaptive thinking (implicit on this model), `output_config.effort`, streaming, and structured outputs via `output_config.format`. Don't add `thinking: {type: "disabled"}` or `budget_tokens` (400 on this model) or forced `tool_choice`.

## Commands

```bash
.venv/bin/python -m pytest -q                       # all tests (needs ffmpeg; graphics tests need Playwright Chromium)
roughcut build <folder> --preset venture-oc -c "brief"
roughcut build <folder> --provider none             # silence-only, no API
roughcut revise <out_dir> "notes"                   # re-plan from notes as a new version
roughcut render <out_dir> --style tight             # rebuild from saved plan, no model
roughcut render <out_dir> --design-missing          # design graphics a stopped run never designed
roughcut preset list | show | save | remove
roughcut update                                     # git pull + reinstall when needed
roughcut doctor                                     # ffmpeg / transcriber / graphics / Claude Code / API key

cd mac && swift test                                # RoughcutKit tests (needs Xcode)
mac/scripts/build-app.sh --install                  # build Roughcut.app into /Applications
```

CI (`.github/workflows/ci.yml`) runs on every push: the Python suite on Linux; the Python suite plus a real-speech mlx-whisper smoke test (`.github/scripts/mac-engine-smoke.sh`: preset, `--music`, versions, vertical re-cut, DTD) on an Apple Silicon Mac; and `swift build`, `swift test`, `build-app.sh` and a launch check for the Mac app on Xcode 16.4.

## Status

The CLI was first built in a Linux cloud session; the owner runs it from Terminal and the app on their Mac.

### Verified

- 400+ Python tests (DTD validation, timing invariants, a regression test per review finding, the app's progress contract), on Linux and macOS (CI).
- The Mac app compiles, its tests pass, it packages and launches (CI, Xcode 16.4, macOS 15 arm64).
- mlx-whisper on Apple Silicon transcribes real speech (macOS `say`) and the pause is cut (CI).
- Live `--provider claude-code` runs on synthetic footage: planning, revise, graphics design and rendering.
- The owner imported earlier versions' FCPXML into Final Cut Pro 11 (A-Roll and vision keywords showed up on the clip).

### Not verified yet (needs the owner's Mac and Final Cut)

- Importing this release's FCPXML: roles and subroles, clip notes, `adjust-volume` levels with fade handles, music-bed volume keyframes, alt-take keyword ranges, project names with version suffixes, a second import into the same event.
- A Claude-planned cut of real footage end to end (only synthetic footage has been planned live), and the planner's use of B-roll `words` and kept lengths on a long shoot.
- Level matching on real sources (thresholds to tune: busy-background note 20 dB, music lift 8 dB, spine B-roll −6 dB), breath detection, music-bed promo detection.
- Interactive use of the app: presets, music picker, revise box, notifications, Finish without AI, Update Roughcut.
- Captions (`<name> vN.srt`) imported with File > Import > Captions onto the project, and punch-ins (`<adjust-transform scale>` on spine clips) as they look in the viewer, including a 16:9 clip in a vertical cut.

### First run on the Mac: checklist

1. `roughcut update`, then `mac/scripts/build-app.sh --install`, then `roughcut doctor`.
2. A small folder with `--provider claude-code --preset venture-oc`: import into FCP. Check that it imports cleanly, cuts land on words, B-roll starts on its words, levels sound even, the music ducks, graphics sit above the B-roll, and roles show in the Timeline Index.
3. `roughcut revise` with a note, then import v2 beside v1.
4. Mixed media: iPhone HDR/VFR clips, vertical clips, a GoPro, a still, a separate audio recording. Watch the report's Warnings and Levels sections.
5. Field-recorder WAVs: the asset `start` comes from the BWF `time_reference`. Confirm FCP agrees; if not, clips will read "outside the media".
6. Mixed frame rates: we now write FCP's own `<conform-rate scaleEnabled="0">` (play at real speed) and the clip's `format`. Check that a 25p clip in a 23.98 cut plays at real speed with cuts on the words, and that 16:9 clips fit a vertical cut.
8. Roles: subroles are written on the clip (`audioRole="dialogue.Narration"`); real exports put subroles on `<audio-channel-source>`. Check Timeline Index > Roles shows Narration, Music Bed, Nat Sound, Planned, Auto-fill and Graphics; if not, move the subrole to `audio-channel-source`.
9. Captions and punch-ins: File > Import > Captions with the project open (they should sit on the words, names spelled as in the brief); build once with "punch in on jump cuts" in the brief and check the alternating 112% framing.
7. If FCP rejects the XML, it names the line. Fix `fcpxml.py`, add a regression test, and re-validate against the DTD.

## Roadmap

1. **Validate on real footage + FCP** (checklist above), then tune prompts, thresholds and presets from what the owner changes in FCP.
2. **Auditions for alternates**: wrap a chosen take and its alternates in `<audition>` (first child = active pick). Alternates are keyword ranges and markers today; chosen lines often span several spine clips, so this needs FCP testing first.
3. **Mac app polish**: Developer ID signing + notarization, a Recent list from each folder's `versions.json`, the paper edit inside the app, an "approve these graphics" action (`preset save --look-from`).
4. **Venture Studio**: source effects from the owner's Venture Studio app once its code is available to study (read-only until the owner says otherwise).
5. **Native speed-ups**: Apple SpeechAnalyzer (macOS 26) or WhisperKit instead of mlx-whisper; parallel transcription.
6. **Dual-system audio / multicam sync** (waveform cross-correlation → `sync-clip`/`mc-clip`), beat-aware music edits, a `--vertical-reframe` pass for shorts.

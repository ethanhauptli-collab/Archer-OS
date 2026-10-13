# Roughcut for Mac

A SwiftUI window for the `roughcut` CLI in this repo. Drop footage, pick a preset, add to the brief, click **Build Rough Cut**, and watch each stage finish. Then **Open in Final Cut Pro**.

It runs the same Python engine you already set up (`.venv/bin/roughcut`) and reads its `--progress-json` stream. Nothing is reimplemented in Swift.

## Build and run

Set up the Python CLI first (repo README → Setup). Then, from the repo:

```bash
mac/scripts/build-app.sh --install
open /Applications/Roughcut.app
```

That builds `mac/build/Roughcut.app` and copies it to Applications. After `roughcut update` says the Mac app changed, run the same build command again.

While working on the app: `open mac/Package.swift` (opens in Xcode), pick the **Roughcut** scheme, and press Run. `swift run Roughcut` works too, but notifications need the packaged app.

Needs macOS 14+ and Xcode or the Command Line Tools (`xcode-select --install`). `swift test` runs the RoughcutKit tests and needs full Xcode for XCTest. CI builds, tests, packages and launch-checks the app on every push (`.github/workflows/ci.yml`, Xcode 16.4).

The build is signed ad hoc, so after each rebuild macOS may ask again to allow Keychain access (for your API key) and access to footage folders. To stop that, sign with your free Apple Development certificate: `ROUGHCUT_SIGN_IDENTITY="Apple Development: …" mac/scripts/build-app.sh --install` (`security find-identity -v -p codesigning` lists yours).

## First launch

1. The app looks for the CLI in this order: the path set in Settings, the repo it was built from, `~/Archer-OS/.venv/bin/roughcut` and a few other usual spots. If it can't find it, the banner at the top of the window says so. Point to it in **Settings → General**.
2. Choose who plans the edit. **Claude (your Pro/Max plan, via Claude Code)** needs Claude Code installed and signed in (`claude`, then `/login`). **Claude (API key)** needs a key in **Settings → API Keys** (or click **Use keys from Terminal** to import `ANTHROPIC_API_KEY` from your `~/.zshrc`). Apps opened from the Dock don't see shell variables, so keys are stored in the macOS Keychain and passed to the CLI only while it runs.
3. **Settings → General → Setup check** confirms ffmpeg, transcription, the graphics renderer, Claude Code and keys (this runs `roughcut doctor`). When Venture Studio is on this Mac, a Venture Studio row says what Roughcut can read from it (the catalog snapshot's date, items, folders) or why not; it never blocks a build, and there's no row without Studio. A build of footage inside a Studio library doesn't ask anything in the app: the paper edit's warnings say the output folder is inside the library (in Terminal, `roughcut build` offers a folder outside it).

## What's in the window

- **Preset**: Venture OC, Save the Kid, or one you saved with `roughcut preset save`. Picking one fills the brief, length, pacing, format and graphics look; change anything after. A brief you'd already typed is kept, after the preset's.
- **Pacing**: Auto (the default) is what Claude reads in the brief while it plans (tight for tight cuts, snappy or no dead air; loose for let it breathe or slow and reflective; otherwise medium), what you typed winning over the preset's part. Without AI, plain wording rules stand in ("tight", "snappy", "punchy" cut tight; "let it breathe", "keep it loose", "slow-paced" cut loose; "don't cut it too tight" or anything else, medium). Claude plans the target length at the pacing it picks. Pick Tight, Medium or Loose to choose it yourself.
- **Punch-ins**: a 112% zoom alternating at each jump cut no B-roll covers (not on a clip letterboxed in the cut's format). Auto (the default) turns them on when Claude reads the brief asking for punch-ins at the jump cuts (not "no punch-ins" or a one-off "punch in on Roy's face"); without AI, when it says "punch-ins" or "punch in on the jump cuts". On and Off decide it yourself.
- **Audio**: the dialogue level every clip's speech is matched to, short fades at edits, and a **Music bed** song (it ducks under speech and ends with the cut).
- **Output**: format, motion graphics with their style and a **Logo**, project name, Stringout, um/uh, and **Open in Final Cut Pro when it's done**.
- **Recent rough cuts**: reopen an earlier result (right-click to show it in Finder or remove it).

While a run is going the Mac doesn't idle-sleep. When it ends while you're in another app, the Dock icon bounces and a notification says it's ready (or why it stopped).

The result screen shows the version, the project and event to open in Final Cut, how many graphics made it, how many clips were looked at, what the AI cost, and anything left out. From there:

- **Re-cut** runs `roughcut render`: the same plan at a different pace or format, or with punch-ins on or off, with no model call.
- **Revise with notes** runs `roughcut revise`: Claude re-plans from your notes as the next version, and the earlier cut stays in `history/`.
- **Paper Edit** opens the printable paper edit.

If a run stops after the plan was saved, the error offers **Finish without AI**. If your Claude plan's limit or your API credit ran out, it says nothing is lost: build again after the reset.

**Roughcut → Update Roughcut…** runs `roughcut update` and shows what changed.

## How it's put together

| | |
|---|---|
| `Sources/RoughcutKit/` | Foundation-only core: `BuildOptions` (form → CLI arguments; saved settings that survive app updates), `Revisions.swift` (`Preset`, `ReviseOptions`, reading the `resume` command), `RunState` (where the window goes when a run ends), `ProgressEvent`/`RunResult` (JSON-lines decoding, run summary; `DoctorReport` with its optional `StudioStatus`), `CLIProcess` (runs the CLI and streams its output, never dropping the final `done` event), `CLILocator` (finds the CLI, fixes PATH for Homebrew). Unit-tested. |
| `Sources/Roughcut/` | The SwiftUI app: `AppModel` (state machine: form → running → finished/failed; build, re-cut and revise; presets; recent runs; keep-awake and notifications; update), `ContentView` (form, drop zone, music and logo pickers), `RunView` (stage checklist and log), `ResultView` (open in FCP, Finder, paper edit, re-cut, revise), `SettingsView`, `Keychain`. |
| `Tests/RoughcutKitTests/Fixtures/progress-sample.jsonl` | Real CLI output. `tests/test_progress.py` in the Python suite fails if the CLI's output drifts from it. |
| `scripts/build-app.sh` | Packages the SwiftPM executable as a signed (ad-hoc) `.app` with an icon. It also records this repo's CLI path in `Info.plist`. |

**Re-cut** in the result screen runs `roughcut render`: the same plan at a different pace or format, with no model call. Format starts at **Same as this cut**, so a vertical cut stays vertical; pick **Match footage** to go back to the footage's own shape. **Punch-ins** also starts at **Same as this cut** (it says whether the cut has them); On or Off changes them, e.g. when Auto read the brief differently from what you meant. Revise notes can change them too ("make it looser", "remove the punch-ins"): the new version takes the pacing and punch-ins Claude reads in the notes, and the paper edit's revision block says so. Only the new notes count, so a Re-cut you made since stays unless they ask to change it. Keep um/uh and the Stringout stay as the cut has them (the build form's toggles are for the next build). **Finish without AI** runs `roughcut render <folder>` with the run's own settings, as the CLI's hint says.

**Revise** re-plans the cut from your notes. A cut made without AI (provider None) has no planner to reuse, so the box asks which one re-plans it: Claude through your plan (Claude Code) or your API key, or the form's OpenAI/Ollama model. If a revision or re-cut stops (plan limit, error, Cancel), the window goes back to the cut with your notes still in the box and says what happened; **Finish without AI** appears there when the new plan was saved.

**Presets**: the picked preset is remembered across launches and filled in again from the preset as it is now (brief, length, pacing, format and graphics look). A format or length saved in Terminal (`--format 1080x1920@29.97`, `--target "90 sec"`) carries over as written. If an approved graphic or logo has gone since, the form says so before you build.

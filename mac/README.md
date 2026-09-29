# Roughcut for Mac

A SwiftUI window for the `roughcut` CLI in this repo. Drop footage, write a brief, click **Build Rough Cut**, and watch each stage finish. Then **Open in Final Cut Pro**.

It runs the same Python engine you already set up (`.venv/bin/roughcut`) and reads its `--progress-json` stream. Nothing is reimplemented in Swift.

## Build and run

Set up the Python CLI first (repo README → Setup). Then:

```bash
cd mac
scripts/build-app.sh --install   # builds mac/build/Roughcut.app and copies it to /Applications
open /Applications/Roughcut.app
```

Or, while working on the app: `open Package.swift` (opens in Xcode), pick the **Roughcut** scheme, and press Run. `swift run Roughcut` works too.

Needs macOS 14+ and Xcode or the Command Line Tools (`xcode-select --install`). `swift test` runs the RoughcutKit tests and needs full Xcode for XCTest.

The build is signed ad hoc, so after each rebuild macOS may ask again to allow Keychain access (for your API key) and access to footage folders. To stop that, sign with your free Apple Development certificate: `ROUGHCUT_SIGN_IDENTITY="Apple Development: …" scripts/build-app.sh --install` (`security find-identity -v -p codesigning` lists yours).

## First launch

1. The app looks for the CLI in this order: the path set in Settings, the repo it was built from, `~/Archer-OS/.venv/bin/roughcut` and a few other usual spots. If it can't find it, the banner at the top of the window says so. Point to it in **Settings → General**.
2. **Settings → API Keys**: paste your Claude key, or click **Use keys from Terminal** to import `ANTHROPIC_API_KEY` from your `~/.zshrc`. Apps opened from the Dock don't see shell variables, so keys are stored in the macOS Keychain and passed to the CLI only while it runs.
3. **Settings → General → Setup check** confirms ffmpeg, transcription and keys (this runs `roughcut doctor`).

## How it's put together

| | |
|---|---|
| `Sources/RoughcutKit/` | Foundation-only core: `BuildOptions` (form → CLI arguments), `ProgressEvent`/`RunResult` (JSON-lines decoding), `CLIProcess` (runs the CLI and streams its output, never dropping the final `done` event), `CLILocator` (finds the CLI, fixes PATH for Homebrew). Unit-tested. |
| `Sources/Roughcut/` | The SwiftUI app: `AppModel` (state machine: form → running → finished/failed), `ContentView` (form and drop zone), `RunView` (stage checklist and log), `ResultView` (open in FCP, Finder, paper edit, re-cut), `SettingsView`, `Keychain`. |
| `Tests/RoughcutKitTests/Fixtures/progress-sample.jsonl` | Real CLI output. `tests/test_progress.py` in the Python suite fails if the CLI's output drifts from it. |
| `scripts/build-app.sh` | Packages the SwiftPM executable as a signed (ad-hoc) `.app` with an icon. It also records this repo's CLI path in `Info.plist`. |

**Re-cut** in the result screen runs `roughcut render`: the same plan at a different pace or format, with no model call.

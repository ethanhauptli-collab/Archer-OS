import AppKit
import Foundation
import RoughcutKit

/// Screens the app can open itself at, with made-up content, so a picture of each can be taken on a Mac (the "Mac UI
/// pictures" workflow does it) and the design checked against the real thing. `ROUGHCUT_UI_PREVIEW` names the screen,
/// `ROUGHCUT_UI_APPEARANCE=dark` takes the dark look. Nothing else in the app reads either.
enum UIPreview {
    static var screen: String? {
        let value = ProcessInfo.processInfo.environment["ROUGHCUT_UI_PREVIEW"] ?? ""
        return value.isEmpty ? nil : value
    }

    static var dark: Bool {
        ProcessInfo.processInfo.environment["ROUGHCUT_UI_APPEARANCE"] == "dark"
    }

    /// Every screen, in the order of the line.
    static let screens = [
        "getready", "footage-empty", "footage", "brief", "add", "music", "effects", "graphics", "ready", "ready-problem", "failed",
        "finetune", "running-listen", "running", "done", "done-stopped", "adjust",
    ]

    /// `roughcut doctor --json` as it printed on a Mac where everything is installed.
    static let doctorLine = #"""
    {"version": "1.0.0", "commit": "3f9c2ab", "app_source_commit": "a41d7e0", "python": "3.12.4", "executable": "/Users/you/Archer-OS/.venv/bin/python", "ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe", "transcriber": "mlx", "anthropic_key": false, "anthropic_key_status": "missing", "anthropic_key_message": "No Claude API key is set.", "openai_key": false, "openai_installed": true, "claude_code": "/opt/homebrew/bin/claude", "claude_code_auth": "claude.ai", "claude_code_version": "2.1.299", "claude_code_pictures": true, "claude_code_pictures_note": null, "graphics": "not checked", "venture_studio": {"installed": true, "available": true, "reason": null, "summary": "Ready", "notes": []}}
    """#

    /// The two built-in presets, as `roughcut preset list --json` prints them.
    static let presetsJSON = #"""
    [
     {"name": "venture-oc", "description": "Venture Orange County promos", "brief": "What it is: a Venture Orange County promo about local business, food, culture and the people behind them. Tone: energetic, confident and upbeat. Short and punchy: every line earns its place. Structure: open cold on the strongest, most quotable line or moment, not on an introduction. Keep it moving with quick B-roll changes and no logistics talk. End on a clear, upbeat closing line or call to action if one is said. Must keep: names, places and numbers people will remember.", "target": "90s", "style": "tight", "graphics": true, "graphics_style": "Venture Orange County house look: deep navy plates, orange as the one accent, warm gold sparingly. Jost bold for headlines.", "source": "built in"},
     {"name": "save-the-kid", "description": "Save the Kid episodes", "brief": "What it is: an episode of Save the Kid, a personal YouTube channel about fatherhood and adventure. Tone: intimate, reflective and candid: a dad talking honestly, not a presenter. Let moments breathe; keep the laughter and the unscripted moments with the kids.", "target": "10m", "style": "loose", "graphics": true, "graphics_style": "Understated and warm: clean white type with a soft shadow, slow gentle fades.", "source": "built in"}
    ]
    """#

    /// A finished cut, as the `done` event carries it.
    static let doneLine = #"""
    {"event": "done", "result": {"title": "OC Food Fest", "out_dir": "/Users/you/Movies/OC Food Fest_roughcut", "fcpxml": "/Users/you/Movies/OC Food Fest_roughcut/OC Food Fest v1.fcpxml", "report": "/Users/you/Movies/OC Food Fest_roughcut/edit_report.md", "captions": "/Users/you/Movies/OC Food Fest_roughcut/OC Food Fest v1.srt", "rough_seconds": 33.4, "stringout_seconds": 35.2, "edits": 10, "broll": 6, "sections": ["Cold open", "Maria", "On the pier", "Wrap"], "planned_by": "claude-code:claude-opus-5-5", "fell_back": false, "warnings": ["3 graphics sit outside the title-safe area.", "V03.S004: an um was left in because it is part of a word."], "version": 1, "project": "OC Food Fest - Rough Cut v1 (tight, opus-5-5)", "event": "OC Food Fest (2026-10-16)", "graphics": {"planned": 4, "placed": 4, "rendered": 4}, "visual_log": {"described": 7, "total": 7}, "skipped": {"files": [], "plan_refs": 0}, "usage": {"calls": 4, "dollars": 0.0, "subscription_calls": 4, "unpriced_calls": 0}, "report_html": "/Users/you/Movies/OC Food Fest_roughcut/edit_report.html", "cut": {"style": "tight", "format": null, "keep_fillers": false, "stringout": true, "punch_in": false}, "changes": null}}
    """#
}

extension UIPreview {
    /// The same cut when a run stopped after writing it, before its graphics were made.
    static let doneStoppedLine = doneLine
        .replacingOccurrences(of: #""graphics": {"planned": 4, "placed": 4, "rendered": 4}"#, with: #""graphics": {"planned": 4, "placed": 4, "rendered": 0, "note": "none of the 4 were made yet: the run stopped before their designs came back"}, "partial": true"#)
}

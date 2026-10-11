import XCTest
@testable import RoughcutKit

final class RevisionTests: XCTestCase {
    private func options() -> BuildOptions {
        var o = BuildOptions()
        o.sources = [URL(fileURLWithPath: "/Users/you/Movies/OCVIBE")]
        return o
    }

    func testAudioMusicLogoAndLookFlags() {
        var o = options()
        o.music = [URL(fileURLWithPath: "/Users/you/Music/bed one.m4a")]
        o.dialogueLevel = .louder
        o.fades = false
        o.graphics = true
        o.graphicsLogo = "/Users/you/Brand/logo.png"
        o.graphicsLook = ["/Users/you/Movies/Old_roughcut/graphics/G01-lower-third.html"]
        let args = o.arguments()
        XCTAssertTrue(args.contains("--music=/Users/you/Music/bed one.m4a"))
        XCTAssertTrue(args.contains("--dialogue-level=-14"))  // with "=", so argparse never reads -14 as a flag
        XCTAssertEqual(args.firstIndex(of: "--fades").map { args[$0 + 1] }, "off")
        XCTAssertTrue(args.contains("--graphics-logo=/Users/you/Brand/logo.png"))
        XCTAssertTrue(args.contains("--graphics-look=/Users/you/Movies/Old_roughcut/graphics/G01-lower-third.html"))
        XCTAssertFalse(args.contains { $0.hasPrefix("--preset") })  // the form carries the preset's values
    }

    func testDefaultsSendNoAudioFlags() {
        let args = options().arguments()
        XCTAssertFalse(args.contains { $0.hasPrefix("--dialogue-level") || $0 == "--fades" || $0.hasPrefix("--music") })
    }

    func testLogoOnlyGoesWithGraphics() {
        var o = options()
        o.graphicsLogo = "/x/logo.png"
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--graphics-logo") })
    }

    func testMusicIsPerProject() {
        var o = options()
        o.music = [URL(fileURLWithPath: "/m.m4a")]
        o.dialogueLevel = .off
        XCTAssertEqual(o.preferencesOnly.music, [])
        XCTAssertEqual(o.preferencesOnly.dialogueLevel, .off)
    }

    func testPresetFillsTheForm() throws {
        let json = #"[{"name": "venture-oc", "description": "Venture Orange County promos", "brief": "Promo brief", "target": "90s", "style": "tight", "format": null, "graphics": true, "graphics_style": "navy and orange", "logo": "/b/logo.png", "look": ["/b/G01.html"], "source": "built in"}, {"name": "save-the-kid", "description": null, "brief": "Episode brief", "target": "10m", "style": "loose", "format": "vertical", "graphics": false, "graphics_style": "", "logo": null, "look": [], "source": "built in"}]"#
        let presets = try XCTUnwrap(JSONLines.presets(from: "\n" + json + "\n"))
        XCTAssertEqual(presets.map(\.name), ["venture-oc", "save-the-kid"])
        XCTAssertEqual(presets[0].title, "Venture OC: Venture Orange County promos")

        var o = options()
        presets[0].apply(to: &o)
        XCTAssertEqual(o.presetName, "venture-oc")
        XCTAssertEqual(o.brief, "Promo brief")
        XCTAssertEqual(o.targetLength, "90s")
        XCTAssertEqual(o.style, .tight)
        XCTAssertEqual(o.format, .auto)
        XCTAssertTrue(o.graphics)
        XCTAssertEqual(o.graphicsStyle, "navy and orange")
        XCTAssertEqual(o.graphicsLogo, "/b/logo.png")
        XCTAssertEqual(o.graphicsLook, ["/b/G01.html"])

        // Switching presets replaces the other preset's brief...
        presets[1].apply(to: &o, replacing: presets.compactMap(\.brief))
        XCTAssertEqual(o.brief, "Episode brief")
        XCTAssertEqual(o.format, .vertical)
        XCTAssertFalse(o.graphics)
        XCTAssertEqual(o.graphicsLook, [])
        // ...but a brief you typed stays, after the preset's.
        o.brief = "Phoenix's first surf lesson"
        presets[0].apply(to: &o, replacing: presets.compactMap(\.brief))
        XCTAssertEqual(o.brief, "Promo brief\n\nPhoenix's first surf lesson")
    }

    func testReviseArguments() {
        let revise = ReviseOptions(
            outputFolder: URL(fileURLWithPath: "/Users/you/Movies/OCVIBE_roughcut"),
            notesFile: URL(fileURLWithPath: "/tmp/notes.txt"),
            targetLength: "1:30"
        )
        XCTAssertEqual(revise.arguments(), [
            "revise", "/Users/you/Movies/OCVIBE_roughcut", "--progress-json", "--notes-file=/tmp/notes.txt", "--target=90",
        ])
        XCTAssertFalse(ReviseOptions(outputFolder: URL(fileURLWithPath: "/o"), notesFile: URL(fileURLWithPath: "/n")).arguments().contains { $0.hasPrefix("--target") })
    }

    func testResumeCommandGivesTheOutputFolder() {
        XCTAssertEqual(
            ResumeCommand.outputFolder(from: "roughcut render '/Users/you/Desktop/YouTube Current Project Staging/OCVIBE_roughcut'")?.path,
            "/Users/you/Desktop/YouTube Current Project Staging/OCVIBE_roughcut"
        )
        XCTAssertEqual(ResumeCommand.outputFolder(from: "roughcut render /tmp/a_roughcut")?.path, "/tmp/a_roughcut")
        XCTAssertEqual(ResumeCommand.outputFolder(from: #"roughcut render '/tmp/Ethan'"'"'s cut'"#)?.path, "/tmp/Ethan's cut")
        XCTAssertNil(ResumeCommand.outputFolder(from: nil))
        XCTAssertNil(ResumeCommand.outputFolder(from: "roughcut build x"))
        XCTAssertNil(ResumeCommand.outputFolder(from: "roughcut render 'unterminated"))
    }

    func testErrorEventCarriesResumeAndKind() throws {
        let line = #"{"event": "error", "message": "Your Claude plan's usage limit was reached", "resume": "roughcut render '/tmp/x_roughcut'", "kind": "usage_limit"}"#
        let event = try XCTUnwrap(JSONLines.event(from: line))
        XCTAssertEqual(event.kind, "usage_limit")
        XCTAssertEqual(ResumeCommand.outputFolder(from: event.resume)?.path, "/tmp/x_roughcut")
    }

    func testDoneEventWithTheRunSummary() throws {
        let line = #"{"event": "done", "result": {"title": "OC Food Fest", "out_dir": "/o", "fcpxml": "/o/OC Food Fest v3.fcpxml", "report": "/o/edit_report.md", "rough_seconds": 92.5, "stringout_seconds": null, "edits": 41, "broll": 18, "sections": ["Hook"], "planned_by": "anthropic:claude-opus-5-5", "fell_back": false, "warnings": [], "version": 3, "project": "OC Food Fest - Rough Cut v3 (tight, Opus)", "event": "OC Food Fest (2026-10-11)", "graphics": {"planned": 5, "placed": 5, "rendered": 4, "note": null}, "visual_log": {"described": 12, "total": 13}, "skipped": {"files": ["broken.mov"], "plan_refs": 2}, "usage": {"calls": 4, "dollars": 0.8412, "by_purpose": {"edit plan": 0.64, "visual log": 0.2012}, "calls_by_purpose": {"edit plan": 1}, "subscription_calls": 0, "unpriced_calls": 0}, "report_html": "/o/edit_report.html"}}"#
        let result = try XCTUnwrap(JSONLines.event(from: line)?.result)
        XCTAssertEqual(result.version, 3)
        XCTAssertEqual(result.project, "OC Food Fest - Rough Cut v3 (tight, Opus)")
        XCTAssertEqual(result.graphics, GraphicsSummary(planned: 5, placed: 5, rendered: 4, note: nil))
        XCTAssertEqual(result.visualLog?.described, 12)
        XCTAssertEqual(result.skipped?.planRefs, 2)
        XCTAssertEqual(result.usage?.summaryText, "about $0.84 (edit plan $0.64, visual log $0.20)")
        XCTAssertEqual(result.paperEditURL.path, "/o/edit_report.md")  // the .html doesn't exist here

        // An older CLI's payload still decodes, and results survive being saved for Recent runs.
        let old = #"{"event": "done", "result": {"title": "T", "out_dir": "/o", "fcpxml": "/o/T.fcpxml", "report": "/o/r.md", "rough_seconds": 1, "stringout_seconds": 1, "edits": 1, "broll": 0, "sections": [], "planned_by": "none", "fell_back": false, "warnings": []}}"#
        let older = try XCTUnwrap(JSONLines.event(from: old)?.result)
        XCTAssertNil(older.version)
        let saved = try JSONDecoder().decode(RunResult.self, from: JSONEncoder().encode(result))
        XCTAssertEqual(saved, result)
    }

    func testSubscriptionUsageSaysNoCharge() {
        let usage = UsageSummary(calls: 3, dollars: 0, byPurpose: [:], subscriptionCalls: 3, unpricedCalls: 0)
        XCTAssertEqual(usage.summaryText, "3 requests on your Claude plan (no API charge)")
    }
}

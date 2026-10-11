import XCTest
@testable import RoughcutKit

final class RevisionTests: XCTestCase {
    private func options() -> BuildOptions {
        var o = BuildOptions()
        o.sources = [URL(fileURLWithPath: "/Users/you/Movies/OCVIBE")]
        return o
    }

    /// A `done` payload for a cut in /o/Promo_roughcut.
    private func result(plannedBy: String, extra: String = "") throws -> RunResult {
        let line = "{\"event\": \"done\", \"result\": {\"title\": \"Promo\", \"out_dir\": \"/o/Promo_roughcut\", \"fcpxml\": \"/o/Promo_roughcut/Promo v1.fcpxml\", \"report\": \"/o/Promo_roughcut/edit_report.md\", \"rough_seconds\": 90, \"stringout_seconds\": null, \"edits\": 12, \"broll\": 3, \"sections\": [], \"planned_by\": \"\(plannedBy)\", \"fell_back\": false, \"warnings\": []\(extra)}}"
        return try XCTUnwrap(JSONLines.event(from: line)?.result)
    }

    private func doctor(claudeCodeAuth: String?, keyStatus: String) throws -> DoctorReport {
        let auth = claudeCodeAuth.map { "\"\($0)\"" } ?? "null"
        let line = "{\"version\": \"0.1.0\", \"python\": \"3.12.4\", \"executable\": \"/x\", \"anthropic_key\": \(keyStatus == "ok"), \"anthropic_key_status\": \"\(keyStatus)\", \"openai_key\": false, \"openai_installed\": false, \"claude_code\": \"/opt/homebrew/bin/claude\", \"claude_code_auth\": \(auth)}"
        return try XCTUnwrap(JSONLines.doctor(from: line))
    }

    func testPresetFormatsAndLengthsTheCLIAccepts() throws {
        // Saved in Terminal: `preset save reels --format 1080x1920@29.97 --target "90 sec"` and `--format 4K --target "10 mins"`.
        let json = #"[{"name": "reels", "brief": "Reels brief", "target": "90 sec", "style": "tight", "format": "1080x1920@29.97", "graphics": false, "look": []}, {"name": "stk-4k", "brief": "Episode brief", "target": "10 mins", "style": "loose", "format": "4K", "graphics": false}, {"name": "long", "format": "uhd", "target": "1hr"}]"#
        let presets = try XCTUnwrap(JSONLines.presets(from: json))
        var o = options()
        presets[0].apply(to: &o)
        XCTAssertEqual(o.format, .custom("1080x1920@29.97"))  // was "Match footage": a 16:9 cut from a vertical preset
        XCTAssertEqual(o.problems, [])  // "90 sec" blocked Build
        var args = o.arguments()
        XCTAssertTrue(args.contains("--format=1080x1920@29.97"))
        XCTAssertTrue(args.contains("--target=90"))

        presets[1].apply(to: &o, replacing: presets.compactMap(\.brief))
        XCTAssertEqual(o.format, .uhd)
        XCTAssertEqual(o.problems, [])
        args = o.arguments()
        XCTAssertEqual(args.firstIndex(of: "--format").map { args[$0 + 1] }, "4k")
        XCTAssertTrue(args.contains("--target=600"))

        presets[2].apply(to: &o)
        XCTAssertEqual(o.format, .uhd)
        XCTAssertTrue(o.arguments().contains("--target=3600"))
    }

    func testPresetIsFilledInAgainAfterARelaunch() throws {
        let before = #"[{"name": "venture-oc", "description": "Venture Orange County promos", "brief": "Promo brief", "target": "90s", "style": "tight", "format": "vertical", "graphics": true, "graphics_style": "navy and orange", "logo": "/b/logo.png", "look": ["/cfg/venture-oc.look/1-G01-lower-third.html"]}]"#
        var o = options()
        try XCTUnwrap(JSONLines.presets(from: before))[0].apply(to: &o)
        let saved = try JSONEncoder().encode(o.preferencesOnly)

        // Quit and relaunch: preferences keep the preset's name but not its brief and length,
        // so the picker said "Venture OC" while the build got no brief and no target.
        var relaunched = try JSONDecoder().decode(BuildOptions.self, from: saved)
        XCTAssertEqual(relaunched.presetName, "venture-oc")
        XCTAssertEqual(relaunched.brief, "")
        XCTAssertEqual(relaunched.targetLength, "")
        // Meanwhile `roughcut preset save venture-oc --look-from …` replaced the approved design.
        let after = before.replacingOccurrences(of: "1-G01-lower-third.html", with: "2-G02-stat.html")
        let presets = try XCTUnwrap(JSONLines.presets(from: after))
        relaunched.reapplyPreset(from: presets)
        XCTAssertEqual(relaunched.brief, "Promo brief")
        XCTAssertEqual(relaunched.targetLength, "90s")
        XCTAssertEqual(relaunched.style, .tight)
        XCTAssertEqual(relaunched.format, .vertical)
        XCTAssertEqual(relaunched.graphicsLook, ["/cfg/venture-oc.look/2-G02-stat.html"])
        let args = relaunched.arguments()
        XCTAssertTrue(args.contains("--context=Promo brief"))
        XCTAssertTrue(args.contains("--target=90"))

        // What was typed before the list loaded stays.
        var typed = try JSONDecoder().decode(BuildOptions.self, from: saved)
        typed.brief = "OC Food Fest, feature Maria"
        typed.targetLength = "60s"
        typed.reapplyPreset(from: presets)
        XCTAssertEqual(typed.brief, "Promo brief\n\nOC Food Fest, feature Maria")
        XCTAssertEqual(typed.targetLength, "60s")

        // A preset that's gone isn't shown as picked, and the approved graphics it brought go with it.
        var gone = try JSONDecoder().decode(BuildOptions.self, from: saved)
        gone.reapplyPreset(from: [])
        XCTAssertEqual(gone.presetName, "")
        XCTAssertEqual(gone.graphicsLook, [])

        // No preset picked: nothing changes.
        var plain = options()
        plain.brief = "mine"
        let untouched = plain
        plain.reapplyPreset(from: presets)
        XCTAssertEqual(plain, untouched)
    }

    func testReviseOfACutMadeWithoutAIUsesTheChosenPlanner() throws {
        // A "None: just cut the silence" cut has no planner to reuse: the CLI fell back to the API key,
        // which a Claude-plan user doesn't have, and stopped with "No Claude API key is set".
        let quick = try result(plannedBy: "heuristic")
        XCTAssertTrue(quick.madeWithoutAI)
        var form = BuildOptions()
        form.provider = .none  // the form still says how the quick cut was made
        let notes = URL(fileURLWithPath: "/tmp/notes.txt")
        XCTAssertEqual(ReviseOptions.revising(quick, notesFile: notes, targetLength: "60 sec", provider: .claudeCode, form: form).arguments(), [
            "revise", "/o/Promo_roughcut", "--progress-json", "--notes-file=/tmp/notes.txt", "--target=60",
            "--provider", "claude-code", "--effort", "high",
        ])
        // Claude models carry over between the API and Claude Code; another provider's model doesn't.
        form.provider = .claudeCode
        form.model = "claude-sonnet-5-5"
        form.effort = .max
        let viaKey = ReviseOptions.revising(quick, notesFile: notes, targetLength: "", provider: .anthropic, form: form).arguments()
        XCTAssertEqual(Array(viaKey.dropFirst(4)), ["--provider", "anthropic", "--model=claude-sonnet-5-5", "--effort", "max"])
        form.provider = .ollama
        form.model = "llama3.3"
        let local = ReviseOptions.revising(quick, notesFile: notes, targetLength: "", provider: .ollama, form: form).arguments()
        XCTAssertEqual(Array(local.dropFirst(4)), ["--provider", "ollama", "--model=llama3.3"])
        let claude = ReviseOptions.revising(quick, notesFile: notes, targetLength: "", provider: .claudeCode, form: form).arguments()
        XCTAssertEqual(Array(claude.dropFirst(4)), ["--provider", "claude-code", "--effort", "max"])

        // A cut a model planned is re-planned by that model, the CLI's default: no provider flags.
        let planned = try result(plannedBy: "claude-code:claude-opus-5-5")
        XCTAssertFalse(planned.madeWithoutAI)
        XCTAssertEqual(ReviseOptions.revising(planned, notesFile: notes, targetLength: "", provider: .anthropic, form: form).arguments(), [
            "revise", "/o/Promo_roughcut", "--progress-json", "--notes-file=/tmp/notes.txt",
        ])
    }

    func testRevisePlannerChoices() throws {
        var form = BuildOptions()
        form.provider = .none
        XCTAssertEqual(ReviseOptions.providers(form: form), [.claudeCode, .anthropic])
        // Signed in to Claude Code with a plan: that first; with only an API key, the key.
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: try doctor(claudeCodeAuth: "claude.ai", keyStatus: "missing")), .claudeCode)
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: try doctor(claudeCodeAuth: nil, keyStatus: "ok")), .anthropic)
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: try doctor(claudeCodeAuth: "api_key", keyStatus: "ok")), .anthropic)
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: nil), .anthropic)
        // The form's own provider when it can re-plan.
        form.provider = .claudeCode
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: try doctor(claudeCodeAuth: nil, keyStatus: "ok")), .claudeCode)
        form.provider = .ollama
        form.model = "llama3.3"
        XCTAssertEqual(ReviseOptions.providers(form: form), [.claudeCode, .anthropic, .ollama])
        XCTAssertEqual(ReviseOptions.defaultProvider(form: form, doctor: nil), .ollama)
        // `revise` takes no server address, so a compatible server isn't offered.
        form.provider = .openaiCompatible
        XCTAssertEqual(ReviseOptions.providers(form: form), [.claudeCode, .anthropic])
        // A choice the form no longer allows falls back to the first choice.
        XCTAssertEqual(ReviseOptions.planner(.ollama, form: form, doctor: try doctor(claudeCodeAuth: "claude.ai", keyStatus: "missing")), .claudeCode)
        XCTAssertEqual(ReviseOptions.planner(.anthropic, form: form, doctor: nil), .anthropic)
    }

    func testReviseLengthThatDoesNotReadIsCaught() {
        // It used to be dropped without a word, and the revision kept the build's length.
        XCTAssertEqual(ReviseOptions.lengthProblem("about a minute"), "New length should look like 8m, 90s or 8:30.")
        XCTAssertNil(ReviseOptions.lengthProblem(""))
        XCTAssertNil(ReviseOptions.lengthProblem("  "))
        XCTAssertNil(ReviseOptions.lengthProblem("60 sec"))
        XCTAssertNil(ReviseOptions.lengthProblem("1:30"))
    }

    func testDoneEventCarriesTheCutsSettings() throws {
        let vertical = try result(plannedBy: "anthropic:claude-opus-5-5", extra: #", "cut": {"style": "tight", "format": "vertical", "keep_fillers": true, "stringout": false}"#)
        XCTAssertEqual(vertical.cut, CutSettings(style: "tight", format: "vertical", keepFillers: true, stringout: false))
        XCTAssertEqual(vertical.cut?.sequenceFormat, .vertical)
        XCTAssertEqual(vertical.cut?.cutStyle, .tight)
        // Saved with Recent runs and read back.
        let saved = try JSONEncoder().encode(vertical)
        XCTAssertEqual(try JSONDecoder().decode(RunResult.self, from: saved), vertical)
        // The footage's own format is a null format; an older CLI sends no "cut" at all.
        let footage = try result(plannedBy: "heuristic", extra: #", "cut": {"style": "medium", "format": null, "keep_fillers": false, "stringout": true}"#)
        XCTAssertEqual(footage.cut?.sequenceFormat, .auto)
        XCTAssertNil(try result(plannedBy: "heuristic").cut)
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

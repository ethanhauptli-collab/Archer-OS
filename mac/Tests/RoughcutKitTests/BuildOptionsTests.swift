import XCTest
@testable import RoughcutKit

final class BuildOptionsTests: XCTestCase {
    private func options() -> BuildOptions {
        var o = BuildOptions()
        o.sources = [URL(fileURLWithPath: "/Users/you/Movies/Trip")]
        return o
    }

    func testTypicalClaudeBuild() {
        var o = options()
        o.brief = "- open on the lake"  // leading dash must not become a flag
        o.targetLength = " 8m "
        o.style = .tight
        o.format = .vertical
        XCTAssertEqual(o.problems, [])
        XCTAssertEqual(o.arguments(), [
            "build", "/Users/you/Movies/Trip", "--progress-json",
            "--context=- open on the lake", "--target=480",
            "--style", "tight", "--format", "vertical",
            "--provider", "anthropic", "--effort", "high", "--vision",
        ])
    }

    func testSilenceOnlySkipsModelFlags() {
        var o = options()
        o.provider = .none
        o.model = "ignored"
        o.keepFillers = true
        o.stringout = false
        XCTAssertEqual(o.arguments(), [
            "build", "/Users/you/Movies/Trip", "--progress-json",
            "--style", "auto", "--provider", "none", "--keep-fillers", "--no-stringout",
        ])
    }

    func testCompatibleServerNeedsModelAndURL() {
        var o = options()
        o.provider = .openaiCompatible
        XCTAssertEqual(o.problems.count, 2)
        o.model = "qwen"
        o.baseURL = "http://localhost:1234/v1"
        o.vision = false
        XCTAssertEqual(o.problems, [])
        let args = o.arguments()
        XCTAssertTrue(args.contains("--model=qwen"))
        XCTAssertTrue(args.contains("--base-url=http://localhost:1234/v1"))
        XCTAssertTrue(args.contains("--api-key-env=OPENAI_API_KEY"))
        XCTAssertTrue(args.contains("--no-vision"))
        XCTAssertFalse(args.contains("--effort"))
    }

    func testProblems() {
        var o = BuildOptions()
        XCTAssertEqual(o.problems.first, "Add footage: drop folders or clips above.")
        o = options()
        o.targetLength = "about eight minutes"
        XCTAssertEqual(o.problems, ["Target length should look like 8m, 90s or 8:30 (1 second to 24 hours)."])
    }

    func testOutputFolderAndName() {
        var o = options()
        o.outputFolder = URL(fileURLWithPath: "/Volumes/SSD/Cuts")
        o.projectName = "Trip, Day 1"
        let args = o.arguments()
        XCTAssertTrue(args.contains("--out=/Volumes/SSD/Cuts"))
        XCTAssertTrue(args.contains("--name=Trip, Day 1"))
    }

    func testSonnetForPlanningAndDescribing() {
        var o = options()
        o.model = "claude-sonnet-5-5"
        o.visionModel = "claude-sonnet-5-5"
        let args = o.arguments()
        XCTAssertTrue(args.contains("--model=claude-sonnet-5-5"))
        XCTAssertTrue(args.contains("--vision-model=claude-sonnet-5-5"))
        o.vision = false
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--vision-model") })
        // The default Opus choice sends no --model, so the CLI default applies.
        XCTAssertEqual(ClaudeModelChoice.planners.first?.id, "")
        XCTAssertFalse(options().arguments().contains { $0.hasPrefix("--model") })
    }

    func testClaudeCodeUsesTheSubscriptionProvider() {
        var o = options()
        o.provider = .claudeCode
        o.model = "claude-sonnet-5-5"
        XCTAssertEqual(o.problems, [])
        let args = o.arguments()
        XCTAssertTrue(args.contains("claude-code"))
        XCTAssertTrue(args.contains("--model=claude-sonnet-5-5"))
        XCTAssertTrue(args.contains("--effort"))
        XCTAssertNil(ModelProvider.claudeCode.apiKeyVariable)  // no API key: the subscription sign-in
        XCTAssertTrue(ModelProvider.claudeCode.usesClaudeModels)
    }

    func testMotionGraphicsFlags() {
        var o = options()
        o.graphics = true
        o.graphicsStyle = " navy and orange, bold "
        let args = o.arguments()
        XCTAssertTrue(args.contains("--graphics"))
        XCTAssertTrue(args.contains("--graphics-style=navy and orange, bold"))
        o.provider = .none  // set directly, past the form's rules (FormStateTests drives the picker)
        XCTAssertFalse(o.arguments().contains("--graphics"))
        XCTAssertEqual(o.problems, ["Motion graphics are designed by the AI: choose a provider other than None, or turn motion graphics off."])
    }

    func testReferenceImagesGoWithGraphicsOnly() {
        let look = URL(fileURLWithPath: "/Users/you/Pictures/title look.png")
        let sheet = URL(fileURLWithPath: "/Users/you/Pictures/-brand sheet.jpg")
        var o = options()
        o.graphicsReferences = [look, sheet]
        // Graphics off: nothing about them is sent, like the logo and the style.
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--graphics-ref") })
        o.graphics = true
        let args = o.arguments()
        // One flag each, in the order shown, with "=" so a name that starts with "-" can't be read as a flag.
        XCTAssertEqual(args.filter { $0.hasPrefix("--graphics-ref") }, [
            "--graphics-ref=/Users/you/Pictures/title look.png",
            "--graphics-ref=/Users/you/Pictures/-brand sheet.jpg",
        ])
        XCTAssertLessThan(try XCTUnwrap(args.firstIndex(of: "--graphics")), try XCTUnwrap(args.firstIndex { $0.hasPrefix("--graphics-ref") }))
        // The graphics designer needs a model: none, none.
        o.provider = .none
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--graphics-ref") })
    }

    func testBuildWithoutReferencesIsAsItWas() {
        var o = options()
        o.graphics = true
        o.graphicsStyle = "navy and orange"
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--graphics-ref") })
        XCTAssertEqual(o.problems, [])
        XCTAssertEqual(o.arguments(), [
            "build", "/Users/you/Movies/Trip", "--progress-json", "--style", "auto",
            "--provider", "anthropic", "--effort", "high", "--vision",
            "--graphics", "--graphics-style=navy and orange",
        ])
    }

    func testAtMostEightReferencesAreSent() {
        var o = options()
        o.graphics = true
        o.graphicsReferences = (1...10).map { URL(fileURLWithPath: "/Users/you/Pictures/look \($0).png") }
        let sent = o.arguments().filter { $0.hasPrefix("--graphics-ref") }
        XCTAssertEqual(sent.count, 8)
        XCTAssertEqual(sent.last, "--graphics-ref=/Users/you/Pictures/look 8.png")
    }

    func testReferencesAreRememberedByPathAndOnlyWhileTheyExist() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-prefs-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let kept = dir.appendingPathComponent("look.png")
        let soonGone = dir.appendingPathComponent("gone.png")
        try Data("x".utf8).write(to: kept)
        try Data("x".utf8).write(to: soonGone)

        var o = options()
        o.graphics = true
        o.graphicsReferences = [kept, soonGone]
        let saved = try JSONEncoder().encode(o.preferencesOnly)
        // Remembered with the other preferences, as paths (they aren't per project like the footage).
        XCTAssertEqual(o.preferencesOnly.graphicsReferences, [kept, soonGone])
        XCTAssertEqual(try JSONDecoder().decode(BuildOptions.self, from: saved).graphicsReferences.map(\.path), [kept.path, soonGone.path])

        // A file that has gone since isn't restored.
        try FileManager.default.removeItem(at: soonGone)
        XCTAssertEqual(try JSONDecoder().decode(BuildOptions.self, from: saved).graphicsReferences.map(\.path), [kept.path])

        // Preferences from an app before reference images have none.
        XCTAssertEqual(try JSONDecoder().decode(BuildOptions.self, from: Data("{}".utf8)).graphicsReferences, [])
        XCTAssertEqual(BuildOptions().graphicsReferences, [])
    }

    func testTargetIsSentAsSeconds() {
        var o = options()
        o.targetLength = "8 min"
        XCTAssertTrue(o.arguments().contains("--target=480"))
        o.targetLength = "1:30"
        XCTAssertTrue(o.arguments().contains("--target=90"))
    }

    func testRecut() {
        let r = RecutOptions(outputFolder: URL(fileURLWithPath: "/tmp/Trip_roughcut"), style: .loose, format: .uhd, keepFillers: true)
        XCTAssertEqual(r.arguments(), ["render", "/tmp/Trip_roughcut", "--progress-json", "--style", "loose", "--format", "4k", "--keep-fillers"])
    }

    func testRecutKeepsTheCutsOwnSettingsUnlessGiven() {
        // A vertical promo re-cut "tighter" came back 16:9: "Match footage" was the picker's default and sent nothing.
        let folder = URL(fileURLWithPath: "/tmp/Promo_roughcut")
        let base = ["render", "/tmp/Promo_roughcut", "--progress-json"]
        // Pacing changed, Format "Same as this cut": no format, fillers or stringout flags, so the CLI keeps the cut's.
        XCTAssertEqual(RecutOptions(outputFolder: folder, style: .tight).arguments(), base + ["--style", "tight"])
        // "Match footage" is a choice of its own now.
        XCTAssertEqual(RecutOptions(outputFolder: folder, style: .tight, format: .auto).arguments(), base + ["--style", "tight", "--format", "auto"])
        // Finish without AI: just the folder, like `roughcut render <folder>` in Terminal.
        XCTAssertEqual(RecutOptions(outputFolder: folder).arguments(), base)
        // Toggles, when given, go both ways.
        XCTAssertEqual(RecutOptions(outputFolder: folder, keepFillers: false, stringout: true).arguments(), base + ["--no-keep-fillers", "--stringout"])
        XCTAssertEqual(RecutOptions(outputFolder: folder, keepFillers: true, stringout: false).arguments(), base + ["--keep-fillers", "--no-stringout"])
        XCTAssertEqual(RecutOptions(outputFolder: folder, format: .custom("1080x1920@29.97")).arguments(), base + ["--format=1080x1920@29.97"])
    }

    func testRecutCanTurnPunchInsOnOrOff() {
        // An Auto build that misread its brief could only lose its punch-ins by hand in Final Cut, or a billed re-plan.
        let folder = URL(fileURLWithPath: "/tmp/Promo_roughcut")
        let base = ["render", "/tmp/Promo_roughcut", "--progress-json"]
        XCTAssertEqual(RecutOptions(outputFolder: folder, style: .tight, punchIn: false).arguments(), base + ["--style", "tight", "--no-punch-in"])
        XCTAssertEqual(RecutOptions(outputFolder: folder, punchIn: true).arguments(), base + ["--punch-in"])
        XCTAssertEqual(RecutOptions(outputFolder: folder, style: .medium).arguments(), base + ["--style", "medium"])  // as the cut has them
        // The picker's first row says what the cut has.
        XCTAssertEqual(RecutOptions.samePunchInTitle(CutSettings(style: "tight", punchIn: true)), "Same as this cut (On)")
        XCTAssertEqual(RecutOptions.samePunchInTitle(CutSettings(style: "tight", punchIn: false)), "Same as this cut (Off)")
        XCTAssertEqual(RecutOptions.samePunchInTitle(CutSettings(style: "tight")), "Same as this cut")  // an older CLI
        XCTAssertEqual(RecutOptions.samePunchInTitle(nil), "Same as this cut")
    }

    func testFinishGraphicsIsARenderThatDesignsWhatIsMissing() throws {
        let folder = URL(fileURLWithPath: "/tmp/Promo_roughcut")
        let base = ["render", "/tmp/Promo_roughcut", "--progress-json"]
        // Without the flag nothing changes: a re-cut and Finish without AI send what they always did.
        XCTAssertFalse(RecutOptions(outputFolder: folder).designMissing)
        XCTAssertEqual(RecutOptions(outputFolder: folder).arguments(), base)
        XCTAssertEqual(RecutOptions(outputFolder: folder, designMissing: false).arguments(), base)
        XCTAssertFalse(RecutOptions(outputFolder: folder, style: .tight).arguments().contains("--design-missing"))
        // With it, only the folder and the flag: every cut setting comes from the saved run.
        XCTAssertEqual(RecutOptions(outputFolder: folder, designMissing: true).arguments(), base + ["--design-missing"])
        XCTAssertEqual(RecutOptions(outputFolder: folder, style: .tight, punchIn: true, designMissing: true).arguments(), base + ["--style", "tight", "--punch-in", "--design-missing"])
        // Finish graphics of a result: its own folder, the flag, and nothing else.
        let line = #"{"event":"done","result":{"title":"T","out_dir":"/tmp/Promo_roughcut","fcpxml":"/tmp/Promo_roughcut/T v2.fcpxml","report":"/tmp/Promo_roughcut/edit_report.md","rough_seconds":20.5,"stringout_seconds":null,"edits":4,"broll":1,"sections":["A"],"planned_by":"anthropic:claude-opus-5-5","fell_back":false,"warnings":[],"version":2}}"#
        let result = try XCTUnwrap(JSONLines.event(from: line)?.result)
        XCTAssertEqual(RecutOptions.finishingGraphics(of: result).arguments(), base + ["--design-missing"])

        // A revision writes only the Rough Cut project while its saved Stringout setting stays on: the version that finishes
        // its graphics writes none either, so the event's project list is the same from one version to the next.
        var revision = result
        revision.cut = CutSettings(style: "medium", stringout: true)
        XCTAssertNil(revision.stringoutSeconds)
        XCTAssertEqual(RecutOptions.finishingGraphics(of: revision).arguments(), base + ["--no-stringout", "--design-missing"])
        // A cut that has a Stringout project keeps writing it; so does one whose setting is off or unknown.
        var built = revision
        built.stringoutSeconds = 12.5
        XCTAssertEqual(RecutOptions.finishingGraphics(of: built).arguments(), base + ["--design-missing"])
        revision.cut = CutSettings(style: "medium", stringout: false)
        XCTAssertEqual(RecutOptions.finishingGraphics(of: revision).arguments(), base + ["--design-missing"])

        // Finish without AI follows the same rule: a revision on screen brings no Stringout project back; anything else
        // (a build, a cut whose setting is off, nothing on screen) sends a bare render as it always did.
        revision.cut = CutSettings(style: "medium", stringout: true)
        XCTAssertEqual(RecutOptions.finishingWithoutAI(in: folder, shown: revision).arguments(), base + ["--no-stringout"])
        XCTAssertEqual(RecutOptions.finishingWithoutAI(in: folder, shown: built).arguments(), base)
        XCTAssertEqual(RecutOptions.finishingWithoutAI(in: folder, shown: nil).arguments(), base)
        revision.cut = CutSettings(style: "medium", stringout: false)
        XCTAssertEqual(RecutOptions.finishingWithoutAI(in: folder, shown: revision).arguments(), base)
    }

    func testRecutPickerStartsFromTheCut() {
        let vertical = CutSettings(style: "tight", format: "vertical", keepFillers: true, stringout: false)
        XCTAssertEqual(RecutOptions.sameFormatTitle(vertical), "Same as this cut (Vertical 9:16)")
        XCTAssertEqual(RecutOptions.sameFormatTitle(CutSettings(style: "medium")), "Same as this cut (Match footage)")
        XCTAssertEqual(RecutOptions.sameFormatTitle(nil), "Same as this cut")  // an older run: still keeps its format
        // The pace offered first is the other one from the cut's own, not the build form's.
        XCTAssertEqual(RecutOptions.suggestedStyle(for: vertical, otherwise: .loose), .medium)
        XCTAssertEqual(RecutOptions.suggestedStyle(for: CutSettings(style: "loose"), otherwise: .tight), .tight)
        XCTAssertEqual(RecutOptions.suggestedStyle(for: nil, otherwise: .tight), .medium)
        // A custom format stays selectable in the picker.
        let custom = SequenceFormat(cliText: "1080x1920@29.97")
        XCTAssertEqual(SequenceFormat.choices(including: custom), SequenceFormat.standard + [custom])
        XCTAssertEqual(SequenceFormat.choices(including: .vertical), SequenceFormat.standard)
        XCTAssertEqual(SequenceFormat.choices(including: nil), SequenceFormat.standard)
    }

    func testSequenceFormatReadsWhatTheCLIWrites() throws {
        XCTAssertEqual(SequenceFormat(cliText: "4K"), .uhd)
        XCTAssertEqual(SequenceFormat(cliText: "uhd"), .uhd)
        XCTAssertEqual(SequenceFormat(cliText: " Vertical "), .vertical)
        XCTAssertEqual(SequenceFormat(cliText: "1080P"), .hd)
        XCTAssertEqual(SequenceFormat(cliText: "square"), .square)
        XCTAssertEqual(SequenceFormat(cliText: nil), .auto)
        XCTAssertEqual(SequenceFormat(cliText: ""), .auto)
        XCTAssertEqual(SequenceFormat(cliText: "auto"), .auto)
        XCTAssertEqual(SequenceFormat(cliText: "720p"), .custom("720p"))
        XCTAssertEqual(SequenceFormat(cliText: "3840x2160@23.98"), .custom("3840x2160@23.98"))
        XCTAssertEqual(SequenceFormat.custom("720p").title, "720p")
        XCTAssertNil(SequenceFormat.auto.cliValue)
        XCTAssertEqual(SequenceFormat.custom("720p").cliValue, "720p")

        // Preferences saved by the earlier app (raw values) still read, and a custom format survives a save.
        let earlier: [(String, SequenceFormat)] = [("auto", .auto), ("1080p", .hd), ("4k", .uhd), ("vertical", .vertical), ("square", .square)]
        for (saved, format) in earlier {
            let decoded = try JSONDecoder().decode([SequenceFormat].self, from: Data("[\"\(saved)\"]".utf8))
            XCTAssertEqual(decoded, [format])
            let encoded = try JSONEncoder().encode([format])
            XCTAssertEqual(String(decoding: encoded, as: UTF8.self), "[\"\(saved)\"]")
        }
        var o = options()
        o.format = .custom("1080x1920@29.97")
        let back = try JSONDecoder().decode(BuildOptions.self, from: JSONEncoder().encode(o))
        XCTAssertEqual(back.format, .custom("1080x1920@29.97"))
        XCTAssertTrue(back.arguments().contains("--format=1080x1920@29.97"))
    }

    func testGraphicsFilesThatAreGoneStopTheBuildBeforeItStarts() throws {
        var o = options()
        o.graphics = true
        o.graphicsLogo = "/nowhere/logo.png"
        o.graphicsLook = ["/nowhere/venture-oc.look/1-G01-lower-third.html"]
        XCTAssertEqual(o.problems, [
            "The logo logo.png isn't there any more: clear it or choose it again.",
            "The approved graphic 1-G01-lower-third.html from an earlier video isn't there any more: click Don't match.",
        ])
        // Files that exist are fine.
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-look-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let logo = dir.appendingPathComponent("logo.png")
        let look = dir.appendingPathComponent("G01.html")
        try Data("x".utf8).write(to: logo)
        try Data("<html></html>".utf8).write(to: look)
        o.graphicsLogo = logo.path
        o.graphicsLook = [look.path]
        XCTAssertEqual(o.problems, [])
        // Without graphics (or without a model) they aren't sent, so they don't matter.
        o.graphicsLook = ["/nowhere/G01.html"]
        o.graphics = false
        XCTAssertEqual(o.problems, [])
    }

    func testPreferencesDropProjectFields() {
        var o = options()
        o.brief = "brief"
        o.targetLength = "8m"
        o.projectName = "x"
        o.style = .loose
        let prefs = o.preferencesOnly
        XCTAssertTrue(prefs.sources.isEmpty)
        XCTAssertEqual(prefs.brief, "")
        XCTAssertEqual(prefs.targetLength, "")
        XCTAssertEqual(prefs.style, .loose)
    }

    func testTimeTextMatchesCLI() {
        XCTAssertEqual(TimeText.seconds(from: "8m"), 480)
        XCTAssertEqual(TimeText.seconds(from: "90s"), 90)
        XCTAssertEqual(TimeText.seconds(from: "1:30"), 90)
        XCTAssertEqual(TimeText.seconds(from: "1h5m"), 3900)
        XCTAssertEqual(TimeText.seconds(from: "480"), 480)
        XCTAssertEqual(TimeText.seconds(from: "2.5m"), 150)
        XCTAssertEqual(TimeText.seconds(from: "8 min"), 480)
        // Spellings parse_duration takes, e.g. from `roughcut preset save --target "90 sec"`.
        XCTAssertEqual(TimeText.seconds(from: "90 sec"), 90)
        XCTAssertEqual(TimeText.seconds(from: "90sec"), 90)
        XCTAssertEqual(TimeText.seconds(from: "10 mins"), 600)
        XCTAssertEqual(TimeText.seconds(from: "1hr"), 3600)
        XCTAssertEqual(TimeText.seconds(from: "2 hours"), 7200)
        XCTAssertEqual(TimeText.seconds(from: "1h 30m"), 5400)
        XCTAssertEqual(TimeText.seconds(from: "8m30"), 510)
        XCTAssertEqual(TimeText.seconds(from: "1 hour 5 minutes"), 3900)
        XCTAssertEqual(TimeText.seconds(from: "1: 30"), 90)
        XCTAssertNil(TimeText.seconds(from: ""))
        XCTAssertNil(TimeText.seconds(from: "0"))
        XCTAssertNil(TimeText.seconds(from: "eight"))
        XCTAssertNil(TimeText.seconds(from: "8x"))
        XCTAssertNil(TimeText.seconds(from: "8 mx"))
        XCTAssertNil(TimeText.seconds(from: "about a minute"))
        XCTAssertNil(TimeText.seconds(from: "m"))
        XCTAssertNil(TimeText.seconds(from: "8m!"))
    }

    func testClock() {
        XCTAssertEqual(TimeText.clock(483), "8:03")
        XCTAssertEqual(TimeText.clock(3725), "1:02:05")
        XCTAssertEqual(TimeText.clock(15.68), "0:16")
    }

    func testEnvironmentPutsHomebrewAndVenvOnPath() {
        let cli = URL(fileURLWithPath: "/Users/you/Archer-OS/.venv/bin/roughcut")
        let env = CLILocator.environment(for: cli, extra: ["ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": ""], base: ["PATH": "/usr/bin:/bin", "HOME": "/Users/you"])
        let path = env["PATH"]!.split(separator: ":").map(String.init)
        XCTAssertEqual(path.first, "/Users/you/Archer-OS/.venv/bin")
        XCTAssertTrue(path.contains("/opt/homebrew/bin"))
        XCTAssertEqual(path.filter { $0 == "/usr/bin" }.count, 1)
        XCTAssertEqual(env["ANTHROPIC_API_KEY"], "sk-test")
        XCTAssertNil(env["OPENAI_API_KEY"])  // empty values aren't passed
        XCTAssertEqual(env["PYTHONUNBUFFERED"], "1")
    }

    func testOlderSavedPreferencesKeepWhatStillReads() throws {
        // Written by an earlier app: no openInFinalCutWhenDone, and a style this version doesn't know.
        let json = #"{"sources":[],"projectName":"","brief":"","targetLength":"","style":"extra-tight","format":"vertical","provider":"claude-code","model":"claude-sonnet-5-5","visionModel":"","baseURL":"","effort":"max","vision":false,"keepFillers":true,"stringout":false,"language":"en","graphics":true,"graphicsStyle":"navy and orange"}"#
        let o = try JSONDecoder().decode(BuildOptions.self, from: Data(json.utf8))
        XCTAssertEqual(o.style, .auto)  // unknown value falls back to the default
        XCTAssertEqual(o.format, .vertical)
        XCTAssertEqual(o.provider, .claudeCode)
        XCTAssertEqual(o.model, "claude-sonnet-5-5")
        XCTAssertEqual(o.effort, .max)
        XCTAssertFalse(o.vision)
        XCTAssertTrue(o.keepFillers)
        XCTAssertTrue(o.graphics)
        XCTAssertEqual(o.graphicsStyle, "navy and orange")
        XCTAssertFalse(o.openInFinalCutWhenDone)
    }

    func testPreferencesRoundTrip() throws {
        var o = options()
        o.openInFinalCutWhenDone = true
        o.style = .loose
        let back = try JSONDecoder().decode(BuildOptions.self, from: JSONEncoder().encode(o))
        XCTAssertEqual(back, o)
        XCTAssertFalse(o.arguments().contains { $0.contains("open") })  // app-side setting, not a CLI flag
    }

    func testPunchInsAutoOnOff() throws {
        var o = options()
        XCTAssertFalse(o.arguments().contains { $0.contains("punch-in") })  // auto: the CLI decides from the brief
        o.punchIn = .on
        XCTAssertEqual(o.arguments()[3...6], ["--style", "auto", "--punch-in", "--provider"])
        o.punchIn = .off
        XCTAssertEqual(o.arguments()[3...6], ["--style", "auto", "--no-punch-in", "--provider"])  // even if the brief says punch in
        let back = try JSONDecoder().decode(BuildOptions.self, from: JSONEncoder().encode(o))
        XCTAssertEqual(back.punchIn, .off)
        XCTAssertEqual(try JSONDecoder().decode(BuildOptions.self, from: Data("{}".utf8)).punchIn, .auto)
    }

    func testPacingSavedBeforeAutoExisted() throws {
        func decoded(_ json: String) throws -> CutStyle {
            try JSONDecoder().decode(BuildOptions.self, from: Data(json.utf8)).style
        }
        XCTAssertEqual(try decoded(#"{"style":"medium"}"#), .auto)  // the old default, not a choice
        XCTAssertEqual(try decoded(#"{"style":"tight"}"#), .tight)
        XCTAssertEqual(try decoded(#"{"style":"loose"}"#), .loose)
        XCTAssertEqual(try decoded(#"{}"#), .auto)
        var o = options()
        o.style = .medium  // chosen in this version: kept
        let back = try JSONDecoder().decode(BuildOptions.self, from: JSONEncoder().encode(o))
        XCTAssertEqual(back.style, .medium)
        XCTAssertEqual(o.arguments()[3...4], ["--style", "medium"])
        XCTAssertFalse(CutStyle.recut.contains(.auto))  // a cut's pacing is always a real one
    }

    func testSoundEffectsFlags() throws {
        var o = options()
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--sfx") || $0.hasPrefix("--preset-sfx") })  // none chosen: no accents
        o.sfx = [URL(fileURLWithPath: "/Volumes/Media/Sound Effects"), URL(fileURLWithPath: "/Users/you/Downloads/whoosh_soft_01.wav")]
        o.sfxFrom = [" Studio Whooshes ", "", "kit:Promo Kit"]
        // Remembered between sessions, like the rest of the settings (a sound library is used every time).
        let prefs = o.preferencesOnly
        XCTAssertEqual(prefs.sfx, o.sfx)
        XCTAssertEqual(prefs.sfxFrom, o.sfxFrom)
        let back = try JSONDecoder().decode(BuildOptions.self, from: JSONEncoder().encode(o))
        XCTAssertEqual(back, o)
        // Preferences saved before sound effects existed read as none.
        let older = try JSONDecoder().decode(BuildOptions.self, from: Data(#"{"graphics":true}"#.utf8))
        XCTAssertEqual(older.sfx, [])
        XCTAssertEqual(older.sfxFrom, [])
        XCTAssertEqual(older.sfxFromTyped, [])
        // Names that were remembered or a preset's go as a preset's do: Studio losing one is a warning, never a stop.
        let args = o.arguments()
        XCTAssertEqual(args.filter { $0.hasPrefix("--sfx") || $0.hasPrefix("--preset-sfx") }, [
            "--sfx=/Volumes/Media/Sound Effects", "--sfx=/Users/you/Downloads/whoosh_soft_01.wav",
            "--preset-sfx-from=Studio Whooshes", "--preset-sfx-from=kit:Promo Kit",
        ])
        // A name typed into the form in this session must exist in Studio: the build stops before it makes anything.
        o.sfxFrom.append("Hits")
        o.sfxFromTyped = [" Hits ", "A name since removed"]
        XCTAssertEqual(o.arguments().filter { $0.hasPrefix("--sfx-from") || $0.hasPrefix("--preset-sfx-from") }, [
            "--preset-sfx-from=Studio Whooshes", "--preset-sfx-from=kit:Promo Kit", "--sfx-from=Hits",
        ])
        // What was typed isn't remembered as typed: next launch, a name is one that was remembered.
        XCTAssertEqual(o.preferencesOnly.sfxFromTyped, [])
        // A folder on a share that isn't connected isn't a problem in the form: the run warns and goes on.
        o.sfx = [URL(fileURLWithPath: "/Volumes/Not Connected/SFX")]
        XCTAssertEqual(o.problems, [])
    }
}

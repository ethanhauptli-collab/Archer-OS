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

    func testPresetSoundEffectsFillTheFormOnlyWhenItHasThem() throws {
        let json = #"[{"name": "venture-oc", "brief": "Promo brief", "graphics": true, "sfx": [], "sfx_from": []}, {"name": "promos", "brief": "Mine", "sfx": ["/Volumes/Media/SFX"], "sfx_from": ["Whooshes"]}, {"name": "older"}]"#
        let presets = try XCTUnwrap(JSONLines.presets(from: json))
        XCTAssertEqual(presets[1].sfx, ["/Volumes/Media/SFX"])
        XCTAssertEqual(presets[1].sfxFrom, ["Whooshes"])
        XCTAssertNil(presets[2].sfx)  // from a CLI before sound effects
        var o = options()
        o.sfx = [URL(fileURLWithPath: "/Users/you/SFX")]
        presets[0].apply(to: &o)  // a built-in preset has none: yours stay
        XCTAssertEqual(o.sfx, [URL(fileURLWithPath: "/Users/you/SFX")])
        presets[1].apply(to: &o)
        XCTAssertEqual(o.sfx, [URL(fileURLWithPath: "/Volumes/Media/SFX")])
        XCTAssertEqual(o.sfxFrom, ["Whooshes"])
        // A preset's Studio names are a preset's to send: one Studio has lost is a warning in the paper edit, never a stop.
        XCTAssertTrue(o.arguments().contains("--preset-sfx-from=Whooshes"))
        XCTAssertFalse(o.arguments().contains("--sfx-from=Whooshes"))
        presets[2].apply(to: &o)
        XCTAssertEqual(o.sfxFrom, ["Whooshes"])
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

    func testPresetBriefIsSentApartSoYourWordsDecidePacing() throws {
        let json = #"[{"name": "walk", "brief": "Promo brief. Short and punchy.", "style": null, "graphics": false}]"#
        let presets = try XCTUnwrap(JSONLines.presets(from: json))
        var o = options()
        o.brief = "Let the chefs' stories breathe."
        presets[0].apply(to: &o)
        XCTAssertEqual(o.brief, "Promo brief. Short and punchy.\n\nLet the chefs' stories breathe.")
        // The whole brief for the planner, and the preset's part apart so pacing and punch-ins read yours first.
        let args = o.arguments()
        XCTAssertTrue(args.contains("--context=Promo brief. Short and punchy.\n\nLet the chefs' stories breathe."))
        XCTAssertTrue(args.contains("--preset-brief=Promo brief. Short and punchy."))
        // The preset's part edited: it's all yours now.
        o.brief = "Promo brief, slow and warm.\n\nLet the chefs' stories breathe."
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--preset-brief") })
        // No preset picked: never sent.
        var plain = options()
        plain.brief = "Promo brief. Short and punchy."
        XCTAssertFalse(plain.arguments().contains { $0.hasPrefix("--preset-brief") })
        // Not remembered with the preferences: the preset fills it in again after a relaunch.
        let saved = try JSONEncoder().encode(o.preferencesOnly)
        var relaunched = try JSONDecoder().decode(BuildOptions.self, from: saved)
        XCTAssertEqual(relaunched.presetBrief, "")
        relaunched.reapplyPreset(from: presets)
        XCTAssertEqual(relaunched.presetBrief, "Promo brief. Short and punchy.")
        XCTAssertTrue(relaunched.arguments().contains("--preset-brief=Promo brief. Short and punchy."))
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

    /// A cut a model planned, with its graphics line as the CLI writes it ("graphics": null when they're off).
    private func plannedCut(graphics: String) throws -> RunResult {
        try result(plannedBy: "anthropic:claude-opus-5-5", extra: ", \"graphics\": \(graphics)")
    }

    private let withGraphics = #"{"planned": 3, "placed": 3, "rendered": 3, "note": null}"#

    func testReviseSendsReferencePicturesAndTheRedesignToggle() throws {
        let cut = try plannedCut(graphics: withGraphics)
        let notes = URL(fileURLWithPath: "/tmp/notes.txt")
        let form = BuildOptions()
        let pictures = [URL(fileURLWithPath: "/Users/you/Pictures/title look.png"), URL(fileURLWithPath: "/Users/you/Pictures/-sheet.jpg")]
        let base = ["revise", "/o/Promo_roughcut", "--progress-json", "--notes-file=/tmp/notes.txt"]

        // Nothing given: the arguments are what they were before.
        XCTAssertEqual(ReviseOptions.revising(cut, notesFile: notes, targetLength: "", provider: .anthropic, form: form).arguments(), base)
        // Pictures and the toggle: one flag per picture, then the toggle.
        let both = ReviseOptions.revising(cut, notesFile: notes, targetLength: "1:30", provider: .anthropic, form: form, references: pictures, redesignGraphics: true)
        XCTAssertEqual(both.arguments(), base + [
            "--target=90", "--graphics-ref=/Users/you/Pictures/title look.png", "--graphics-ref=/Users/you/Pictures/-sheet.jpg", "--redesign-graphics",
        ])
        // The toggle alone, and pictures alone.
        XCTAssertEqual(ReviseOptions.revising(cut, notesFile: notes, targetLength: "", provider: .anthropic, form: form, redesignGraphics: true).arguments(), base + ["--redesign-graphics"])
        XCTAssertEqual(
            ReviseOptions.revising(cut, notesFile: notes, targetLength: "", provider: .anthropic, form: form, references: [pictures[0]]).arguments(),
            base + ["--graphics-ref=/Users/you/Pictures/title look.png"]
        )
        // Straight from the initializer too.
        let direct = ReviseOptions(outputFolder: URL(fileURLWithPath: "/o"), notesFile: notes, references: pictures, redesignGraphics: true)
        XCTAssertEqual(direct.arguments().suffix(3), ["--graphics-ref=/Users/you/Pictures/title look.png", "--graphics-ref=/Users/you/Pictures/-sheet.jpg", "--redesign-graphics"])
        XCTAssertEqual(ReviseOptions(outputFolder: URL(fileURLWithPath: "/o"), notesFile: notes).references, [])
        XCTAssertFalse(ReviseOptions(outputFolder: URL(fileURLWithPath: "/o"), notesFile: notes).redesignGraphics)
        // At most eight.
        let ten = (1...10).map { URL(fileURLWithPath: "/p/\($0).png") }
        XCTAssertEqual(ReviseOptions.revising(cut, notesFile: notes, targetLength: "", provider: .anthropic, form: form, references: ten).arguments().filter { $0.hasPrefix("--graphics-ref") }.count, 8)
    }

    func testReferencesAndRedesignOnlyCountForACutWithGraphics() throws {
        let notes = URL(fileURLWithPath: "/tmp/notes.txt")
        let pictures = [URL(fileURLWithPath: "/p/look.png")]
        let graphicsOff = try plannedCut(graphics: "null")  // graphics were off for this cut
        XCTAssertFalse(ReviseOptions.acceptsReferences(graphicsOff))
        XCTAssertFalse(ReviseOptions.canRedesign(graphicsOff))
        XCTAssertEqual(
            ReviseOptions.revising(graphicsOff, notesFile: notes, targetLength: "", provider: .anthropic, form: BuildOptions(), references: pictures, redesignGraphics: true).arguments(),
            ["revise", "/o/Promo_roughcut", "--progress-json", "--notes-file=/tmp/notes.txt"]
        )
        // Graphics on but none planned: pictures set the look of what comes later, but there is nothing to redesign.
        let nonePlanned = try plannedCut(graphics: #"{"planned": 0, "placed": 0, "rendered": 0, "note": null}"#)
        XCTAssertTrue(ReviseOptions.acceptsReferences(nonePlanned))
        XCTAssertFalse(ReviseOptions.canRedesign(nonePlanned))
        XCTAssertEqual(
            ReviseOptions.revising(nonePlanned, notesFile: notes, targetLength: "", provider: .anthropic, form: BuildOptions(), references: pictures, redesignGraphics: true).arguments().suffix(1),
            ["--graphics-ref=/p/look.png"]
        )
        let designed = try plannedCut(graphics: withGraphics)
        XCTAssertTrue(ReviseOptions.acceptsReferences(designed))
        XCTAssertTrue(ReviseOptions.canRedesign(designed))
        XCTAssertEqual(ReviseOptions.usableReferences(pictures, for: designed), pictures)
        XCTAssertEqual(ReviseOptions.usableReferences(pictures, for: graphicsOff), [])
        XCTAssertTrue(ReviseOptions.usableRedesign(true, for: designed))
        XCTAssertFalse(ReviseOptions.usableRedesign(false, for: designed))
        XCTAssertFalse(ReviseOptions.usableRedesign(true, for: nonePlanned))
    }

    func testReviseStartsWithNotesOrPicturesButNeverOnTheRedesignAlone() {
        let picture = [URL(fileURLWithPath: "/p/look.png")]
        XCTAssertFalse(ReviseOptions.canStart(notes: "", references: []))
        XCTAssertFalse(ReviseOptions.canStart(notes: " \n ", references: []))
        XCTAssertTrue(ReviseOptions.canStart(notes: "tighter hook", references: []))
        XCTAssertTrue(ReviseOptions.canStart(notes: "", references: picture))  // the CLI takes pictures alone
        XCTAssertTrue(ReviseOptions.canStart(notes: "bolder titles", references: picture))
        // "Redesign the graphics" changes what notes or pictures do; with neither there is nothing to ask the AI.
        XCTAssertTrue(ReviseOptions.redesignHint.contains("hand edits included"))
        XCTAssertTrue(ReviseOptions.redesignHint.contains("version history"))
    }

    func testTheRedesignSwitchComesOnWithAPictureAndGoesBackOffWithTheLastOne() {
        var redesign = RedesignSwitch()
        XCTAssertFalse(redesign.isOn)
        redesign.pictureAdded()
        XCTAssertTrue(redesign.isOn)
        redesign.picturesChanged(remaining: 2)  // one of several removed: still wanted
        XCTAssertTrue(redesign.isOn)
        redesign.picturesChanged(remaining: 0)  // the last one: it was only on for the picture
        XCTAssertFalse(redesign.isOn)

        // Switched on by hand, it stays on whatever happens to the pictures...
        redesign.set(true)
        redesign.pictureAdded()
        redesign.picturesChanged(remaining: 0)
        XCTAssertTrue(redesign.isOn)
        // A picture added after the editor switched it off asks for it again.
        redesign.set(false)
        XCTAssertFalse(redesign.isOn)
        redesign.pictureAdded()
        XCTAssertTrue(redesign.isOn)
        // Touching it after a picture put it on makes it the editor's choice.
        redesign.set(true)
        redesign.picturesChanged(remaining: 0)
        XCTAssertTrue(redesign.isOn)

        redesign.reset()
        XCTAssertEqual(redesign, RedesignSwitch())
        redesign.picturesChanged(remaining: 0)  // nothing to take back
        XCTAssertFalse(redesign.isOn)
    }

    func testReviseOfACutMadeWithoutAIKeepsThePlannerFlagsBeforeThePictures() throws {
        let quick = try result(plannedBy: "heuristic", extra: #", "graphics": {"planned": 2, "placed": 2, "rendered": 2, "note": null}"#)
        var form = BuildOptions()
        form.provider = .none
        let revise = ReviseOptions.revising(
            quick, notesFile: URL(fileURLWithPath: "/tmp/notes.txt"), targetLength: "", provider: .claudeCode, form: form,
            references: [URL(fileURLWithPath: "/p/look.png")], redesignGraphics: true
        )
        XCTAssertEqual(Array(revise.arguments().dropFirst(4)), ["--provider", "claude-code", "--effort", "high", "--graphics-ref=/p/look.png", "--redesign-graphics"])
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

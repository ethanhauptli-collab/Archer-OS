import XCTest
@testable import RoughcutKit

/// The build as a line of steps: which parts are on it, how a person moves along it, and what each station and footer says.
final class BuildLineTests: XCTestCase {
    private func form(footage: Bool = true) -> BuildOptions {
        var options = BuildOptions()
        options.provider = .claudeCode
        if footage {
            options.sources = [URL(fileURLWithPath: "/Users/you/Movies/Trip")]
        }
        return options
    }

    // MARK: The line

    func testTheBasicLineIsFootageBriefAddReady() {
        let flow = BuildFlow()
        XCTAssertEqual(flow.steps(for: form()), [.footage, .brief, .add, .ready])
        XCTAssertEqual(flow.stations(for: form()), [.footage, .brief, .add, .build])
    }

    func testAddedPartsBecomeStepsInTheOrderOfTheLineNotTheOrderAdded() {
        var options = form()
        var flow = BuildFlow()
        XCTAssertTrue(flow.add(.graphics, to: &options))
        XCTAssertTrue(flow.add(.music, to: &options))
        XCTAssertEqual(flow.steps(for: options), [.footage, .brief, .add, .part(.music), .part(.graphics), .ready])
        XCTAssertTrue(options.graphics, "adding the graphics part turns the graphics on")
    }

    func testGraphicsCannotBeAddedWithoutAModel() {
        var options = form()
        options.choose(provider: .none)
        var flow = BuildFlow()
        XCTAssertFalse(flow.add(.graphics, to: &options))
        XCTAssertFalse(options.graphics)
        XCTAssertEqual(flow.parts(for: options), [])
    }

    /// A preset's `apply` turns graphics on; the line reads the options, so the part is there without a click.
    func testAPresetThatTurnsGraphicsOnPutsThemOnTheLine() {
        var options = form()
        options.setGraphics(true)
        XCTAssertEqual(BuildFlow().parts(for: options), [.graphics])
        options.choose(provider: .none)
        XCTAssertEqual(BuildFlow().parts(for: options), [], "no model, no graphics, no step")
    }

    func testARememberedSoundSourceIsOnTheLine() {
        var options = form()
        options.sfx = [URL(fileURLWithPath: "/Users/you/Sounds")]
        XCTAssertEqual(BuildFlow().parts(for: options), [.effects])
        options.sfx = []
        options.sfxFrom = ["Whooshes"]
        XCTAssertEqual(BuildFlow().parts(for: options), [.effects])
    }

    func testAPartWaitsForItsFilesAndTakesThemWhenRemoved() {
        var options = form()
        var flow = BuildFlow()
        flow.add(.music, to: &options)
        XCTAssertEqual(flow.parts(for: options), [.music], "added with no song yet: still a step")
        options.music = [URL(fileURLWithPath: "/Users/you/Music/bed.m4a")]
        flow.remove(.music, from: &options)
        XCTAssertEqual(flow.parts(for: options), [])
        XCTAssertEqual(options.music, [], "removing the part clears its songs")
    }

    func testRemovingEffectsClearsEverySoundSource() {
        var options = form()
        options.sfx = [URL(fileURLWithPath: "/Users/you/Sounds")]
        options.sfxFrom = ["Whooshes"]
        options.sfxFromTyped = ["Whooshes"]
        var flow = BuildFlow()
        flow.remove(.effects, from: &options)
        XCTAssertEqual(options.sfx, [])
        XCTAssertEqual(options.sfxFrom, [])
        XCTAssertEqual(options.sfxFromTyped, [])
        XCTAssertFalse(options.arguments().contains { $0.hasPrefix("--sfx") || $0.hasPrefix("--preset-sfx") })
    }

    func testRemovingGraphicsTurnsThemOffAndKeepsTheLookForNextTime() {
        var options = form()
        options.graphicsStyle = "navy and orange"
        var flow = BuildFlow()
        flow.add(.graphics, to: &options)
        XCTAssertTrue(options.arguments().contains("--graphics"))
        flow.remove(.graphics, from: &options)
        XCTAssertFalse(options.graphics)
        XCTAssertFalse(options.arguments().contains("--graphics"))
        XCTAssertEqual(options.graphicsStyle, "navy and orange")
    }

    // MARK: Moving

    func testNextAndBackWalkTheWholeLine() {
        var options = form()
        var flow = BuildFlow()
        flow.add(.music, to: &options)
        var seen = [flow.step]
        while let next = flow.next(for: options) {
            flow.go(to: next)
            seen.append(next)
        }
        XCTAssertEqual(seen, [.footage, .brief, .add, .part(.music), .ready])
        XCTAssertNil(flow.next(for: options), "Ready is the end of the line")
        while let back = flow.back(for: options) {
            flow.go(to: back)
        }
        XCTAssertEqual(flow.step, .footage)
        XCTAssertNil(flow.back(for: options))
    }

    func testFineTuneIsADetourFromReady() {
        let flow = BuildFlow(step: .fineTune)
        XCTAssertEqual(flow.back(for: form()), .ready)
        XCTAssertNil(flow.next(for: form()))
        XCTAssertEqual(flow.station(of: .fineTune), .build)
        XCTAssertEqual(flow.nodes(for: form()).last?.state, .current, "the line still says Build")
    }

    func testAStepWhosePartIsGoneFallsBackToAdd() {
        var options = form()
        var flow = BuildFlow()
        flow.add(.music, to: &options)
        flow.go(to: .part(.music))
        flow.remove(.music, from: &options)
        XCTAssertEqual(flow.step, .add)

        // A preset turned the graphics off while their step was on screen.
        var onGraphics = BuildFlow(step: .part(.graphics))
        onGraphics.repair(for: options)
        XCTAssertEqual(onGraphics.step, .add)
    }

    // MARK: The strip

    func testNodesAreDoneCurrentOrUpcoming() {
        var options = form()
        options.style = .tight
        options.targetLength = "90s"
        let nodes = BuildFlow(step: .add).nodes(for: options)
        XCTAssertEqual(nodes.map(\.label), ["Footage", "Brief", "Add", "Build"])
        XCTAssertEqual(nodes.map(\.state), [.done, .done, .current, .upcoming])
        XCTAssertEqual(nodes.map(\.summary), ["1 source", "Tight · 1:30", "None yet", nil])
    }

    func testAnUpcomingAddStationSaysItIsOptional() {
        let nodes = BuildFlow(step: .brief).nodes(for: form())
        XCTAssertEqual(nodes.map(\.state), [.done, .current, .upcoming, .upcoming])
        XCTAssertEqual(nodes[2].summary, "Optional")
        XCTAssertNil(nodes[3].summary)
    }

    func testTheFootageStationSaysWhatWasCounted() {
        let counted = FootageCount(videos: 14, recordings: 1, stills: 2)
        XCTAssertEqual(BuildFlow(step: .brief).nodes(for: form(), footage: counted)[0].summary, "17 files")
        XCTAssertEqual(BuildFlow(step: .footage).nodes(for: form(footage: false))[0].summary, "Start here")
        XCTAssertEqual(BuildFlow(step: .brief).nodes(for: form(), footage: FootageCount())[0].summary, "1 source", "nothing counted yet falls back to the sources")
    }

    func testEachPartSaysWhatItHolds() {
        var options = form()
        options.presetName = "venture-oc"
        options.setGraphics(true)
        options.music = [URL(fileURLWithPath: "/Volumes/Music/upbeat_bed.m4a")]
        options.sfx = [URL(fileURLWithPath: "/Users/you/Sounds/Whooshes")]
        let nodes = BuildFlow(step: .ready).nodes(for: options)
        XCTAssertEqual(nodes.map(\.label), ["Footage", "Brief", "Add", "Music", "Effects", "Graphics", "Build"])
        XCTAssertEqual(nodes[2].summary, "3 added")
        XCTAssertEqual(nodes[3].summary, "upbeat_bed")
        XCTAssertEqual(nodes[4].summary, "Whooshes")
        XCTAssertEqual(nodes[5].summary, "Venture OC look")
        XCTAssertEqual(nodes[6].summary, "Ready")

        options.music.append(URL(fileURLWithPath: "/Volumes/Music/second.m4a"))
        options.sfxFrom = ["Hits"]
        options.presetName = ""
        options.graphicsStyle = "warm and quiet"
        let more = BuildFlow(step: .ready).nodes(for: options)
        XCTAssertEqual(more[3].summary, "2 songs")
        XCTAssertEqual(more[4].summary, "2 sources")
        XCTAssertEqual(more[5].summary, "Your look")
    }

    func testPartsThatAreWaitingForFilesSayWhat() {
        var options = form()
        var flow = BuildFlow(step: .ready)
        flow.add(.music, to: &options)
        flow.add(.effects, to: &options)
        let nodes = flow.nodes(for: options)
        XCTAssertEqual(nodes[3].summary, "No song yet")
        XCTAssertEqual(nodes[4].summary, "Nothing chosen")
        XCTAssertEqual(nodes[2].summary, "None yet", "the Add station counts parts that hold something")
    }

    func testTheBuildStationIsNotReadyWhileThereIsAProblem() {
        var options = form()
        options.targetLength = "soon"
        XCTAssertNil(BuildFlow(step: .ready).nodes(for: options).last?.summary)
    }

    // MARK: The footer

    func testFootageNeedsSomethingToCut() {
        let flow = BuildFlow()
        XCTAssertFalse(flow.canAdvance(for: form(footage: false)))
        XCTAssertEqual(flow.footerNote(for: form(footage: false), footage: nil), "Add footage to continue")
        XCTAssertTrue(flow.canAdvance(for: form()))
        XCTAssertEqual(flow.footerNote(for: form(), footage: nil), "1 source")
        XCTAssertEqual(flow.nextTitle, "Next")
    }

    func testReadyBuildsOnlyWhenNothingIsInTheWay() {
        let ready = BuildFlow(step: .ready)
        XCTAssertEqual(ready.nextTitle, "Build rough cut")
        XCTAssertTrue(ready.canAdvance(for: form()))
        XCTAssertEqual(ready.footerNote(for: form(), footage: nil), "Uses your Claude plan")

        var bad = form()
        bad.targetLength = "soon"
        XCTAssertFalse(ready.canAdvance(for: bad))
        XCTAssertEqual(ready.footerNote(for: bad, footage: nil), "One thing to fix before building")
        bad.music = [URL(fileURLWithPath: "/Users/you/Music/gone.m4a")]
        XCTAssertEqual(ready.footerNote(for: bad, footage: nil), "2 things to fix before building")
    }

    // MARK: The Ready step

    func testReadySaysWhatEachStationHolds() {
        var options = form()
        options.presetName = "venture-oc"
        options.targetLength = "90s"
        options.style = .tight
        options.setGraphics(true)
        options.music = [URL(fileURLWithPath: "/Volumes/Music/upbeat_bed.m4a")]
        options.sfx = [URL(fileURLWithPath: "/Volumes/Sounds/Whooshes")]
        options.sfxFrom = ["Hits"]
        func detail(_ station: LineStation, footage: FootageCount? = nil) -> String {
            BuildFlow.readyDetail(of: station, options: options, footage: footage)
        }
        XCTAssertEqual(detail(.footage), "1 source")
        XCTAssertEqual(detail(.footage, footage: FootageCount(videos: 14, recordings: 1, stills: 2)), "14 videos, 2 stills and 1 recording")
        XCTAssertEqual(detail(.brief), "Venture OC. About 1:30, tight pace.")
        XCTAssertEqual(detail(.part(.music)), "upbeat_bed.m4a. Ends on the last frame.")
        XCTAssertEqual(detail(.part(.effects)), "Whooshes, Studio: Hits")
        XCTAssertEqual(detail(.part(.graphics)), "Venture OC look, designed by Claude.")
        options.presetName = ""
        XCTAssertEqual(detail(.part(.graphics)), "Designed by Claude in its own look.")
        options.graphicsStyle = "warm"
        XCTAssertEqual(detail(.part(.graphics)), "Your look, designed by Claude.")
        options.sources = []
        XCTAssertEqual(detail(.footage), "No footage yet")
    }

    func testTheBriefInTheReadyListIsItsFirstSentenceOrAShortLead() {
        var options = form()
        options.brief = "A promo for the pier. Keep the numbers."
        XCTAssertEqual(BuildFlow.readyDetail(of: .brief, options: options, footage: nil), "A promo for the pier.")
        options.brief = String(repeating: "word ", count: 40)
        let long = BuildFlow.readyDetail(of: .brief, options: options, footage: nil)
        XCTAssertTrue(long.hasSuffix("…"))
        XCTAssertLessThanOrEqual(long.count, 82)
        options.brief = ""
        XCTAssertEqual(BuildFlow.readyDetail(of: .brief, options: options, footage: nil), "No brief")
        options.targetLength = "45s"
        XCTAssertEqual(BuildFlow.readyDetail(of: .brief, options: options, footage: nil), "About 0:45.")
        options.targetLength = ""
        options.style = .tight
        XCTAssertEqual(BuildFlow.readyDetail(of: .brief, options: options, footage: nil), "Tight pace.", "the shape starts a sentence")
        options.presetName = "venture-oc"
        XCTAssertEqual(BuildFlow.readyDetail(of: .brief, options: options, footage: nil), "Venture OC. Tight pace.")
    }

    func testFineTuneSummarySaysWhatTheDefaultsAre() {
        var options = form()
        XCTAssertEqual(
            BuildFlow.fineTuneSummary(for: options),
            "Plans with Claude Code. YouTube loudness. Matches your footage. Also builds a dialogue-only version."
        )
        options.dialogueLevel = .louder
        options.format = .vertical
        options.stringout = false
        options.keepFillers = true
        options.fades = false
        options.punchIn = .on
        options.provider = .none
        XCTAssertEqual(
            BuildFlow.fineTuneSummary(for: options),
            "No AI planning. Loud for phones. Format Vertical 9:16. Keeps um and uh. No audio fades. Punch-ins on."
        )
    }

    func testEveryPlannerSaysWhatItCosts() {
        for provider in ModelProvider.allCases {
            XCTAssertFalse(provider.usageNote.isEmpty, "\(provider)")
        }
        XCTAssertEqual(ModelProvider.claudeCode.usageNote, "Uses your Claude plan")
        XCTAssertEqual(ModelProvider.none.usageNote, "No AI: only the dead air is cut")
    }

    func testPartsAreNamedForThePeopleWhoAddThem() {
        XCTAssertEqual(LinePart.allCases.map(\.title), ["Music", "Sound effects", "Motion graphics"])
        XCTAssertEqual(LinePart.allCases.map(\.label), ["Music", "Effects", "Graphics"])
        XCTAssertEqual(Preset.displayName(for: "venture-oc"), "Venture OC")
        XCTAssertEqual(Preset.displayName(for: "save-the-kid"), "Save The Kid")
    }
}

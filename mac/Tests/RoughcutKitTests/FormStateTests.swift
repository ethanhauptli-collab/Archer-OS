import XCTest
@testable import RoughcutKit

/// What the build form's controls can leave behind. The view calls exactly these (`choose(provider:)`, `setGraphics`,
/// `TimeText`), so a test that drives them in the order a person clicks is a test of the form.
final class FormStateTests: XCTestCase {
    private func form() -> BuildOptions {
        var o = BuildOptions()
        o.sources = [URL(fileURLWithPath: "/Users/you/Movies/Trip")]
        return o
    }

    // MARK: Motion graphics and provider None

    /// Turn graphics on, then pick None, then come back: no step leaves a state the person can't get out of.
    func testPickingNoneAfterTurningGraphicsOnLeavesNothingStuck() {
        let picture = URL(fileURLWithPath: "/Users/you/Pictures/look.png")
        var o = form()
        o.setGraphics(true)
        o.graphicsStyle = "navy and orange"
        o.graphicsReferences = [picture]
        XCTAssertTrue(o.graphicsActive)
        XCTAssertTrue(o.graphicsSwitchEnabled)
        XCTAssertTrue(o.arguments().contains("--graphics"))
        XCTAssertEqual(o.problems, [])

        o.choose(provider: .none)
        XCTAssertFalse(o.graphics, "the switch goes off with the provider")
        XCTAssertFalse(o.graphicsActive)
        XCTAssertFalse(o.graphicsSwitchEnabled, "nothing to turn on without a model")
        XCTAssertEqual(o.problems, [], "the build isn't blocked by a switch that can't be reached")
        XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--graphics") }, "no graphics flag, style, logo or picture is sent")
        XCTAssertTrue(o.arguments().contains("none"))
        // What was set up stays remembered, hidden, for when graphics are wanted again.
        XCTAssertEqual(o.graphicsStyle, "navy and orange")
        XCTAssertEqual(o.graphicsReferences, [picture])

        o.setGraphics(true)  // a click on a switch that is disabled can't turn it on either
        XCTAssertFalse(o.graphics)

        o.choose(provider: .claudeCode)
        XCTAssertTrue(o.graphicsSwitchEnabled)
        XCTAssertFalse(o.graphics, "graphics stay off until asked for again")
        o.setGraphics(true)
        let args = o.arguments()
        XCTAssertTrue(args.contains("--graphics"))
        XCTAssertTrue(args.contains("--graphics-style=navy and orange"))
        XCTAssertTrue(args.contains("--graphics-ref=/Users/you/Pictures/look.png"))
    }

    func testChoosingAProviderStartsFromItsOwnDefaults() {
        var o = form()
        o.model = "claude-sonnet-5-5"
        o.visionModel = "claude-sonnet-5-5"
        o.choose(provider: .openai)
        XCTAssertEqual(o.provider, .openai)
        XCTAssertEqual(o.model, "", "model names don't carry over between providers")
        XCTAssertEqual(o.visionModel, "")
        XCTAssertTrue(o.vision, "OpenAI models can look at frames")
        o.choose(provider: .ollama)
        XCTAssertFalse(o.vision, "local models are usually text-only")
        o.model = "llama"
        o.choose(provider: .ollama)  // the same provider again: nothing is reset
        XCTAssertEqual(o.model, "llama")
        o.choose(provider: .anthropic)
        XCTAssertTrue(o.vision)
    }

    /// A form that is somehow stuck (graphics on, no provider) still has a way out in the form itself.
    func testGraphicsLeftOnWithoutAProviderCanBeTurnedOff() {
        var o = form()
        o.graphics = true  // set directly, past the form's own rules
        o.provider = .none
        XCTAssertTrue(o.graphicsSwitchEnabled, "the switch stays usable to turn it off")
        XCTAssertEqual(o.problems, ["Motion graphics are designed by the AI: choose a provider other than None, or turn motion graphics off."])
        XCTAssertFalse(o.arguments().contains("--graphics"))
        o.setGraphics(false)
        XCTAssertEqual(o.problems, [])
        XCTAssertFalse(o.graphicsSwitchEnabled)
    }

    func testAPresetWithGraphicsDoesNotTurnThemOnWithoutAProvider() throws {
        let presets = try XCTUnwrap(JSONLines.presets(from: #"[{"name":"promo","graphics":true,"graphics_style":"navy and orange","target":"90s"}]"#))
        var o = form()
        o.choose(provider: .none)
        presets[0].apply(to: &o)
        XCTAssertFalse(o.graphics)
        XCTAssertEqual(o.graphicsStyle, "navy and orange", "the look is filled in, ready for a provider")
        XCTAssertEqual(o.targetLength, "90s")
        XCTAssertEqual(o.problems, [])
        o.choose(provider: .anthropic)
        presets[0].apply(to: &o)
        XCTAssertTrue(o.graphics)
        // Saved as it was, then the app opens with None chosen: still no stuck state.
        o.choose(provider: .none)
        o.reapplyPreset(from: presets)
        XCTAssertFalse(o.graphics)
    }

    /// The old form let graphics stay on after None was picked, and saved it that way.
    func testSavedPreferencesWithGraphicsAndNoProviderOpenSettled() throws {
        let stuck = try JSONDecoder().decode(BuildOptions.self, from: Data(#"{"provider":"none","graphics":true,"graphicsStyle":"navy"}"#.utf8))
        XCTAssertEqual(stuck.provider, .none)
        XCTAssertFalse(stuck.graphics)
        XCTAssertEqual(stuck.graphicsStyle, "navy")
        let fine = try JSONDecoder().decode(BuildOptions.self, from: Data(#"{"provider":"claude-code","graphics":true}"#.utf8))
        XCTAssertTrue(fine.graphics)
        XCTAssertTrue(ModelProvider.allCases.filter { !$0.usesAI } == [.none])
    }

    // MARK: Target length

    /// Fixtures/target-lengths.json is read by tests/test_timecode.py too, so the app and the CLI take the same lengths.
    private func fixture() throws -> (accept: [(String, Double)], reject: [String]) {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "target-lengths", withExtension: "json", subdirectory: "Fixtures"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        var accept: [(String, Double)] = []
        for pair in try XCTUnwrap(root["accept"] as? [[Any]]) {
            let text = try XCTUnwrap(pair[0] as? String)
            let seconds = try XCTUnwrap(pair[1] as? NSNumber).doubleValue
            accept.append((text, seconds))
        }
        return (accept, try XCTUnwrap(root["reject"] as? [String]))
    }

    private var overflowing: [String] {
        let huge = String(repeating: "9", count: 400)  // a run of digits this long is infinity as a Double
        return [huge, huge + ":0", "1:" + huge, huge + "s", huge + "m", huge + "h", "1" + String(repeating: "0", count: 400)]
    }

    func testTargetLengthReadsTheSameAsTheCLI() throws {
        let lengths = try fixture()
        XCTAssertGreaterThan(lengths.accept.count, 20)
        for (text, seconds) in lengths.accept {
            XCTAssertEqual(TimeText.seconds(from: text), seconds, "\(text)")
            XCTAssertEqual(TimeText.wholeSeconds(from: text), Int(seconds.rounded()), "\(text)")
        }
        XCTAssertEqual(TimeText.minSeconds, 1)
        XCTAssertEqual(TimeText.maxSeconds, 24 * 3600)
    }

    func testTargetLengthRefusesWhatIsNotALength() throws {
        let lengths = try fixture()
        XCTAssertGreaterThan(lengths.reject.count, 50)
        for text in lengths.reject + overflowing {
            XCTAssertNil(TimeText.seconds(from: text), "\(text.prefix(20))")
            XCTAssertNil(TimeText.wholeSeconds(from: text), "\(text.prefix(20))")
        }
    }

    /// The form formats a target while it is being typed, before Build is pressed: none of it may trap, and a bad one says what a length looks like.
    func testTypingAnUnsafeTargetShowsTheMessageAndNeverCrashes() throws {
        let lengths = try fixture()
        var o = form()
        for text in lengths.reject + overflowing where !text.trimmed.isEmpty {
            o.targetLength = text
            XCTAssertEqual(o.problems, ["Target length should look like 8m, 90s or 8:30."], "\(text.prefix(20))")
            XCTAssertFalse(o.arguments().contains { $0.hasPrefix("--target") }, "\(text.prefix(20))")
            XCTAssertNil(TimeText.seconds(from: o.targetLength))  // so the form's "Aiming for about" line doesn't format it
            XCTAssertEqual(ReviseOptions.lengthProblem(text), "New length should look like 8m, 90s or 8:30.", "\(text.prefix(20))")
        }
        o.targetLength = "8:30"
        XCTAssertEqual(o.problems, [])
        XCTAssertTrue(o.arguments().contains("--target=510"))
        o.targetLength = "24h"
        XCTAssertTrue(o.arguments().contains("--target=86400"))
        o.targetLength = "1.4s"
        XCTAssertTrue(o.arguments().contains("--target=1"))
        o.targetLength = "90.5s"
        XCTAssertTrue(o.arguments().contains("--target=91"), "whole seconds, rounded")
    }

    func testReviseSendsOnlyASafeLength() {
        let folder = URL(fileURLWithPath: "/Users/you/Movies/Trip_roughcut")
        let notes = URL(fileURLWithPath: "/tmp/notes.txt")
        var revise = ReviseOptions(outputFolder: folder, notesFile: notes, targetLength: "1e999:0")
        XCTAssertFalse(revise.arguments().contains { $0.hasPrefix("--target") })
        revise.targetLength = "1:30"
        XCTAssertTrue(revise.arguments().contains("--target=90"))
    }

    func testClockNeverTrapsAndKeepsItsFormat() {
        for value in [Double.infinity, -Double.infinity, Double.nan, 1e300, -1e300, 1e13, Double.greatestFiniteMagnitude] {
            XCTAssertEqual(TimeText.clock(value), "--:--", "\(value)")
        }
        XCTAssertEqual(TimeText.clock(0), "0:00")
        XCTAssertEqual(TimeText.clock(483), "8:03")
        XCTAssertEqual(TimeText.clock(3725), "1:02:05")
        XCTAssertEqual(TimeText.clock(86400), "24:00:00")
        XCTAssertEqual(TimeText.clock(15.68), "0:16")
        XCTAssertEqual(TimeText.clock(-5), "-0:05")
        XCTAssertEqual(TimeText.clock(-0.2), "0:00", "no sign on zero")
        XCTAssertEqual(TimeText.clock(1e12), "277777777:46:40")
    }
}

import XCTest
@testable import RoughcutKit

/// How a finished cut says who planned it.
final class RunResultTextTests: XCTestCase {
    private func result(plannedBy: String) throws -> RunResult {
        let line = try XCTUnwrap(try FixtureFiles.lines("progress-sample").last(where: { $0.contains(#""event": "done""#) }))
        var result = try XCTUnwrap(JSONLines.event(from: line)?.result)
        result.plannedBy = plannedBy
        return result
    }

    func testClaudeThroughThePlanOrTheKey() throws {
        XCTAssertEqual(try result(plannedBy: "claude-code:claude-opus-5-5").plannerSentence, "Claude Opus 5.5, through your Claude plan")
        XCTAssertEqual(try result(plannedBy: "anthropic:claude-sonnet-5-5").plannerSentence, "Claude Sonnet 5.5, through your API key")
    }

    func testOtherModelsKeepTheirNames() throws {
        XCTAssertEqual(try result(plannedBy: "openai:gpt-5").plannerSentence, "gpt-5 (OpenAI)")
        XCTAssertEqual(try result(plannedBy: "ollama:llama3").plannerSentence, "llama3 (on this Mac)")
        XCTAssertEqual(try result(plannedBy: "fake:fake-model").plannerSentence, "fake-model (fake)")
    }

    func testACutMadeWithoutAISaysWhatTheEngineSaid() throws {
        XCTAssertEqual(try result(plannedBy: "stringout (no AI)").plannerSentence, "stringout (no AI)")
        XCTAssertEqual(try result(plannedBy: "").plannerSentence, "no AI")
    }

    func testTheLineUnderTheHeadline() throws {
        var planned = try result(plannedBy: "claude-code:claude-opus-5-5")
        planned.version = 2
        planned.partial = nil
        planned.fellBack = false
        XCTAssertEqual(planned.subtitle, "Version 2. Planned by Claude Opus 5.5, through your Claude plan.")
        planned.partial = true
        XCTAssertEqual(planned.subtitle, "Version 2. No graphics yet. Planned by Claude Opus 5.5, through your Claude plan.")

        var plain = try result(plannedBy: "heuristic")
        plain.version = 1
        plain.partial = nil
        plain.fellBack = false
        XCTAssertEqual(plain.subtitle, "Version 1. Made without AI: the dead air is cut and nothing is reordered.")
        XCTAssertEqual(plain.plannerSentence, "no AI")

        plain.fellBack = true
        XCTAssertTrue(plain.subtitle.hasPrefix("The AI step failed"))
    }

    func testTheHeadlineSaysWhenGraphicsAreMissing() throws {
        var cut = try result(plannedBy: "claude-code:claude-opus-5-5")
        cut.fellBack = false
        cut.partial = nil
        cut.graphics = GraphicsSummary(planned: 2, placed: 2, rendered: 2, note: nil, pending: nil)
        XCTAssertEqual(cut.headline, "Your rough cut is ready")

        cut.graphics = GraphicsSummary(planned: 2, placed: 2, rendered: 0, note: "2 not designed: the usage limit was reached", pending: nil)
        XCTAssertEqual(cut.headline, "Written, but the graphics aren't made yet")
        cut.graphics = GraphicsSummary(planned: 2, placed: 2, rendered: 1, note: nil, pending: nil)
        XCTAssertEqual(cut.headline, "Written, but some graphics aren't made yet")

        cut.graphics = GraphicsSummary(planned: 3, placed: 2, rendered: 2, note: nil, pending: nil)
        XCTAssertEqual(cut.headline, "Your rough cut is ready", "a graphic with no room in the cut is not a missing one")
        cut.graphics = GraphicsSummary(planned: 2, placed: 2, rendered: 2, note: nil, pending: 2)
        XCTAssertEqual(cut.headline, "Your rough cut is ready", "graphics in their earlier look are made")

        cut.partial = true
        XCTAssertEqual(cut.headline, "Written, but the graphics aren't made yet")
        cut.fellBack = true
        XCTAssertEqual(cut.headline, "Made without the AI")
    }

    func testModelNames() {
        XCTAssertEqual(RunResult.modelName("claude-opus-5-5"), "Claude Opus 5.5")
        XCTAssertEqual(RunResult.modelName("claude-haiku-5-5"), "Claude Haiku 5.5")
        XCTAssertEqual(RunResult.modelName("claude-opus-5"), "claude-opus-5", "not a version it knows how to say")
        XCTAssertEqual(RunResult.modelName("gpt-5"), "gpt-5")
    }
}

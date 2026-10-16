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

    func testModelNames() {
        XCTAssertEqual(RunResult.modelName("claude-opus-5-5"), "Claude Opus 5.5")
        XCTAssertEqual(RunResult.modelName("claude-haiku-5-5"), "Claude Haiku 5.5")
        XCTAssertEqual(RunResult.modelName("claude-opus-5"), "claude-opus-5", "not a version it knows how to say")
        XCTAssertEqual(RunResult.modelName("gpt-5"), "gpt-5")
    }
}

import XCTest
@testable import RoughcutKit

final class RunStateTests: XCTestCase {
    private func cut(_ version: Int) throws -> RunResult {
        let line = "{\"event\": \"done\", \"result\": {\"title\": \"Promo\", \"out_dir\": \"/o/Promo_roughcut\", \"fcpxml\": \"/o/Promo_roughcut/Promo v\(version).fcpxml\", \"report\": \"/o/r.md\", \"rough_seconds\": 90, \"stringout_seconds\": null, \"edits\": 12, \"broll\": 3, \"sections\": [], \"planned_by\": \"claude-code:claude-opus-5-5\", \"fell_back\": false, \"warnings\": [], \"version\": \(version)}}"
        return try XCTUnwrap(JSONLines.event(from: line)?.result)
    }

    func testAStoppedRevisionGoesBackToItsCut() throws {
        // It used to drop to the build form: the typed notes were gone and the advice said "Build again".
        let v1 = try cut(1)
        let limit = "Claude Code: your plan's usage limit was reached. Nothing changed: v1 is still the latest cut. Run the same revise again after the reset."
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: limit), .returned(v1, problem: limit))
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: true, error: "roughcut stopped (exit code 15)."), .returned(v1, problem: "Revision cancelled."))
        XCTAssertEqual(RunEnding.after(.recut, from: v1, result: nil, cancelled: false, error: "No space left"), .returned(v1, problem: "No space left"))
        XCTAssertEqual(RunEnding.after(.recut, from: v1, result: nil, cancelled: true, error: ""), .returned(v1, problem: "Re-cut cancelled."))
        // A finished one shows the new cut.
        let v2 = try cut(2)
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: v2, cancelled: false, error: ""), .finished(v2))
    }

    func testABuildThatStopsGoesToTheForm() throws {
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: false, error: "No media files found."), .failed("No media files found."))
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: true, error: ""), .cancelled)
        let v1 = try cut(1)
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: v1, cancelled: false, error: ""), .finished(v1))
        // Finishing a stopped build without AI has no cut to go back to either.
        XCTAssertEqual(RunEnding.after(.recut, from: nil, result: nil, cancelled: false, error: "x"), .failed("x"))
    }

    func testUsageLimitAdviceFitsTheRun() {
        let revise = RunKind.revise.limitAdvice ?? ""
        XCTAssertTrue(revise.contains("Revise again"))
        XCTAssertFalse(revise.contains("Build"))  // a new build re-plans from scratch as a new version
        XCTAssertTrue(RunKind.build.limitAdvice?.contains("Build again") ?? false)
        XCTAssertNil(RunKind.recut.limitAdvice)  // a re-cut makes no model call
        XCTAssertEqual(RunKind.revise.readyTitle, "Revision ready")
        XCTAssertEqual(RunKind.recut.noun, "Re-cut")
    }
}

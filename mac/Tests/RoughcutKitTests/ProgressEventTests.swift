import XCTest
@testable import RoughcutKit

final class ProgressEventTests: XCTestCase {
    /// Fixtures/progress-sample.jsonl is real `roughcut build --progress-json`
    /// output. tests/test_progress.py checks it still matches the CLI.
    func testDecodesRealCLIStream() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "progress-sample", withExtension: "jsonl", subdirectory: "Fixtures"))
        let lines = try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n")
            .map(String.init)
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")

        var stages: [Stage] = []
        for stage in events.compactMap(\.stageValue) where stages.last != stage {
            stages.append(stage)
        }
        XCTAssertEqual(stages, Stage.allCases)

        let transcribe = try XCTUnwrap(events.last { $0.stageValue == .transcribe })
        XCTAssertEqual(transcribe.current, transcribe.total)

        let done = try XCTUnwrap(events.last)
        XCTAssertEqual(done.event, "done")
        let result = try XCTUnwrap(done.result)
        XCTAssertEqual(result.title, "Mountain Day")
        XCTAssertTrue(result.fcpxml.hasSuffix(".fcpxml"))
        XCTAssertEqual(result.fcpxmlURL.pathExtension, "fcpxml")
        XCTAssertEqual(result.sections.first, "Cold open")
        XCTAssertGreaterThan(result.roughSeconds, 0)
        XCTAssertNotNil(result.stringoutSeconds)
        XCTAssertFalse(result.fellBack)
        XCTAssertFalse(result.warnings.isEmpty)
    }

    func testUnknownStageStillDecodes() throws {
        let event = try XCTUnwrap(JSONLines.event(from: #"{"event":"stage","stage":"future-stage","message":"x"}"#))
        XCTAssertNil(event.stageValue)
        XCTAssertEqual(event.message, "x")
    }

    func testErrorEvent() throws {
        let event = try XCTUnwrap(JSONLines.event(from: #"{"event":"error","message":"No media files found."}"#))
        XCTAssertEqual(event.event, "error")
        XCTAssertNil(event.result)
    }

    func testNonEventLinesAreNotEvents() {
        XCTAssertNil(JSONLines.event(from: "[roughcut] found 5 media files"))
        XCTAssertNil(JSONLines.event(from: #"{"version":"0.1.0"}"#))  // doctor output has no "event"
    }

    func testDoctorReport() throws {
        let line = #"{"version": "0.1.0", "python": "3.12.4", "executable": "/x/.venv/bin/python", "ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": null, "transcriber": "mlx", "anthropic_key": true, "openai_key": false, "openai_installed": false}"#
        let report = try XCTUnwrap(JSONLines.doctor(from: line))
        XCTAssertEqual(report.transcriber, "mlx")
        XCTAssertTrue(report.anthropicKey)
        XCTAssertFalse(report.hasFFmpeg)  // ffprobe missing
    }
}

import XCTest
@testable import RoughcutKit

final class VersionStampTests: XCTestCase {
    private func engine(commit: String?, source: String?) throws -> DoctorReport {
        func field(_ name: String, _ value: String?) -> String {
            value.map { #","\#(name)":"\#($0)""# } ?? ""
        }
        let json = #"{"version":"1.0.0","python":"3.12.4","executable":"/x","anthropic_key":false,"openai_key":false,"openai_installed":false"#
            + field("commit", commit) + field("app_source_commit", source) + "}"
        return try XCTUnwrap(JSONLines.doctor(from: json))
    }

    /// Fixtures/doctor-sample.json is `roughcut doctor --json --offline`; tests/test_progress.py checks its keys are the CLI's.
    func testDoctorFixtureDecodesWithTheCommit() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "doctor-sample", withExtension: "json", subdirectory: "Fixtures"))
        let line = try String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        let report = try XCTUnwrap(JSONLines.doctor(from: line))
        XCTAssertEqual(report.version, "1.0.0")
        XCTAssertEqual(report.commit, "3f9c2ab")
        XCTAssertEqual(report.appSourceCommit, "a41d7e0")
        XCTAssertEqual(report.transcriber, "mlx")
        XCTAssertFalse(report.ventureStudio?.isInstalled ?? true)
    }

    func testAnOlderCLIHasNoCommit() throws {
        let report = try engine(commit: nil, source: nil)
        XCTAssertNil(report.commit)
        XCTAssertNil(report.appSourceCommit)
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: report)
        XCTAssertEqual(stamp.line, "App 3f9c2ab · Engine unknown")
        XCTAssertNil(stamp.warning, "nothing to compare with")
    }

    func testTheLineNamesBothCommits() throws {
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "3f9c2ab-dirty", source: "a41d7e0"))
        XCTAssertEqual(stamp.line, "App 3f9c2ab · Engine 3f9c2ab-dirty")
        XCTAssertNil(stamp.warning)
        XCTAssertEqual(VersionStamp(app: AppStamp(), engine: nil).line, "App unknown · Engine not checked yet")
    }

    func testAnEngineUpdateThatLeavesTheAppAloneIsNotAWarning() throws {
        // roughcut update pulled engine-only commits: HEAD moved, the app's source didn't.
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "9d8e7f6", source: "a41d7e0"))
        XCTAssertEqual(stamp.line, "App 3f9c2ab · Engine 9d8e7f6")
        XCTAssertNil(stamp.warning)
    }

    func testAnAppBehindTheEnginesCopyOfItAsksForARebuild() throws {
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "9d8e7f6", source: "c0ffee1"))
        let warning = try XCTUnwrap(stamp.warning)
        XCTAssertTrue(warning.contains("out of date"))
        XCTAssertTrue(warning.contains("a41d7e0") && warning.contains("c0ffee1"))
        XCTAssertTrue(warning.contains("mac/scripts/build-app.sh --install"))
    }

    func testWithoutTheEnginesSourceCommitTheWholeCommitIsCompared() throws {
        let behind = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "9d8e7f6", source: nil))
        XCTAssertNotNil(behind.warning)
        let same = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "3f9c2ab", source: nil))
        XCTAssertNil(same.warning)
    }

    func testAbbreviationsOfTheSameCommitAgreeAndDirtyIsNotAnotherCommit() throws {
        // git lengthens an abbreviation as a repository grows; -dirty marks uncommitted changes, it isn't part of the commit.
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab", sourceCommit: "a41d7e0"), engine: try engine(commit: "3f9c2abc1-dirty", source: "a41d7e0f"))
        XCTAssertNil(stamp.warning)
        XCTAssertEqual(VersionStamp.same("3f9c2ab-dirty", "3f9c2ab"), true)
        XCTAssertEqual(VersionStamp.same("3f9c2ab", "9d8e7f6"), false)
        XCTAssertNil(VersionStamp.same(nil, "9d8e7f6"))
        XCTAssertNil(VersionStamp.same("-dirty", "9d8e7f6"))
    }

    func testAnAppBuiltFromUncommittedChangesSaysSo() throws {
        let stamp = VersionStamp(app: AppStamp(commit: "3f9c2ab-dirty", sourceCommit: "a41d7e0"), engine: try engine(commit: "3f9c2ab", source: "a41d7e0"))
        XCTAssertTrue(try XCTUnwrap(stamp.warning).contains("uncommitted changes"))
        XCTAssertTrue(AppStamp(commit: "3f9c2ab-dirty").isDirty)
        XCTAssertFalse(AppStamp(commit: "3f9c2ab").isDirty)
        // Behind wins over dirty: one thing to do first.
        let both = VersionStamp(app: AppStamp(commit: "3f9c2ab-dirty", sourceCommit: "a41d7e0"), engine: try engine(commit: "9d8e7f6", source: "c0ffee1"))
        XCTAssertTrue(try XCTUnwrap(both.warning).contains("out of date"))
    }

    func testAnAppNotBuiltByTheScriptHasNothingToCompare() throws {
        // swift run / Xcode: no stamp in Info.plist.
        XCTAssertEqual(AppStamp(infoDictionary: nil), AppStamp())
        XCTAssertEqual(AppStamp(infoDictionary: ["CFBundleName": "Roughcut"]), AppStamp())
        let stamp = VersionStamp(app: AppStamp(infoDictionary: [:]), engine: try engine(commit: "9d8e7f6", source: "c0ffee1"))
        XCTAssertEqual(stamp.line, "App unknown · Engine 9d8e7f6")
        XCTAssertNil(stamp.warning)
    }

    func testInfoPlistValuesAreRead() {
        let stamp = AppStamp(infoDictionary: [AppStamp.commitKey: " 3f9c2ab-dirty ", AppStamp.sourceCommitKey: "a41d7e0"])
        XCTAssertEqual(stamp.commit, "3f9c2ab-dirty")
        XCTAssertEqual(stamp.sourceCommit, "a41d7e0")
        // build-app.sh writes "unknown" outside a git checkout; other types and empty text are nothing.
        XCTAssertEqual(AppStamp(infoDictionary: [AppStamp.commitKey: "unknown", AppStamp.sourceCommitKey: ""]), AppStamp())
        XCTAssertEqual(AppStamp(infoDictionary: [AppStamp.commitKey: 7]), AppStamp())
        XCTAssertEqual(AppStamp.commitKey, "RoughcutAppCommit")  // scripts/build-app.sh writes these two keys
        XCTAssertEqual(AppStamp.sourceCommitKey, "RoughcutAppSourceCommit")
    }
}

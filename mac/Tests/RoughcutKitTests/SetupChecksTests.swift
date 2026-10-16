import XCTest
@testable import RoughcutKit

/// The Get ready page's rows, read from `roughcut doctor --json` (Fixtures/doctor-sample.json is the CLI's own output).
final class SetupChecksTests: XCTestCase {
    private func sample() throws -> DoctorReport {
        let text = try String(contentsOf: FixtureFiles.url("doctor-sample", "json"), encoding: .utf8)
        return try XCTUnwrap(JSONLines.doctor(from: text.trimmingCharacters(in: .whitespacesAndNewlines)))
    }

    private func options(_ provider: ModelProvider) -> BuildOptions {
        var options = BuildOptions()
        options.choose(provider: provider)
        return options
    }

    private func rows(_ doctor: DoctorReport?, _ provider: ModelProvider, error: String? = nil, cliFound: Bool = true) -> [SetupCheck] {
        SetupChecks.rows(cliFound: cliFound, doctor: doctor, doctorError: error, options: options(provider))
    }

    func testEverythingReadyMeansNoPage() throws {
        let list = rows(try sample(), .claudeCode)
        XCTAssertEqual(list.map(\.id), ["video", "transcription", "planner"])
        XCTAssertTrue(list.allSatisfy(\.ok))
        XCTAssertFalse(SetupChecks.isBlocked(list))
        XCTAssertEqual(list[2].detail, "Signed in with your Claude plan.")
    }

    func testClaudeCodeSignedOutAsksToSignIn() throws {
        var doctor = try sample()
        doctor.claudeCodeAuth = nil
        let list = rows(doctor, .claudeCode)
        XCTAssertEqual(list.map(\.ok), [true, true, false])
        XCTAssertEqual(list[2].fix, .signIn)
        XCTAssertTrue(SetupChecks.isBlocked(list))
        XCTAssertTrue(SetupChecks.onlyThePlannerIsMissing(list), "a build without AI would still run")
    }

    func testClaudeCodeSignedInWithAnAPIKeyIsNotThePlan() throws {
        var doctor = try sample()
        doctor.claudeCodeAuth = "api_key"
        XCTAssertEqual(rows(doctor, .claudeCode)[2].fix, .signIn)
    }

    func testClaudeCodeMissingSaysHowToInstallIt() throws {
        var doctor = try sample()
        doctor.claudeCode = nil
        doctor.claudeCodeAuth = nil
        let planner = rows(doctor, .claudeCode)[2]
        XCTAssertFalse(planner.ok)
        XCTAssertEqual(planner.fix, .terminal("curl -fsSL https://claude.ai/install.sh | bash"))
    }

    func testMissingVideoToolsBlockEveryPlannerAndGetTheirCommand() throws {
        var doctor = try sample()
        doctor.ffmpeg = nil
        let list = rows(doctor, .claudeCode)
        XCTAssertEqual(list[0].fix, .terminal("brew install ffmpeg"))
        XCTAssertFalse(SetupChecks.onlyThePlannerIsMissing(list), "going without AI doesn't bring ffmpeg back")
        doctor = try sample()
        doctor.transcriber = nil
        XCTAssertEqual(rows(doctor, .none)[1].fix, .terminal("pip install -e '.[mac]'"))
    }

    func testAnAPIKeyThatIsMissingOrRejectedSaysWhy() throws {
        let doctor = try sample()  // the sample has no key
        let planner = rows(doctor, .anthropic)[2]
        XCTAssertFalse(planner.ok)
        XCTAssertEqual(planner.fix, .key)
        XCTAssertTrue(planner.detail.contains("No Claude API key is set"), planner.detail)
    }

    func testOpenAINeedsItsPackageAndItsKey() throws {
        var doctor = try sample()
        doctor.openaiInstalled = false
        XCTAssertEqual(rows(doctor, .openai)[2].fix, .terminal("pip install -e '.[openai]'"))
        doctor.openaiInstalled = true
        doctor.openaiKey = false
        XCTAssertEqual(rows(doctor, .openai)[2].fix, .key)
        XCTAssertTrue(rows(doctor, .openaiCompatible)[2].ok, "a server of your own needs no key from here")
    }

    func testNoAIAndLocalModelsNeedNoAccount() throws {
        XCTAssertEqual(rows(try sample(), .none).map(\.id), ["video", "transcription"])
        XCTAssertEqual(rows(try sample(), .ollama).map(\.id), ["video", "transcription"])
    }

    func testTheGraphicsRendererIsCheckedOnlyWhenGraphicsAreOn() throws {
        var doctor = try sample()
        doctor.graphics = "Playwright's Chromium isn't installed."
        XCTAssertEqual(rows(doctor, .claudeCode).map(\.id), ["video", "transcription", "planner"])
        var on = options(.claudeCode)
        on.setGraphics(true)
        let list = SetupChecks.rows(cliFound: true, doctor: doctor, doctorError: nil, options: on)
        XCTAssertEqual(list.last?.id, "graphics")
        XCTAssertEqual(list.last?.detail, "Playwright's Chromium isn't installed.")
        doctor.graphics = "not checked"
        XCTAssertEqual(SetupChecks.rows(cliFound: true, doctor: doctor, doctorError: nil, options: on).count, 3)
    }

    func testTheCommandLineToolItselfMissingOrBroken() throws {
        let missing = rows(nil, .claudeCode, cliFound: false)
        XCTAssertEqual(missing.map(\.id), ["cli"])
        XCTAssertEqual(missing[0].fix, .settings)

        let broken = rows(nil, .claudeCode, error: "Python 3.8 is too old")
        XCTAssertEqual(broken.map(\.id), ["cli"])
        XCTAssertTrue(broken[0].detail.contains("Python 3.8 is too old"))

        XCTAssertEqual(rows(nil, .claudeCode), [], "the first check is still running: no page yet")
    }
}

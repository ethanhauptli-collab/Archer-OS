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
            "--style", "medium", "--provider", "none", "--keep-fillers", "--no-stringout",
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
        XCTAssertEqual(o.problems, ["Target length should look like 8m, 90s or 8:30."])
    }

    func testOutputFolderAndName() {
        var o = options()
        o.outputFolder = URL(fileURLWithPath: "/Volumes/SSD/Cuts")
        o.projectName = "Trip, Day 1"
        let args = o.arguments()
        XCTAssertTrue(args.contains("--out=/Volumes/SSD/Cuts"))
        XCTAssertTrue(args.contains("--name=Trip, Day 1"))
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
        XCTAssertNil(TimeText.seconds(from: ""))
        XCTAssertNil(TimeText.seconds(from: "0"))
        XCTAssertNil(TimeText.seconds(from: "eight"))
        XCTAssertNil(TimeText.seconds(from: "8x"))
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
}

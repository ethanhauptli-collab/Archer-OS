import XCTest
@testable import RoughcutKit

final class CLIProcessTests: XCTestCase {
    private let sh = URL(fileURLWithPath: "/bin/sh")

    private func collect(_ process: CLIProcess) async -> [RunOutput] {
        var outputs: [RunOutput] = []
        for await output in process.run() {
            outputs.append(output)
        }
        return outputs
    }

    func testStreamsEventsLogsAndExitStatus() async {
        let script = #"printf '{"event":"stage","stage":"scan","message":"Finding media"}\n'; printf 'bar 10%%\rbar 90%%\n' >&2; printf 'no newline at end'; exit 3"#
        let outputs = await collect(CLIProcess(executable: sh, arguments: ["-c", script], environment: ProcessInfo.processInfo.environment))

        XCTAssertEqual(outputs.last, .exited(status: 3, signaled: false))
        XCTAssertTrue(outputs.contains { output in
            if case .event(let event) = output { return event.stageValue == .scan }
            return false
        })
        // \r splits tqdm-style redraws into separate lines.
        XCTAssertTrue(outputs.contains(.stderr("bar 10%")))
        XCTAssertTrue(outputs.contains(.stderr("bar 90%")))
        // A final line without a newline is still delivered before the exit.
        XCTAssertTrue(outputs.contains(.stdout("no newline at end")))
    }

    func testDoneEventIsNeverLostAtExit() async {
        // Lots of output right before exiting: the stream must drain it all.
        let script = #"i=0; while [ $i -lt 500 ]; do echo "line $i"; i=$((i+1)); done; printf '{"event":"done","result":{"title":"t","out_dir":"/o","fcpxml":"/o/x.fcpxml","report":"/o/r.md","rough_seconds":1.5,"stringout_seconds":null,"edits":1,"broll":0,"sections":[],"planned_by":"heuristic","fell_back":false,"warnings":[]}}\n'"#
        let outputs = await collect(CLIProcess(executable: sh, arguments: ["-c", script], environment: ProcessInfo.processInfo.environment))
        let stdoutCount = outputs.filter { if case .stdout = $0 { return true }; return false }.count
        XCTAssertEqual(stdoutCount, 500)
        let done = outputs.compactMap { output -> RunResult? in
            if case .event(let event) = output { return event.result }
            return nil
        }
        XCTAssertEqual(done.first?.title, "t")
        XCTAssertEqual(outputs.last, .exited(status: 0, signaled: false))
    }

    func testCancelTerminatesTheRun() async {
        let process = CLIProcess(executable: sh, arguments: ["-c", "exec sleep 30"], environment: ProcessInfo.processInfo.environment)
        let stream = process.run()
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            process.cancel()
        }
        var last: RunOutput?
        for await output in stream {
            last = output
        }
        XCTAssertEqual(last, .exited(status: 15, signaled: true))
    }

    func testMissingExecutableReportsLaunchFailure() async {
        let outputs = await collect(CLIProcess(executable: URL(fileURLWithPath: "/nonexistent/roughcut"), arguments: [], environment: [:]))
        XCTAssertEqual(outputs.count, 1)
        if case .failedToLaunch = outputs.first {} else {
            XCTFail("expected failedToLaunch, got \(outputs)")
        }
    }

    func testLineBuffer() {
        let buffer = LineBuffer(separators: [0x0A, 0x0D])
        XCTAssertEqual(buffer.append(Data("one\ntw".utf8)), ["one"])
        XCTAssertEqual(buffer.append(Data("o\r\nthree".utf8)), ["two"])
        XCTAssertEqual(buffer.flush(), "three")
        XCTAssertNil(buffer.flush())
    }
}

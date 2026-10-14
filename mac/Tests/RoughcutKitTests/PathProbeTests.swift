import XCTest
@testable import RoughcutKit

final class PathProbeTests: XCTestCase {
    private func tempFile() throws -> URL {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-probe-\(UUID().uuidString).txt")
        try Data("x".utf8).write(to: file)
        addTeardownBlock { try? FileManager.default.removeItem(at: file) }
        return file
    }

    func testAFileThatIsThereAndOneThatIsNotAreTold() async throws {
        let file = try tempFile()
        let there = await PathProbe.presence(of: file.path)
        let gone = await PathProbe.presence(of: file.path + ".missing")
        XCTAssertEqual(there, .there)
        XCTAssertEqual(gone, .gone)
    }

    func testADriveThatNeverAnswersIsGivenUpOnAtTheDeadline() async {
        let started = Date()
        let answer = await PathProbe.presence(of: "/Volumes/Stalled/never-\(UUID().uuidString)", timeout: 0.2) { _ in
            Thread.sleep(forTimeInterval: 2)
            return true
        }
        XCTAssertEqual(answer, .silent)
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5, "the deadline, not the drive, decided when to stop waiting")
    }

    func testAPathStillWaitingOnItsDriveIsNotAskedAgain() async {
        let path = "/Volumes/Stalled/again-\(UUID().uuidString)"
        let first = await PathProbe.presence(of: path, timeout: 0.1) { _ in
            Thread.sleep(forTimeInterval: 0.8)
            return true
        }
        XCTAssertEqual(first, .silent)
        let started = Date()
        let second = await PathProbe.presence(of: path, timeout: 5) { _ in true }
        XCTAssertEqual(second, .silent, "the first check is still waiting on the drive: a second thread is not started behind it")
        XCTAssertLessThan(Date().timeIntervalSince(started), 1, "and nobody waits for it")
        try? await Task.sleep(nanoseconds: 1_200_000_000)  // the drive answers at last
        let third = await PathProbe.presence(of: path, timeout: 5) { _ in true }
        XCTAssertEqual(third, .there)
    }

    func testManyPathsAreAskedAtOnceSoTheWaitIsTheSlowestNotTheSum() async throws {
        let file = try tempFile()
        let stalled = "/Volumes/Stalled/many-\(UUID().uuidString)"
        let started = Date()
        let answers = await PathProbe.presence(of: [file.path, file.path + ".missing", stalled, stalled], timeout: 0.3) { path in
            if path == stalled { Thread.sleep(forTimeInterval: 2) }
            return FileManager.default.fileExists(atPath: path)
        }
        XCTAssertEqual(answers[file.path], .there)
        XCTAssertEqual(answers[file.path + ".missing"], .gone)
        XCTAssertEqual(answers[stalled], .silent)
        XCTAssertEqual(answers.count, 3, "a path given twice is asked once")
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5)
    }

    func testAnEmptyListIsAnEmptyAnswer() async {
        let answers = await PathProbe.presence(of: [String]())
        XCTAssertTrue(answers.isEmpty)
    }
}

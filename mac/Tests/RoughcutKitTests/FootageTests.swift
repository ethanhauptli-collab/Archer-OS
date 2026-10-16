import XCTest
@testable import RoughcutKit

/// What the Footage step says about a dropped folder, counted from file names the way the engine's scan reads them.
final class FootageTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("footage-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func touch(_ path: String) throws {
        let url = root.appendingPathComponent(path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: url)
    }

    private func count(_ path: String, limit: Int = 20_000, deadline: Date = .distantFuture) -> FootageCount {
        FootageCount.count(source: root.appendingPathComponent(path), limit: limit, deadline: deadline)
    }

    // MARK: Types

    /// Fixtures/media-extensions.json is read by tests/test_media_extensions_fixture.py too: the app and the engine take the same files.
    func testTheTypesAreTheEngines() throws {
        struct Fixture: Decodable {
            var video: [String]
            var audio: [String]
            var still: [String]
            var skipFolderSuffixes: [String]
        }
        let fixture = try JSONDecoder().decode(Fixture.self, from: FixtureFiles.data("media-extensions", "json"))
        XCTAssertEqual(MediaKind.videoExtensions, Set(fixture.video))
        XCTAssertEqual(MediaKind.audioExtensions, Set(fixture.audio))
        XCTAssertEqual(MediaKind.stillExtensions, Set(fixture.still))
        XCTAssertEqual(MediaKind.skippedFolderSuffixes, fixture.skipFolderSuffixes)
    }

    func testTheKindIsByExtensionInAnyCase() {
        XCTAssertEqual(MediaKind.of(URL(fileURLWithPath: "/a/CLIP.MOV")), .video)
        XCTAssertEqual(MediaKind.of(URL(fileURLWithPath: "/a/take.Wav")), .audio)
        XCTAssertEqual(MediaKind.of(URL(fileURLWithPath: "/a/menu.JPG")), .still)
        XCTAssertNil(MediaKind.of(URL(fileURLWithPath: "/a/notes.txt")))
        XCTAssertNil(MediaKind.of(URL(fileURLWithPath: "/a/noextension")))
    }

    // MARK: Walking

    func testAFolderIsWalkedWithItsSubfolders() throws {
        try touch("Pier/a.mov")
        try touch("Pier/b.MP4")
        try touch("Pier/deep/er/c.mov")
        try touch("Pier/voice.wav")
        try touch("Pier/menu.jpg")
        try touch("Pier/notes.txt")
        let found = count("Pier")
        XCTAssertEqual(found, FootageCount(videos: 3, recordings: 1, stills: 1))
        XCTAssertEqual(found.total, 5)
        XCTAssertFalse(found.partial)
    }

    /// The engine's scan skips hidden files and folders and library bundles; counting them would promise clips it never reads.
    func testHiddenFilesAndLibraryBundlesAreLeftOutAsTheEnginesScanDoes() throws {
        try touch("Shoot/a.mov")
        try touch("Shoot/._a.mov")
        try touch("Shoot/.hidden/b.mov")
        try touch("Shoot/Old.fcpbundle/c.mov")
        try touch("Shoot/Photos.photoslibrary/d.jpg")
        XCTAssertEqual(count("Shoot"), FootageCount(videos: 1))
    }

    func testAFileCountsAsOneOfItsKindAndAnythingElseAsNothing() throws {
        try touch("one.mov")
        try touch("notes.txt")
        try touch(".shadow.mov")
        XCTAssertEqual(count("one.mov"), FootageCount(videos: 1))
        XCTAssertTrue(count("notes.txt").isEmpty)
        XCTAssertTrue(count(".shadow.mov").isEmpty)
        XCTAssertTrue(count("missing").isEmpty, "a path that isn't there counts as nothing")
    }

    func testAWalkThatRunsOutOfRoomSaysSo() throws {
        for index in 0..<10 {
            try touch("Big/\(index).mov")
        }
        let limited = count("Big", limit: 4)
        XCTAssertTrue(limited.partial)
        XCTAssertEqual(limited.videos, 4)

        for index in 0..<200 {
            try touch("Huge/\(index).mov")
        }
        let late = count("Huge", deadline: .distantPast)
        XCTAssertTrue(late.partial, "a deadline that has passed stops the walk")
        XCTAssertLessThan(late.videos, 200)
    }

    // MARK: Saying it

    func testTheSentenceReadsLikeAPerson() {
        XCTAssertEqual(FootageCount(videos: 14, recordings: 1, stills: 2).sentence, "14 videos, 2 stills and 1 recording")
        XCTAssertEqual(FootageCount(videos: 1).sentence, "1 video")
        XCTAssertEqual(FootageCount(videos: 2, stills: 1).sentence, "2 videos and 1 still")
        XCTAssertEqual(FootageCount().sentence, "No video, audio or stills found")
        XCTAssertEqual(FootageCount(videos: 3, partial: true).sentence, "At least 3 videos")
    }

    func testTheShortSummaryAndSums() {
        XCTAssertEqual(FootageCount(videos: 9, recordings: 1).shortSummary, "10 files")
        XCTAssertEqual(FootageCount(videos: 1).shortSummary, "1 file")
        XCTAssertEqual(FootageCount(videos: 4, partial: true).shortSummary, "At least 4 files")
        XCTAssertEqual(
            FootageCount(videos: 9) + FootageCount(recordings: 1, partial: true),
            FootageCount(videos: 9, recordings: 1, partial: true)
        )
    }
}

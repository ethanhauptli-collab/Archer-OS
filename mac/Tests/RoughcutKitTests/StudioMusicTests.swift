import XCTest
@testable import RoughcutKit

final class StudioMusicTests: XCTestCase {
    /// Fixtures/studio-music-sample.json is `roughcut studio music --json` output, written by the Python suite
    /// (tests/test_studio_music.py fails there when the listing changes shape).
    private func sample() throws -> [StudioSong] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "studio-music-sample", withExtension: "json", subdirectory: "Fixtures"))
        let text = try String(contentsOf: url, encoding: .utf8)
        return try XCTUnwrap(StudioMusic.songs(from: text))
    }

    func testDecodesTheCLIsListing() throws {
        let songs = try sample()
        XCTAssertFalse(songs.isEmpty)
        let sunrise = try XCTUnwrap(songs.first { $0.name == "Sunrise.m4a" })
        XCTAssertEqual(sunrise.studioID, 1)
        XCTAssertTrue(sunrise.favorite)
        XCTAssertEqual(sunrise.length, "3:04")
        XCTAssertEqual(sunrise.tags, ["upbeat", "warm"])
        XCTAssertEqual(sunrise.provider, "Vendor A")
        XCTAssertEqual(sunrise.collections, ["Promo Music"])
        XCTAssertTrue(sunrise.usable)
        XCTAssertEqual(sunrise.url.path, "/Volumes/Media/Studio Library/Music/Sunrise.m4a")
        XCTAssertTrue(sunrise.asOf.contains("catalog snapshot"))
        let unmeasured = try XCTUnwrap(songs.first { $0.seconds == nil })
        XCTAssertEqual(unmeasured.length, "?")
        let flac = try XCTUnwrap(songs.first { !$0.usable })
        XCTAssertNotNil(flac.whyNot)
    }

    func testAJSONArrayIsNeverAProgressEvent() {
        XCTAssertNil(JSONLines.event(from: #"[{"id": 1, "path": "/a.wav", "event": "done"}]"#))
        XCTAssertEqual(CLIProcess.classify(#"[{"id": 1, "path": "/a.wav"}]"#), .stdout(#"[{"id": 1, "path": "/a.wav"}]"#))
    }

    func testMissingFieldsDefaultAndOnlyThePathIsRequired() {
        let songs = StudioMusic.songs(from: #"[{"path": "/Volumes/Media/Studio Library/Music/Calm.wav", "a_later_field": 1}]"#)
        XCTAssertEqual(songs?.first?.name, "Calm.wav")
        XCTAssertEqual(songs?.first?.usable, true)
        XCTAssertEqual(songs?.first?.length, "?")
        XCTAssertNil(StudioMusic.songs(from: "roughcut: Venture Studio has no catalog snapshot yet"))
    }

    func testTheSheetFiltersBySearchCollectionAndFavorites() {
        let json = #"""
        [{"id": 1, "name": "Sunrise", "path": "/L/Sunrise.m4a", "favorite": true, "tags": ["upbeat"], "collections": ["Promo Music"], "guess": "music"},
         {"id": 2, "name": "Calm Piano", "path": "/L/Calm.wav", "provider": "Vendor B", "collections": ["Quiet"], "guess": "unknown"},
         {"id": 3, "name": "Whoosh", "path": "/L/whoosh.wav", "collections": ["Promo Music"], "guess": "sfx"}]
        """#
        let songs = StudioMusic.songs(from: json) ?? []
        XCTAssertEqual(StudioMusic.collections(in: songs), ["Promo Music", "Quiet"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "").map(\.name), ["Calm Piano", "Sunrise", "Whoosh"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "UPBEAT").map(\.name), ["Sunrise"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "vendor piano").map(\.name), ["Calm Piano"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "", collection: "Promo Music").map(\.name), ["Sunrise", "Whoosh"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "", collection: "Promo Music", songsOnly: true).map(\.name), ["Sunrise"])
        XCTAssertEqual(StudioMusic.filter(songs, search: "", favoritesOnly: true).map(\.name), ["Sunrise"])
    }

    func testArguments() {
        XCTAssertEqual(StudioMusic.arguments(), ["studio", "music", "--json"])
    }

    func testTheMusicBedTakesOnlySongFiles() {
        let dropped = [
            URL(fileURLWithPath: "/Volumes/Media/Studio Library/Music/Sunrise.m4a"),
            URL(fileURLWithPath: "/Users/me/Downloads/Song.WAV"),
            URL(fileURLWithPath: "/Volumes/Media/Studio Library/Music/Lossless.flac"),
            URL(fileURLWithPath: "/Volumes/Media/Studio Library/Footage/clip.mov"),
            URL(string: "https://example.com/song.mp3")!,
            URL(fileURLWithPath: "/Users/me/Downloads/Song.WAV"),
        ]
        let sorted = MusicFiles.sort(dropped)
        XCTAssertEqual(sorted.songs.map(\.lastPathComponent), ["Sunrise.m4a", "Song.WAV"])
        XCTAssertEqual(sorted.leftOut, ["Lossless.flac", "clip.mov", "https://example.com/song.mp3"])
        let note = MusicFiles.leftOutNote(sorted.leftOut) ?? ""
        XCTAssertTrue(note.contains("Lossless.flac") && note.contains("Footage"))
        XCTAssertNil(MusicFiles.leftOutNote([]))
    }

    func testPastedText() {
        XCTAssertEqual(MusicFiles.url(fromPastedText: "/Volumes/Media/Studio Library/Music/Sunrise.m4a\n")?.path, "/Volumes/Media/Studio Library/Music/Sunrise.m4a")
        XCTAssertEqual(MusicFiles.url(fromPastedText: "'/Volumes/Media/A Song.wav'")?.path, "/Volumes/Media/A Song.wav")
        XCTAssertEqual(MusicFiles.url(fromPastedText: "file:///Volumes/Media/A%20Song.wav")?.path, "/Volumes/Media/A Song.wav")
        XCTAssertNil(MusicFiles.url(fromPastedText: "Sunrise.m4a"))
        XCTAssertNil(MusicFiles.url(fromPastedText: "/a.wav\n/b.wav"))
        XCTAssertNil(MusicFiles.url(fromPastedText: "  "))
    }

    func testSongsOnOtherVolumesAreLeftToTheCLI() {
        var options = BuildOptions()
        options.sources = [URL(fileURLWithPath: "/tmp")]
        options.music = [URL(fileURLWithPath: "/Volumes/Media/Studio Library/Music/Not mounted.m4a")]
        XCTAssertTrue(options.problems.isEmpty)  // the CLI checks it, with a deadline
        XCTAssertEqual(options.arguments().filter { $0.hasPrefix("--music=") }, ["--music=/Volumes/Media/Studio Library/Music/Not mounted.m4a"])
        options.music = [URL(fileURLWithPath: "/tmp/roughcut-no-such-song.m4a")]
        XCTAssertEqual(options.problems.count, 1)
    }

    func testCollectStopsACommandThatRunsPastItsTime() async {
        let sh = URL(fileURLWithPath: "/bin/sh")
        let slow = CLIProcess(executable: sh, arguments: ["-c", "echo started; exec sleep 30"], environment: ProcessInfo.processInfo.environment)
        let started = Date()
        let result = await slow.collect(timeout: 0.5)
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)
        XCTAssertTrue(result.timedOut)
        XCTAssertEqual(result.problem, "roughcut didn't answer in time.")
        XCTAssertEqual(result.stdout, ["started"])

        let quick = CLIProcess(executable: sh, arguments: ["-c", #"echo '[{"path": "/a.wav"}]'; echo 'roughcut: careful' >&2"#], environment: ProcessInfo.processInfo.environment)
        let done = await quick.collect(timeout: 10)
        XCTAssertFalse(done.timedOut)
        XCTAssertNil(done.problem)
        XCTAssertEqual(StudioMusic.songs(from: done.output)?.count, 1)

        let failing = CLIProcess(executable: sh, arguments: ["-c", "echo 'roughcut: Venture Studio has no catalog snapshot yet' >&2; exit 1"], environment: ProcessInfo.processInfo.environment)
        let failed = await failing.collect(timeout: 10)
        XCTAssertEqual(failed.problem, "Venture Studio has no catalog snapshot yet")
    }
}

import XCTest
@testable import RoughcutKit

final class ReferenceImagesTests: XCTestCase {
    private func pictures(_ count: Int, in folder: String = "/Users/you/Pictures") -> [URL] {
        (1...count).map { URL(fileURLWithPath: "\(folder)/look \($0).png") }
    }

    /// A folder of its own for files a test writes; removed when the test ends.
    private func scratchFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-refs-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }

    func testOnlyPicturesTheCLIReadsCount() {
        for name in ["a.png", "B.PNG", "c.jpg", "d.jpeg", "e.webp", "f.gif", "g.tif", "h.tiff", "i.bmp", "j.heic", "k.HEIF"] {
            XCTAssertTrue(ReferenceImages.isPicture(URL(fileURLWithPath: "/x/\(name)")), name)
        }
        for name in ["notes.txt", "clip.mov", "logo.svg", "sheet.pdf", "noextension"] {
            XCTAssertFalse(ReferenceImages.isPicture(URL(fileURLWithPath: "/x/\(name)")), name)
        }
        XCTAssertTrue(ReferenceImages.isJunk(URL(fileURLWithPath: "/x/._look.png")))
        XCTAssertTrue(ReferenceImages.isJunk(URL(fileURLWithPath: "/x/.DS_Store")))
        XCTAssertFalse(ReferenceImages.isJunk(URL(fileURLWithPath: "/x/look.png")))
    }

    func testAddingKeepsOrderAndSkipsRepeatsAndJunkWithoutAWord() {
        let existing = pictures(2)
        let offered = [
            existing[0],  // already there
            URL(fileURLWithPath: "/Users/you/Pictures/new.jpg"),
            URL(fileURLWithPath: "/Users/you/Pictures/._new.jpg"),  // macOS companion file
            URL(fileURLWithPath: "/Users/you/Pictures/../Pictures/new.jpg"),  // the same file by another route
        ]
        let result = ReferenceImages.adding(offered, to: existing)
        XCTAssertEqual(result.list.map(\.lastPathComponent), ["look 1.png", "look 2.png", "new.jpg"])
        XCTAssertEqual(result.added, 1)
        XCTAssertNil(result.note)
        // Nothing offered, nothing said.
        let none = ReferenceImages.adding([], to: existing)
        XCTAssertEqual(none.list, existing)
        XCTAssertEqual(none.added, 0)
        XCTAssertNil(none.note)
    }

    func testOtherItemsAreLeftOutWithAShortNote() {
        let one = ReferenceImages.adding([URL(fileURLWithPath: "/x/notes.txt"), URL(fileURLWithPath: "/x/ok.png")], to: [])
        XCTAssertEqual(one.list.map(\.lastPathComponent), ["ok.png"])
        XCTAssertEqual(one.added, 1)
        XCTAssertEqual(one.note, "notes.txt isn't a picture Roughcut can use (PNG, JPEG, WebP, GIF, TIFF, BMP or HEIC), so it was left out.")

        let many = ReferenceImages.adding(["a.mov", "b.pdf", "c.txt", "d.svg", "e.mp3"].map { URL(fileURLWithPath: "/x/\($0)") }, to: [])
        XCTAssertTrue(many.list.isEmpty)
        XCTAssertEqual(many.note, "a.mov, b.pdf, c.txt and 2 more aren't pictures Roughcut can use (PNG, JPEG, WebP, GIF, TIFF, BMP or HEIC), so they were left out.")

        // A web address isn't a file.
        let web = ReferenceImages.adding([URL(string: "https://example.com/look.png")!], to: [])
        XCTAssertTrue(web.list.isEmpty)
        XCTAssertNotNil(web.note)
    }

    func testAnEighthPictureFitsAndANinthSaysWhy() {
        let seven = pictures(7)
        let eighth = ReferenceImages.adding([URL(fileURLWithPath: "/y/eighth.png")], to: seven)
        XCTAssertEqual(eighth.list.count, 8)
        XCTAssertNil(eighth.note)

        let ninth = ReferenceImages.adding([URL(fileURLWithPath: "/y/ninth.png")], to: eighth.list)
        XCTAssertEqual(ninth.list, eighth.list)
        XCTAssertEqual(ninth.added, 0)
        XCTAssertEqual(ninth.note, "Only 8 reference images are used, so ninth.png wasn't added. Remove one to make room.")

        // Some of a batch fit, the rest are named.
        let batch = ReferenceImages.adding((1...4).map { URL(fileURLWithPath: "/y/batch \($0).png") }, to: pictures(6))
        XCTAssertEqual(batch.list.count, 8)
        XCTAssertEqual(batch.added, 2)
        XCTAssertEqual(batch.note, "Only 8 reference images are used, so batch 3.png, batch 4.png weren't added. Remove one to make room.")
    }

    func testArgumentsUseTheEqualsFormAndStopAtEight() {
        let list = [URL(fileURLWithPath: "/Users/you/Pictures/-dash first.png")] + pictures(9)
        let args = ReferenceImages.arguments(for: list)
        XCTAssertEqual(args.count, 8)
        XCTAssertEqual(args.first, "--graphics-ref=/Users/you/Pictures/-dash first.png")  // "=": never read as a flag
        XCTAssertEqual(args.last, "--graphics-ref=/Users/you/Pictures/look 7.png")
        XCTAssertEqual(ReferenceImages.arguments(for: []), [])
    }

    func testOnlyPicturesThatAreStillThereComeBack() throws {
        let folder = try scratchFolder()
        let there = folder.appendingPathComponent("there.png")
        try Data("x".utf8).write(to: there)
        let gone = folder.appendingPathComponent("gone.png")
        XCTAssertEqual(ReferenceImages.restoring([gone, there]).map(\.lastPathComponent), ["there.png"])
        XCTAssertEqual(ReferenceImages.restoring([]), [])

        // Never more than eight, whatever was saved.
        let many = try (1...10).map { index -> URL in
            let file = folder.appendingPathComponent("many \(index).png")
            try Data("x".utf8).write(to: file)
            return file
        }
        XCTAssertEqual(ReferenceImages.restoring(many).count, 8)
    }

    func testPastedBitmapsAreKeptInAFolderMadeOnDemand() throws {
        let base = try scratchFolder()
        let folder = ReferenceImages.storeFolder(base: base)
        XCTAssertEqual(folder.path, base.appendingPathComponent("Roughcut/References").path)
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.path))  // nothing is made until a picture is pasted

        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3])
        let first = try ReferenceImages.store(png: png, in: folder, now: Date(timeIntervalSince1970: 1_700_000_000))
        let second = try ReferenceImages.store(png: png, in: folder, now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertEqual(first.deletingLastPathComponent().path, folder.path)
        XCTAssertEqual(first.pathExtension, "png")
        XCTAssertTrue(first.lastPathComponent.hasPrefix("pasted-"))
        XCTAssertEqual(try Data(contentsOf: first), png)
        XCTAssertNotEqual(first, second)  // two pastes in one second are two files
        XCTAssertTrue(ReferenceImages.isPicture(first))

        // The system default is under Application Support.
        XCTAssertTrue(ReferenceImages.storeFolder().path.hasSuffix("/Roughcut/References"))
    }

    func testOldPicturesNobodyUsesAreRemoved() throws {
        let folder = try scratchFolder()
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        func make(_ name: String, daysOld: Double) throws -> URL {
            let file = folder.appendingPathComponent(name)
            try Data("x".utf8).write(to: file)
            try FileManager.default.setAttributes([.modificationDate: now.addingTimeInterval(-daysOld * 86_400)], ofItemAtPath: file.path)
            return file
        }
        let stale = try make("stale.png", daysOld: 31)
        let recent = try make("recent.png", daysOld: 29)
        let inUse = try make("in use.png", daysOld: 90)
        let notAPicture = try make("notes.txt", daysOld: 90)
        let hidden = try make(".hidden.png", daysOld: 90)
        let subfolder = folder.appendingPathComponent("sub")
        try FileManager.default.createDirectory(at: subfolder, withIntermediateDirectories: true)
        let nested = subfolder.appendingPathComponent("nested.png")
        try Data("x".utf8).write(to: nested)
        try FileManager.default.setAttributes([.modificationDate: now.addingTimeInterval(-90 * 86_400)], ofItemAtPath: nested.path)

        let removed = ReferenceImages.removeStale(in: folder, keeping: [inUse], now: now)
        XCTAssertEqual(removed.map(\.lastPathComponent), ["stale.png"])
        XCTAssertFalse(FileManager.default.fileExists(atPath: stale.path))
        for kept in [recent, inUse, notAPicture, hidden, nested] {
            XCTAssertTrue(FileManager.default.fileExists(atPath: kept.path), kept.lastPathComponent)
        }
        // A folder that isn't there yet is nothing to tidy.
        XCTAssertEqual(ReferenceImages.removeStale(in: folder.appendingPathComponent("nope"), keeping: [], now: now), [])
    }
}

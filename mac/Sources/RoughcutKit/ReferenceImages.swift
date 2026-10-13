import Foundation

/// Reference images for the motion graphics (`--graphics-ref`): pictures of the look the editor wants.
///
/// This is the app's side of `roughcut/references.py`: which files the lists take, how many, where a
/// pasted bitmap is kept and when it goes. Reading a picture, converting it and everything that can go
/// wrong with one is the CLI's job (it leaves a bad picture out with a warning naming its file).
/// The pictures are the editor's private ones: the app passes their paths to the CLI and nothing else.
public enum ReferenceImages {
    /// The CLI uses at most this many (`references.MAX_REFERENCES`); the rest it leaves out with a warning.
    public static let maxCount = 8

    /// What the CLI reads (`references.EXTENSIONS`), lower case.
    public static let extensions: Set<String> = ["png", "jpg", "jpeg", "webp", "gif", "tif", "tiff", "bmp", "heic", "heif"]

    /// Pasted and dropped bitmaps are kept this many days once no list uses them.
    public static let keepDays = 30

    /// The line under a references row.
    public static let caption = "Show the look you want: screenshots, other videos' titles, brand sheets. Roughcut matches the style, not the content."

    private static let junkNames: Set<String> = [".DS_Store", ".localized", "Icon\r"]

    public static func isPicture(_ url: URL) -> Bool {
        extensions.contains(url.pathExtension.lowercased())
    }

    /// "._name" companions and .DS_Store, which macOS leaves beside real files (the CLI skips them too).
    public static func isJunk(_ url: URL) -> Bool {
        let name = url.lastPathComponent
        return name.hasPrefix("._") || junkNames.contains(name)
    }

    /// What adding some files to a list did.
    public struct Addition: Equatable, Sendable {
        /// The list after adding.
        public var list: [URL]
        /// How many of the offered files joined it.
        public var added: Int
        /// What was left out and why, in a sentence or two; nil when nothing offered was refused for a reason worth saying.
        public var note: String?
    }

    /// `offered` added to `list`: files already in it and macOS junk are skipped without a word; anything that
    /// isn't a picture, and anything past `maxCount`, is left out with a note saying so.
    public static func adding(_ offered: [URL], to list: [URL]) -> Addition {
        var result = list
        var taken = Set(list.map { $0.standardizedFileURL.path })
        var added = 0
        var notPictures: [String] = []
        var pastTheLimit: [String] = []
        for url in offered {
            guard url.isFileURL else {
                notPictures.append(url.lastPathComponent)
                continue
            }
            let clean = url.standardizedFileURL
            if isJunk(clean) || taken.contains(clean.path) {
                continue
            }
            if !isPicture(clean) {
                notPictures.append(clean.lastPathComponent)
            } else if result.count >= maxCount {
                pastTheLimit.append(clean.lastPathComponent)
            } else {
                result.append(clean)
                taken.insert(clean.path)
                added += 1
            }
        }
        var notes: [String] = []
        if !notPictures.isEmpty {
            let one = notPictures.count == 1
            notes.append("\(named(notPictures)) \(one ? "isn't a picture" : "aren't pictures") Roughcut can use (PNG, JPEG, WebP, GIF, TIFF, BMP or HEIC), so \(one ? "it was" : "they were") left out.")
        }
        if !pastTheLimit.isEmpty {
            let one = pastTheLimit.count == 1
            notes.append("Only \(maxCount) reference images are used, so \(named(pastTheLimit)) \(one ? "wasn't" : "weren't") added. Remove one to make room.")
        }
        return Addition(list: result, added: added, note: notes.isEmpty ? nil : notes.joined(separator: " "))
    }

    /// "a.png", "a.png, b.png, c.png", "a.png, b.png, c.png and 2 more".
    static func named(_ names: [String]) -> String {
        let shown = names.prefix(3).joined(separator: ", ")
        return names.count > 3 ? "\(shown) and \(names.count - 3) more" : shown
    }

    /// `--graphics-ref=<path>` for each picture, at most `maxCount`. With "=", a path that starts with "-" can't be read as a flag.
    public static func arguments(for list: [URL]) -> [String] {
        list.prefix(maxCount).map { "--graphics-ref=\($0.path)" }
    }

    /// A remembered list as it can still be used: the pictures that are still there, at most `maxCount`.
    public static func restoring(_ saved: [URL]) -> [URL] {
        Array(saved.filter { FileManager.default.fileExists(atPath: $0.path) }.prefix(maxCount))
    }

    // MARK: Pasted bitmaps

    /// Where pasted and dropped bitmaps are written: `~/Library/Application Support/Roughcut/References/`.
    /// `base` is for tests.
    public static func storeFolder(base: URL? = nil) -> URL {
        let support = base ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return support.appendingPathComponent("Roughcut", isDirectory: true).appendingPathComponent("References", isDirectory: true)
    }

    /// Writes a pasted bitmap (already PNG) into `folder`, which is created on demand. Returns the new file.
    public static func store(png: Data, in folder: URL, now: Date = Date()) throws -> URL {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let stamp = DateFormatter()
        stamp.locale = Locale(identifier: "en_US_POSIX")
        stamp.dateFormat = "yyyyMMdd-HHmmss"
        let unique = UUID().uuidString.prefix(8).lowercased()
        let file = folder.appendingPathComponent("pasted-\(stamp.string(from: now))-\(unique).png")
        try png.write(to: file, options: .atomic)
        return file
    }

    /// Removes the pictures in `folder` that were last written more than `days` days before `now` and are not
    /// in `keeping`. Only the folder's own picture files: no subfolder, nothing hidden. Returns what it removed.
    @discardableResult
    public static func removeStale(in folder: URL, keeping: [URL], now: Date = Date(), days: Int = ReferenceImages.keepDays) -> [URL] {
        let fileManager = FileManager.default
        let keys: [URLResourceKey] = [.contentModificationDateKey, .isRegularFileKey]
        guard let files = try? fileManager.contentsOfDirectory(
            at: folder, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles, .skipsSubdirectoryDescendants]
        ) else { return [] }
        let cutoff = now.addingTimeInterval(-Double(days) * 86_400)
        let kept = Set(keeping.map { $0.standardizedFileURL.path })
        var removed: [URL] = []
        for file in files where isPicture(file) && !kept.contains(file.standardizedFileURL.path) {
            guard let values = try? file.resourceValues(forKeys: Set(keys)),
                  values.isRegularFile == true,
                  let modified = values.contentModificationDate, modified < cutoff else { continue }
            if (try? fileManager.removeItem(at: file)) != nil {
                removed.append(file)
            }
        }
        return removed
    }
}

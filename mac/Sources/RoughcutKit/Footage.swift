import Foundation

/// What a footage file is, by its extension. The lists are the engine's (`roughcut/media.py`: VIDEO_EXTS, AUDIO_EXTS,
/// IMAGE_EXTS); `Fixtures/media-extensions.json` is read by `tests/test_media.py` and by the Swift tests, so the two agree.
public enum MediaKind: String, CaseIterable, Sendable {
    case video, audio, still

    public static let videoExtensions: Set<String> = ["3gp", "avi", "insv", "m2ts", "m4v", "mov", "mp4", "mts", "mxf"]
    public static let audioExtensions: Set<String> = ["aac", "aif", "aiff", "bwf", "caf", "m4a", "mp3", "wav"]
    public static let stillExtensions: Set<String> = ["bmp", "gif", "heic", "heif", "jpeg", "jpg", "png", "psd", "tif", "tiff"]

    /// The kind of file `url` names, or nil for anything the engine doesn't take as footage.
    public static func of(_ url: URL) -> MediaKind? {
        let ext = url.pathExtension.lowercased()
        if videoExtensions.contains(ext) { return .video }
        if audioExtensions.contains(ext) { return .audio }
        if stillExtensions.contains(ext) { return .still }
        return nil
    }

    /// Folders the engine's scan never goes into (`SKIP_DIR_SUFFIXES`): libraries and bundles that aren't source media.
    public static let skippedFolderSuffixes = [".fcpbundle", ".photoslibrary", ".imovielibrary", ".app", ".fcpxmld"]
}

/// How much footage a source holds: counted from the file names alone (no probing), so it can be shown the moment a folder is dropped.
public struct FootageCount: Equatable, Sendable {
    public var videos: Int
    public var recordings: Int
    public var stills: Int
    /// The walk stopped at its file limit or its time limit: there are at least this many.
    public var partial: Bool

    public init(videos: Int = 0, recordings: Int = 0, stills: Int = 0, partial: Bool = false) {
        self.videos = videos
        self.recordings = recordings
        self.stills = stills
        self.partial = partial
    }

    public var total: Int { videos + recordings + stills }
    public var isEmpty: Bool { total == 0 }

    public mutating func add(_ kind: MediaKind) {
        switch kind {
        case .video: videos += 1
        case .audio: recordings += 1
        case .still: stills += 1
        }
    }

    public static func + (lhs: FootageCount, rhs: FootageCount) -> FootageCount {
        FootageCount(
            videos: lhs.videos + rhs.videos, recordings: lhs.recordings + rhs.recordings, stills: lhs.stills + rhs.stills,
            partial: lhs.partial || rhs.partial
        )
    }

    /// "14 videos, 2 stills and 1 recording", "No video, audio or stills found" when there is nothing. A walk that stopped early says "at least".
    public var sentence: String {
        var parts: [String] = []
        if videos > 0 { parts.append("\(videos) video\(videos == 1 ? "" : "s")") }
        if stills > 0 { parts.append("\(stills) still\(stills == 1 ? "" : "s")") }
        if recordings > 0 { parts.append("\(recordings) recording\(recordings == 1 ? "" : "s")") }
        guard !parts.isEmpty else { return "No video, audio or stills found" }
        let joined: String
        if parts.count == 1 {
            joined = parts[0]
        } else {
            joined = parts.dropLast().joined(separator: ", ") + " and " + parts[parts.count - 1]
        }
        return partial ? "At least " + joined : joined
    }

    /// For under the Footage station: "17 files".
    public var shortSummary: String {
        "\(partial ? "At least " : "")\(total) file\(total == 1 ? "" : "s")"
    }

    /// Counts what `source` holds: a file is one of its kind (or nothing), a folder is walked the way the engine's scan walks it
    /// (subfolders included; hidden files and folders, and library bundles, left out). Gives up after `limit` entries or at
    /// `deadline`, and says so (`partial`). A path that isn't there counts as nothing.
    public static func count(source: URL, limit: Int = 20_000, deadline: Date) -> FootageCount {
        var count = FootageCount()
        let manager = FileManager.default
        var isFolder: ObjCBool = false
        guard manager.fileExists(atPath: source.path, isDirectory: &isFolder) else { return count }
        if !isFolder.boolValue {
            if !source.lastPathComponent.hasPrefix("."), let kind = MediaKind.of(source) {
                count.add(kind)
            }
            return count
        }
        guard let walker = manager.enumerator(
            at: source, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles, .skipsPackageDescendants]
        ) else { return count }
        var seen = 0
        while let item = walker.nextObject() as? URL {
            seen += 1
            if seen > limit || (seen % 64 == 0 && Date() > deadline) {
                count.partial = true
                break
            }
            let isDirectory = (try? item.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
            if isDirectory {
                if MediaKind.skippedFolderSuffixes.contains(where: { item.lastPathComponent.hasSuffix($0) }) {
                    walker.skipDescendants()
                }
                continue
            }
            if let kind = MediaKind.of(item) {
                count.add(kind)
            }
        }
        return count
    }
}

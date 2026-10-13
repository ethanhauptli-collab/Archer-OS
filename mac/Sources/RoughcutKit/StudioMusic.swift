import Foundation

/// One row of `roughcut studio music --json`: Venture Studio audio a music bed can use, as Studio's catalog
/// snapshot lists it (read only; nothing on its library folders is touched to list it).
public struct StudioSong: Decodable, Equatable, Identifiable, Sendable {
    /// Studio's item id (`--music studio:ID` in Terminal); nil for an item of an exported kit that has none.
    public var studioID: Int?
    public var name: String
    public var file: String
    /// The original file's absolute path, as Studio has it: what goes on the Music bed.
    public var path: String
    public var library: String
    /// Studio's measured length, or nil when it hasn't measured the file yet.
    public var seconds: Double?
    /// "3:04", or "?" for a length Studio doesn't know.
    public var length: String
    public var provider: String
    public var license: String
    public var favorite: Bool
    public var tags: [String]
    public var notes: String
    /// The Studio collections it's in.
    public var collections: [String]
    /// "music", "sfx" or "unknown": a known length decides (45 s and longer: a song), else its name, folders and tags.
    public var guess: String
    /// Its type is one Roughcut reads (Studio also lists .flac and .ogg, which it doesn't).
    public var usable: Bool
    public var whyNot: String?
    /// Where the list is from: "the catalog snapshot from 12 Oct 21:04 (14 h old)", or a kit.
    public var asOf: String

    public var id: String { path }
    public var url: URL { URL(fileURLWithPath: path) }

    enum CodingKeys: String, CodingKey {
        case studioID = "id"
        case name, file, path, library, seconds, length, provider, license, favorite, tags, notes, collections, guess, usable, whyNot, asOf
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Only the path is required: a field a later CLI drops or adds never loses the list.
        path = try c.decode(String.self, forKey: .path)
        studioID = (try? c.decodeIfPresent(Int.self, forKey: .studioID)) ?? nil
        file = ((try? c.decodeIfPresent(String.self, forKey: .file)) ?? nil) ?? URL(fileURLWithPath: path).lastPathComponent
        name = ((try? c.decodeIfPresent(String.self, forKey: .name)) ?? nil) ?? file
        library = ((try? c.decodeIfPresent(String.self, forKey: .library)) ?? nil) ?? ""
        seconds = (try? c.decodeIfPresent(Double.self, forKey: .seconds)) ?? nil
        length = ((try? c.decodeIfPresent(String.self, forKey: .length)) ?? nil) ?? "?"
        provider = ((try? c.decodeIfPresent(String.self, forKey: .provider)) ?? nil) ?? ""
        license = ((try? c.decodeIfPresent(String.self, forKey: .license)) ?? nil) ?? ""
        favorite = ((try? c.decodeIfPresent(Bool.self, forKey: .favorite)) ?? nil) ?? false
        tags = ((try? c.decodeIfPresent([String].self, forKey: .tags)) ?? nil) ?? []
        notes = ((try? c.decodeIfPresent(String.self, forKey: .notes)) ?? nil) ?? ""
        collections = ((try? c.decodeIfPresent([String].self, forKey: .collections)) ?? nil) ?? []
        guess = ((try? c.decodeIfPresent(String.self, forKey: .guess)) ?? nil) ?? "unknown"
        usable = ((try? c.decodeIfPresent(Bool.self, forKey: .usable)) ?? nil) ?? MusicFiles.isAudio(URL(fileURLWithPath: path))
        whyNot = (try? c.decodeIfPresent(String.self, forKey: .whyNot)) ?? nil
        asOf = ((try? c.decodeIfPresent(String.self, forKey: .asOf)) ?? nil) ?? ""
    }

    /// Whether `search` (every word, any case) is in its name, file, tags, notes, provider or collections.
    public func matches(_ search: String) -> Bool {
        let words = search.lowercased().split(whereSeparator: { $0.isWhitespace })
        guard !words.isEmpty else { return true }
        let text = ([name, file, provider, license, notes] + tags + collections).joined(separator: " ").lowercased()
        return words.allSatisfy { text.contains($0) }
    }
}

/// `roughcut studio music --json`, and what the Music bed accepts.
public enum StudioMusic {
    /// The command's arguments: every song (the sheet filters them itself), as a JSON array.
    public static func arguments() -> [String] {
        ["studio", "music", "--json"]
    }

    /// The whole stdout of `roughcut studio music --json` (one line: a JSON array). Nil when it isn't one.
    public static func songs(from output: String) -> [StudioSong]? {
        guard let start = output.firstIndex(of: "["), let data = String(output[start...]).data(using: .utf8) else { return nil }
        return try? JSONLines.decoder().decode([StudioSong].self, from: data)
    }

    /// The sheet's list: songs in `collection` ("" for all) matching `search`, favorites first if asked, sorted by name.
    public static func filter(_ songs: [StudioSong], search: String, collection: String = "", favoritesOnly: Bool = false, songsOnly: Bool = false) -> [StudioSong] {
        songs.filter { song in
            (collection.isEmpty || song.collections.contains(collection))
                && (!favoritesOnly || song.favorite)
                && (!songsOnly || song.guess != "sfx")
                && song.matches(search)
        }
        .sorted { ($0.name.lowercased(), $0.path) < ($1.name.lowercased(), $1.path) }
    }

    /// The collections the songs are in, for the sheet's filter.
    public static func collections(in songs: [StudioSong]) -> [String] {
        Array(Set(songs.flatMap(\.collections))).sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }
}

/// Which files can go on the Music bed: the audio types Roughcut reads (`media.AUDIO_EXTS`; a Python test keeps them the same).
public enum MusicFiles {
    public static let extensions: [String] = ["aac", "aif", "aiff", "bwf", "caf", "m4a", "mp3", "wav"]

    public static func isAudio(_ url: URL) -> Bool {
        extensions.contains(url.pathExtension.lowercased())
    }

    /// Dropped or pasted items split into songs (file URLs of a type Roughcut reads) and the names of everything else.
    public static func sort(_ urls: [URL]) -> (songs: [URL], leftOut: [String]) {
        var songs: [URL] = []
        var leftOut: [String] = []
        for url in urls {
            if url.isFileURL && isAudio(url) {
                let clean = url.standardizedFileURL
                if !songs.contains(clean) { songs.append(clean) }
            } else {
                leftOut.append(url.isFileURL ? url.lastPathComponent : url.absoluteString)
            }
        }
        return (songs, leftOut)
    }

    /// Pasted text that names one file by its absolute path (a "Copy file path" from Studio or Finder), or nil.
    public static func url(fromPastedText text: String) -> URL? {
        var path = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if path.count >= 2, let first = path.first, first == path.last, first == "'" || first == "\"" {
            path = String(path.dropFirst().dropLast())
        }
        guard !path.isEmpty, !path.contains("\n") else { return nil }
        if path.hasPrefix("file://"), let url = URL(string: path), url.isFileURL {
            return url.standardizedFileURL
        }
        if path.hasPrefix("~/") {
            path = NSString(string: path).expandingTildeInPath
        }
        guard path.hasPrefix("/") else { return nil }
        return URL(fileURLWithPath: path).standardizedFileURL
    }

    /// The note shown under the Music bed for items that went nowhere.
    public static func leftOutNote(_ names: [String]) -> String? {
        guard !names.isEmpty else { return nil }
        let shown = names.prefix(3).joined(separator: ", ") + (names.count > 3 ? " and \(names.count - 3) more" : "")
        return "Not added: \(shown). The Music bed takes song files Roughcut reads (\(extensions.joined(separator: ", "))); footage goes in Footage above."
    }
}

/// What a short CLI command printed, collected to the end or until its time ran out.
public struct CollectedOutput: Equatable, Sendable {
    public var stdout: [String] = []
    public var stderr: [String] = []
    /// Nil when it never started (`launchError`).
    public var status: Int32?
    /// It was stopped because it ran past its time.
    public var timedOut = false
    /// It was stopped by `cancel()` (or its time ran out).
    public var stopped = false
    public var launchError: String?

    public init() {}

    public var output: String { stdout.joined(separator: "\n") }
    /// What to show when it didn't work: its last error lines, or why it stopped.
    public var problem: String? {
        if let launchError { return "Couldn't start roughcut: \(launchError)" }
        if timedOut { return "roughcut didn't answer in time." }
        if stopped { return nil }
        guard let status, status != 0 else { return nil }
        let said = stderr.suffix(3).joined(separator: "\n").replacingOccurrences(of: "roughcut: ", with: "")
        return said.isEmpty ? "roughcut stopped (exit code \(status))." : said
    }
}

/// Set once, read from any thread.
final class OnceFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

extension CLIProcess {
    /// Runs to the end and returns what it printed. After `timeout` seconds it's stopped (`timedOut`); `cancel()`
    /// stops it sooner. The output is read off the main thread; awaiting this never blocks the caller's thread.
    public func collect(timeout: TimeInterval) async -> CollectedOutput {
        let expired = OnceFlag()
        let process = self
        let timer = Task.detached {
            try? await Task.sleep(nanoseconds: UInt64(max(0, timeout) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            expired.set()
            process.cancel()
        }
        defer { timer.cancel() }
        var result = CollectedOutput()
        for await output in run() {
            switch output {
            case .stdout(let line):
                result.stdout.append(line)
            case .event(let event):
                result.stdout.append(event.message ?? "")
            case .stderr(let line):
                result.stderr.append(line)
            case .failedToLaunch(let message):
                result.launchError = message
            case .exited(let status, let signaled):
                result.status = status
                result.stopped = signaled
            }
        }
        result.timedOut = expired.isSet
        if result.timedOut { result.stopped = true }
        return result
    }
}

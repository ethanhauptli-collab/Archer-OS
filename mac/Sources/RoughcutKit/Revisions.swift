import Foundation

/// A preset from `roughcut preset list --json`: a brand's or channel's usual settings.
public struct Preset: Decodable, Equatable, Identifiable, Sendable {
    public var name: String
    public var description: String?
    public var brief: String?
    public var target: String?
    public var style: String?
    public var format: String?
    public var graphics: Bool?
    public var graphicsStyle: String?
    public var logo: String?
    public var look: [String]?
    /// "built in" or where the user's copy is saved.
    public var source: String?

    public var id: String { name }

    public var title: String {
        let words = name.split(separator: "-").map { $0.prefix(1).uppercased() + $0.dropFirst() }
        let pretty = words.joined(separator: " ").replacingOccurrences(of: "Oc", with: "OC")
        return (description?.isEmpty == false) ? "\(pretty): \(description!)" : pretty
    }

    /// The form filled from this preset. A brief already typed is kept after the preset's
    /// unless it was another preset's brief (`replacing`).
    public func apply(to options: inout BuildOptions, replacing previousBriefs: [String] = []) {
        options.presetName = name
        let typed = options.brief.trimmed
        let presetBrief = (brief ?? "").trimmed
        if typed.isEmpty || previousBriefs.map(\.trimmed).contains(typed) {
            options.brief = presetBrief
        } else if !presetBrief.isEmpty && !typed.hasPrefix(presetBrief) {
            options.brief = presetBrief + "\n\n" + typed
        }
        options.targetLength = target ?? ""
        options.style = style.flatMap(CutStyle.init(rawValue:)) ?? .medium
        options.format = format.flatMap(SequenceFormat.init(rawValue:)) ?? .auto
        options.graphics = graphics ?? false
        options.graphicsStyle = graphicsStyle ?? ""
        options.graphicsLogo = logo ?? ""
        options.graphicsLook = look ?? []
    }
}

extension JSONLines {
    /// The whole stdout of `roughcut preset list --json`.
    public static func presets(from output: String) -> [Preset]? {
        guard let start = output.firstIndex(of: "["), let data = String(output[start...]).data(using: .utf8) else { return nil }
        return try? decoder().decode([Preset].self, from: data)
    }
}

/// `roughcut revise`: re-plan the latest cut from editor notes, as a new version.
public struct ReviseOptions: Equatable, Sendable {
    public var outputFolder: URL
    /// The notes, written to a file so text starting with "-" can't be read as a flag.
    public var notesFile: URL
    /// New target length; empty keeps the build's.
    public var targetLength: String

    public init(outputFolder: URL, notesFile: URL, targetLength: String = "") {
        self.outputFolder = outputFolder
        self.notesFile = notesFile
        self.targetLength = targetLength
    }

    public func arguments() -> [String] {
        var args = ["revise", outputFolder.path, "--progress-json", "--notes-file=\(notesFile.path)"]
        if let seconds = TimeText.seconds(from: targetLength) {
            args.append("--target=\(Int(seconds.rounded()))")
        }
        return args
    }
}

/// The output folder in an `error` event's `resume` command (`roughcut render '<folder>'`).
public enum ResumeCommand {
    public static func outputFolder(from command: String?) -> URL? {
        guard let command, let words = shellWords(command), words.count >= 3,
              words[0].hasSuffix("roughcut"), words[1] == "render" else { return nil }
        return URL(fileURLWithPath: words[2], isDirectory: true)
    }

    /// POSIX shell words: single quotes, double quotes and backslashes (what Python's shlex.quote produces).
    static func shellWords(_ text: String) -> [String]? {
        var words: [String] = []
        var current = ""
        var inWord = false
        var quote: Character? = nil
        var escaped = false
        for ch in text {
            if escaped {
                current.append(ch)
                escaped = false
                continue
            }
            if let q = quote {
                if ch == q {
                    quote = nil
                } else if ch == "\\" && q == "\"" {
                    escaped = true
                } else {
                    current.append(ch)
                }
                continue
            }
            switch ch {
            case "'", "\"":
                quote = ch
                inWord = true
            case "\\":
                escaped = true
                inWord = true
            case " ", "\t", "\n":
                if inWord {
                    words.append(current)
                    current = ""
                    inWord = false
                }
            default:
                current.append(ch)
                inWord = true
            }
        }
        if quote != nil || escaped { return nil }
        if inWord { words.append(current) }
        return words
    }
}

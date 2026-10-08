import Foundation

/// How hard pauses are trimmed (`--style`).
public enum CutStyle: String, CaseIterable, Codable, Identifiable, Sendable {
    case tight, medium, loose

    public var id: String { rawValue }
    public var title: String { rawValue.capitalized }

    public var detail: String {
        switch self {
        case .tight: return "Pauses trimmed hard. Punchy YouTube pace."
        case .medium: return "Natural pauses up to about half a second."
        case .loose: return "Room to breathe. Good for reflective pieces."
        }
    }
}

/// Sequence format (`--format`). `.auto` matches the footage.
public enum SequenceFormat: String, CaseIterable, Codable, Identifiable, Sendable {
    case auto
    case hd = "1080p"
    case uhd = "4k"
    case vertical
    case square

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .auto: return "Match footage"
        case .hd: return "1080p"
        case .uhd: return "4K"
        case .vertical: return "Vertical 9:16"
        case .square: return "Square 1:1"
        }
    }

    public var cliValue: String? { self == .auto ? nil : rawValue }
}

/// Who plans the edit (`--provider`).
public enum ModelProvider: String, CaseIterable, Codable, Identifiable, Sendable {
    case anthropic
    case claudeCode = "claude-code"
    case openai
    case ollama
    case openaiCompatible = "openai-compatible"
    case none

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .anthropic: return "Claude (API key)"
        case .claudeCode: return "Claude (your Pro/Max plan, via Claude Code)"
        case .openai: return "OpenAI"
        case .ollama: return "Ollama (on this Mac)"
        case .openaiCompatible: return "OpenAI-compatible server"
        case .none: return "None: just cut the silence"
        }
    }

    /// The CLI has no default model for these providers.
    public var requiresModel: Bool { self == .openai || self == .ollama || self == .openaiCompatible }
    public var usesBaseURL: Bool { self == .openaiCompatible }
    /// Claude through the API or through Claude Code: same models, same menus.
    public var usesClaudeModels: Bool { self == .anthropic || self == .claudeCode }

    public var modelPlaceholder: String {
        switch self {
        case .anthropic, .claudeCode: return "claude-opus-5-5 (default)"
        case .ollama: return "e.g. a model you've pulled in Ollama"
        default: return "model name"
        }
    }

    /// Environment variable the CLI reads the key from, if any.
    public var apiKeyVariable: String? {
        switch self {
        case .anthropic: return "ANTHROPIC_API_KEY"
        case .openai, .openaiCompatible: return "OPENAI_API_KEY"
        case .claudeCode, .ollama, .none: return nil  // Claude Code uses your subscription sign-in
        }
    }
}

/// Claude models offered in the app's menus. An empty id means the CLI default (Opus).
public struct ClaudeModelChoice: Identifiable, Hashable, Sendable {
    public let id: String
    public let title: String

    public static let planners: [ClaudeModelChoice] = [
        ClaudeModelChoice(id: "", title: "Claude Opus 5.5 (best judgment)"),
        ClaudeModelChoice(id: "claude-sonnet-5-5", title: "Claude Sonnet 5.5 (faster, half the price)"),
    ]

    /// For describing clips; most of a big shoot's tokens are spent here.
    public static let describers: [ClaudeModelChoice] = [
        ClaudeModelChoice(id: "", title: "Same as above"),
        ClaudeModelChoice(id: "claude-sonnet-5-5", title: "Claude Sonnet 5.5 (cheaper for big shoots)"),
    ]
}

/// Reasoning effort for Claude (`--effort`).
public enum Effort: String, CaseIterable, Codable, Identifiable, Sendable {
    case low, medium, high, xhigh, max
    public var id: String { rawValue }
    public var title: String { self == .xhigh ? "Extra high" : rawValue.capitalized }
}

/// Everything the main window collects for `roughcut build`.
public struct BuildOptions: Codable, Equatable, Sendable {
    public var sources: [URL] = []
    public var outputFolder: URL? = nil
    public var projectName: String = ""
    public var brief: String = ""
    public var targetLength: String = ""
    public var style: CutStyle = .medium
    public var format: SequenceFormat = .auto
    public var provider: ModelProvider = .anthropic
    public var model: String = ""
    /// Model for clip descriptions; empty = same as `model`.
    public var visionModel: String = ""
    public var baseURL: String = ""
    public var effort: Effort = .high
    public var vision: Bool = true
    public var keepFillers: Bool = false
    public var stringout: Bool = true
    public var language: String = ""

    public init() {}

    /// Reasons the build can't start yet, in the order the user should fix them.
    public var problems: [String] {
        var list: [String] = []
        if sources.isEmpty {
            list.append("Add footage: drop folders or clips above.")
        }
        let target = targetLength.trimmed
        if !target.isEmpty && TimeText.seconds(from: target) == nil {
            list.append("Target length should look like 8m, 90s or 8:30.")
        }
        if provider.requiresModel && model.trimmed.isEmpty {
            list.append("Enter a model name for \(provider.title).")
        }
        if provider.usesBaseURL && baseURL.trimmed.isEmpty {
            list.append("Enter the server URL, e.g. http://localhost:1234/v1.")
        }
        return list
    }

    /// Arguments for the `roughcut` executable. Free-text values use the
    /// `--flag=value` form so a brief that starts with "-" can't be read as a flag.
    public func arguments() -> [String] {
        var args = ["build"]
        args += sources.map { $0.path }
        args.append("--progress-json")
        if let outputFolder {
            args.append("--out=\(outputFolder.path)")
        }
        if !projectName.trimmed.isEmpty {
            args.append("--name=\(projectName.trimmed)")
        }
        if !brief.trimmed.isEmpty {
            args.append("--context=\(brief.trimmed)")
        }
        if let seconds = TimeText.seconds(from: targetLength) {
            // Send plain seconds: the app's parser decided it's valid, so the
            // CLI never sees a spelling it reads differently.
            args.append("--target=\(Int(seconds.rounded()))")
        }
        args += ["--style", style.rawValue]
        if let format = format.cliValue {
            args += ["--format", format]
        }
        args += ["--provider", provider.rawValue]
        if provider != .none {
            if !model.trimmed.isEmpty {
                args.append("--model=\(model.trimmed)")
            }
            if provider.usesClaudeModels {
                args += ["--effort", effort.rawValue]
            }
            if provider.usesBaseURL {
                args.append("--base-url=\(baseURL.trimmed)")
                args.append("--api-key-env=OPENAI_API_KEY")
            }
            args.append(vision ? "--vision" : "--no-vision")
            if vision && !visionModel.trimmed.isEmpty {
                args.append("--vision-model=\(visionModel.trimmed)")
            }
        }
        if keepFillers {
            args.append("--keep-fillers")
        }
        if !stringout {
            args.append("--no-stringout")
        }
        if !language.trimmed.isEmpty {
            args.append("--language=\(language.trimmed)")
        }
        return args
    }

    /// A copy without the per-project fields, for remembering preferences.
    public var preferencesOnly: BuildOptions {
        var copy = self
        copy.sources = []
        copy.outputFolder = nil
        copy.projectName = ""
        copy.brief = ""
        copy.targetLength = ""
        return copy
    }
}

/// `roughcut render`: rebuild a finished run with a different pace or format,
/// without calling the model again.
public struct RecutOptions: Equatable, Sendable {
    public var outputFolder: URL
    public var style: CutStyle
    public var format: SequenceFormat
    public var keepFillers: Bool
    public var stringout: Bool

    public init(outputFolder: URL, style: CutStyle, format: SequenceFormat, keepFillers: Bool = false, stringout: Bool = true) {
        self.outputFolder = outputFolder
        self.style = style
        self.format = format
        self.keepFillers = keepFillers
        self.stringout = stringout
    }

    public func arguments() -> [String] {
        var args = ["render", outputFolder.path, "--progress-json", "--style", style.rawValue]
        if let format = format.cliValue {
            args += ["--format", format]
        }
        if keepFillers {
            args.append("--keep-fillers")
        }
        if !stringout {
            args.append("--no-stringout")
        }
        return args
    }
}

/// Mirrors the CLI's `parse_duration` so the form can validate as you type.
public enum TimeText {
    /// "8m", "90s", "8:30", "1h5m", "480" -> seconds. Nil when unreadable or zero.
    public static func seconds(from text: String) -> Double? {
        let raw = text.trimmed.lowercased()
        guard !raw.isEmpty else { return nil }
        if raw.contains(":") {
            var total = 0.0
            for part in raw.split(separator: ":", omittingEmptySubsequences: false) {
                guard let value = Double(part) else { return nil }
                total = total * 60 + value
            }
            return total > 0 ? total : nil
        }
        var total = 0.0
        var number = ""
        var index = raw.startIndex
        while index < raw.endIndex {
            let char = raw[index]
            if char.isNumber || char == "." {
                number.append(char)
            } else if char == " " {
                // ignore spacing like "8 m"
            } else {
                guard let value = Double(number) else { return nil }
                let rest = raw[index...]
                if rest.hasPrefix("min") {
                    total += value * 60
                    index = raw.index(index, offsetBy: 3)
                    number = ""
                    continue
                }
                switch char {
                case "h": total += value * 3600
                case "m": total += value * 60
                case "s": total += value
                default: return nil
                }
                number = ""
            }
            index = raw.index(after: index)
        }
        if !number.isEmpty {
            guard let value = Double(number) else { return nil }
            total += value
        }
        return total > 0 ? total : nil
    }

    /// 483 -> "8:03", 3725 -> "1:02:05".
    public static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, s)
        }
        return String(format: "%d:%02d", m, s)
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

import Foundation

/// How hard pauses are trimmed (`--style`). `.auto` lets the CLI take it from the planner's reading of the brief.
public enum CutStyle: String, CaseIterable, Codable, Identifiable, Sendable {
    case auto, tight, medium, loose

    public var id: String { rawValue }
    public var title: String { rawValue.capitalized }

    /// What Re-cut offers: a cut's pacing is always one of these.
    public static let recut: [CutStyle] = [.tight, .medium, .loose]

    public var detail: String {
        switch self {
        case .auto: return "As Claude reads the brief, your words before the preset's: tight for tight cuts, snappy or no dead air; loose for let it breathe or slow and reflective; otherwise medium. Without AI, from its wording."
        case .tight: return "Pauses trimmed hard. Punchy YouTube pace."
        case .medium: return "Natural pauses up to about half a second."
        case .loose: return "Room to breathe. Good for reflective pieces."
        }
    }
}

/// Punch-ins on jump cuts (`--punch-in` / `--no-punch-in`). `.auto`: on when the planner reads the brief asking for them.
public enum PunchIn: String, CaseIterable, Codable, Identifiable, Sendable {
    case auto, on, off

    public var id: String { rawValue }
    public var title: String { rawValue.capitalized }
}

/// Sequence format (`--format`). `.auto` matches the footage. `.custom` is any other format the
/// CLI reads (`timeline.parse_format`), e.g. a preset saved in Terminal with 720p or 1080x1920@29.97.
public enum SequenceFormat: Hashable, Codable, Identifiable, Sendable {
    case auto
    case hd
    case uhd
    case vertical
    case square
    case custom(String)

    /// The formats the pickers always list.
    public static let standard: [SequenceFormat] = [.auto, .hd, .uhd, .vertical, .square]

    /// A picker's rows: the standard formats, plus `current` when it's a custom one (so it stays selectable).
    public static func choices(including current: SequenceFormat?) -> [SequenceFormat] {
        guard let current, !standard.contains(current) else { return standard }
        return standard + [current]
    }

    /// Reads a format the way the CLI does: any case, "uhd" is 4K, and "auto" (or nothing) is
    /// the footage's own format. Anything else is kept as written and passed on for the CLI to read.
    public init(cliText: String?) {
        let text = (cliText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        switch text.lowercased() {
        case "", "auto", "footage", "from footage": self = .auto
        case "1080p": self = .hd
        case "4k", "uhd": self = .uhd
        case "vertical": self = .vertical
        case "square": self = .square
        default: self = .custom(text)
        }
    }

    /// The CLI's spelling ("auto" is the footage's own format).
    public var rawValue: String {
        switch self {
        case .auto: return "auto"
        case .hd: return "1080p"
        case .uhd: return "4k"
        case .vertical: return "vertical"
        case .square: return "square"
        case .custom(let text): return text
        }
    }

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .auto: return "Match footage"
        case .hd: return "1080p"
        case .uhd: return "4K"
        case .vertical: return "Vertical 9:16"
        case .square: return "Square 1:1"
        case .custom(let text): return text
        }
    }

    /// Nil for `.auto`: a build leaves the format to the footage.
    public var cliValue: String? { self == .auto ? nil : rawValue }

    /// `--format` and its value. Text from a preset file goes as `--format=…`, so it can't be read as another flag.
    public var arguments: [String] {
        if case .custom(let text) = self {
            return ["--format=\(text)"]
        }
        return ["--format", rawValue]
    }

    // Saved preferences hold the CLI spelling ("vertical", "1080p"...), as this type's earlier raw values did.
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        self.init(cliText: text)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
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

/// How loud speech plays after level matching (`--dialogue-level`).
public enum DialogueLevel: String, CaseIterable, Codable, Identifiable, Sendable {
    case standard = "-16"
    case louder = "-14"
    case off

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .standard: return "Matched at -16 LUFS (YouTube, most web)"
        case .louder: return "Matched at -14 LUFS (louder, for phones)"
        case .off: return "Off: keep each clip's own level"
        }
    }
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
    public var style: CutStyle = .auto
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
    /// Claude-designed motion graphics rendered above the footage (`--graphics`).
    public var graphics: Bool = false
    /// Look for the graphics: brand colors, fonts, mood (`--graphics-style`).
    public var graphicsStyle: String = ""
    /// The app opens the new project in Final Cut Pro as soon as a run finishes.
    public var openInFinalCutWhenDone: Bool = false
    /// The preset the form was last filled from (shown in the picker; never sent: the form holds its values).
    public var presetName: String = ""
    /// That preset's brief, which the form's brief starts with. Sent apart too (`--preset-brief`) so pacing and
    /// punch-ins follow what you typed after it first. Not remembered: the preset fills it in again.
    public var presetBrief: String = ""
    /// Songs laid under the cut (`--music`), not transcribed.
    public var music: [URL] = []
    public var dialogueLevel: DialogueLevel = .standard
    /// De-click fades at audio edits and soft fades on B-roll nat sound (`--fades`).
    public var fades: Bool = true
    /// Alternate a punch-in at each jump cut no B-roll covers. Auto: when the brief asks for it.
    public var punchIn: PunchIn = .auto
    /// A logo file graphics may show (`--graphics-logo`).
    public var graphicsLogo: String = ""
    /// Approved graphics (.html) new designs match (`--graphics-look`).
    public var graphicsLook: [String] = []

    public init() {}

    enum CodingKeys: String, CodingKey {
        case sources, outputFolder, projectName, brief, targetLength, format, provider, model, visionModel
        case style = "pacing"
        case baseURL, effort, vision, keepFillers, stringout, language, graphics, graphicsStyle, openInFinalCutWhenDone
        case presetName, music, dialogueLevel, fades, graphicsLogo, graphicsLook, punchIn
    }

    /// How apps from before Auto pacing saved it.
    enum LegacyKeys: String, CodingKey {
        case style
    }

    /// Saved preferences from an older version of the app are missing newer
    /// fields (and may hold values this version dropped): keep everything that
    /// still reads, default the rest, instead of losing all of them.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func value<T: Decodable>(_ key: CodingKeys, _ fallback: T) -> T {
            ((try? c.decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
        }
        let d = BuildOptions()
        sources = value(.sources, d.sources)
        outputFolder = value(.outputFolder, d.outputFolder)
        projectName = value(.projectName, d.projectName)
        brief = value(.brief, d.brief)
        targetLength = value(.targetLength, d.targetLength)
        if let pacing = (try? c.decodeIfPresent(CutStyle.self, forKey: .style)) ?? nil {
            style = pacing
        } else {
            // Saved before Auto pacing existed, when Medium was the default: Medium there means "not chosen".
            let legacy = try? decoder.container(keyedBy: LegacyKeys.self)
            let old: CutStyle? = (try? legacy?.decodeIfPresent(CutStyle.self, forKey: .style)) ?? nil
            style = old.flatMap { $0 == .medium ? nil : $0 } ?? d.style
        }
        format = value(.format, d.format)
        provider = value(.provider, d.provider)
        model = value(.model, d.model)
        visionModel = value(.visionModel, d.visionModel)
        baseURL = value(.baseURL, d.baseURL)
        effort = value(.effort, d.effort)
        vision = value(.vision, d.vision)
        keepFillers = value(.keepFillers, d.keepFillers)
        stringout = value(.stringout, d.stringout)
        language = value(.language, d.language)
        graphics = value(.graphics, d.graphics)
        graphicsStyle = value(.graphicsStyle, d.graphicsStyle)
        openInFinalCutWhenDone = value(.openInFinalCutWhenDone, d.openInFinalCutWhenDone)
        presetName = value(.presetName, d.presetName)
        music = value(.music, d.music)
        dialogueLevel = value(.dialogueLevel, d.dialogueLevel)
        fades = value(.fades, d.fades)
        graphicsLogo = value(.graphicsLogo, d.graphicsLogo)
        graphicsLook = value(.graphicsLook, d.graphicsLook)
        punchIn = value(.punchIn, d.punchIn)
    }

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
        if graphics && provider == .none {
            list.append("Motion graphics are designed by the AI: choose a provider other than None.")
        }
        if let missing = music.first(where: { !FileManager.default.fileExists(atPath: $0.path) }) {
            list.append("The music file \(missing.lastPathComponent) isn't there any more: remove it or pick it again.")
        }
        if graphics && provider != .none {
            // Remembered from a preset or an earlier session: the CLI would stop on a missing file after launch.
            let logo = graphicsLogo.trimmed
            if !logo.isEmpty && !Self.fileExists(logo) {
                list.append("The logo \(URL(fileURLWithPath: logo).lastPathComponent) isn't there any more: clear it or choose it again.")
            }
            if let missing = graphicsLook.first(where: { !Self.fileExists($0) }) {
                list.append("The approved graphic \(URL(fileURLWithPath: missing).lastPathComponent) from an earlier video isn't there any more: click Don't match.")
            }
        }
        return list
    }

    static func fileExists(_ path: String) -> Bool {
        FileManager.default.fileExists(atPath: NSString(string: path).expandingTildeInPath)
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
            let fromPreset = presetBrief.trimmed
            if !presetName.isEmpty && !fromPreset.isEmpty && brief.trimmed.hasPrefix(fromPreset) {
                args.append("--preset-brief=\(fromPreset)")
            }
        }
        if let seconds = TimeText.seconds(from: targetLength) {
            // Send plain seconds: the app's parser decided it's valid, so the
            // CLI never sees a spelling it reads differently.
            args.append("--target=\(Int(seconds.rounded()))")
        }
        args += ["--style", style.rawValue]
        if format != .auto {
            args += format.arguments
        }
        switch punchIn {
        case .on: args.append("--punch-in")
        case .off: args.append("--no-punch-in")
        case .auto: break
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
        args += music.map { "--music=\($0.path)" }
        if dialogueLevel != .standard {
            args.append("--dialogue-level=\(dialogueLevel.rawValue)")
        }
        if !fades {
            args += ["--fades", "off"]
        }
        if graphics && provider != .none {
            args.append("--graphics")
            if !graphicsStyle.trimmed.isEmpty {
                args.append("--graphics-style=\(graphicsStyle.trimmed)")
            }
            if !graphicsLogo.trimmed.isEmpty {
                args.append("--graphics-logo=\(graphicsLogo.trimmed)")
            }
            args += graphicsLook.map { "--graphics-look=\($0)" }
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
        copy.music = []
        return copy
    }
}

/// `roughcut render`: rebuild a finished run with a different pace or format,
/// without calling the model again. A setting left nil keeps the one the cut has
/// (the CLI reads it from the run's plan.json), as `roughcut render` does in Terminal.
public struct RecutOptions: Equatable, Sendable {
    public var outputFolder: URL
    public var style: CutStyle?
    /// `.auto` sends `--format auto`: back to the footage's own format.
    public var format: SequenceFormat?
    public var keepFillers: Bool?
    public var stringout: Bool?
    /// Punch-ins on jump cuts: On or Off, or nil for as the cut has them (an Auto build read them from its brief).
    public var punchIn: Bool?

    public init(outputFolder: URL, style: CutStyle? = nil, format: SequenceFormat? = nil, keepFillers: Bool? = nil, stringout: Bool? = nil, punchIn: Bool? = nil) {
        self.outputFolder = outputFolder
        self.style = style
        self.format = format
        self.keepFillers = keepFillers
        self.stringout = stringout
        self.punchIn = punchIn
    }

    public func arguments() -> [String] {
        var args = ["render", outputFolder.path, "--progress-json"]
        if let style {
            args += ["--style", style.rawValue]
        }
        if let format {
            args += format.arguments
        }
        if let keepFillers {
            args.append(keepFillers ? "--keep-fillers" : "--no-keep-fillers")
        }
        if let stringout {
            args.append(stringout ? "--stringout" : "--no-stringout")
        }
        if let punchIn {
            args.append(punchIn ? "--punch-in" : "--no-punch-in")
        }
        return args
    }

    /// The Re-cut format picker's first row, which keeps the cut's own format.
    public static func sameFormatTitle(_ cut: CutSettings?) -> String {
        guard let cut else { return "Same as this cut" }
        return "Same as this cut (\(cut.sequenceFormat.title))"
    }

    /// The Re-cut punch-ins picker's first row, which keeps them as the cut has them.
    public static func samePunchInTitle(_ cut: CutSettings?) -> String {
        guard let on = cut?.punchIn else { return "Same as this cut" }
        return "Same as this cut (\(on ? "On" : "Off"))"
    }

    /// The pace Re-cut offers first: the other one from the cut's own (tight gets medium, the rest tight).
    public static func suggestedStyle(for cut: CutSettings?, otherwise fallback: CutStyle) -> CutStyle {
        let own = cut?.cutStyle ?? fallback
        return own == .tight ? .medium : .tight
    }
}

/// Mirrors the CLI's `parse_duration` so the form can validate as you type.
public enum TimeText {
    /// Unit words `parse_duration` reads (plus a few spellings it doesn't; the app sends plain seconds).
    static let unitSeconds: [String: Double] = [
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    ]

    /// "8m", "90s", "90 sec", "10 mins", "1hr", "8:30", "1h5m", "480" -> seconds. Nil when unreadable or zero.
    public static func seconds(from text: String) -> Double? {
        let raw = text.trimmed.lowercased()
        guard !raw.isEmpty else { return nil }
        if raw.contains(":") {
            var total = 0.0
            for part in raw.split(separator: ":", omittingEmptySubsequences: false) {
                guard let value = Double(part.trimmingCharacters(in: .whitespaces)) else { return nil }
                total = total * 60 + value
            }
            return total > 0 ? total : nil
        }
        // Number-and-unit pairs ("1h 30m"); a number without a unit is seconds.
        var pairs: [(number: String, unit: String)] = []
        var number = ""
        var unit = ""
        for char in raw {
            if char.isNumber || char == "." {
                if !unit.isEmpty {  // "1h30": the next number starts a new pair
                    pairs.append((number: number, unit: unit))
                    number = ""
                    unit = ""
                }
                number.append(char)
            } else if char.isLetter {
                guard !number.isEmpty else { return nil }  // a unit with no number: "eight minutes"
                unit.append(char)
            } else if char != " " {
                return nil
            }
        }
        if !number.isEmpty {
            pairs.append((number: number, unit: unit))
        }
        var total = 0.0
        for pair in pairs {
            guard let value = Double(pair.number) else { return nil }
            var scale = 1.0
            if !pair.unit.isEmpty {
                guard let known = unitSeconds[pair.unit] else { return nil }
                scale = known
            }
            total += value * scale
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

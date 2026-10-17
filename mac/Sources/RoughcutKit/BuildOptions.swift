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

    /// A model plans the edit. `.none` only cuts the silence: nothing in the build form that needs a model applies to it
    /// (clip descriptions, motion graphics and what hangs off them).
    public var usesAI: Bool { self != .none }
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
    /// Pictures of the look wanted for the graphics (`--graphics-ref`): at most `ReferenceImages.maxCount`, shown to
    /// the graphics designer only. Remembered by path; the ones that have gone aren't restored.
    public var graphicsReferences: [URL] = []
    /// Sound effects for accents (`--sfx`): folders or single sounds. Remembered, like a library you use every time.
    public var sfx: [URL] = []
    /// Venture Studio collections (or kits) to take sound effects from, by name. Remembered, and filled in by a preset.
    public var sfxFrom: [String] = []
    /// The names in `sfxFrom` typed into the form in this session. Only these are sent as `--sfx-from`, which stops the build
    /// when Venture Studio has no such collection; the others (remembered, or a preset's) go as `--preset-sfx-from`, a warning
    /// in the paper edit, so a collection renamed in Studio doesn't block every build. Not remembered.
    public var sfxFromTyped: [String] = []

    public init() {}

    enum CodingKeys: String, CodingKey {
        case sources, outputFolder, projectName, brief, targetLength, format, provider, model, visionModel
        case style = "pacing"
        case baseURL, effort, vision, keepFillers, stringout, language, graphics, graphicsStyle, openInFinalCutWhenDone
        case presetName, music, dialogueLevel, fades, graphicsLogo, graphicsLook, punchIn, graphicsReferences, sfx, sfxFrom
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
        graphicsReferences = ReferenceImages.restoring(value(.graphicsReferences, d.graphicsReferences))
        sfx = value(.sfx, d.sfx)
        sfxFrom = value(.sfxFrom, d.sfxFrom)
        settle()  // preferences saved while graphics were on and the provider was None (the old form allowed it)
    }

    // MARK: Provider and graphics: what the form can hold

    /// Motion graphics are designed by the AI, so with no provider the build can't have them. The form's rules, kept here
    /// (not in the view) so every way into the state (the picker, a preset, saved preferences) goes through the same ones:
    /// graphics are never on with provider None, and choosing None turns them off. The style, logo, approved designs and
    /// reference pictures stay remembered (and are not sent or shown) for when graphics are turned on again.
    public static let graphicsNeedAIHint = "Motion graphics are designed by the AI: choose a provider other than None to use them."

    /// Graphics are part of this build: asked for, and a model is there to design them. What the CLI is told, and what the form shows options for.
    public var graphicsActive: Bool { graphics && provider.usesAI }

    /// The Motion graphics switch can be clicked: to turn graphics on, or off whatever left them on (`settle()` normally prevents that).
    public var graphicsSwitchEnabled: Bool { provider.usesAI || graphics }

    /// The Plans-the-edit picker's choice. A different provider starts from its own defaults: local and compatible
    /// servers are usually text-only models (no clip descriptions), and model names don't carry over. Choosing None turns graphics off.
    public mutating func choose(provider new: ModelProvider) {
        guard new != provider else { return }
        provider = new
        vision = new.usesClaudeModels || new == .openai
        model = ""
        visionModel = ""
        settle()
    }

    /// The Motion graphics switch. It can't turn graphics on without a provider that can design them.
    public mutating func setGraphics(_ on: Bool) {
        graphics = on && provider.usesAI
    }

    /// Takes out what can't hold together: graphics without a model. Called by every way the provider or graphics can change.
    public mutating func settle() {
        if !provider.usesAI {
            graphics = false
        }
    }

    /// Reasons the build can't start yet, in the order the user should fix them.
    public var problems: [String] {
        var list: [String] = []
        if sources.isEmpty {
            list.append("Add footage: drop folders or clips on the first step.")
        }
        let target = targetLength.trimmed
        if !target.isEmpty && TimeText.seconds(from: target) == nil {
            list.append("Target length \(TimeText.shouldLookLike).")
        }
        if provider.requiresModel && model.trimmed.isEmpty {
            list.append("Enter a model name for \(provider.title).")
        }
        if provider.usesBaseURL && baseURL.trimmed.isEmpty {
            list.append("Enter the server URL, e.g. http://localhost:1234/v1.")
        }
        if graphics && !provider.usesAI {
            // Only reachable by setting the fields directly: the form's own controls settle it first.
            list.append("Motion graphics are designed by the AI: choose a provider other than None, or turn motion graphics off.")
        }
        // Not a file on another volume (a network share can take minutes to answer, and this runs on every
        // keystroke): the CLI checks those with a deadline when the build starts.
        if let missing = music.first(where: { !Self.onAnotherVolume($0.path) && !FileManager.default.fileExists(atPath: $0.path) }) {
            list.append("The music file \(missing.lastPathComponent) isn't there any more: remove it or pick it again.")
        }
        if graphicsActive {
            // Remembered from a preset or an earlier session: the CLI would stop on a missing file after launch.
            let logo = graphicsLogo.trimmed
            if !logo.isEmpty && !Self.onAnotherVolume(logo) && !Self.fileExists(logo) {
                list.append("The logo \(URL(fileURLWithPath: logo).lastPathComponent) isn't there any more: clear it or choose it again.")
            }
            if let missing = graphicsLook.first(where: { !Self.onAnotherVolume($0) && !Self.fileExists($0) }) {
                list.append("The approved graphic \(URL(fileURLWithPath: missing).lastPathComponent) from an earlier video isn't there any more: click Don't match.")
            }
        }
        return list
    }

    static func fileExists(_ path: String) -> Bool {
        FileManager.default.fileExists(atPath: NSString(string: path).expandingTildeInPath)
    }

    /// Under /Volumes: an external drive or a network share, which the app never waits on.
    public static func onAnotherVolume(_ path: String) -> Bool {
        path.hasPrefix("/Volumes/")
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
        if let seconds = TimeText.wholeSeconds(from: targetLength) {
            // Send plain seconds: the app's parser decided it's valid, so the
            // CLI never sees a spelling it reads differently.
            args.append("--target=\(seconds)")
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
        if provider.usesAI {
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
        // Read only, and never checked here: a folder on a network share that's asleep would hang the form.
        args += sfx.map { "--sfx=\($0.path)" }
        let typed = Set(sfxFromTyped.map(\.trimmed))
        for name in sfxFrom.map(\.trimmed) where !name.isEmpty {
            args.append(typed.contains(name) ? "--sfx-from=\(name)" : "--preset-sfx-from=\(name)")
        }
        if dialogueLevel != .standard {
            args.append("--dialogue-level=\(dialogueLevel.rawValue)")
        }
        if !fades {
            args += ["--fades", "off"]
        }
        if graphicsActive {
            args.append("--graphics")
            if !graphicsStyle.trimmed.isEmpty {
                args.append("--graphics-style=\(graphicsStyle.trimmed)")
            }
            if !graphicsLogo.trimmed.isEmpty {
                args.append("--graphics-logo=\(graphicsLogo.trimmed)")
            }
            args += graphicsLook.map { "--graphics-look=\($0)" }
            args += ReferenceImages.arguments(for: graphicsReferences)
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
        copy.sfxFromTyped = []
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
    /// Also design the graphics that have no design yet, or still stand on an earlier look (`--design-missing`): Finish
    /// graphics. It asks the model that planned the cut for those designs only; the cut isn't planned again.
    public var designMissing: Bool

    public init(
        outputFolder: URL, style: CutStyle? = nil, format: SequenceFormat? = nil, keepFillers: Bool? = nil, stringout: Bool? = nil,
        punchIn: Bool? = nil, designMissing: Bool = false
    ) {
        self.outputFolder = outputFolder
        self.style = style
        self.format = format
        self.keepFillers = keepFillers
        self.stringout = stringout
        self.punchIn = punchIn
        self.designMissing = designMissing
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
        if designMissing {
            args.append("--design-missing")
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

/// Mirrors the CLI's `parse_duration` (roughcut/timecode.py) so the form can validate as you type.
///
/// A length is from 1 second to 24 hours, written with the digits 0-9 only: no signs, exponents, "nan" or "inf", other
/// scripts' digits, or more than hours:minutes:seconds. A number so long it overflows to infinity is not a length either.
/// Anything outside that is nil (the form says "Target length should look like 8m, 90s or 8:30"); nothing here can trap.
public enum TimeText {
    /// The shortest and longest length a cut can be asked for. The same limits as `MIN_DURATION_SECONDS` and
    /// `MAX_DURATION_SECONDS` in the CLI.
    public static let minSeconds = 1.0
    public static let maxSeconds = 24.0 * 3600
    /// The longest text read as a length (`MAX_DURATION_TEXT` in the CLI): a length is a few characters, and the CLI's
    /// pattern is not meant for pages of them.
    public static let maxTextLength = 64
    /// What a length looks like, for the messages that refuse one: "Target length \(shouldLookLike)." The CLI says the same.
    public static let shouldLookLike = "should look like 8m, 90s or 8:30 (1 second to 24 hours)"

    /// Unit words `parse_duration` reads (plus a few spellings it doesn't; the app sends plain seconds).
    static let unitSeconds: [String: Double] = [
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    ]

    /// "8", "90", "2.5": ASCII digits, then optionally a point and more digits. Finite, or nil.
    static func number(_ text: String) -> Double? {
        var digitsBefore = 0
        var digitsAfter = 0
        var afterPoint = false
        for scalar in text.unicodeScalars {
            if scalar.value >= 0x30 && scalar.value <= 0x39 {
                if afterPoint { digitsAfter += 1 } else { digitsBefore += 1 }
            } else if scalar == "." && !afterPoint && digitsBefore > 0 {
                afterPoint = true
            } else {
                return nil
            }
        }
        guard digitsBefore > 0, !afterPoint || digitsAfter > 0, let value = Double(text), value.isFinite else { return nil }
        return value
    }

    /// "8m", "90s", "90 sec", "10 mins", "1hr", "8:30", "1h5m", "480" -> seconds. Nil when unreadable or outside 1 second to 24 hours.
    public static func seconds(from text: String) -> Double? {
        let raw = text.trimmed.lowercased()
        guard !raw.isEmpty, raw.count <= maxTextLength else { return nil }
        var total = 0.0
        if raw.contains(":") {
            // Minutes:seconds or hours:minutes:seconds.
            let parts = raw.split(separator: ":", omittingEmptySubsequences: false)
            guard parts.count <= 3 else { return nil }
            for part in parts {
                guard let value = number(part.trimmingCharacters(in: .whitespaces)) else { return nil }
                total = total * 60 + value
            }
        } else {
            // Number-and-unit pairs ("1h 30m"); a number without a unit is seconds.
            var pairs: [(number: String, unit: String)] = []
            var digits = ""
            var unit = ""
            var gap = false  // whitespace after a number that has no unit yet
            for char in raw {
                if char.isASCII && (char.isNumber || char == ".") {
                    if !unit.isEmpty {  // "1h30": the next number starts a new pair
                        pairs.append((number: digits, unit: unit))
                        digits = ""
                        unit = ""
                        gap = false
                    } else if gap {
                        return nil  // "1 30": two numbers with no unit between them are not one
                    }
                    digits.append(char)
                } else if char.isLetter {
                    guard !digits.isEmpty else { return nil }  // a unit with no number: "eight minutes"
                    unit.append(char)
                    gap = false
                } else if char.isWhitespace {
                    gap = gap || (unit.isEmpty && !digits.isEmpty)
                } else {
                    return nil
                }
            }
            if !digits.isEmpty {
                pairs.append((number: digits, unit: unit))
            }
            var lastRank = -1
            for pair in pairs {
                guard let value = number(pair.number) else { return nil }
                var scale = 1.0
                if !pair.unit.isEmpty {
                    guard let known = unitSeconds[pair.unit] else { return nil }
                    scale = known
                }
                let rank = scale == 3600 ? 0 : (scale == 60 ? 1 : 2)
                guard rank > lastRank else { return nil }  // hours, minutes and seconds each once, in that order ("5m5m" is not a length)
                lastRank = rank
                total += value * scale
            }
        }
        guard total.isFinite, total >= minSeconds, total <= maxSeconds else { return nil }
        return total
    }

    /// The length as the whole seconds `--target` is sent with; nil when `seconds(from:)` is.
    public static func wholeSeconds(from text: String) -> Int? {
        seconds(from: text).map { Int($0.rounded()) }
    }

    /// What a clock shows for something that isn't a time (not a number, or beyond what fits): the CLI's `NO_CLOCK`.
    public static let noClock = "--:--"

    /// 483 -> "8:03", 3725 -> "1:02:05". Total: a value that isn't a time (infinity, NaN, absurdly large) is "--:--", never a crash.
    public static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite, abs(seconds) <= 1e12 else { return noClock }
        let total = Int(abs(seconds).rounded())
        let sign = seconds < 0 && total > 0 ? "-" : ""
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return sign + String(format: "%d:%02d:%02d", h, m, s)
        }
        return sign + String(format: "%d:%02d", m, s)
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

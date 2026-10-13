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
        options.presetBrief = presetBrief
        if typed.isEmpty || previousBriefs.map(\.trimmed).contains(typed) {
            options.brief = presetBrief
        } else if !presetBrief.isEmpty && !typed.hasPrefix(presetBrief) {
            options.brief = presetBrief + "\n\n" + typed
        }
        options.targetLength = target ?? ""
        options.style = style.flatMap(CutStyle.init(rawValue:)) ?? .auto
        // As saved by `roughcut preset save --format`: 4K, uhd, 720p, 1080x1920@29.97...
        options.format = SequenceFormat(cliText: format)
        options.graphics = graphics ?? false
        options.graphicsStyle = graphicsStyle ?? ""
        options.graphicsLogo = logo ?? ""
        options.graphicsLook = look ?? []
    }
}

extension BuildOptions {
    /// Preferences remember which preset was picked but not its brief and length (`preferencesOnly`),
    /// and the preset may have changed since (`roughcut preset save`, `roughcut update`): fill the form
    /// from the preset as it is now, once the app has read the list. A brief or length typed in the
    /// meantime stays. A preset that's gone is cleared, with the approved graphics it brought.
    public mutating func reapplyPreset(from presets: [Preset]) {
        guard !presetName.isEmpty else { return }
        guard let preset = presets.first(where: { $0.name == presetName }) else {
            presetName = ""
            graphicsLook = []
            return
        }
        let typedLength = targetLength
        preset.apply(to: &self, replacing: presets.compactMap(\.brief))
        if !typedLength.trimmed.isEmpty {
            targetLength = typedLength
        }
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
    /// Who re-plans; nil is the model that planned this cut (the CLI's default).
    public var provider: ModelProvider?
    /// With `provider`: its model ("" for its default) and, for Claude, the effort.
    public var model: String
    public var effort: Effort?
    /// Pictures of the look wanted for the graphics (`--graphics-ref`): they replace the ones the cut has and every
    /// graphic is designed again from them. Shown to the graphics designer only.
    public var references: [URL]
    /// Design every graphic again (`--redesign-graphics`), with the notes as the designer's direction.
    public var redesignGraphics: Bool

    public init(
        outputFolder: URL, notesFile: URL, targetLength: String = "", provider: ModelProvider? = nil, model: String = "", effort: Effort? = nil,
        references: [URL] = [], redesignGraphics: Bool = false
    ) {
        self.outputFolder = outputFolder
        self.notesFile = notesFile
        self.targetLength = targetLength
        self.provider = provider
        self.model = model
        self.effort = effort
        self.references = references
        self.redesignGraphics = redesignGraphics
    }

    /// Reference images matter to a cut that has motion graphics on. Without them the CLI leaves the pictures out, with a warning.
    public static func acceptsReferences(_ result: RunResult) -> Bool {
        result.graphics != nil
    }

    /// There is a graphic in the cut to design again.
    public static func canRedesign(_ result: RunResult) -> Bool {
        (result.graphics?.planned ?? 0) > 0
    }

    /// The pictures a revision of `result` sends: none for a cut without graphics, never more than `ReferenceImages.maxCount`.
    public static func usableReferences(_ list: [URL], for result: RunResult) -> [URL] {
        acceptsReferences(result) ? Array(list.prefix(ReferenceImages.maxCount)) : []
    }

    /// Whether "Redesign the graphics" is in force for `result`: only a cut with graphics has any to redesign.
    public static func usableRedesign(_ wanted: Bool, for result: RunResult) -> Bool {
        wanted && canRedesign(result)
    }

    /// Revise needs something to say: notes, or pictures (the CLI takes either alone). "Redesign the graphics" is only a
    /// modifier of them: on its own it would re-plan the whole cut for nothing the editor asked for.
    public static func canStart(notes: String, references: [URL]) -> Bool {
        !notes.trimmed.isEmpty || !references.isEmpty
    }

    /// Under "Redesign the graphics": what it does, and what it replaces.
    public static let redesignHint = "Notes about how graphics look are applied when this is on; the AI also decides from your words. Every graphic is designed again, hand edits included; the earlier designs stay in the version history."

    /// A revision of `result`. A cut a model planned is re-planned by that model. One made without AI
    /// has none to reuse (the CLI would fall back to the API key), so `provider` re-plans it, with
    /// the build form's model and effort where they fit that provider.
    /// `references` and `redesignGraphics` are what the Revise box holds; they only count for a cut with graphics.
    public static func revising(
        _ result: RunResult, notesFile: URL, targetLength: String, provider: ModelProvider, form: BuildOptions,
        references: [URL] = [], redesignGraphics: Bool = false
    ) -> ReviseOptions {
        var options = ReviseOptions(outputFolder: result.outDirURL, notesFile: notesFile, targetLength: targetLength)
        options.references = usableReferences(references, for: result)
        options.redesignGraphics = usableRedesign(redesignGraphics, for: result)
        guard result.madeWithoutAI, provider != .none else { return options }
        options.provider = provider
        let sameModels = provider == form.provider || (provider.usesClaudeModels && form.provider.usesClaudeModels)
        options.model = sameModels ? form.model : ""
        options.effort = provider.usesClaudeModels ? form.effort : nil
        return options
    }

    /// Who can re-plan a cut made without AI from the app: Claude either way, or the form's OpenAI or
    /// Ollama model (`revise` takes no server address, so an OpenAI-compatible server can't).
    public static func providers(form: BuildOptions) -> [ModelProvider] {
        var list: [ModelProvider] = [.claudeCode, .anthropic]
        if (form.provider == .openai || form.provider == .ollama) && !form.model.trimmed.isEmpty {
            list.append(form.provider)
        }
        return list
    }

    /// The first choice for that: the form's provider when it can, else Claude through whichever sign-in works here.
    public static func defaultProvider(form: BuildOptions, doctor: DoctorReport?) -> ModelProvider {
        if providers(form: form).contains(form.provider) {
            return form.provider
        }
        if let doctor {
            if doctor.claudeCode != nil && doctor.claudeCodeUsesSubscription {
                return .claudeCode
            }
            if doctor.anthropicKey && doctor.anthropicKeyWorks {
                return .anthropic
            }
        }
        return .anthropic
    }

    /// `chosen` while it can still re-plan from here (the form may have changed since), else the first choice.
    public static func planner(_ chosen: ModelProvider, form: BuildOptions, doctor: DoctorReport?) -> ModelProvider {
        providers(form: form).contains(chosen) ? chosen : defaultProvider(form: form, doctor: doctor)
    }

    /// Why Revise can't start with this "New length" (nil: empty keeps the build's, or it reads fine).
    public static func lengthProblem(_ text: String) -> String? {
        let length = text.trimmed
        guard !length.isEmpty, TimeText.seconds(from: length) == nil else { return nil }
        return "New length should look like 8m, 90s or 8:30."
    }

    public func arguments() -> [String] {
        var args = ["revise", outputFolder.path, "--progress-json", "--notes-file=\(notesFile.path)"]
        if let seconds = TimeText.seconds(from: targetLength) {
            args.append("--target=\(Int(seconds.rounded()))")
        }
        if let provider, provider != .none {
            args += ["--provider", provider.rawValue]
            if !model.trimmed.isEmpty {
                args.append("--model=\(model.trimmed)")
            }
            if provider.usesClaudeModels, let effort {
                args += ["--effort", effort.rawValue]
            }
        }
        args += ReferenceImages.arguments(for: references)
        if redesignGraphics {
            args.append("--redesign-graphics")
        }
        return args
    }
}

/// The Revise box's "Redesign the graphics" switch. Adding a picture turns it on (the graphics are designed again from
/// it); it goes off again when the last picture is removed, unless the editor has touched the switch since.
public struct RedesignSwitch: Equatable, Sendable {
    public private(set) var isOn = false
    /// On only because a picture was added.
    public private(set) var byPicture = false

    public init() {}

    /// The editor set the switch.
    public mutating func set(_ on: Bool) {
        isOn = on
        byPicture = false
    }

    /// A picture was added to the Revise box.
    public mutating func pictureAdded() {
        guard !isOn else { return }
        isOn = true
        byPicture = true
    }

    /// Pictures were removed; `remaining` are left.
    public mutating func picturesChanged(remaining: Int) {
        guard remaining == 0, byPicture else { return }
        isOn = false
        byPicture = false
    }

    /// Back to off: after a revision, and for another cut.
    public mutating func reset() {
        isOn = false
        byPicture = false
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

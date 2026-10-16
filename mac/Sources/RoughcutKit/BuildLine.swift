import Foundation

// The build as a line of steps: Footage, Brief, Add, then each part the person added (Music, Effects, Graphics), then Build.
// The rules live here, not in the views, so they can be tested: which parts are on the line, which step comes next, what
// each station says under its node, and what the footer of each step offers.

/// A part a person can add to the basic Footage, Brief, Build line. Each one added becomes a step.
public enum LinePart: String, CaseIterable, Identifiable, Sendable {
    case music, effects, graphics

    public var id: String { rawValue }

    /// Under its node in the line.
    public var label: String {
        switch self {
        case .music: return "Music"
        case .effects: return "Effects"
        case .graphics: return "Graphics"
        }
    }

    /// The Add screen's row, and the step's heading.
    public var title: String {
        switch self {
        case .music: return "Music"
        case .effects: return "Sound effects"
        case .graphics: return "Motion graphics"
        }
    }

    /// What it does, in a line, on the Add screen.
    public var blurb: String {
        switch self {
        case .music: return "A song under the edit. It ducks under speech and ends right on the last frame."
        case .effects: return "Whooshes and hits on section changes and graphics."
        case .graphics: return "Lower thirds, stats and captions, designed by Claude from what people say."
        }
    }

    /// An SF Symbol.
    public var symbol: String {
        switch self {
        case .music: return "music.note"
        case .effects: return "waveform"
        case .graphics: return "wand.and.stars"
        }
    }

    /// The part has something to use in this build: songs, a sound folder or Studio collection, the graphics switch (with a model to design them).
    public func isInUse(_ options: BuildOptions) -> Bool {
        switch self {
        case .music: return !options.music.isEmpty
        case .effects: return !options.sfx.isEmpty || !options.sfxFrom.isEmpty
        case .graphics: return options.graphicsActive
        }
    }

    /// Takes the part off the line for this build: its songs or sound sources are cleared; graphics go off, and the look,
    /// logo and pictures stay remembered for when they're wanted again.
    public func remove(from options: inout BuildOptions) {
        switch self {
        case .music:
            options.music = []
        case .effects:
            options.sfx = []
            options.sfxFrom = []
            options.sfxFromTyped = []
        case .graphics:
            options.setGraphics(false)
        }
    }
}

/// Where the person is in the build.
public enum FlowStep: Hashable, Sendable {
    case footage, brief, add
    case part(LinePart)
    case ready
    /// The settings most cuts leave alone; a detour from Ready.
    case fineTune
}

/// A station on the line: one node.
public enum LineStation: Hashable, Identifiable, Sendable {
    case footage, brief, add
    case part(LinePart)
    case build

    public var id: String {
        switch self {
        case .footage: return "footage"
        case .brief: return "brief"
        case .add: return "add"
        case .part(let part): return part.rawValue
        case .build: return "build"
        }
    }

    public var label: String {
        switch self {
        case .footage: return "Footage"
        case .brief: return "Brief"
        case .add: return "Add"
        case .part(let part): return part.label
        case .build: return "Build"
        }
    }

    /// An SF Symbol.
    public var symbol: String {
        switch self {
        case .footage: return "film"
        case .brief: return "text.alignleft"
        case .add: return "plus"
        case .part(let part): return part.symbol
        case .build: return "bolt.fill"
        }
    }
}

public enum StationState: Equatable, Sendable {
    case done, current, upcoming
}

/// One node of the strip: the station, where the person is relative to it, and the line under its label.
public struct LineNode: Equatable, Identifiable, Sendable {
    public var station: LineStation
    public var state: StationState
    public var summary: String?

    public var id: String { station.id }
    public var label: String { station.label }
    public var symbol: String { station.symbol }
}

/// The steps of a build and where the person is. `waiting` holds the parts added but not given anything yet (Music and
/// Effects wait for their files in their own step); everything else about the line is read from the options, so a preset
/// that turns graphics on, or a remembered sound folder, puts that part on the line by itself.
public struct BuildFlow: Equatable, Sendable {
    public private(set) var step: FlowStep
    public private(set) var waiting: Set<LinePart>

    public init(step: FlowStep = .footage, waiting: Set<LinePart> = []) {
        self.step = step
        self.waiting = waiting
    }

    // MARK: The line

    /// The parts on the line, in the order of the line.
    public func parts(for options: BuildOptions) -> [LinePart] {
        LinePart.allCases.filter { waiting.contains($0) || $0.isInUse(options) }
    }

    /// Every step in order, ending with Ready. Fine-tune is a detour from Ready, so it isn't one of them.
    public func steps(for options: BuildOptions) -> [FlowStep] {
        [.footage, .brief, .add] + parts(for: options).map { FlowStep.part($0) } + [.ready]
    }

    public func station(of step: FlowStep) -> LineStation {
        switch step {
        case .footage: return .footage
        case .brief: return .brief
        case .add: return .add
        case .part(let part): return .part(part)
        case .ready, .fineTune: return .build
        }
    }

    public func stations(for options: BuildOptions) -> [LineStation] {
        steps(for: options).map { station(of: $0) }
    }

    /// Where the current step sits in `list`. A part that is no longer on the line (just removed) counts as the Add step.
    private func position(in list: [FlowStep]) -> Int {
        if step == .fineTune { return list.count - 1 }
        return list.firstIndex(of: step) ?? list.firstIndex(of: .add) ?? 0
    }

    /// The strip: a node for each station, done before the current step, current at it, upcoming after.
    public func nodes(for options: BuildOptions, footage: FootageCount? = nil) -> [LineNode] {
        let list = steps(for: options)
        let current = position(in: list)
        var nodes: [LineNode] = []
        for (index, listed) in list.enumerated() {
            let place = station(of: listed)
            let state: StationState = index < current ? .done : (index == current ? .current : .upcoming)
            nodes.append(LineNode(station: place, state: state, summary: Self.summary(of: place, state: state, options: options, footage: footage)))
        }
        return nodes
    }

    /// The line under a station's label: what it holds. Nothing before the person has reached it, except that Add says it's optional.
    public static func summary(of station: LineStation, state: StationState, options: BuildOptions, footage: FootageCount?) -> String? {
        if state == .upcoming {
            return station == .add ? "Optional" : nil
        }
        switch station {
        case .footage:
            if options.sources.isEmpty { return state == .current ? "Start here" : nil }
            if let footage, !footage.isEmpty { return footage.shortSummary }
            return "\(options.sources.count) source\(options.sources.count == 1 ? "" : "s")"
        case .brief:
            var bits: [String] = []
            if options.style != .auto { bits.append(options.style.title) }
            if let seconds = TimeText.seconds(from: options.targetLength) { bits.append(TimeText.clock(seconds)) }
            return bits.isEmpty ? nil : bits.joined(separator: " · ")
        case .add:
            let count = LinePart.allCases.filter { $0.isInUse(options) }.count
            return count == 0 ? "None yet" : "\(count) added"
        case .part(.music):
            switch options.music.count {
            case 0: return "No song yet"
            case 1: return options.music[0].deletingPathExtension().lastPathComponent
            default: return "\(options.music.count) songs"
            }
        case .part(.effects):
            let count = options.sfx.count + options.sfxFrom.count
            switch count {
            case 0: return "Nothing chosen"
            case 1:
                if let folder = options.sfx.first { return folder.lastPathComponent }
                return options.sfxFrom.first.map { "Studio: \($0)" }
            default: return "\(count) sources"
            }
        case .part(.graphics):
            if !options.presetName.isEmpty { return "\(Preset.displayName(for: options.presetName)) look" }
            return options.graphicsStyle.trimmed.isEmpty ? "Default look" : "Your look"
        case .build:
            return state == .current && options.problems.isEmpty ? "Ready" : nil
        }
    }

    // MARK: The Ready step

    /// A station in a sentence, for the Ready step's list.
    public static func readyDetail(of station: LineStation, options: BuildOptions, footage: FootageCount?) -> String {
        switch station {
        case .footage:
            if options.sources.isEmpty { return "No footage yet" }
            if let footage, !footage.isEmpty { return footage.sentence }
            return "\(options.sources.count) source\(options.sources.count == 1 ? "" : "s")"
        case .brief:
            var shape: [String] = []
            if let seconds = TimeText.seconds(from: options.targetLength) { shape.append("About \(TimeText.clock(seconds))") }
            if options.style != .auto { shape.append("\(options.style.rawValue) pace") }
            var said = shape.joined(separator: ", ")
            if let first = said.first {
                said = first.uppercased() + said.dropFirst()  // "tight pace" starts a sentence
            }
            let tail = shape.isEmpty ? "" : " " + said + "."
            if !options.presetName.isEmpty { return Preset.displayName(for: options.presetName) + "." + tail }
            let lead = Self.lead(of: options.brief)
            return lead.isEmpty ? (tail.isEmpty ? "No brief" : tail.trimmingCharacters(in: .whitespaces)) : lead + tail
        case .add, .build:
            return ""
        case .part(.music):
            let names = options.music.map(\.lastPathComponent)
            switch names.count {
            case 0: return "No song chosen yet"
            case 1: return names[0] + ". Ends on the last frame."
            default: return "\(names.count) songs: " + names.prefix(2).joined(separator: ", ") + (names.count > 2 ? " and more" : "")
            }
        case .part(.effects):
            var names = options.sfx.map(\.lastPathComponent)
            names += options.sfxFrom.map { "Studio: \($0)" }
            switch names.count {
            case 0: return "Nothing chosen yet"
            case 1: return names[0]
            default: return names.prefix(2).joined(separator: ", ") + (names.count > 2 ? " and more" : "")
            }
        case .part(.graphics):
            if !options.presetName.isEmpty { return "\(Preset.displayName(for: options.presetName)) look, designed by Claude." }
            return options.graphicsStyle.trimmed.isEmpty ? "Designed by Claude in its own look." : "Your look, designed by Claude."
        }
    }

    /// The first sentence of a brief, or its first 80 characters, with an ellipsis when it was cut.
    static func lead(of brief: String) -> String {
        let text = brief.trimmed.replacingOccurrences(of: "\n", with: " ")
        guard !text.isEmpty else { return "" }
        if let end = text.firstIndex(where: { ".!?".contains($0) }), text.distance(from: text.startIndex, to: end) < 80 {
            return String(text[...end])
        }
        return text.count <= 80 ? text : String(text.prefix(80)).trimmingCharacters(in: .whitespaces) + "…"
    }

    /// What Fine-tune would change, in a line: the settings most cuts leave alone.
    public static func fineTuneSummary(for options: BuildOptions) -> String {
        var parts = [options.provider.plannerPhrase]
        switch options.dialogueLevel {
        case .standard: parts.append("YouTube loudness")
        case .louder: parts.append("Loud for phones")
        case .off: parts.append("Original levels")
        }
        parts.append(options.format == .auto ? "Matches your footage" : "Format \(options.format.title)")
        if options.stringout { parts.append("Also builds a dialogue-only version") }
        if options.keepFillers { parts.append("Keeps um and uh") }
        if !options.fades { parts.append("No audio fades") }
        switch options.punchIn {
        case .on: parts.append("Punch-ins on")
        case .off: parts.append("No punch-ins")
        case .auto: break
        }
        return parts.joined(separator: ". ") + "."
    }

    // MARK: Moving

    public mutating func go(to step: FlowStep) {
        self.step = step
    }

    /// The step after this one; nil at Ready (and in Fine-tune, which goes back to Ready).
    public func next(for options: BuildOptions) -> FlowStep? {
        guard step != .fineTune else { return nil }
        let list = steps(for: options)
        let index = position(in: list)
        return index + 1 < list.count ? list[index + 1] : nil
    }

    public func back(for options: BuildOptions) -> FlowStep? {
        guard step != .fineTune else { return .ready }
        let list = steps(for: options)
        let index = position(in: list)
        return index > 0 ? list[index - 1] : nil
    }

    public mutating func goNext(for options: BuildOptions) {
        if let next = next(for: options) { step = next }
    }

    public mutating func goBack(for options: BuildOptions) {
        if let back = back(for: options) { step = back }
    }

    /// After anything that can change the line (a preset, the provider, a removed part): a step whose part is no longer
    /// on the line becomes the Add step.
    public mutating func repair(for options: BuildOptions) {
        if case .part(let part) = step, !parts(for: options).contains(part) {
            step = .add
        }
    }

    // MARK: Adding and removing parts

    /// Puts a part on the line. Music and Effects wait for their files in their own step; Graphics need a model to design
    /// them, so with provider None this returns false and nothing changes.
    @discardableResult
    public mutating func add(_ part: LinePart, to options: inout BuildOptions) -> Bool {
        switch part {
        case .music, .effects:
            waiting.insert(part)
            return true
        case .graphics:
            options.setGraphics(true)
            return options.graphicsActive
        }
    }

    /// Takes a part off the line, clearing what it brought into the build.
    public mutating func remove(_ part: LinePart, from options: inout BuildOptions) {
        waiting.remove(part)
        part.remove(from: &options)
        repair(for: options)
    }

    // MARK: Each step's footer

    /// The step can be left for the next: footage needs something to cut, and Ready needs a build that can start.
    public func canAdvance(for options: BuildOptions) -> Bool {
        switch step {
        case .footage: return !options.sources.isEmpty
        case .ready: return options.problems.isEmpty
        default: return true
        }
    }

    /// The footer's main button.
    public var nextTitle: String {
        step == .ready ? "Build rough cut" : "Next"
    }

    /// The quiet line at the left of the footer: why Next waits, or what building costs.
    public func footerNote(for options: BuildOptions, footage: FootageCount?) -> String? {
        switch step {
        case .footage:
            if options.sources.isEmpty { return "Add footage to continue" }
            let sources = "\(options.sources.count) source\(options.sources.count == 1 ? "" : "s")"
            return sources
        case .ready:
            let problems = options.problems.count
            if problems == 0 { return options.provider.usageNote }
            return problems == 1 ? "One thing to fix before building" : "\(problems) things to fix before building"
        default:
            return nil
        }
    }
}

public extension ModelProvider {
    /// "Plans with Claude Code", for the Fine-tune summary.
    var plannerPhrase: String {
        switch self {
        case .anthropic: return "Plans with Claude (API key)"
        case .claudeCode: return "Plans with Claude Code"
        case .openai: return "Plans with OpenAI"
        case .ollama: return "Plans with Ollama"
        case .openaiCompatible: return "Plans with your own server"
        case .none: return "No AI planning"
        }
    }

    /// What building with this planner costs the person, in a line.
    var usageNote: String {
        switch self {
        case .anthropic: return "Uses your Claude API key"
        case .claudeCode: return "Uses your Claude plan"
        case .openai: return "Uses your OpenAI key"
        case .ollama: return "Runs on this Mac"
        case .openaiCompatible: return "Uses your own server"
        case .none: return "No AI: only the dead air is cut"
        }
    }
}

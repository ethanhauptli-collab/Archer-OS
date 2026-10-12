import Foundation

/// Pipeline stages, in the order the CLI reports them (`roughcut/progress.py`).
public enum Stage: String, CaseIterable, Codable, Identifiable, Sendable {
    case scan, probe, silence, transcribe, vision, plan, cut, graphics, write

    public var id: String { rawValue }
    public var order: Int { Stage.allCases.firstIndex(of: self) ?? 0 }

    public var title: String {
        switch self {
        case .scan: return "Find media"
        case .probe: return "Read clips"
        case .silence: return "Find the pauses"
        case .transcribe: return "Transcribe"
        case .vision: return "Look at the footage"
        case .plan: return "Plan the edit"
        case .cut: return "Cut"
        case .graphics: return "Motion graphics"
        case .write: return "Write the Final Cut file"
        }
    }

    public var symbol: String {
        switch self {
        case .scan: return "magnifyingglass"
        case .probe: return "film"
        case .silence: return "waveform"
        case .transcribe: return "text.quote"
        case .vision: return "eye"
        case .plan: return "sparkles"
        case .cut: return "scissors"
        case .graphics: return "wand.and.stars"
        case .write: return "square.and.arrow.down"
        }
    }
}

/// One line of `--progress-json` output.
public struct ProgressEvent: Decodable, Equatable, Sendable {
    public var event: String
    public var stage: String?
    public var message: String?
    public var current: Int?
    public var total: Int?
    public var result: RunResult?
    /// On an `error` after the plan was saved: the command that finishes the cut without AI.
    public var resume: String?
    /// On an `error`: "usage_limit" when a Claude plan or API credit ran out.
    public var kind: String?

    /// Nil for stages this version of the app doesn't know about.
    public var stageValue: Stage? { stage.flatMap(Stage.init(rawValue:)) }
}

/// Payload of the `done` event (`pipeline.result_summary`).
public struct RunResult: Codable, Equatable, Sendable {
    public var title: String
    public var outDir: String
    public var fcpxml: String
    public var report: String
    public var roughSeconds: Double
    public var stringoutSeconds: Double?
    public var edits: Int
    public var broll: Int
    public var sections: [String]
    public var plannedBy: String
    public var fellBack: Bool
    public var warnings: [String]
    // Added later; nil from older CLIs.
    public var version: Int?
    /// The Final Cut project and event this cut imports as.
    public var project: String?
    public var event: String?
    public var graphics: GraphicsSummary?
    public var visualLog: VisualLogSummary?
    public var skipped: SkippedSummary?
    public var usage: UsageSummary?
    /// The paper edit as a web page.
    public var reportHtml: String?
    /// The settings a re-cut of this version keeps unless it changes them (nil from older CLIs and older recent runs).
    public var cut: CutSettings?
    /// `<name> vN.srt` beside the Final Cut file: the cut's captions (nil when nothing in it is spoken, or from older CLIs).
    public var captions: String?

    /// No model planned this cut (`--provider none`, or the stringout fallback): `planned_by` names no "provider:model".
    public var madeWithoutAI: Bool { !plannedBy.contains(":") }

    public var outDirURL: URL { URL(fileURLWithPath: outDir, isDirectory: true) }
    public var fcpxmlURL: URL { URL(fileURLWithPath: fcpxml) }
    public var captionsURL: URL? { captions.map { URL(fileURLWithPath: $0) } }
    public var reportURL: URL { URL(fileURLWithPath: report) }

    /// The paper edit to open: the web page when there is one.
    public var paperEditURL: URL {
        if let reportHtml, FileManager.default.fileExists(atPath: reportHtml) {
            return URL(fileURLWithPath: reportHtml)
        }
        return reportURL
    }
}

/// A version's cut settings, as saved in its plan.json (`result_summary`'s "cut").
public struct CutSettings: Codable, Equatable, Sendable {
    public var style: String?
    /// The `--format` it was cut with; nil is the footage's own format.
    public var format: String?
    public var keepFillers: Bool?
    public var stringout: Bool?

    public init(style: String? = nil, format: String? = nil, keepFillers: Bool? = nil, stringout: Bool? = nil) {
        self.style = style
        self.format = format
        self.keepFillers = keepFillers
        self.stringout = stringout
    }

    public var cutStyle: CutStyle? { style.flatMap(CutStyle.init(rawValue:)) }
    public var sequenceFormat: SequenceFormat { SequenceFormat(cliText: format) }
}

public struct GraphicsSummary: Codable, Equatable, Sendable {
    public var planned: Int
    public var placed: Int
    public var rendered: Int
    public var note: String?
}

public struct VisualLogSummary: Codable, Equatable, Sendable {
    public var described: Int
    public var total: Int
}

public struct SkippedSummary: Codable, Equatable, Sendable {
    public var files: [String]
    public var planRefs: Int
}

/// What the AI steps cost (`providers.cost_breakdown`).
public struct UsageSummary: Codable, Equatable, Sendable {
    public var calls: Int
    public var dollars: Double
    public var byPurpose: [String: Double]?
    public var subscriptionCalls: Int?
    public var unpricedCalls: Int?

    /// "about $0.84 (visual log $0.20, edit plan $0.64)", or the plan's name when nothing was billed.
    public var summaryText: String {
        let subscription = subscriptionCalls ?? 0
        if dollars <= 0 && subscription > 0 {
            return "\(subscription) request\(subscription == 1 ? "" : "s") on your Claude plan (no API charge)"
        }
        var text = String(format: "about $%.2f", dollars)
        let parts = (byPurpose ?? [:]).filter { $0.value >= 0.005 }.sorted { $0.value > $1.value }
            .map { String(format: "%@ $%.2f", $0.key, $0.value) }
        if !parts.isEmpty {
            text += " (" + parts.joined(separator: ", ") + ")"
        }
        if let unpriced = unpricedCalls, unpriced > 0 {
            text += ", plus \(unpriced) request\(unpriced == 1 ? "" : "s") without a known price"
        }
        return text
    }
}

/// `roughcut doctor --json`.
public struct DoctorReport: Decodable, Equatable, Sendable {
    public var version: String
    public var python: String
    public var executable: String
    public var ffmpeg: String?
    public var ffprobe: String?
    public var transcriber: String?
    public var anthropicKey: Bool
    /// ok / rejected / missing / unreachable / not checked (nil from older CLIs).
    public var anthropicKeyStatus: String?
    /// Why the key didn't work, in words the user can act on.
    public var anthropicKeyMessage: String?
    public var openaiKey: Bool
    public var openaiInstalled: Bool
    /// Path to the `claude` program, if installed (nil from older CLIs too).
    public var claudeCode: String?
    /// How Claude Code is signed in: claude.ai / oauth_token / api_key; nil when signed out.
    public var claudeCodeAuth: String?
    /// Why motion graphics can't render here (nil = ready, or an older CLI).
    public var graphics: String?

    public var hasFFmpeg: Bool { ffmpeg != nil && ffprobe != nil }
    public var claudeCodeUsesSubscription: Bool { claudeCodeAuth.map { $0 != "api_key" } ?? false }
    public var anthropicKeyWorks: Bool { anthropicKeyStatus.map { $0 == "ok" || $0 == "not checked" } ?? anthropicKey }
}

public enum JSONLines {
    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    public static func event(from line: String) -> ProgressEvent? {
        guard line.first == "{", let data = line.data(using: .utf8) else { return nil }
        return try? decoder().decode(ProgressEvent.self, from: data)
    }

    public static func doctor(from line: String) -> DoctorReport? {
        guard line.first == "{", let data = line.data(using: .utf8) else { return nil }
        return try? decoder().decode(DoctorReport.self, from: data)
    }
}

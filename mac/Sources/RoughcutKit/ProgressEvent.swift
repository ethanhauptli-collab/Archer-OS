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

    /// Nil for stages this version of the app doesn't know about.
    public var stageValue: Stage? { stage.flatMap(Stage.init(rawValue:)) }
}

/// Payload of the `done` event (`pipeline.result_summary`).
public struct RunResult: Decodable, Equatable, Sendable {
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

    public var outDirURL: URL { URL(fileURLWithPath: outDir, isDirectory: true) }
    public var fcpxmlURL: URL { URL(fileURLWithPath: fcpxml) }
    public var reportURL: URL { URL(fileURLWithPath: report) }
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

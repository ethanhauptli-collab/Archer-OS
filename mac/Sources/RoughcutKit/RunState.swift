import Foundation

/// What a run is doing.
public enum RunKind: Equatable, Sendable {
    case build, recut, revise

    /// "Build", "Re-cut", "Revision".
    public var noun: String {
        switch self {
        case .build: return "Build"
        case .recut: return "Re-cut"
        case .revise: return "Revision"
        }
    }

    /// The notification when one finishes.
    public var readyTitle: String {
        switch self {
        case .build: return "Rough cut ready"
        case .recut: return "Re-cut ready"
        case .revise: return "Revision ready"
        }
    }

    /// After a Claude plan or API credit ran out (`kind: usage_limit`): what to do once it resets.
    /// Nil for a re-cut, which makes no model call.
    public var limitAdvice: String? {
        switch self {
        case .build:
            return "Nothing is lost: transcripts and clip notes are saved. Build again after your plan resets and it picks up from there."
        case .revise:
            return "Click Revise again after your plan resets. Your notes are still here."
        case .recut:
            return nil
        }
    }
}

/// Where the window goes when a run ends.
public enum RunEnding: Equatable, Sendable {
    /// The new cut.
    case finished(RunResult)
    /// Back to the cut a re-cut or revision started from (the revision notes kept), saying why it stopped.
    case returned(RunResult, problem: String)
    /// The build form, saying why the run stopped.
    case failed(String)
    /// The build form, after Cancel.
    case cancelled

    /// - Parameters:
    ///   - source: the cut a re-cut or revision started from; nil for a build (or finishing a stopped build).
    ///   - result: the `done` event's cut, if one came.
    ///   - error: the `error` event's message, or what the app saw instead.
    public static func after(_ kind: RunKind, from source: RunResult?, result: RunResult?, cancelled: Bool, error: String) -> RunEnding {
        if let result {
            return .finished(result)
        }
        if let source {
            return .returned(source, problem: cancelled ? "\(kind.noun) cancelled." : error)
        }
        return cancelled ? .cancelled : .failed(error)
    }
}

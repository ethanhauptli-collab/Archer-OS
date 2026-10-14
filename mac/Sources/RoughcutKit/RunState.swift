import Foundation

/// What a run is doing.
public enum RunKind: Equatable, Sendable {
    /// `finishGraphics` is `roughcut render FOLDER --design-missing`: the cut isn't planned again, the graphics it lacks are made.
    case build, recut, revise, finishGraphics

    /// "Build", "Re-cut", "Revision", "Finish graphics".
    public var noun: String {
        switch self {
        case .build: return "Build"
        case .recut: return "Re-cut"
        case .revise: return "Revision"
        case .finishGraphics: return "Finish graphics"
        }
    }

    /// "the build was cancelled", for the sentence over a version a cancelled run left.
    public var cancelledPhrase: String {
        switch self {
        case .build: return "the build was cancelled."
        case .recut: return "the re-cut was cancelled."
        case .revise: return "the revision was cancelled."
        case .finishGraphics: return "finishing the graphics was cancelled."
        }
    }

    /// The notification when one finishes.
    public var readyTitle: String {
        switch self {
        case .build: return "Rough cut ready"
        case .recut: return "Re-cut ready"
        case .revise: return "Revision ready"
        case .finishGraphics: return "Graphics ready"
        }
    }

    /// The heading while it runs.
    public var runningTitle: String {
        switch self {
        case .build: return "Building your rough cut…"
        case .recut: return "Re-cutting…"
        case .revise: return "Revising from your notes…"
        case .finishGraphics: return "Finishing the graphics…"
        }
    }

    /// After a Claude plan or API credit ran out (`kind: usage_limit`) before the run wrote a version: what to do once it resets.
    /// Nil for a re-cut, which makes no model call.
    public var limitAdvice: String? {
        switch self {
        case .build:
            return "Nothing is lost: transcripts and clip notes are saved. Build again after your plan resets and it picks up from there."
        case .revise:
            return "Click Revise again after your plan resets. Your notes are still here."
        case .recut:
            return nil
        case .finishGraphics:
            return "Nothing is lost: the plan and every design that came back are saved. Click Finish graphics again after your plan resets."
        }
    }

    /// What to do after a run stopped, under the banner. When it left a newer version whose graphics aren't all made (a
    /// stopped revision or build, or a usage limit while the graphics were designed), that is Finish graphics, which makes
    /// the rest from the saved plan; a limit that came before anything was written keeps `limitAdvice`.
    /// - Parameters:
    ///   - usageLimit: the `error` event's kind was `usage_limit`.
    ///   - newest: the newer version the run left, if any.
    public func stoppedAdvice(usageLimit: Bool, newest: RunResult?) -> String? {
        if let newest, newest.canFinishGraphics {
            if usageLimit {
                return "Nothing is lost: the plan and every design that came back are saved. After your plan resets, click Finish graphics: it makes the rest without planning the cut again."
            }
            return "Click Finish graphics to make them from the saved plan; the cut isn't planned again."
        }
        return usageLimit ? limitAdvice : nil
    }
}

/// Where the window goes when a run ends.
public enum RunEnding: Equatable, Sendable {
    /// The new cut.
    case finished(RunResult)
    /// Back to the cut a re-cut or revision started from (the revision notes kept), saying why it stopped.
    case returned(RunResult, problem: String)
    /// The run stopped after writing a newer version (a cut without its graphics, or one with the graphics that were done):
    /// that version, saying what it lacks and why the run stopped. The cut the run started from is still there.
    case wroteAndStopped(RunResult, problem: String)
    /// The build form, saying why the run stopped.
    case failed(String)
    /// The build form, after Cancel.
    case cancelled

    /// - Parameters:
    ///   - source: the cut a re-cut or revision started from; nil for a build (or finishing a stopped build).
    ///   - result: the `done` event's cut, if one came.
    ///   - error: the `error` event's message, or what the app saw instead.
    ///   - saved: the output folder's `result.json`, read after the run stopped (see `StoppedRun.newest`).
    ///   - written: the `error` event's `written`.
    ///   - baseline: the version `result.json` held when the run began (see `StoppedRun.newest`).
    ///   - usageLimit: the `error` event's kind was `usage_limit`; the banner then leaves out the engine's Terminal command
    ///     (the Finish graphics button, or Revise again, is what the window offers).
    public static func after(
        _ kind: RunKind, from source: RunResult?, result: RunResult?, cancelled: Bool, error: String,
        saved: RunResult? = nil, written: WrittenCut? = nil, baseline: Int? = nil, usageLimit: Bool = false
    ) -> RunEnding {
        if let result {
            return .finished(result)
        }
        if let newest = StoppedRun.newest(saved: saved, source: source, written: written, baseline: baseline) {
            let problem = StoppedRun.problem(for: newest, kind: kind, cancelled: cancelled, reason: error, earlier: source, written: written)
            return .wroteAndStopped(newest, problem: problem)
        }
        if let source {
            var problem = cancelled ? "\(kind.noun) cancelled." : (usageLimit ? StoppedRun.withoutTerminalCommands(error) : error)
            if let note = StoppedRun.unreadVersionNote(written: written, source: source) {
                // It wrote a newer version, but result.json wasn't there (or was an older one) to show it.
                if let last = problem.last, !".!?".contains(last) {
                    problem += "."
                }
                problem += note
            }
            return .returned(source, problem: problem)
        }
        return cancelled ? .cancelled : .failed(error)
    }
}

/// A revision that stopped after writing its version: the folder whose Revise box still holds its notes. They are in that
/// version already, so finishing the version (Finish graphics, Finish without AI) clears the box as a finished revision
/// does; a box that kept them would apply them again with the next Revise.
public struct StoppedRevision: Equatable, Sendable {
    public private(set) var folder: String?

    public init() {}

    /// A revision stopped after writing `newest`.
    public mutating func remember(_ newest: RunResult) {
        folder = newest.outDir
    }

    /// Finishing `result` completes the revision that stopped: it is the cut in that folder.
    public func isFinished(by result: RunResult?) -> Bool {
        guard let folder, let result else { return false }
        return folder == result.outDir
    }

    /// The box was cleared, another revision started, or another project is on screen.
    public mutating func clear() {
        folder = nil
    }
}

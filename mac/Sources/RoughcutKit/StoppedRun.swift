import Foundation

// What a run that stopped left in its output folder, and the graphics a cut still waits for.
//
// A revision (or a build with graphics) writes its cut without the graphics first, as the newest, partial version,
// then makes the graphics and writes the finished cut. A run that stops in between (the usage limit, Cancel, a crash)
// leaves that version, or, after the usage limit, a version written with the graphics that were done. The CLI keeps
// `result.json` for the newest version in the folder, so the app can show it instead of the cut the run started from.

extension GraphicsSummary {
    /// Graphics that wait for a design or a render: placed and not made, or on the timeline in an earlier version's design
    /// or clip (`pending`). A graphic the cut has no room for isn't one of them: nothing can be done for it.
    public var unfinished: Int {
        (pending ?? 0) + max(placed - rendered, 0)
    }
}

extension GraphicsSummary {
    /// The engine's note without the parts that send the reader to Terminal. The engine says how to finish in a command
    /// ("…; after the reset run `roughcut render FOLDER --design-missing`"); the window has the Finish graphics button for that.
    /// Nil when nothing else is left.
    public var displayNote: String? {
        guard let note, !note.isEmpty else { return nil }
        let kept = note.components(separatedBy: "; ").filter { !$0.contains("`") }
        let text = kept.joined(separator: "; ").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}

extension RunResult {
    /// Some graphics planned for this cut aren't made yet (in a cut written before they were made, none are), so
    /// `roughcut render FOLDER --design-missing` has something to do.
    public var canFinishGraphics: Bool {
        guard let graphics, graphics.planned > 0 else { return false }
        return partial == true || graphics.unfinished > 0
    }

    /// A Finish graphics run ended on this version, changed nothing from the version before, and graphics are still to be
    /// finished: the model was busy, or a render failed again. The button then offers another try, not a fresh start, and
    /// says each try is a version of its own.
    public var finishGraphicsMadeNothing: Bool {
        canFinishGraphics && changes?.unchanged == true
    }

    /// The Finish graphics button's title.
    public func finishGraphicsTitle(afterEmptyTry: Bool = false) -> String {
        afterEmptyTry ? "Try again" : "Finish graphics"
    }

    /// Under the Finish graphics button: what it does for this version. It names the cause: a cut written before its
    /// graphics, graphics in an earlier look, or graphics that were designed and didn't render.
    /// - Parameter afterEmptyTry: the last try changed nothing (see `finishGraphicsMadeNothing`).
    public func finishGraphicsCaption(afterEmptyTry: Bool = false) -> String {
        let stays = " The cut isn't planned again, and this version stays as an earlier one."
        if afterEmptyTry {
            return "The last try changed nothing: the model may have been busy, or a render failed again. Each try is a version of its own, so give it a little while first." + stays
        }
        if partial == true {
            return "Designs the graphics this version was written without, then renders them." + stays
        }
        if (graphics?.pending ?? 0) > 0 {
            return "Designs the new look for the graphics that are still in their earlier look (and any with no design yet), then renders them." + stays
        }
        return "Tries again to design and render the graphics that didn't come back." + stays
    }

    /// What Final Cut may ask when a later version goes into the event that already holds the one before it (the same
    /// event name on the same day). The same sentence the paper edit and the Terminal say.
    public static let sameNamesHint = "If Final Cut asks about items with the same names, choose Keep Both: each version is its own project."

    /// The line under the buttons: where the project is in Final Cut, and, for a later version, what to choose if asked.
    public var importHint: String {
        var text: String
        if let project, let event {
            text = "In Final Cut Pro choose a library; then open the project “\(project)” in the event “\(event)”."
        } else {
            text = "Final Cut opens its import dialog. Choose a library and the event appears with both projects."
        }
        if let version, version > 1 {
            text += " " + RunResult.sameNamesHint
        }
        return text
    }
}

extension RecutOptions {
    /// Finish graphics for `result`: the run's own settings, and the graphics it lacks designed and rendered.
    ///
    /// A revision writes only the Rough Cut project while its saved Stringout setting stays on, so a plain render would bring
    /// the Stringout project back and the event's project list would change between versions. The version that finishes a
    /// cut with no Stringout project writes none either (the CLI says nothing about it in the list of changes).
    public static func finishingGraphics(of result: RunResult) -> RecutOptions {
        RecutOptions(outputFolder: result.outDirURL, stringout: leavesStringoutOut(result) ? false : nil, designMissing: true)
    }

    /// Finish without AI in `folder`: `roughcut render` with the run's own settings. When the cut on screen is a revision (it has
    /// no Stringout project while its saved setting is on) the finishing version writes none either, as Finish graphics does.
    public static func finishingWithoutAI(in folder: URL, shown: RunResult?) -> RecutOptions {
        RecutOptions(outputFolder: folder, stringout: shown.map(leavesStringoutOut) == true ? false : nil)
    }

    /// A cut written without a Stringout project although its saved Stringout setting is on (a revision).
    private static func leavesStringoutOut(_ result: RunResult) -> Bool {
        result.stringoutSeconds == nil && result.cut?.stringout == true
    }
}

/// The `result.json` the CLI keeps in an output folder for the newest version written there.
public enum SavedResult {
    public static let fileName = "result.json"

    /// How long the app waits for the file: an output folder on a network share that has stalled never answers, and the
    /// window must not wait on it (the run that just stopped is often the one the share stalled).
    public static let readSeconds: TimeInterval = 3

    /// The folder's newest version, as the `done` event described it (plus `partial` for a cut written before its graphics).
    /// Nil when the file is missing or can't be read: the app then goes on as if nothing newer was written.
    public static func read(in folder: URL) -> RunResult? {
        guard let data = try? Data(contentsOf: folder.appendingPathComponent(fileName)) else { return nil }
        return decode(data)
    }

    /// The same, read off the calling thread and given up after `timeout` seconds (nil then, as for a file that is not there).
    /// A read that never returns is left behind on its own thread; nothing waits for it.
    public static func read(in folder: URL, timeout: TimeInterval) async -> RunResult? {
        await withCheckedContinuation { (continuation: CheckedContinuation<RunResult?, Never>) in
            let once = ResumeOnce(continuation)
            DispatchQueue.global(qos: .userInitiated).async {
                once.resume(with: SavedResult.read(in: folder))
            }
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + max(0, timeout)) {
                once.resume(with: nil)
            }
        }
    }

    public static func decode(_ data: Data) -> RunResult? {
        try? JSONLines.decoder().decode(RunResult.self, from: data)
    }
}

/// Resumes a continuation once, whichever of two threads gets there first.
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<RunResult?, Never>?

    init(_ continuation: CheckedContinuation<RunResult?, Never>) {
        self.continuation = continuation
    }

    func resume(with value: RunResult?) {
        lock.lock()
        let taken = continuation
        continuation = nil
        lock.unlock()
        taken?.resume(returning: value)
    }
}

/// A run that stopped: did it leave a newer version, and what the window says about it.
public enum StoppedRun {
    /// The version a stopped run left in the folder, or nil when the cut it started from is still the newest.
    ///
    /// - Parameters:
    ///   - saved: the folder's `result.json`, read after the run stopped.
    ///   - source: the cut a re-cut or revision started from. A version is newer when its number is higher than the source's
    ///     and than `baseline`. With no number to compare (a cut from an older CLI), a partial cut, or one the `error`
    ///     event named, counts.
    ///   - written: the `error` event's `written`. A build has no cut to compare with, and its folder may hold an earlier
    ///     run's files, so only the version the run itself says it wrote counts.
    ///   - baseline: the version `result.json` held when the run began. The folder can hold a version newer than the cut on
    ///     screen (a partial cut an earlier run left, beside the cut it was opened from): a run that wrote nothing is not
    ///     the one that wrote it.
    public static func newest(saved: RunResult?, source: RunResult?, written: WrittenCut?, baseline: Int? = nil) -> RunResult? {
        guard let saved else { return nil }
        if let source {
            if let now = saved.version, let before = [source.version, baseline].compactMap({ $0 }).max() {
                return now > before ? saved : nil
            }
            return saved.partial == true || written != nil ? saved : nil
        }
        guard let written, written.version == nil || written.version == saved.version else { return nil }
        return saved
    }

    /// The banner over the newer version: what was written, what it lacks, and why the run stopped.
    /// "Version 2 was written, but its graphics weren't finished: <reason> Your earlier version, Trip v1.fcpxml, is still there."
    /// - Parameters:
    ///   - earlier: the cut the run started from, when there was one.
    ///   - written: the `error` event's `written`; it names the file the engine's own sentence is cut before (see `reason`).
    public static func problem(
        for newest: RunResult, kind: RunKind, cancelled: Bool, reason: String, earlier: RunResult?, written: WrittenCut? = nil
    ) -> String {
        let which = newest.version.map { "Version \($0)" } ?? "A newer version"
        let why = cancelled ? kind.cancelledPhrase : StoppedRun.reason(from: reason, written: written)
        var text: String
        if newest.partial != true, let pending = newest.graphics?.pending, pending > 0 {
            // After the usage limit: every graphic is there, in the look it had; the new look is what's missing.
            text = "\(which) was written with \(pending == 1 ? "1 graphic" : "\(pending) graphics") in \(pending == 1 ? "its" : "their") earlier look: "
        } else if newest.canFinishGraphics {
            text = "\(which) was written, but its graphics weren't finished: "
        } else {
            text = "\(which) was written before the run stopped: "
        }
        text += why.isEmpty ? "the run stopped" : why
        if let last = text.last, !".!?".contains(last) {
            text += "."
        }
        if let earlier {
            // A partial cut leaves the cut before it where it was; a finished write moves that one to the history folder.
            if newest.partial == true {
                let file = URL(fileURLWithPath: earlier.fcpxml).lastPathComponent
                text += earlier.fcpxml.isEmpty ? " Your earlier version is still there." : " Your earlier version, \(file), is still there."
            } else {
                text += " Your earlier version is kept in the folder's history."
            }
        }
        return text
    }

    /// Why the run stopped, as the window says it. The engine's usage-limit error goes on to say which file it wrote and which
    /// Terminal command finishes it ("Trip v2.fcpxml is written with 2 motion graphics … After the reset, `roughcut render …`"):
    /// the banner already says what was written, and the button under it does the rest.
    public static func reason(from error: String, written: WrittenCut? = nil) -> String {
        var text = error.trimmingCharacters(in: .whitespacesAndNewlines)
        if let path = written?.fcpxml, !path.isEmpty {
            let name = URL(fileURLWithPath: path).lastPathComponent
            if let range = text.range(of: " \(name) is written") {
                text = String(text[..<range.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        return withoutTerminalCommands(text)
    }

    /// `text` without the sentences that send the reader to Terminal (a command in backticks): the app has a button for that.
    /// Nothing is dropped when nothing else would be left.
    public static func withoutTerminalCommands(_ text: String) -> String {
        guard text.contains("`") else { return text }
        let kept = sentences(of: text).filter { !$0.contains("`") }
        return kept.isEmpty ? text : kept.joined(separator: " ")
    }

    /// Splits at a full stop, "!" or "?" followed by a space.
    private static func sentences(of text: String) -> [String] {
        var found: [String] = []
        var current = ""
        var previous: Character = "\n"
        for character in text {
            if character == " ", ".!?".contains(previous) {
                found.append(current)
                current = ""
            } else {
                current.append(character)
            }
            previous = character
        }
        found.append(current)
        return found.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    /// Beside the banner's Finish without AI button: what it does, and how it differs from Finish graphics when both are on screen.
    public static func finishWithoutAICaption(finishGraphicsOffered: Bool) -> String {
        finishGraphicsOffered
            ? "The new plan was saved. Finish without AI adds the graphics whose designs were saved and calls no model; Finish graphics, below, also designs the rest."
            : "The new plan was saved before it stopped."
    }

    /// A line for a stopped run that went back to its cut although the error event says it wrote a newer version: result.json
    /// couldn't be read in time (or didn't say), so the window can't show that version, but the file is there.
    public static func unreadVersionNote(written: WrittenCut?, source: RunResult?) -> String? {
        guard let written, let version = written.version, let path = written.fcpxml, !path.isEmpty else { return nil }
        if let before = source?.version, version <= before { return nil }
        return " Version \(version) was written to \(URL(fileURLWithPath: path).lastPathComponent) in the same folder."
    }
}

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

extension RunResult {
    /// Some graphics planned for this cut aren't made yet (in a cut written before they were made, none are), so
    /// `roughcut render FOLDER --design-missing` has something to do.
    public var canFinishGraphics: Bool {
        guard let graphics, graphics.planned > 0 else { return false }
        return partial == true || graphics.unfinished > 0
    }

    /// Under the Finish graphics button.
    public static let finishGraphicsCaption = "Makes the graphics this version is missing from the saved plan, asking the model that planned the cut to design them. The cut isn't planned again, and this version stays as an earlier one."

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

/// The `result.json` the CLI keeps in an output folder for the newest version written there.
public enum SavedResult {
    public static let fileName = "result.json"

    /// The folder's newest version, as the `done` event described it (plus `partial` for a cut written before its graphics).
    /// Nil when the file is missing or can't be read: the app then goes on as if nothing newer was written.
    public static func read(in folder: URL) -> RunResult? {
        guard let data = try? Data(contentsOf: folder.appendingPathComponent(fileName)) else { return nil }
        return decode(data)
    }

    public static func decode(_ data: Data) -> RunResult? {
        try? JSONLines.decoder().decode(RunResult.self, from: data)
    }
}

/// A run that stopped: did it leave a newer version, and what the window says about it.
public enum StoppedRun {
    /// The version a stopped run left in the folder, or nil when the cut it started from is still the newest.
    ///
    /// - Parameters:
    ///   - saved: the folder's `result.json`, read after the run stopped.
    ///   - source: the cut a re-cut or revision started from. A version is newer when its number is higher. With no
    ///     number to compare (a cut from an older CLI), a partial cut, or one the `error` event named, counts.
    ///   - written: the `error` event's `written`. A build has no cut to compare with, and its folder may hold an earlier
    ///     run's files, so only the version the run itself says it wrote counts.
    public static func newest(saved: RunResult?, source: RunResult?, written: WrittenCut?) -> RunResult? {
        guard let saved else { return nil }
        if let source {
            if let now = saved.version, let before = source.version {
                return now > before ? saved : nil
            }
            return saved.partial == true || written != nil ? saved : nil
        }
        guard let written, written.version == nil || written.version == saved.version else { return nil }
        return saved
    }

    /// The banner over the newer version: what was written, what it lacks, and why the run stopped.
    /// "Version 2 was written, but its graphics weren't finished: <reason> Your earlier version is still there."
    public static func problem(for newest: RunResult, kind: RunKind, cancelled: Bool, reason: String, hasEarlier: Bool) -> String {
        let which = newest.version.map { "Version \($0)" } ?? "A newer version"
        let why = cancelled ? kind.cancelledPhrase : reason.trimmingCharacters(in: .whitespacesAndNewlines)
        var text = newest.canFinishGraphics
            ? "\(which) was written, but its graphics weren't finished: "
            : "\(which) was written before the run stopped: "
        text += why.isEmpty ? "the run stopped" : why
        if let last = text.last, !".!?".contains(last) {
            text += "."
        }
        if hasEarlier {
            // A partial cut leaves the cut before it where it was; a finished write moves that one to the history folder.
            text += newest.partial == true ? " Your earlier version is still there." : " Your earlier version is kept in the folder's history."
        }
        return text
    }
}

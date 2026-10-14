import XCTest
@testable import RoughcutKit

final class RunStateTests: XCTestCase {
    private typealias Graphics = (planned: Int, placed: Int, rendered: Int, pending: Int?)

    /// A cut as the `done` event (or the folder's result.json) describes it. `version` nil leaves the key out, as an older CLI did.
    private func cut(_ version: Int?, graphics: Graphics? = nil, partial: Bool = false) throws -> RunResult {
        var extra = ""
        if let version {
            extra += ", \"version\": \(version)"
        }
        if let graphics {
            extra += ", \"graphics\": {\"planned\": \(graphics.planned), \"placed\": \(graphics.placed), \"rendered\": \(graphics.rendered), \"note\": null"
            if let pending = graphics.pending {
                extra += ", \"pending\": \(pending)"
            }
            extra += "}"
        }
        if partial {
            extra += ", \"partial\": true"
        }
        let tag = version.map { " v\($0)" } ?? ""
        let line = "{\"event\": \"done\", \"result\": {\"title\": \"Promo\", \"out_dir\": \"/o/Promo_roughcut\", \"fcpxml\": \"/o/Promo_roughcut/Promo\(tag).fcpxml\", \"report\": \"/o/r.md\", \"rough_seconds\": 90, \"stringout_seconds\": null, \"edits\": 12, \"broll\": 3, \"sections\": [], \"planned_by\": \"claude-code:claude-opus-5-5\", \"fell_back\": false, \"warnings\": []\(extra)}}"
        return try XCTUnwrap(JSONLines.event(from: line)?.result)
    }

    /// result.json as the CLI wrote it (Fixtures/, real output).
    private func saved(_ name: String) throws -> RunResult {
        try XCTUnwrap(SavedResult.decode(FixtureFiles.data(name, "json")))
    }

    func testAStoppedRevisionGoesBackToItsCut() throws {
        // It used to drop to the build form: the typed notes were gone and the advice said "Build again".
        let v1 = try cut(1)
        let limit = "Claude Code: your plan's usage limit was reached. Nothing changed: v1 is still the latest cut. Run the same revise again after the reset."
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: limit), .returned(v1, problem: limit))
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: true, error: "roughcut stopped (exit code 15)."), .returned(v1, problem: "Revision cancelled."))
        XCTAssertEqual(RunEnding.after(.recut, from: v1, result: nil, cancelled: false, error: "No space left"), .returned(v1, problem: "No space left"))
        XCTAssertEqual(RunEnding.after(.recut, from: v1, result: nil, cancelled: true, error: ""), .returned(v1, problem: "Re-cut cancelled."))
        // A finished one shows the new cut.
        let v2 = try cut(2)
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: v2, cancelled: false, error: ""), .finished(v2))
    }

    func testABuildThatStopsGoesToTheForm() throws {
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: false, error: "No media files found."), .failed("No media files found."))
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: true, error: ""), .cancelled)
        let v1 = try cut(1)
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: v1, cancelled: false, error: ""), .finished(v1))
        // Finishing a stopped build without AI has no cut to go back to either.
        XCTAssertEqual(RunEnding.after(.recut, from: nil, result: nil, cancelled: false, error: "x"), .failed("x"))
    }

    // MARK: A run that stopped after writing a newer version

    func testARevisionThatStoppedAfterItsCutWithoutGraphicsShowsThatVersion() throws {
        // The revision wrote version 2 without its graphics (the newest version, the cut before it still in place), then
        // stopped. The window used to go back to version 1, whose files had moved, and never offered version 2.
        let v1 = try cut(1, graphics: (planned: 2, placed: 2, rendered: 2, pending: nil))
        let partial = try saved("result-partial-revise-sample")
        XCTAssertEqual(partial.version, 2)
        XCTAssertEqual(partial.partial, true)
        XCTAssertTrue(partial.canFinishGraphics)
        let written = WrittenCut(version: 2, fcpxml: partial.fcpxml, project: partial.project, partial: true)

        let crashed = RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: "the renderer crashed", saved: partial, written: written)
        XCTAssertEqual(crashed, .wroteAndStopped(partial, problem: "Version 2 was written, but its graphics weren't finished: the renderer crashed. Your earlier version is still there."))

        // Cancel says so; the app's own words for the reason (the CLI's "Stopped: cancelled." is not shown).
        let cancelled = RunEnding.after(.revise, from: v1, result: nil, cancelled: true, error: "roughcut stopped (exit code 15).", saved: partial, written: written)
        XCTAssertEqual(cancelled, .wroteAndStopped(partial, problem: "Version 2 was written, but its graphics weren't finished: the revision was cancelled. Your earlier version is still there."))

        // A crash or a Mac that slept sends no error event at all: result.json alone shows the version is newer.
        let silent = RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: "roughcut stopped (exit code 1).", saved: partial, written: nil)
        XCTAssertEqual(silent, .wroteAndStopped(partial, problem: "Version 2 was written, but its graphics weren't finished: roughcut stopped (exit code 1). Your earlier version is still there."))

        // Re-cuts and finishing graphics follow the same rule.
        XCTAssertEqual(
            RunEnding.after(.recut, from: v1, result: nil, cancelled: true, error: "", saved: partial),
            .wroteAndStopped(partial, problem: "Version 2 was written, but its graphics weren't finished: the re-cut was cancelled. Your earlier version is still there.")
        )
        // A finished run is shown as it was, whatever the folder says.
        let v3 = try cut(3)
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: v3, cancelled: false, error: "", saved: partial, written: written), .finished(v3))
    }

    func testAUsageLimitDuringTheGraphicsShowsTheVersionItWrote() throws {
        // The limit hit while the graphics were designed: version 2 is written with the graphics that were done (here,
        // the earlier ones in their earlier look). The error event says so, and so does result.json.
        let v1 = try cut(1, graphics: (planned: 2, placed: 2, rendered: 2, pending: nil))
        let events = try FixtureFiles.lines("progress-limit-revise-sample").compactMap(JSONLines.event(from:))
        let error = try XCTUnwrap(events.last)
        XCTAssertEqual(error.event, "error")
        XCTAssertEqual(error.kind, "usage_limit")
        XCTAssertEqual(error.written?.designMissing, true)
        let limited = try saved("result-pending-revise-sample")
        XCTAssertNil(limited.partial)
        XCTAssertEqual(limited.graphics?.pending, 2)
        XCTAssertTrue(limited.canFinishGraphics)

        let ending = RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: error.message ?? "", saved: limited, written: error.written)
        guard case .wroteAndStopped(let shown, let problem) = ending else {
            return XCTFail("a usage limit that came after version 2 was written should show version 2, not \(ending)")
        }
        XCTAssertEqual(shown, limited)
        XCTAssertTrue(problem.hasPrefix("Version 2 was written, but its graphics weren't finished: You've hit your usage limit."), problem)
        // The finished write moved version 1 to the folder's history, so the banner doesn't say it is still beside it.
        XCTAssertTrue(problem.hasSuffix("Your earlier version is kept in the folder's history."), problem)
        XCTAssertFalse(problem.contains("is still there"))
    }

    func testARunThatWroteNothingNewerGoesBackToItsCut() throws {
        let v1 = try cut(1)
        let v2 = try cut(2)
        let limit = "Claude Code: your plan's usage limit was reached. Nothing changed."
        // No result.json (or one that can't be read), or one that describes the cut the run started from.
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: limit, saved: nil, written: nil), .returned(v1, problem: limit))
        XCTAssertEqual(RunEnding.after(.revise, from: v1, result: nil, cancelled: false, error: limit, saved: v1, written: nil), .returned(v1, problem: limit))
        // An older one (a folder restored from a backup) is not newer.
        XCTAssertEqual(RunEnding.after(.recut, from: v2, result: nil, cancelled: false, error: "x", saved: v1, written: nil), .returned(v2, problem: "x"))
        // A partial cut that is the cut on screen already (Finish graphics stopped before it wrote anything): back to it.
        let partial = try cut(2, graphics: (planned: 2, placed: 0, rendered: 0, pending: nil), partial: true)
        XCTAssertEqual(
            RunEnding.after(.finishGraphics, from: partial, result: nil, cancelled: true, error: "", saved: partial),
            .returned(partial, problem: "Finish graphics cancelled.")
        )
    }

    func testNewerIsNotDecidedByPartialWhenTheVersionsCanBeCompared() throws {
        // A partial cut left by an earlier run is what the folder says, but a run that wrote nothing at all, started from
        // a cut that is already that version or a later one, does not go back to an older partial.
        let v3 = try cut(3)
        let olderPartial = try cut(2, graphics: (planned: 2, placed: 0, rendered: 0, pending: nil), partial: true)
        XCTAssertNil(StoppedRun.newest(saved: olderPartial, source: v3, written: nil))
        XCTAssertNil(StoppedRun.newest(saved: nil, source: v3, written: nil))
        XCTAssertEqual(StoppedRun.newest(saved: try cut(4), source: v3, written: nil), try cut(4))

        // A source from an older CLI has no version: a partial cut, or one the error event named, is newer; nothing else is.
        let unnumbered = try cut(nil)
        XCTAssertEqual(StoppedRun.newest(saved: olderPartial, source: unnumbered, written: nil), olderPartial)
        XCTAssertEqual(StoppedRun.newest(saved: try cut(2), source: unnumbered, written: WrittenCut(version: 2)), try cut(2))
        XCTAssertNil(StoppedRun.newest(saved: try cut(2), source: unnumbered, written: nil))
    }

    func testABuildThatStoppedAfterWritingShowsItsCutOnlyWhenTheRunSaysItWroteIt() throws {
        let v1 = try cut(1, graphics: (planned: 2, placed: 2, rendered: 1, pending: nil))
        let limit = "You've hit your usage limit."
        let written = WrittenCut(version: 1, fcpxml: "/o/Promo_roughcut/Promo v1.fcpxml", project: "Promo", partial: false, designMissing: true)
        XCTAssertEqual(written.folder?.path, "/o/Promo_roughcut")
        let ending = RunEnding.after(.build, from: nil, result: nil, cancelled: false, error: limit, saved: v1, written: written)
        // A build has no earlier version to mention.
        XCTAssertEqual(ending, .wroteAndStopped(v1, problem: "Version 1 was written, but its graphics weren't finished: You've hit your usage limit."))
        // The folder's result.json may be an earlier run's: without the error event's `written`, or for another version, it counts for nothing.
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: false, error: "x", saved: v1, written: nil), .failed("x"))
        XCTAssertEqual(RunEnding.after(.build, from: nil, result: nil, cancelled: true, error: "", saved: v1, written: nil), .cancelled)
        XCTAssertEqual(
            RunEnding.after(.build, from: nil, result: nil, cancelled: false, error: "x", saved: v1, written: WrittenCut(version: 3)),
            .failed("x")
        )
        // `written` names no version: the file is taken as the run's.
        XCTAssertEqual(StoppedRun.newest(saved: v1, source: nil, written: WrittenCut(fcpxml: "/o/Promo_roughcut/Promo v1.fcpxml")), v1)
    }

    func testTheBannerOfAVersionWhoseGraphicsAreAllMadeDoesNotClaimTheyAreNot() throws {
        let finished = try cut(2, graphics: (planned: 2, placed: 2, rendered: 2, pending: nil))
        XCTAssertFalse(finished.canFinishGraphics)
        let problem = StoppedRun.problem(for: finished, kind: .revise, cancelled: false, reason: "Disk full", hasEarlier: true)
        XCTAssertEqual(problem, "Version 2 was written before the run stopped: Disk full. Your earlier version is kept in the folder's history.")
        XCTAssertEqual(
            StoppedRun.problem(for: try cut(nil), kind: .recut, cancelled: false, reason: "  ", hasEarlier: false),
            "A newer version was written before the run stopped: the run stopped."
        )
        // A reason that already ends a sentence is not given a second full stop.
        XCTAssertEqual(
            StoppedRun.problem(for: finished, kind: .revise, cancelled: false, reason: "Is the disk full?", hasEarlier: false),
            "Version 2 was written before the run stopped: Is the disk full?"
        )
    }

    // MARK: What the window says

    func testUsageLimitAdviceFitsTheRun() {
        let revise = RunKind.revise.limitAdvice ?? ""
        XCTAssertTrue(revise.contains("Revise again"))
        XCTAssertFalse(revise.contains("Build"))  // a new build re-plans from scratch as a new version
        XCTAssertTrue(RunKind.build.limitAdvice?.contains("Build again") ?? false)
        XCTAssertNil(RunKind.recut.limitAdvice)  // a re-cut makes no model call
        XCTAssertTrue(RunKind.finishGraphics.limitAdvice?.contains("Finish graphics again") ?? false)
        XCTAssertEqual(RunKind.revise.readyTitle, "Revision ready")
        XCTAssertEqual(RunKind.recut.noun, "Re-cut")
    }

    func testFinishGraphicsIsARunKindOfItsOwn() {
        XCTAssertEqual(RunKind.finishGraphics.noun, "Finish graphics")
        XCTAssertEqual(RunKind.finishGraphics.readyTitle, "Graphics ready")
        XCTAssertEqual(RunKind.finishGraphics.runningTitle, "Finishing the graphics…")
        XCTAssertEqual(RunKind.finishGraphics.cancelledPhrase, "finishing the graphics was cancelled.")
        XCTAssertEqual(RunKind.revise.runningTitle, "Revising from your notes…")
        XCTAssertEqual(RunKind.recut.runningTitle, "Re-cutting…")
        XCTAssertEqual(RunKind.build.runningTitle, "Building your rough cut…")
    }

    func testAfterARunThatLeftAVersionTheAdviceIsFinishGraphics() throws {
        let partial = try cut(2, graphics: (planned: 2, placed: 0, rendered: 0, pending: nil), partial: true)
        let limited = try cut(2, graphics: (planned: 2, placed: 2, rendered: 2, pending: 2))
        for newest in [partial, limited] {
            let afterLimit = try XCTUnwrap(RunKind.revise.stoppedAdvice(usageLimit: true, newest: newest))
            XCTAssertTrue(afterLimit.contains("Finish graphics"), afterLimit)
            XCTAssertTrue(afterLimit.contains("After your plan resets"), afterLimit)
            XCTAssertFalse(afterLimit.contains("Revise again"), afterLimit)  // revising again is not what finishes it
            let afterCancel = try XCTUnwrap(RunKind.revise.stoppedAdvice(usageLimit: false, newest: newest))
            XCTAssertTrue(afterCancel.contains("Click Finish graphics"), afterCancel)
            XCTAssertFalse(afterCancel.contains("plan resets"), afterCancel)  // a cancel is not a limit
        }
        // A build that left a version says the same.
        XCTAssertTrue(try XCTUnwrap(RunKind.build.stoppedAdvice(usageLimit: true, newest: limited)).contains("Finish graphics"))
    }

    func testALimitBeforeAnythingWasWrittenKeepsItsAdvice() throws {
        XCTAssertEqual(RunKind.revise.stoppedAdvice(usageLimit: true, newest: nil), RunKind.revise.limitAdvice)
        XCTAssertTrue(RunKind.revise.stoppedAdvice(usageLimit: true, newest: nil)?.contains("Revise again") ?? false)
        XCTAssertEqual(RunKind.build.stoppedAdvice(usageLimit: true, newest: nil), RunKind.build.limitAdvice)
        XCTAssertEqual(RunKind.finishGraphics.stoppedAdvice(usageLimit: true, newest: nil), RunKind.finishGraphics.limitAdvice)
        XCTAssertNil(RunKind.recut.stoppedAdvice(usageLimit: true, newest: nil))
        XCTAssertNil(RunKind.revise.stoppedAdvice(usageLimit: false, newest: nil))  // an ordinary error adds nothing
        // A newer version with nothing left to finish has nothing for Finish graphics to do: as if nothing was written.
        let made = try cut(2, graphics: (planned: 2, placed: 2, rendered: 2, pending: nil))
        XCTAssertEqual(RunKind.revise.stoppedAdvice(usageLimit: true, newest: made), RunKind.revise.limitAdvice)
        XCTAssertNil(RunKind.revise.stoppedAdvice(usageLimit: false, newest: made))
    }

    // MARK: Finish graphics

    func testFinishGraphicsIsOfferedOnlyWhereThereIsSomethingToFinish() throws {
        func offered(_ graphics: Graphics?, partial: Bool = false) throws -> Bool {
            try cut(2, graphics: graphics, partial: partial).canFinishGraphics
        }
        XCTAssertFalse(try offered(nil))  // graphics off for this cut
        XCTAssertFalse(try offered((planned: 2, placed: 2, rendered: 2, pending: nil)))  // all made
        XCTAssertFalse(try offered((planned: 0, placed: 0, rendered: 0, pending: nil)))  // the plan asked for none
        XCTAssertFalse(try offered((planned: 3, placed: 2, rendered: 2, pending: nil)))  // one has no room in this cut: nothing to make
        XCTAssertFalse(try offered((planned: 2, placed: 0, rendered: 0, pending: nil)))  // none fits this cut
        XCTAssertFalse(try offered((planned: 2, placed: 2, rendered: 2, pending: 0)))  // a zero count is none

        XCTAssertTrue(try offered((planned: 2, placed: 2, rendered: 2, pending: 2)))  // on an earlier look or clip, the new one to come
        XCTAssertTrue(try offered((planned: 3, placed: 3, rendered: 1, pending: nil)))  // placed and not designed, or not rendered
        XCTAssertTrue(try offered((planned: 2, placed: 2, rendered: 0, pending: nil)))  // every render failed
        XCTAssertTrue(try offered((planned: 2, placed: 0, rendered: 0, pending: nil), partial: true))  // written before the graphics
        XCTAssertFalse(try offered(nil, partial: true))  // nothing planned: nothing to finish
        XCTAssertFalse(try offered((planned: 0, placed: 0, rendered: 0, pending: nil), partial: true))
    }

    func testHowManyGraphicsAreUnfinished() {
        XCTAssertEqual(GraphicsSummary(planned: 2, placed: 2, rendered: 2, note: nil, pending: nil).unfinished, 0)
        XCTAssertEqual(GraphicsSummary(planned: 2, placed: 2, rendered: 2, note: nil, pending: 2).unfinished, 2)
        XCTAssertEqual(GraphicsSummary(planned: 5, placed: 4, rendered: 1, note: nil, pending: nil).unfinished, 3)
        XCTAssertEqual(GraphicsSummary(planned: 5, placed: 4, rendered: 3, note: nil, pending: 1).unfinished, 2)
        // Never negative, whatever an odd result.json says.
        XCTAssertEqual(GraphicsSummary(planned: 1, placed: 1, rendered: 3, note: nil, pending: nil).unfinished, 0)
    }

    func testFinishGraphicsReadsTheRealResultsOfStoppedRevisions() throws {
        XCTAssertTrue(try saved("result-partial-revise-sample").canFinishGraphics)  // a cut written before its graphics
        XCTAssertTrue(try saved("result-pending-revise-sample").canFinishGraphics)  // a usage limit: the earlier look stands in
    }

    // MARK: The import hint

    func testTheImportHintSaysWhatToChooseForLaterVersions() throws {
        let line = #"{"event":"done","result":{"title":"T","out_dir":"/o","fcpxml":"/o/T v2.fcpxml","report":"/o/edit_report.md","rough_seconds":20.5,"stringout_seconds":null,"edits":4,"broll":1,"sections":["A"],"planned_by":"anthropic:claude-opus-5-5","fell_back":false,"warnings":[],"version":2,"project":"T - Rough Cut v2 (medium, Opus)","event":"T (2026-08-12)"}}"#
        let v2 = try XCTUnwrap(JSONLines.event(from: line)?.result)
        XCTAssertEqual(
            v2.importHint,
            "In Final Cut Pro choose a library; then open the project “T - Rough Cut v2 (medium, Opus)” in the event “T (2026-08-12)”. If Final Cut asks about items with the same names, choose Keep Both: each version is its own project."
        )
        XCTAssertEqual(RunResult.sameNamesHint, "If Final Cut asks about items with the same names, choose Keep Both: each version is its own project.")

        // The first version goes into an event of its own: nothing to choose.
        var v1 = v2
        v1.version = 1
        XCTAssertEqual(v1.importHint, "In Final Cut Pro choose a library; then open the project “T - Rough Cut v2 (medium, Opus)” in the event “T (2026-08-12)”.")
        XCTAssertFalse(v1.importHint.contains("Keep Both"))
        // An older CLI names no version, project or event: the plain hint it always had.
        let older = try cut(nil)
        XCTAssertEqual(older.importHint, "Final Cut opens its import dialog. Choose a library and the event appears with both projects.")
        let withoutProject = try cut(3)
        XCTAssertTrue(withoutProject.importHint.hasSuffix("Keep Both: each version is its own project."))
    }
}

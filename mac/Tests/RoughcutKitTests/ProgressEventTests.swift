import XCTest
@testable import RoughcutKit

final class ProgressEventTests: XCTestCase {
    /// Fixtures/progress-sample.jsonl is real `roughcut build --progress-json`
    /// output. tests/test_progress.py checks it still matches the CLI.
    func testDecodesRealCLIStream() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "progress-sample", withExtension: "jsonl", subdirectory: "Fixtures"))
        let lines = try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n")
            .map(String.init)
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")

        var stages: [Stage] = []
        for stage in events.compactMap(\.stageValue) where stages.last != stage {
            stages.append(stage)
        }
        // Stages arrive in pipeline order; the sample run had motion graphics off.
        XCTAssertEqual(stages, stages.sorted { $0.order < $1.order })
        XCTAssertEqual(Set(Stage.allCases).subtracting(stages), stages.contains(.graphics) ? [] : [.graphics])

        let transcribe = try XCTUnwrap(events.last { $0.stageValue == .transcribe })
        XCTAssertEqual(transcribe.current, transcribe.total)

        let done = try XCTUnwrap(events.last)
        XCTAssertEqual(done.event, "done")
        let result = try XCTUnwrap(done.result)
        XCTAssertEqual(result.title, "Mountain Day")
        XCTAssertTrue(result.fcpxml.hasSuffix(".fcpxml"))
        XCTAssertEqual(result.fcpxmlURL.pathExtension, "fcpxml")
        XCTAssertEqual(result.sections.first, "Cold open")
        XCTAssertGreaterThan(result.roughSeconds, 0)
        XCTAssertNotNil(result.stringoutSeconds)
        XCTAssertFalse(result.fellBack)
        XCTAssertFalse(result.warnings.isEmpty)
        // Keys added with numbered versions and the run summary.
        XCTAssertEqual(result.version, 1)
        XCTAssertTrue(result.project?.contains("Rough Cut v1") ?? false)
        XCTAssertNotNil(result.event)
        XCTAssertNotNil(result.visualLog)
        XCTAssertNotNil(result.skipped)
        XCTAssertTrue(result.reportHtml?.hasSuffix(".html") ?? false)
        // What a re-cut keeps unless it changes it (Re-cut's "Same as this cut").
        XCTAssertEqual(result.cut, CutSettings(style: "medium", format: nil, keepFillers: false, stringout: true, punchIn: false))
        XCTAssertEqual(result.cut?.sequenceFormat, .auto)
        XCTAssertFalse(result.madeWithoutAI)
        XCTAssertEqual(result.captionsURL?.lastPathComponent, "Trip v1.srt")  // beside "Trip v1.fcpxml"
        XCTAssertNil(result.changes)  // a first build changed nothing from an earlier version
    }

    func testWhatARevisionOrRecutChangedDecodes() throws {
        func done(_ changes: String?) -> String {
            let tail = changes.map { #","changes":\#($0)"# } ?? ""
            return #"{"event":"done","result":{"title":"T","out_dir":"/o","fcpxml":"/o/T v2.fcpxml","report":"/o/edit_report.md","rough_seconds":20.5,"stringout_seconds":null,"edits":4,"broll":1,"sections":["A"],"planned_by":"anthropic:claude-opus-5-5","fell_back":false,"warnings":[],"version":2\#(tail)}}"#
        }
        let changed = try XCTUnwrap(JSONLines.event(from: done(#"{"from_version":1,"items":["Length: 0:30 → 0:20 (−0:10)","Graphics: 2 redesigned (every graphic is designed again)"],"unchanged":false}"#))?.result)
        XCTAssertEqual(changed.changes, RunChanges(fromVersion: 1, items: ["Length: 0:30 → 0:20 (−0:10)", "Graphics: 2 redesigned (every graphic is designed again)"], unchanged: false))

        let same = try XCTUnwrap(JSONLines.event(from: done(#"{"from_version":1,"items":["This version plays the same lines, B-roll and graphics as v1. If you meant how the graphics look, attach reference images or turn on Redesign the graphics (Terminal: --graphics-ref IMAGE or --redesign-graphics)."],"unchanged":true}"#))?.result)
        XCTAssertEqual(same.changes?.unchanged, true)
        XCTAssertEqual(same.changes?.items.count, 1)

        let unknownVersion = try XCTUnwrap(JSONLines.event(from: done(#"{"from_version":null,"items":[],"unchanged":false}"#))?.result)
        XCTAssertEqual(unknownVersion.changes, RunChanges(fromVersion: nil, items: [], unchanged: false))

        // A first build, and a CLI from before the key existed, both have none.
        XCTAssertNil(try XCTUnwrap(JSONLines.event(from: done("null"))?.result).changes)
        XCTAssertNil(try XCTUnwrap(JSONLines.event(from: done(nil))?.result).changes)
    }

    /// Fixtures/progress-revise-sample.jsonl is real `roughcut revise --progress-json` output (done event with
    /// `changes`). tests/test_progress.py checks it still matches the CLI.
    func testDecodesARealReviseStream() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "progress-revise-sample", withExtension: "jsonl", subdirectory: "Fixtures"))
        let lines = try String(contentsOf: url, encoding: .utf8)
            .split(separator: "\n")
            .map(String.init)
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")

        let stages = events.compactMap(\.stageValue)
        XCTAssertEqual(stages, [.plan, .cut, .write])  // a revision plans, cuts and writes
        let result = try XCTUnwrap(events.last?.result)
        XCTAssertEqual(result.version, 2)
        XCTAssertTrue(result.fcpxml.hasSuffix("Trip v2.fcpxml"))
        let changes = try XCTUnwrap(result.changes)
        XCTAssertEqual(changes.fromVersion, 1)
        XCTAssertFalse(changes.unchanged)
        XCTAssertTrue(changes.hasContent)
        XCTAssertTrue(changes.items.contains { $0.hasPrefix("Length:") })
        XCTAssertTrue(changes.items.contains { $0.hasPrefix("Removed V01.S004") })
        XCTAssertNil(changes.highlighted)
        XCTAssertEqual(changes.bullets, changes.items)
        XCTAssertEqual(changes.comparison, "Compared with version 1")
    }

    func testTheWhatChangedBoxReadsItsLines() throws {
        let changed = RunChanges(fromVersion: 2, items: ["Length: 0:30 → 0:20 (−0:10)", "Graphics: 2 redesigned (every graphic is designed again)"], unchanged: false)
        XCTAssertTrue(changed.hasContent)
        XCTAssertEqual(changed.comparison, "Compared with version 2")
        XCTAssertNil(changed.highlighted)  // a change isn't set apart
        XCTAssertEqual(changed.bullets, changed.items)

        // Nothing a viewer would see changed: the first line says so, set apart; the rest are bullets.
        let same = RunChanges(fromVersion: 1, items: ["This version plays the same lines as v1. Try X.", "Another thing to try"], unchanged: true)
        XCTAssertEqual(same.highlighted, "This version plays the same lines as v1. Try X.")
        XCTAssertEqual(same.bullets, ["Another thing to try"])
        XCTAssertEqual(RunChanges(items: ["Only line"], unchanged: true).bullets, [])

        // Advice listed beside it never takes its place, whichever comes first (the CLI puts the sentence first).
        let advice = "Your notes ask for graphics, but motion graphics are off for this cut, so the revision has none."
        let sentence = "This version plays the same lines and B-roll as v1. Motion graphics are off for this cut: turn them on to add or change them."
        let first = RunChanges(fromVersion: 1, items: [sentence, advice], unchanged: true)
        XCTAssertEqual(first.highlighted, sentence)
        XCTAssertEqual(first.bullets, [advice])
        let second = RunChanges(fromVersion: 1, items: [advice, sentence], unchanged: true)
        XCTAssertEqual(second.highlighted, sentence)
        XCTAssertEqual(second.bullets, [advice])
        let sameCut = RunChanges(fromVersion: 2, items: ["Fades: on → off", "This is the same cut as v2."], unchanged: true)
        XCTAssertEqual(sameCut.highlighted, "This is the same cut as v2.")
        XCTAssertEqual(sameCut.bullets, ["Fades: on → off"])
        // A change never highlights anything, whatever it says.
        XCTAssertNil(RunChanges(items: [sentence], unchanged: false).highlighted)

        // Nothing to say, no box; a folder that doesn't say its earlier version has no comparison line.
        XCTAssertFalse(RunChanges().hasContent)
        XCTAssertNil(RunChanges(items: ["x"]).comparison)
        XCTAssertEqual(RunChanges.heading, "What changed in this version")
    }

    func testWhatChangedSurvivesBeingSavedWithRecentRuns() throws {
        let line = #"{"event":"done","result":{"title":"T","out_dir":"/o","fcpxml":"/o/T v2.fcpxml","report":"/o/edit_report.md","rough_seconds":20.5,"stringout_seconds":null,"edits":4,"broll":1,"sections":["A"],"planned_by":"anthropic:claude-opus-5-5","fell_back":false,"warnings":[],"version":2,"changes":{"from_version":1,"items":["This is the same cut as v1."],"unchanged":true}}}"#
        let result = try XCTUnwrap(JSONLines.event(from: line)?.result)
        XCTAssertEqual(result.changes, RunChanges(fromVersion: 1, items: ["This is the same cut as v1."], unchanged: true))
        let saved = try JSONEncoder().encode(result)
        XCTAssertEqual(try JSONDecoder().decode(RunResult.self, from: saved), result)
        // A recent run saved before the app knew the key still opens.
        let older = #"{"title":"T","outDir":"/o","fcpxml":"/o/T.fcpxml","report":"/o/r.md","roughSeconds":1,"edits":1,"broll":0,"sections":[],"plannedBy":"none","fellBack":false,"warnings":[]}"#
        XCTAssertNil(try JSONDecoder().decode(RunResult.self, from: Data(older.utf8)).changes)
    }

    // MARK: Runs that stopped, and graphics that wait

    /// Fixtures/progress-stopped-revise-sample.jsonl is real `roughcut revise --progress-json` output: the renderer crashed
    /// after the cut without graphics was written. tests/test_progress.py checks it still matches the CLI.
    func testAStoppedRevisionsErrorEventNamesTheCutItLeft() throws {
        let lines = try FixtureFiles.lines("progress-stopped-revise-sample")
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")
        let error = try XCTUnwrap(events.last)
        XCTAssertEqual(error.event, "error")
        XCTAssertNil(error.kind)  // a crash: neither a usage limit nor a cancel
        XCTAssertEqual(error.resume, "roughcut render /Users/you/Movies/Trip_roughcut")
        let written = try XCTUnwrap(error.written)
        XCTAssertEqual(written.version, 2)
        XCTAssertEqual(written.partial, true)
        XCTAssertNil(written.designMissing)
        XCTAssertTrue(written.fcpxml?.hasSuffix("Trip v2.fcpxml") ?? false)
        XCTAssertEqual(written.project, "Trip - Rough Cut v2 (medium, fake-model)")
        XCTAssertEqual(written.folder?.path, "/Users/you/Movies/Trip_roughcut")
        XCTAssertEqual(ResumeCommand.outputFolder(from: error.resume)?.path, written.folder?.path)
    }

    /// Fixtures/progress-limit-revise-sample.jsonl: the usage limit hit while the graphics were designed, after version 2
    /// was written with the graphics that were done.
    func testAUsageLimitAfterTheCutWasWrittenNamesTheVersion() throws {
        let lines = try FixtureFiles.lines("progress-limit-revise-sample")
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")
        let stages = events.compactMap(\.stageValue)
        XCTAssertEqual(stages, stages.sorted { $0.order < $1.order })  // only ever forward
        let error = try XCTUnwrap(events.last)
        XCTAssertEqual(error.event, "error")
        XCTAssertEqual(error.kind, "usage_limit")
        XCTAssertNil(error.resume)  // the cut is written: plain `render` would only make it again
        let written = try XCTUnwrap(error.written)
        XCTAssertEqual(written.version, 2)
        XCTAssertEqual(written.partial, false)
        XCTAssertEqual(written.designMissing, true)
        XCTAssertEqual(written.folder?.path, "/Users/you/Movies/Trip_roughcut")
        XCTAssertTrue(error.message?.contains("--design-missing") ?? false)
    }

    func testAnErrorThatWroteNothingHasNoWrittenCut() throws {
        // Older CLIs never send the key, and a stop before anything was written doesn't either.
        let plain = try XCTUnwrap(JSONLines.event(from: #"{"event":"error","message":"No media files found."}"#))
        XCTAssertNil(plain.written)
        let limit = try XCTUnwrap(JSONLines.event(from: #"{"event":"error","message":"limit","resume":"roughcut render /o","kind":"usage_limit"}"#))
        XCTAssertNil(limit.written)
        XCTAssertEqual(limit.kind, "usage_limit")
        // Keys the app doesn't know yet are ignored; the ones it does are all optional.
        let sparse = try XCTUnwrap(JSONLines.event(from: #"{"event":"error","message":"x","written":{"version":3,"future":"key"}}"#)?.written)
        XCTAssertEqual(sparse, WrittenCut(version: 3))
        XCTAssertNil(sparse.folder)
    }

    /// Fixtures/progress-pending-revise-sample.jsonl: a done event whose graphics stand on an earlier version's clips.
    func testGraphicsThatKeepAnEarlierClipDecode() throws {
        let lines = try FixtureFiles.lines("progress-pending-revise-sample")
        let events = lines.compactMap(JSONLines.event(from:))
        XCTAssertEqual(events.count, lines.count, "every line should decode")
        let result = try XCTUnwrap(events.last?.result)
        XCTAssertEqual(result.graphics?.planned, 2)
        XCTAssertEqual(result.graphics?.placed, 2)
        XCTAssertEqual(result.graphics?.rendered, 2)
        XCTAssertEqual(result.graphics?.pending, 2)
        XCTAssertTrue(result.graphics?.note?.contains("earlier clip") ?? false)
        XCTAssertNil(result.partial)  // a done event never carries it
        XCTAssertTrue(result.canFinishGraphics)
    }

    /// Fixtures/result-partial-revise-sample.json is the `result.json` that run left: the cut written before its graphics.
    func testASavedPartialResultDecodesWithTheNewKeys() throws {
        let partial = try XCTUnwrap(SavedResult.decode(FixtureFiles.data("result-partial-revise-sample", "json")))
        XCTAssertEqual(partial.partial, true)
        XCTAssertEqual(partial.version, 2)
        XCTAssertEqual(partial.title, "Mountain Day")
        XCTAssertTrue(partial.fcpxml.hasSuffix("Trip v2.fcpxml"))
        XCTAssertEqual(partial.graphics, GraphicsSummary(planned: 2, placed: 0, rendered: 0, note: "not made yet: this cut was written before its graphics", pending: nil))
        XCTAssertEqual(partial.changes?.fromVersion, 1)
        // The list of a cut written before its graphics says none is in it yet, not what became of each (nothing has).
        XCTAssertEqual(partial.changes?.items, ["Graphics: none in this cut yet; it was written before they were made."])
        XCTAssertTrue(partial.warnings.contains { $0.hasPrefix("This cut is partial") })
        XCTAssertTrue(partial.canFinishGraphics)
        // Recent runs keep it and read it back, the new key included.
        let kept = try JSONEncoder().encode(partial)
        XCTAssertEqual(try JSONDecoder().decode(RunResult.self, from: kept), partial)

        // The finished write of a version that stopped for the usage limit has `pending` and no `partial`.
        let limited = try XCTUnwrap(SavedResult.decode(FixtureFiles.data("result-pending-revise-sample", "json")))
        XCTAssertNil(limited.partial)
        XCTAssertEqual(limited.graphics?.pending, 2)
        XCTAssertEqual(limited.version, 2)
        // ...and does not say the redesign happened: no new design came back.
        XCTAssertEqual(limited.changes?.items.first, "Redesigning the graphics: no new design came back, so they keep their earlier look (see Warnings).")
    }

    func testASavedResultWithoutTheNewKeysDecodes() throws {
        // A result.json from before `partial` and `pending` existed.
        let old = #"{"title":"T","out_dir":"/o","fcpxml":"/o/T v1.fcpxml","report":"/o/edit_report.md","rough_seconds":20.5,"stringout_seconds":null,"edits":4,"broll":1,"sections":["A"],"planned_by":"fake:x","fell_back":false,"warnings":[],"version":1,"graphics":{"planned":2,"placed":2,"rendered":2,"note":null}}"#
        let result = try XCTUnwrap(SavedResult.decode(Data(old.utf8)))
        XCTAssertNil(result.partial)
        XCTAssertNil(result.graphics?.pending)
        XCTAssertEqual(result.graphics, GraphicsSummary(planned: 2, placed: 2, rendered: 2, note: nil))
        XCTAssertFalse(result.canFinishGraphics)
        // A build with graphics off has none of it; its real done event decodes the same way.
        let events = try FixtureFiles.lines("progress-sample").compactMap(JSONLines.event(from:))
        let build = try XCTUnwrap(events.last?.result)
        XCTAssertNil(build.graphics)
        XCTAssertNil(build.partial)
        XCTAssertFalse(build.canFinishGraphics)
        // Something that isn't a result is none.
        XCTAssertNil(SavedResult.decode(Data("not json".utf8)))
        XCTAssertNil(SavedResult.decode(Data(#"{"version":2}"#.utf8)))
    }

    func testTheSavedResultIsReadFromTheOutputFolder() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-saved-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        XCTAssertNil(SavedResult.read(in: folder))  // nothing written there
        let file = folder.appendingPathComponent(SavedResult.fileName)
        XCTAssertEqual(SavedResult.fileName, "result.json")
        try "half a file {".write(to: file, atomically: true, encoding: .utf8)
        XCTAssertNil(SavedResult.read(in: folder))  // unreadable is as good as missing
        try FixtureFiles.data("result-partial-revise-sample", "json").write(to: file)
        let read = try XCTUnwrap(SavedResult.read(in: folder))
        XCTAssertEqual(read.version, 2)
        XCTAssertEqual(read.partial, true)
        XCTAssertNil(SavedResult.read(in: folder.appendingPathComponent("missing", isDirectory: true)))
    }

    func testUnknownStageStillDecodes() throws {
        let event = try XCTUnwrap(JSONLines.event(from: #"{"event":"stage","stage":"future-stage","message":"x"}"#))
        XCTAssertNil(event.stageValue)
        XCTAssertEqual(event.message, "x")
    }

    func testErrorEvent() throws {
        let event = try XCTUnwrap(JSONLines.event(from: #"{"event":"error","message":"No media files found."}"#))
        XCTAssertEqual(event.event, "error")
        XCTAssertNil(event.result)
    }

    func testNonEventLinesAreNotEvents() {
        XCTAssertNil(JSONLines.event(from: "[roughcut] found 5 media files"))
        XCTAssertNil(JSONLines.event(from: #"{"version":"0.1.0"}"#))  // doctor output has no "event"
    }

    func testDoctorReport() throws {
        let line = #"{"version": "0.1.0", "python": "3.12.4", "executable": "/x/.venv/bin/python", "ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": null, "transcriber": "mlx", "anthropic_key": true, "openai_key": false, "openai_installed": false}"#
        let report = try XCTUnwrap(JSONLines.doctor(from: line))
        XCTAssertEqual(report.transcriber, "mlx")
        XCTAssertTrue(report.anthropicKey)
        XCTAssertTrue(report.anthropicKeyWorks)  // older CLI: no status, fall back to "set"
        XCTAssertFalse(report.hasFFmpeg)  // ffprobe missing

        let rejected = #"{"version": "0.1.0", "python": "3.12.4", "executable": "/x", "ffmpeg": null, "ffprobe": null, "transcriber": null, "anthropic_key": true, "anthropic_key_status": "rejected", "anthropic_key_message": "Anthropic rejected the key.", "openai_key": false, "openai_installed": false}"#
        let bad = try XCTUnwrap(JSONLines.doctor(from: rejected))
        XCTAssertFalse(bad.anthropicKeyWorks)
        XCTAssertEqual(bad.anthropicKeyMessage, "Anthropic rejected the key.")
    }

    func testDoctorReportVentureStudio() throws {
        // An older CLI has no venture_studio key: still decodes, no row.
        let older = #"{"version": "1.0.0", "python": "3.12.4", "executable": "/x", "ffmpeg": null, "ffprobe": null, "transcriber": null, "anthropic_key": false, "openai_key": false, "openai_installed": false}"#
        XCTAssertNil(try XCTUnwrap(JSONLines.doctor(from: older)).ventureStudio)

        // As `roughcut doctor --json --offline` prints it.
        let line = #"{"version": "1.0.0", "python": "3.12.4", "executable": "/x", "ffmpeg": "/opt/homebrew/bin/ffmpeg", "ffprobe": "/opt/homebrew/bin/ffprobe", "transcriber": "mlx", "anthropic_key": false, "anthropic_key_status": "missing", "anthropic_key_message": "No key.", "anthropic_key_source": null, "openai_key": false, "openai_installed": false, "claude_code": null, "claude_code_auth": null, "graphics": "not checked", "venture_studio": {"installed": true, "available": true, "reason": null, "folder": "/Users/you/Library/Application Support/com.hauptli.venturestudio", "snapshot": {"id": "1700000500-1-2", "name": "Before catalog restore", "created_at": 1700000500, "when": "12 Oct 21:43", "age_hours": 5.0, "before_restore": true, "imported": false, "copy": "/Users/you/Library/Caches/roughcut/venture-studio/1700000500-1-2.sqlite"}, "items": 2, "records": 2, "libraries": [{"name": "Studio Library", "path": "/Volumes/Shared/Studio Library", "online": null}], "notes": ["The newest catalog snapshot is the copy Studio saved just before a catalog restore."], "settings": {"automatic_updates": true, "daily_backups": true}, "summary": "catalog snapshot from 12 Oct 21:43 (5 h old), 2 items, 1 folder (not checked); it's the copy from before a catalog restore", "offline": true}}"#
        let studio = try XCTUnwrap(JSONLines.doctor(from: line)?.ventureStudio)
        XCTAssertTrue(studio.isInstalled)
        XCTAssertFalse(studio.isReady)  // a note to read
        XCTAssertEqual(studio.detail, "catalog snapshot from 12 Oct 21:43 (5 h old), 2 items, 1 folder (not checked); it's the copy from before a catalog restore (1 note: roughcut studio status)")

        let absent = #"{"version": "1.0.0", "python": "3.12.4", "executable": "/x", "anthropic_key": false, "openai_key": false, "openai_installed": false, "venture_studio": {"installed": false, "available": false, "reason": "not installed", "folder": "/Users/you/Library/Application Support/com.hauptli.venturestudio", "snapshot": null, "items": null, "records": null, "libraries": [], "notes": [], "settings": null, "summary": "not installed", "offline": false}}"#
        let notInstalled = try XCTUnwrap(JSONLines.doctor(from: absent)?.ventureStudio)
        XCTAssertFalse(notInstalled.isInstalled)
        XCTAssertFalse(notInstalled.isReady)
        XCTAssertEqual(notInstalled.detail, "Not installed (optional)")

        let noSnapshot = #"{"version": "1.0.0", "python": "3.12.4", "executable": "/x", "anthropic_key": false, "openai_key": false, "openai_installed": false, "venture_studio": {"installed": true, "available": false, "reason": "Venture Studio has no catalog snapshot yet (in Studio, click Back up catalog now)", "libraries": [], "notes": []}}"#
        let waiting = try XCTUnwrap(JSONLines.doctor(from: noSnapshot)?.ventureStudio)
        XCTAssertFalse(waiting.isReady)
        XCTAssertEqual(waiting.detail, "Venture Studio has no catalog snapshot yet (in Studio, click Back up catalog now)")
    }
}

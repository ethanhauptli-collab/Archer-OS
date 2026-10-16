import AppKit
import Observation
import RoughcutKit
import UserNotifications

private let preferencesKey = "buildPreferences.v1"
private let recentsKey = "recentRuns.v1"

/// A finished run, remembered so it can be reopened from the form.
struct RecentRun: Codable, Identifiable, Equatable {
    var result: RunResult
    var finishedAt: Date
    var id: String { result.fcpxml }
}

@MainActor
@Observable
final class AppModel {
    enum Phase: Equatable {
        case idle
        case running
        case finished(RunResult)
        case failed(String)
        case cancelled
    }

    // Form
    var options: BuildOptions = AppModel.loadPreferences()

    // The build as a line of steps (see BuildFlow): where the person is, and what is in each footage source.
    var flow = BuildFlow()
    private(set) var footageCounts: [URL: FootageCount] = [:]
    @ObservationIgnored private var footageTasks: [URL: Task<Void, Never>] = [:]

    // Run state
    private(set) var phase: Phase = .idle
    private(set) var runKind: RunKind = .build
    /// After a failed run whose plan was saved: the folder `roughcut render` can finish without AI.
    private(set) var resumeFolder: URL?
    /// After a failed run: "usage_limit" when a Claude plan or API credit ran out.
    private(set) var failureKind: String?
    /// Why the last re-cut or revision of the cut on screen stopped (shown above it).
    private(set) var runProblem: String?
    /// What to do about it, under the banner (Finish graphics after a run that left a version without all its graphics).
    private(set) var runAdvice: String?
    /// The last Finish graphics run ended on a version that is no further on (the model was busy, a render failed again):
    /// the button then says "Try again" (see `RunResult.finishGraphicsMadeNothing`).
    private(set) var finishGraphicsTriedInVain = false
    private(set) var stageStatus: [Stage: StageStatus] = [:]
    private(set) var activeStage: Stage?
    private(set) var stageDetail = ""
    private(set) var progressCurrent: Int?
    private(set) var progressTotal: Int?
    private(set) var log: [String] = []

    // The Revise box, kept here rather than in the view, so a revision that stops or is cancelled
    // comes back to the cut with the notes still in it.
    var reviseNotes = ""
    var reviseTarget = ""
    /// Who re-plans a cut made without AI (it has no planner of its own to reuse).
    var reviseProvider: ModelProvider = .anthropic
    /// Pictures of the look wanted for the graphics, for this revision only (the build form's are `options.graphicsReferences`).
    var reviseReferences: [URL] = []
    /// Design every graphic again with this revision. Off until asked; adding a picture asks (and removing the last one takes that back).
    var reviseRedesign = RedesignSwitch()

    // Finished runs, newest first
    private(set) var recentRuns: [RecentRun] = AppModel.loadRecents()
    /// Recent runs whose Final Cut file the last check (`refreshRecents`) did not find.
    private var goneRecents: Set<String> = []

    // Presets from `roughcut preset list --json`
    private(set) var presets: [Preset] = []

    // Venture Studio's audio for the Music bed (`roughcut studio music --json`), and what a drop or paste left out
    private(set) var studioSongs: [StudioSong] = []
    private(set) var studioSongsLoading = false
    private(set) var studioSongsProblem: String?
    var musicNote: String?

    // `roughcut update`
    private(set) var updating = false
    var updateOutput: String?

    // Environment
    private(set) var cliURL: URL? = CLILocator.locate()
    private(set) var doctor: DoctorReport?
    private(set) var doctorError: String?
    private(set) var checkingSetup = false

    @ObservationIgnored private var process: CLIProcess?
    @ObservationIgnored private var studioProcess: CLIProcess?
    @ObservationIgnored private var cancelRequested = false
    @ObservationIgnored private var lastError: String?
    @ObservationIgnored private var lastResult: RunResult?
    @ObservationIgnored private var lastResume: String?
    @ObservationIgnored private var lastKind: String?
    /// The `error` event's `written`: the version a stopped run left behind.
    @ObservationIgnored private var lastWritten: WrittenCut?
    /// The cut a re-cut or revision started from: where it goes back to if it stops.
    @ObservationIgnored private var runSource: RunResult?
    /// The version in the folder's result.json when that run began: a run is blamed only for a version newer than it.
    @ObservationIgnored private var runBaseline: Int?
    /// A revision that stopped after writing its version: the Revise box still holds its notes, which that version already
    /// has, so finishing the version (Finish graphics, Finish without AI) clears the box, as a finished revision does.
    @ObservationIgnored private var stoppedRevision = StoppedRevision()
    /// This run finishes the revision that stopped (see `stoppedRevision`).
    @ObservationIgnored private var runFinishesRevision = false
    /// The revision's notes, written for `--notes-file`; deleted when the run ends.
    @ObservationIgnored private var notesFile: URL?
    /// The output folder the Revise box's notes are for.
    @ObservationIgnored private var draftFolder: String?
    /// The remembered preset is filled in again the first time the list is read.
    @ObservationIgnored private var presetsLoaded = false
    /// Keeps the Mac from idle-sleeping while a run is going.
    @ObservationIgnored private var activity: NSObjectProtocol?

    var isRunning: Bool { phase == .running }

    var visibleStages: [Stage] {
        switch runKind {
        case .build: return Stage.allCases
        case .recut, .finishGraphics: return [.cut, .graphics, .write]
        case .revise: return [.plan, .cut, .graphics, .write]
        }
    }

    func status(of stage: Stage) -> StageStatus {
        stageStatus[stage] ?? .pending
    }

    // MARK: Reference images

    /// At launch: pasted pictures older than 30 days that the saved list doesn't use are removed from the app's References folder.
    func tidyReferences() {
        ReferenceFiles.removeStale(keeping: options.graphicsReferences)
    }

    // MARK: Sources

    func addSources(_ urls: [URL]) {
        for url in urls where url.isFileURL {
            let clean = url.standardizedFileURL
            if !options.sources.contains(clean) {
                options.sources.append(clean)
                countFootage(in: clean)
            }
        }
    }

    func removeSource(_ url: URL) {
        options.sources.removeAll { $0 == url }
        footageTasks[url]?.cancel()
        footageTasks[url] = nil
        footageCounts[url] = nil
    }

    /// What the dropped sources hold, once every one has been counted (nil while any is still being read, or when there are none).
    var footageSummary: FootageCount? {
        guard !options.sources.isEmpty else { return nil }
        var total = FootageCount()
        for source in options.sources {
            guard let counted = footageCounts[source] else { return nil }
            total = total + counted
        }
        return total
    }

    /// Counts a source's files from their names, off the main thread and for a few seconds at most: a folder on a share that
    /// has stopped answering never holds up the window (its count just doesn't appear).
    private func countFootage(in source: URL) {
        footageTasks[source]?.cancel()
        footageTasks[source] = Task { [weak self] in
            let counted = await Task.detached(priority: .utility) {
                FootageCount.count(source: source, deadline: Date().addingTimeInterval(4))
            }.value
            guard !Task.isCancelled, let self else { return }
            self.footageCounts[source] = counted
            self.footageTasks[source] = nil
        }
    }

    // MARK: Get ready

    /// The Get ready page was set aside for this session ("Skip for now").
    var getReadySetAside = false

    /// The Get ready page's rows for the planner the build has chosen.
    var setupRows: [SetupCheck] {
        SetupChecks.rows(cliFound: cliURL != nil, doctor: doctor, doctorError: doctorError, options: options)
    }

    /// Something Roughcut needs is missing, and nothing has been dropped yet: the page that says what to do comes first.
    var needsGetReady: Bool {
        !getReadySetAside && options.sources.isEmpty && flow.step == .footage && SetupChecks.isBlocked(setupRows)
    }

    // MARK: The line of steps

    func go(to step: FlowStep) {
        flow.go(to: step)
    }

    func goNext() {
        guard flow.canAdvance(for: options) else { return }
        flow.goNext(for: options)
    }

    func goBack() {
        flow.goBack(for: options)
    }

    /// A click on a station of the strip. Without footage only the first step makes sense.
    func jump(toStation id: String) {
        guard !isRunning else { return }
        guard let step = flow.steps(for: options).first(where: { flow.station(of: $0).id == id }) else { return }
        if options.sources.isEmpty && step != .footage { return }
        flow.go(to: step)
    }

    /// Puts a part on the line (Music and Effects wait for their files in their own step; Graphics need a model).
    func addPart(_ part: LinePart) {
        flow.add(part, to: &options)
    }

    func removePart(_ part: LinePart) {
        flow.remove(part, from: &options)
    }

    /// After anything that can change the line (a preset, the provider): a step whose part is gone becomes the Add step.
    func repairFlow() {
        flow.repair(for: options)
    }

    // MARK: Music bed

    /// Adds the song files among `urls` to the Music bed; anything else goes nowhere, with a note. True when one was added.
    @discardableResult
    func addMusic(_ urls: [URL]) -> Bool {
        let sorted = MusicFiles.sort(urls)
        for url in sorted.songs where !options.music.contains(url) {
            options.music.append(url)
        }
        musicNote = MusicFiles.leftOutNote(sorted.leftOut)
        return !sorted.songs.isEmpty
    }

    /// Studio is on this Mac (doctor's word): the Music bed offers its songs.
    var studioInstalled: Bool { doctor?.ventureStudio?.isInstalled ?? false }

    /// Reads Studio's audio (`roughcut studio music --json`) without holding up the window: the CLI reads only its
    /// catalog snapshot, and is stopped after 30 seconds, or by Stop.
    func loadStudioSongs() async {
        guard !studioSongsLoading else { return }
        guard let cli = cliURL ?? CLILocator.locate() else {
            studioSongsProblem = "Can't find the roughcut command-line tool. Set its location in Settings."
            return
        }
        studioSongsLoading = true
        studioSongsProblem = nil
        let process = CLIProcess(executable: cli, arguments: StudioMusic.arguments(), environment: CLILocator.environment(for: cli, extra: [:]))
        studioProcess = process
        let result = await process.collect(timeout: 30)
        if studioProcess === process {
            studioProcess = nil
        }
        studioSongsLoading = false
        if Task.isCancelled {
            return  // the sheet closed
        }
        if let problem = result.problem {
            studioSongsProblem = problem
        } else if result.stopped {
            studioSongsProblem = "Stopped before Studio's songs were read."
        } else if let songs = StudioMusic.songs(from: result.output) {
            studioSongs = songs
            studioSongsProblem = songs.isEmpty ? "Venture Studio's catalog snapshot lists no audio." : nil
        } else {
            studioSongsProblem = "roughcut studio music printed nothing readable. Run `roughcut studio music` in Terminal to see why."
        }
    }

    func cancelStudioSongs() {
        studioProcess?.cancel()
    }

    // MARK: Running

    func build() {
        guard let cli = resolveCLI() else { return }
        let problems = options.problems
        guard problems.isEmpty else {
            phase = .failed(problems.joined(separator: "\n"))
            return
        }
        savePreferences()
        run(cli: cli, arguments: options.arguments(), kind: .build)
    }

    /// Re-cut at `style`; `format` and `punchIn` nil keep the cut's own. Fillers and the stringout stay as the cut has them
    /// (the build form's toggles belong to the next build, not to this cut).
    func recut(_ result: RunResult, style: CutStyle, format: SequenceFormat?, punchIn: Bool? = nil) {
        guard let cli = resolveCLI() else { return }
        let recut = RecutOptions(outputFolder: result.outDirURL, style: style, format: format, punchIn: punchIn)
        run(cli: cli, arguments: recut.arguments(), kind: .recut, from: result)
    }

    /// Make the graphics a cut lacks (`roughcut render <folder> --design-missing`): the ones with no design yet or still in an
    /// earlier look are designed with the model that planned the cut, every graphic is rendered, and the result is a new
    /// version. The cut isn't planned again. Stages, Cancel, errors and the result are those of a re-cut.
    func finishGraphics(_ result: RunResult) {
        guard result.canFinishGraphics, let cli = resolveCLI() else { return }
        run(
            cli: cli, arguments: RecutOptions.finishingGraphics(of: result).arguments(), kind: .finishGraphics, from: result,
            finishesRevision: stoppedRevision.isFinished(by: result)
        )
    }

    /// Finish a stopped run from its saved plan with the run's own settings and no model call
    /// (`roughcut render <folder>`, as the CLI suggests).
    func finishWithoutAI() {
        guard let folder = resumeFolder, let cli = resolveCLI() else { return }
        var shown: RunResult?
        if case .finished(let result) = phase {
            shown = result  // a stopped re-cut or revision: come back to this cut if finishing stops too
        }
        run(
            cli: cli, arguments: RecutOptions.finishingWithoutAI(in: folder, shown: shown).arguments(), kind: .recut, from: shown,
            finishesRevision: stoppedRevision.isFinished(by: shown)
        )
    }

    /// Re-plan the cut from the Revise box's notes as a new version (`roughcut revise`).
    func revise(_ result: RunResult) {
        guard let cli = resolveCLI() else { return }
        let text = reviseNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        // Pictures and the redesign count only for a cut with graphics; pictures can stand in for notes, the redesign can't.
        let references = ReviseOptions.usableReferences(reviseReferences, for: result)
        let redesign = ReviseOptions.usableRedesign(reviseRedesign.isOn, for: result)
        guard ReviseOptions.canStart(notes: text, references: references), ReviseOptions.lengthProblem(reviseTarget) == nil else { return }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-notes-\(UUID().uuidString).txt")
        do {
            try text.write(to: file, atomically: true, encoding: .utf8)
        } catch {
            clearProblem()
            runProblem = "Couldn't save the notes for the revision: \(error.localizedDescription)"
            return
        }
        notesFile = file
        stoppedRevision.clear()  // these notes are the revision now; an earlier one that stopped is not what the box holds
        let provider = ReviseOptions.planner(reviseProvider, form: options, doctor: doctor)
        let revise = ReviseOptions.revising(
            result, notesFile: file, targetLength: reviseTarget, provider: provider, form: options,
            references: references, redesignGraphics: redesign
        )
        run(cli: cli, arguments: revise.arguments(), kind: .revise, from: result)
    }

    func cancel() {
        cancelRequested = true
        process?.cancel()
    }

    /// Back to the line, keeping the footage and brief for another pass (at Ready; at the first step when there is no footage).
    func startOver() {
        guard !isRunning else { return }
        clearProblem()
        finishGraphicsTriedInVain = false
        phase = .idle
        flow.go(to: options.sources.isEmpty ? .footage : .ready)
    }

    private func clearProblem() {
        runProblem = nil
        runAdvice = nil
        resumeFolder = nil
        failureKind = nil
    }

    private func resolveCLI() -> URL? {
        if cliURL == nil {
            cliURL = CLILocator.locate()
        }
        guard let cli = cliURL else {
            phase = .failed("Can't find the roughcut command-line tool. Open Settings (⌘,) and point to …/Archer-OS/.venv/bin/roughcut.")
            return nil
        }
        return cli
    }

    /// `source`: the cut a re-cut or revision starts from (it comes back to it if the run stops).
    /// `finishesRevision`: it completes a revision that stopped after writing its version.
    private func run(cli: URL, arguments: [String], kind: RunKind, from source: RunResult? = nil, finishesRevision: Bool = false) {
        runKind = kind
        runSource = source
        runBaseline = nil
        runFinishesRevision = finishesRevision
        finishGraphicsTriedInVain = false
        clearProblem()
        lastResume = nil
        lastKind = nil
        lastWritten = nil
        let recut = kind != .build
        stageStatus = [:]
        activeStage = nil
        stageDetail = ""
        progressCurrent = nil
        progressTotal = nil
        log = ["$ roughcut " + arguments.map(Self.shellQuoted).joined(separator: " ")]
        cancelRequested = false
        lastError = nil
        lastResult = nil
        phase = .running
        endActivity()
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled],
            reason: recut ? "Re-cutting a rough cut" : "Building a rough cut"
        )

        let process = CLIProcess(
            executable: cli,
            arguments: arguments,
            environment: CLILocator.environment(for: cli, extra: apiKeyEnvironment())
        )
        self.process = process
        Task {
            // Before the run changes anything: which version the folder holds, so a stop is not blamed for a version it did not
            // write (read off the main thread and given up on after a few seconds, as a stalled share would never answer).
            if let source {
                self.runBaseline = (await SavedResult.read(in: source.outDirURL, timeout: SavedResult.readSeconds))?.version
            }
            if self.cancelRequested {  // Cancel came before the command started
                await self.handle(.exited(status: 15, signaled: true))
                return
            }
            // Ignore stragglers from a previous run that is still shutting down.
            for await output in process.run() where self.process === process {
                await self.handle(output)
            }
        }
    }

    private func handle(_ output: RunOutput) async {
        switch output {
        case .event(let event):
            apply(event)
        case .stdout(let line), .stderr(let line):
            appendLog(line)
        case .failedToLaunch(let message):
            process = nil
            endActivity()
            discardNotesFile()
            end(RunEnding.after(runKind, from: runSource, result: nil, cancelled: false, error: "Couldn't start roughcut: \(message)"))
        case .exited(let status, _):
            process = nil
            endActivity()
            discardNotesFile()
            if phase == .running {
                if lastResult == nil {
                    // A cancelled run whose plan was saved can be finished too: the CLI sends `resume` on SIGTERM.
                    resumeFolder = ResumeCommand.outputFolder(from: lastResume)
                    failureKind = cancelRequested ? nil : lastKind
                }
                let tail = log.suffix(6).joined(separator: "\n")
                let error = lastError ?? "roughcut stopped (exit code \(status)).\n\(tail)"
                // The newest version in the folder (read off the main thread, and given up on after a few seconds: the share the
                // folder is on is often why the run stopped).
                var saved: RunResult?
                if lastResult == nil {
                    saved = await savedResult()
                }
                end(RunEnding.after(
                    runKind, from: runSource, result: lastResult, cancelled: cancelRequested, error: error,
                    saved: saved, written: lastWritten, baseline: runBaseline, usageLimit: failureKind == "usage_limit"
                ))
            }
            runEnded()
        }
    }

    /// The newest version in the output folder (its `result.json`, which the CLI keeps with each version), read after a run
    /// stopped: a revision that stopped while its graphics were made leaves a newer version than the cut it started from.
    private func savedResult() async -> RunResult? {
        guard let folder = runSource?.outDirURL ?? lastWritten?.folder else { return nil }
        return await SavedResult.read(in: folder, timeout: SavedResult.readSeconds)
    }

    /// A failed or cancelled re-cut or revision goes back to its cut (notes kept), or, when it wrote a newer version first,
    /// to that version, saying what it lacks; a build to the form.
    private func end(_ ending: RunEnding) {
        switch ending {
        case .finished(let result):
            if runKind == .revise || runFinishesRevision {
                reviseNotes = ""  // they're in this version now (and in its paper edit)
                reviseTarget = ""
                reviseReferences = []  // the CLI stored them with this version
                reviseRedesign.reset()
                stoppedRevision.clear()
            }
            finishGraphicsTriedInVain = runKind == .finishGraphics && result.finishGraphicsMadeNothing
            show(result)
        case .returned(let source, let problem):
            runProblem = problem
            runAdvice = runKind.stoppedAdvice(usageLimit: failureKind == "usage_limit", newest: nil)
            show(source)
        case .wroteAndStopped(let newest, let problem):
            runProblem = problem
            runAdvice = runKind.stoppedAdvice(usageLimit: failureKind == "usage_limit", newest: newest)
            remember(newest)  // it has a Final Cut file of its own now: the form's recent list can open it
            show(newest)
            if runKind == .revise {
                stoppedRevision.remember(newest)  // its notes are in that version; the box keeps them until it is finished
            }
        case .failed(let message):
            phase = .failed(message)
        case .cancelled:
            phase = .cancelled
        }
    }

    /// Shows a cut. The Revise box keeps its text while it's the same project's cut.
    private func show(_ result: RunResult) {
        if result.outDir != draftFolder {
            reviseNotes = ""
            reviseTarget = ""
            reviseReferences = []
            reviseRedesign.reset()
            stoppedRevision.clear()
            reviseProvider = ReviseOptions.defaultProvider(form: options, doctor: doctor)
            draftFolder = result.outDir
        }
        phase = .finished(result)
    }

    private func discardNotesFile() {
        if let notesFile {
            try? FileManager.default.removeItem(at: notesFile)  // the CLI read it at the start
            self.notesFile = nil
        }
    }

    private func endActivity() {
        if let activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
    }

    /// Remember the result, open it if asked, and tell the user when they're in another app.
    private func runEnded() {
        if let result = lastResult {
            remember(result)
            if options.openInFinalCutWhenDone {
                openInFinalCut(result)
            }
            notify(title: runKind.readyTitle, body: result.version.map { "\(result.title), version \($0)" } ?? result.title)
            return
        }
        guard !cancelRequested else { return }
        if case .failed(let message) = phase {
            notify(title: "Roughcut stopped", body: String(message.prefix(240)))
        } else if let problem = runProblem {
            notify(title: "Roughcut stopped", body: String(problem.prefix(240)))
        }
    }

    private func notify(title: String, body: String) {
        guard !NSApp.isActive else { return }
        NSApp.requestUserAttention(.informationalRequest)
        // Notifications need a real app bundle; `swift run` has none.
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        Task {
            let center = UNUserNotificationCenter.current()
            guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = .default
            try? await center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    private func apply(_ event: ProgressEvent) {
        switch event.event {
        case "stage":
            guard let stage = event.stageValue else { return }
            for earlier in Stage.allCases where earlier.order < stage.order {
                let current = stageStatus[earlier] ?? .pending
                if current == .active {
                    stageStatus[earlier] = .done
                } else if current == .pending {
                    stageStatus[earlier] = .skipped
                }
            }
            stageStatus[stage] = .active
            activeStage = stage
            stageDetail = event.message ?? ""
            progressCurrent = event.current
            progressTotal = event.total
        case "log":
            break  // the same line also arrives on stderr
        case "done":
            guard let result = event.result else { return }
            for stage in Stage.allCases where stageStatus[stage] == .active {
                stageStatus[stage] = .done
            }
            lastResult = result
            end(.finished(result))
        case "error":
            lastError = event.message
            lastResume = event.resume ?? lastResume
            lastKind = event.kind ?? lastKind
            lastWritten = event.written ?? lastWritten
        default:
            break
        }
    }

    private func appendLog(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        // Collapse redraws of the same progress bar into one line.
        if trimmed.contains("%|"), let last = log.last, last.contains("%|") {
            log[log.count - 1] = trimmed
        } else {
            log.append(trimmed)
        }
        if log.count > 600 {
            log.removeFirst(log.count - 600)
        }
    }

    nonisolated private static func shellQuoted(_ arg: String) -> String {
        let safe = arg.allSatisfy { $0.isLetter || $0.isNumber || "-_./=:@".contains($0) }
        return safe ? arg : "'" + arg.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    // MARK: Results

    func openInFinalCut(_ result: RunResult) {
        let workspace = NSWorkspace.shared
        let finalCut = workspace.urlForApplication(withBundleIdentifier: "com.apple.FinalCut")
            ?? workspace.urlForApplication(withBundleIdentifier: "com.apple.FinalCutTrial")
        if let finalCut {
            workspace.open(
                [result.fcpxmlURL],
                withApplicationAt: finalCut,
                configuration: NSWorkspace.OpenConfiguration(),
                completionHandler: { _, _ in }
            )
        } else {
            workspace.open(result.fcpxmlURL)
        }
    }

    func revealInFinder(_ result: RunResult) {
        NSWorkspace.shared.activateFileViewerSelecting([result.fcpxmlURL])
    }

    func revealCaptions(_ result: RunResult) {
        if let captions = result.captionsURL {
            NSWorkspace.shared.activateFileViewerSelecting([captions])
        }
    }

    /// Opens the paper edit: the web page when there is one. Whether it is there is asked off the main thread (the output
    /// folder may be on a network share that has stopped answering, and the window must not wait on it).
    func openReport(_ result: RunResult) {
        Task {
            var url = result.reportURL
            if let html = result.reportHtml, await PathProbe.presence(of: html) == .there {
                url = URL(fileURLWithPath: html)
            }
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: Recent runs

    /// Recent runs whose Final Cut file was still there the last time they were checked (`refreshRecents`). It never asks the
    /// disk itself: a run's folder is often on a network share, and a share that has stopped answering would freeze the window
    /// each time the form is drawn.
    var availableRecents: [RecentRun] {
        recentRuns.filter { !goneRecents.contains($0.id) }
    }

    /// Looks for the recent runs' Final Cut files, off the main thread and at most a couple of seconds each. A file whose drive
    /// doesn't answer keeps what was known of it (shown, until a check finds it gone).
    func refreshRecents() async {
        let answers = await PathProbe.presence(of: recentRuns.map(\.id))
        for (path, answer) in answers {
            switch answer {
            case .gone: goneRecents.insert(path)
            case .there: goneRecents.remove(path)
            case .silent: break
            }
        }
    }

    func showRecent(_ run: RecentRun) {
        guard !isRunning else { return }
        runKind = .build
        clearProblem()
        finishGraphicsTriedInVain = false
        show(run.result)
    }

    func forgetRecent(_ run: RecentRun) {
        recentRuns.removeAll { $0.id == run.id }
        saveRecents()
    }

    private func remember(_ result: RunResult) {
        goneRecents.remove(result.fcpxml)  // written just now, whatever an earlier check found
        recentRuns.removeAll { $0.result.fcpxml == result.fcpxml }
        recentRuns.insert(RecentRun(result: result, finishedAt: Date()), at: 0)
        recentRuns = Array(recentRuns.prefix(12))
        saveRecents()
    }

    private func saveRecents() {
        if let data = try? JSONEncoder().encode(recentRuns) {
            UserDefaults.standard.set(data, forKey: recentsKey)
        }
    }

    nonisolated private static func loadRecents() -> [RecentRun] {
        guard let data = UserDefaults.standard.data(forKey: recentsKey) else { return [] }
        return (try? JSONDecoder().decode([RecentRun].self, from: data)) ?? []
    }

    // MARK: Setup

    func apiKeyEnvironment() -> [String: String] {
        var env: [String: String] = [:]
        for account in Keychain.Account.allCases {
            if let value = Keychain.read(account), !value.isEmpty {
                env[account.rawValue] = value
            }
        }
        return env
    }

    func setCLI(_ url: URL?) {
        if let url {
            UserDefaults.standard.set(url.path, forKey: CLILocator.defaultsKey)
        } else {
            UserDefaults.standard.removeObject(forKey: CLILocator.defaultsKey)
        }
        cliURL = nil
        Task { await refreshDoctor() }
    }

    /// Runs `roughcut doctor --json` with the same environment builds get.
    func refreshDoctor() async {
        checkingSetup = true
        defer { checkingSetup = false }
        cliURL = CLILocator.locate()
        guard let cli = cliURL else {
            doctor = nil
            doctorError = "The roughcut command-line tool wasn't found."
            return
        }
        let process = CLIProcess(
            executable: cli,
            arguments: ["doctor", "--json"],
            environment: CLILocator.environment(for: cli, extra: apiKeyEnvironment())
        )
        var report: DoctorReport?
        var lastLine = ""
        for await output in process.run() {
            switch output {
            case .stdout(let line):
                report = JSONLines.doctor(from: line) ?? report
            case .stderr(let line):
                lastLine = line
            case .failedToLaunch(let message):
                lastLine = message
            default:
                break
            }
        }
        doctor = report
        doctorError = report == nil ? (lastLine.isEmpty ? "`roughcut doctor` didn't answer." : lastLine) : nil
        await refreshPresets()
    }

    // MARK: Presets

    /// Reads the built-in and saved presets (`roughcut preset list --json`).
    func refreshPresets() async {
        guard let cli = cliURL else { return }
        let output = await collectStdout(cli: cli, arguments: ["preset", "list", "--json"])
        guard let list = JSONLines.presets(from: output) else { return }
        presets = list
        if !presetsLoaded {
            // After a relaunch the picker still names the preset, but the brief and length weren't
            // remembered and its look may have changed: fill them in from the preset as it is now.
            // Only once, so a later refresh (Check again, Update) never overwrites the form mid-project.
            presetsLoaded = true
            if !options.presetName.isEmpty {
                options.reapplyPreset(from: list)
            }
        }
    }

    /// Fill the form from a preset; "" clears the choice and leaves the form as it is.
    func applyPreset(named name: String) {
        guard let preset = presets.first(where: { $0.name == name }) else {
            options.presetName = ""
            return
        }
        preset.apply(to: &options, replacing: presets.compactMap(\.brief))  // graphics stay off while the provider is None
    }

    // MARK: Updating

    /// `roughcut update`: pull the latest version into the checkout the CLI runs from.
    func updateRoughcut() {
        guard !updating, let cli = resolveCLI() else { return }
        updating = true
        Task {
            let output = await collectStdout(cli: cli, arguments: ["update"], includeStderr: true)
            updating = false
            updateOutput = output.isEmpty ? "roughcut update didn't say anything." : output
            await refreshDoctor()
        }
    }

    private func collectStdout(cli: URL, arguments: [String], includeStderr: Bool = false) async -> String {
        let process = CLIProcess(
            executable: cli,
            arguments: arguments,
            environment: CLILocator.environment(for: cli, extra: apiKeyEnvironment())
        )
        var lines: [String] = []
        for await output in process.run() {
            switch output {
            case .stdout(let line):
                lines.append(line)
            case .event(let event):
                if let message = event.message { lines.append(message) }
            case .stderr(let line) where includeStderr:
                lines.append(line)
            case .failedToLaunch(let message):
                lines.append(message)
            default:
                break
            }
        }
        return lines.joined(separator: "\n")
    }

    /// Things that will stop a build, or make it worse than expected, for the banner at the top of the window.
    var setupIssues: [String] {
        if cliURL == nil {
            return ["Roughcut's command-line tool wasn't found. Set its location in Settings."]
        }
        if let doctorError {
            return ["The command-line tool didn't run: \(doctorError)"]
        }
        guard let doctor else { return [] }
        var issues: [String] = []
        if !doctor.hasFFmpeg {
            issues.append("ffmpeg isn't installed. In Terminal: brew install ffmpeg")
        }
        if doctor.transcriber == nil {
            issues.append("No transcriber installed. In the repo: pip install -e '.[mac]'")
        }
        if options.provider == .anthropic && !doctor.anthropicKeyWorks {
            if let message = doctor.anthropicKeyMessage, !message.isEmpty {
                issues.append(message)
            } else {
                issues.append("Add your Claude API key in Settings.")
            }
        }
        if options.graphics, let problem = doctor.graphics, problem != "not checked" {
            issues.append(problem)
        }
        if options.provider == .claudeCode {
            if doctor.claudeCode == nil {
                issues.append("Claude Code isn't installed. In Terminal: curl -fsSL https://claude.ai/install.sh | bash   (then open a new Terminal window)")
            } else if !doctor.claudeCodeUsesSubscription {
                issues.append("Sign Claude Code in with your Pro/Max plan: in Terminal run claude and sign in with your Claude account in the browser (type /login if it doesn't ask).")
            }
            if let note = doctor.picturesNote(for: options) {
                issues.append(note)  // not a stop: the build runs, without clip pictures
            }
        }
        if (options.provider == .openai || options.provider == .openaiCompatible) && !doctor.openaiInstalled {
            issues.append("OpenAI support isn't installed. In the repo: pip install -e '.[openai]'")
        } else if options.provider == .openai && !doctor.openaiKey {
            issues.append("Add your OpenAI API key in Settings.")
        }
        return issues
    }

    // MARK: Preferences

    nonisolated private static func loadPreferences() -> BuildOptions {
        guard let data = UserDefaults.standard.data(forKey: preferencesKey),
              let saved = try? JSONDecoder().decode(BuildOptions.self, from: data) else {
            return BuildOptions()
        }
        return saved.preferencesOnly
    }

    func savePreferences() {
        if let data = try? JSONEncoder().encode(options.preferencesOnly) {
            UserDefaults.standard.set(data, forKey: preferencesKey)
        }
    }
}

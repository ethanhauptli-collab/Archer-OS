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

enum StageStatus: Equatable {
    case pending, active, done, skipped
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

    // Run state
    private(set) var phase: Phase = .idle
    private(set) var runKind: RunKind = .build
    var isRecut: Bool { runKind == .recut }
    /// After a failed run whose plan was saved: the folder `roughcut render` can finish without AI.
    private(set) var resumeFolder: URL?
    /// After a failed run: "usage_limit" when a Claude plan or API credit ran out.
    private(set) var failureKind: String?
    /// Why the last re-cut or revision of the cut on screen stopped (shown above it).
    private(set) var runProblem: String?
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

    // Finished runs, newest first
    private(set) var recentRuns: [RecentRun] = AppModel.loadRecents()

    // Presets from `roughcut preset list --json`
    private(set) var presets: [Preset] = []

    // `roughcut update`
    private(set) var updating = false
    var updateOutput: String?

    // Environment
    private(set) var cliURL: URL? = CLILocator.locate()
    private(set) var doctor: DoctorReport?
    private(set) var doctorError: String?
    private(set) var checkingSetup = false

    @ObservationIgnored private var process: CLIProcess?
    @ObservationIgnored private var cancelRequested = false
    @ObservationIgnored private var lastError: String?
    @ObservationIgnored private var lastResult: RunResult?
    @ObservationIgnored private var lastResume: String?
    @ObservationIgnored private var lastKind: String?
    /// The cut a re-cut or revision started from: where it goes back to if it stops.
    @ObservationIgnored private var runSource: RunResult?
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
        case .recut: return [.cut, .graphics, .write]
        case .revise: return [.plan, .cut, .graphics, .write]
        }
    }

    func status(of stage: Stage) -> StageStatus {
        stageStatus[stage] ?? .pending
    }

    // MARK: Sources

    func addSources(_ urls: [URL]) {
        for url in urls where url.isFileURL {
            let clean = url.standardizedFileURL
            if !options.sources.contains(clean) {
                options.sources.append(clean)
            }
        }
    }

    func removeSource(_ url: URL) {
        options.sources.removeAll { $0 == url }
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

    /// Re-cut at `style`; `format` nil keeps the cut's own. Fillers and the stringout stay as the cut has them
    /// (the build form's toggles belong to the next build, not to this cut).
    func recut(_ result: RunResult, style: CutStyle, format: SequenceFormat?) {
        guard let cli = resolveCLI() else { return }
        let recut = RecutOptions(outputFolder: result.outDirURL, style: style, format: format)
        run(cli: cli, arguments: recut.arguments(), kind: .recut, from: result)
    }

    /// Finish a stopped run from its saved plan with the run's own settings and no model call
    /// (`roughcut render <folder>`, as the CLI suggests).
    func finishWithoutAI() {
        guard let folder = resumeFolder, let cli = resolveCLI() else { return }
        var shown: RunResult?
        if case .finished(let result) = phase {
            shown = result  // a stopped re-cut or revision: come back to this cut if finishing stops too
        }
        run(cli: cli, arguments: RecutOptions(outputFolder: folder).arguments(), kind: .recut, from: shown)
    }

    /// Re-plan the cut from the Revise box's notes as a new version (`roughcut revise`).
    func revise(_ result: RunResult) {
        guard let cli = resolveCLI() else { return }
        let text = reviseNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, ReviseOptions.lengthProblem(reviseTarget) == nil else { return }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("roughcut-notes-\(UUID().uuidString).txt")
        do {
            try text.write(to: file, atomically: true, encoding: .utf8)
        } catch {
            clearProblem()
            runProblem = "Couldn't save the notes for the revision: \(error.localizedDescription)"
            return
        }
        notesFile = file
        let provider = ReviseOptions.planner(reviseProvider, form: options, doctor: doctor)
        let revise = ReviseOptions.revising(result, notesFile: file, targetLength: reviseTarget, provider: provider, form: options)
        run(cli: cli, arguments: revise.arguments(), kind: .revise, from: result)
    }

    func cancel() {
        cancelRequested = true
        process?.cancel()
    }

    /// Back to the form, keeping the footage and brief for another pass.
    func startOver() {
        guard !isRunning else { return }
        clearProblem()
        phase = .idle
    }

    private func clearProblem() {
        runProblem = nil
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
    private func run(cli: URL, arguments: [String], kind: RunKind, from source: RunResult? = nil) {
        runKind = kind
        runSource = source
        clearProblem()
        lastResume = nil
        lastKind = nil
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
            // Ignore stragglers from a previous run that is still shutting down.
            for await output in process.run() where self.process === process {
                self.handle(output)
            }
        }
    }

    private func handle(_ output: RunOutput) {
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
                end(RunEnding.after(runKind, from: runSource, result: lastResult, cancelled: cancelRequested, error: error))
            }
            runEnded()
        }
    }

    /// A failed or cancelled re-cut or revision goes back to its cut (notes kept); a build to the form.
    private func end(_ ending: RunEnding) {
        switch ending {
        case .finished(let result):
            if runKind == .revise {
                reviseNotes = ""  // they're in this version now (and in its paper edit)
                reviseTarget = ""
            }
            show(result)
        case .returned(let source, let problem):
            runProblem = problem
            show(source)
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

    func openReport(_ result: RunResult) {
        NSWorkspace.shared.open(result.paperEditURL)
    }

    // MARK: Recent runs

    /// Recent runs whose Final Cut file is still on disk.
    var availableRecents: [RecentRun] {
        recentRuns.filter { FileManager.default.fileExists(atPath: $0.result.fcpxml) }
    }

    func showRecent(_ run: RecentRun) {
        guard !isRunning else { return }
        runKind = .build
        clearProblem()
        show(run.result)
    }

    func forgetRecent(_ run: RecentRun) {
        recentRuns.removeAll { $0.id == run.id }
        saveRecents()
    }

    private func remember(_ result: RunResult) {
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
                graphicsNeedAModel()
            }
        }
    }

    /// Fill the form from a preset; "" clears the choice and leaves the form as it is.
    func applyPreset(named name: String) {
        guard let preset = presets.first(where: { $0.name == name }) else {
            options.presetName = ""
            return
        }
        preset.apply(to: &options, replacing: presets.compactMap(\.brief))
        graphicsNeedAModel()
    }

    private func graphicsNeedAModel() {
        if options.provider == .none {
            options.graphics = false  // graphics are designed by the AI
        }
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

    /// Things that will stop a build, for the banner at the top of the window.
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
                issues.append("Claude Code isn't installed. In Terminal: npm install -g @anthropic-ai/claude-code")
            } else if !doctor.claudeCodeUsesSubscription {
                issues.append("Sign Claude Code in with your subscription: in Terminal run claude, then type /login.")
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

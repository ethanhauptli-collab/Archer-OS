import AppKit
import Observation
import RoughcutKit

private let preferencesKey = "buildPreferences.v1"

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
    private(set) var isRecut = false
    private(set) var stageStatus: [Stage: StageStatus] = [:]
    private(set) var activeStage: Stage?
    private(set) var stageDetail = ""
    private(set) var progressCurrent: Int?
    private(set) var progressTotal: Int?
    private(set) var log: [String] = []

    // Environment
    private(set) var cliURL: URL? = CLILocator.locate()
    private(set) var doctor: DoctorReport?
    private(set) var doctorError: String?
    private(set) var checkingSetup = false

    @ObservationIgnored private var process: CLIProcess?
    @ObservationIgnored private var cancelRequested = false
    @ObservationIgnored private var lastError: String?
    @ObservationIgnored private var lastResult: RunResult?

    var isRunning: Bool { phase == .running }

    var visibleStages: [Stage] {
        isRecut ? [.cut, .write] : Stage.allCases
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
        run(cli: cli, arguments: options.arguments(), recut: false)
    }

    func recut(_ result: RunResult, style: CutStyle, format: SequenceFormat) {
        guard let cli = resolveCLI() else { return }
        let recut = RecutOptions(
            outputFolder: result.outDirURL,
            style: style,
            format: format,
            keepFillers: options.keepFillers,
            stringout: options.stringout
        )
        run(cli: cli, arguments: recut.arguments(), recut: true)
    }

    func cancel() {
        cancelRequested = true
        process?.cancel()
    }

    /// Back to the form, keeping the footage and brief for another pass.
    func startOver() {
        guard !isRunning else { return }
        phase = .idle
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

    private func run(cli: URL, arguments: [String], recut: Bool) {
        isRecut = recut
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
            phase = .failed("Couldn't start roughcut: \(message)")
        case .exited(let status, _):
            process = nil
            guard phase == .running else { return }
            if cancelRequested {
                phase = .cancelled
            } else if let result = lastResult {
                phase = .finished(result)
            } else {
                let tail = log.suffix(6).joined(separator: "\n")
                phase = .failed(lastError ?? "roughcut stopped (exit code \(status)).\n\(tail)")
            }
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
            phase = .finished(result)
        case "error":
            lastError = event.message
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

    func openReport(_ result: RunResult) {
        NSWorkspace.shared.open(result.reportURL)
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

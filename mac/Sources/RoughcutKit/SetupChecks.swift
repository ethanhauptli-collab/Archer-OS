import Foundation

/// One line of the Get ready page: something Roughcut needs, whether it's there, and how to put it right.
public struct SetupCheck: Equatable, Identifiable, Sendable {
    public enum Fix: Equatable, Sendable {
        /// A command to run in Terminal.
        case terminal(String)
        /// Sign Claude Code in: run `claude` in Terminal and sign in with the Claude account.
        case signIn
        /// Add the key under Settings.
        case key
        /// Point the app at the command-line tool, under Settings.
        case settings
    }

    public var id: String
    public var title: String
    public var detail: String
    public var ok: Bool
    public var fix: Fix?

    public init(id: String, title: String, detail: String, ok: Bool, fix: Fix? = nil) {
        self.id = id
        self.title = title
        self.detail = detail
        self.ok = ok
        self.fix = fix
    }
}

public enum SetupChecks {
    /// The rows of the Get ready page for the planner the build has chosen: the tools every build needs, the account the planner
    /// signs in with, and the graphics renderer when graphics are on. Empty while the first check is still running.
    public static func rows(cliFound: Bool, doctor: DoctorReport?, doctorError: String?, options: BuildOptions) -> [SetupCheck] {
        if !cliFound {
            return [SetupCheck(
                id: "cli", title: "Roughcut's command-line tool",
                detail: "Not found. Set its location in Settings.", ok: false, fix: .settings
            )]
        }
        if let doctorError, doctor == nil {
            return [SetupCheck(
                id: "cli", title: "Roughcut's command-line tool",
                detail: "It didn't run: \(doctorError)", ok: false, fix: .settings
            )]
        }
        guard let doctor else { return [] }

        var list: [SetupCheck] = []
        list.append(SetupCheck(
            id: "video", title: "Video tools",
            detail: doctor.hasFFmpeg ? "ffmpeg is installed." : "ffmpeg isn't installed.",
            ok: doctor.hasFFmpeg, fix: doctor.hasFFmpeg ? nil : .terminal("brew install ffmpeg")
        ))
        list.append(SetupCheck(
            id: "transcription", title: "Transcription",
            detail: doctor.transcriber == nil ? "No speech engine is installed." : "The speech engine is ready.",
            ok: doctor.transcriber != nil, fix: doctor.transcriber == nil ? .terminal("pip install -e '.[mac]'") : nil
        ))
        if let planner = plannerRow(doctor: doctor, options: options) {
            list.append(planner)
        }
        if options.graphicsActive, let problem = doctor.graphics, problem != "not checked" {
            list.append(SetupCheck(id: "graphics", title: "Motion graphics", detail: problem, ok: false))
        }
        return list
    }

    private static func plannerRow(doctor: DoctorReport, options: BuildOptions) -> SetupCheck? {
        switch options.provider {
        case .claudeCode:
            if doctor.claudeCode == nil {
                return SetupCheck(
                    id: "planner", title: "Claude", detail: "Claude Code isn't installed.", ok: false,
                    fix: .terminal("curl -fsSL https://claude.ai/install.sh | bash")
                )
            }
            if !doctor.claudeCodeUsesSubscription {
                return SetupCheck(
                    id: "planner", title: "Claude",
                    detail: "Not signed in yet. Roughcut uses your Claude Pro or Max plan to plan each edit.", ok: false, fix: .signIn
                )
            }
            return SetupCheck(id: "planner", title: "Claude", detail: "Signed in with your Claude plan.", ok: true)
        case .anthropic:
            if doctor.anthropicKeyWorks {
                return SetupCheck(id: "planner", title: "Claude", detail: "Your API key works.", ok: true)
            }
            let message = doctor.anthropicKeyMessage ?? ""
            return SetupCheck(
                id: "planner", title: "Claude", detail: message.isEmpty ? "Add your Claude API key." : message, ok: false, fix: .key
            )
        case .openai, .openaiCompatible:
            if !doctor.openaiInstalled {
                return SetupCheck(
                    id: "planner", title: "OpenAI", detail: "OpenAI support isn't installed.", ok: false,
                    fix: .terminal("pip install -e '.[openai]'")
                )
            }
            if options.provider == .openai && !doctor.openaiKey {
                return SetupCheck(id: "planner", title: "OpenAI", detail: "Add your OpenAI API key.", ok: false, fix: .key)
            }
            return SetupCheck(id: "planner", title: "OpenAI", detail: "Ready.", ok: true)
        case .ollama, .none:
            return nil
        }
    }

    /// Something is missing that stops a build.
    public static func isBlocked(_ rows: [SetupCheck]) -> Bool {
        rows.contains { !$0.ok }
    }

    /// Only the planner is missing: a build without AI would still run, so the page can offer it.
    public static func onlyThePlannerIsMissing(_ rows: [SetupCheck]) -> Bool {
        let missing = rows.filter { !$0.ok }
        return !missing.isEmpty && missing.allSatisfy { $0.id == "planner" }
    }
}

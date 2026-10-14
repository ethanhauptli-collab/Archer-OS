import AppKit
import RoughcutKit
import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettings()
                .tabItem { Label("General", systemImage: "gearshape") }
            KeySettings()
                .tabItem { Label("API Keys", systemImage: "key") }
        }
        .frame(width: 560, height: 480)
    }
}

struct GeneralSettings: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section {
                LabeledContent("Command-line tool") {
                    Text(model.cliURL?.path ?? "Not found")
                        .font(.system(.body, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.head)
                        .textSelection(.enabled)
                }
                HStack {
                    Button("Choose…") { chooseCLI() }
                    Button("Find automatically") { model.setCLI(nil) }
                    Spacer()
                }
            } footer: {
                Text("Usually …/Archer-OS/.venv/bin/roughcut inside the repo you cloned.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Versions") {
                CheckRow(ok: versionStamp.warning == nil, title: "Roughcut", detail: versionStamp.line)
                if let warning = versionStamp.warning {
                    Text(warning)
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                        .textSelection(.enabled)
                }
            }

            Section("Setup check") {
                if let doctor = model.doctor {
                    CheckRow(ok: true, title: "roughcut \(doctor.version)", detail: "Python \(doctor.python)")
                    CheckRow(ok: doctor.hasFFmpeg, title: "ffmpeg", detail: doctor.ffmpeg ?? "brew install ffmpeg")
                    CheckRow(ok: doctor.transcriber != nil, title: "Transcription", detail: doctor.transcriber ?? "pip install -e '.[mac]'")
                    CheckRow(ok: doctor.anthropicKeyWorks, title: "Claude API key", detail: keyDetail(doctor))
                    CheckRow(ok: doctor.openaiKey && doctor.openaiInstalled, title: "OpenAI (optional)", detail: doctor.openaiInstalled ? (doctor.openaiKey ? "Ready" : "No key") : "Not installed")
                    if doctor.claudeCode != nil {
                        CheckRow(
                            ok: doctor.claudeCodeUsesSubscription && doctor.claudeCodePicturesNote == nil,
                            title: "Claude Code (optional)",
                            detail: claudeCodeDetail(doctor)
                        )
                    }
                    if let studio = doctor.ventureStudio, studio.isInstalled {
                        CheckRow(ok: studio.isReady, title: "Venture Studio", detail: studio.detail)
                    }
                } else {
                    Text(model.doctorError ?? "Checking…")
                        .foregroundStyle(.secondary)
                }
                Button("Check again") {
                    Task { await model.refreshDoctor() }
                }
                .disabled(model.checkingSetup)
            }
        }
        .formStyle(.grouped)
    }

    /// The commit this app was built from (build-app.sh writes it into Info.plist) next to the one the engine runs.
    private var versionStamp: VersionStamp {
        VersionStamp(app: AppStamp(infoDictionary: Bundle.main.infoDictionary), engine: model.doctor)
    }

    private func claudeCodeDetail(_ doctor: DoctorReport) -> String {
        if let note = doctor.claudeCodePicturesNote { return note }
        let version = doctor.claudeCodeVersion.map { " \($0)" } ?? ""
        return doctor.claudeCodeUsesSubscription ? "Signed in with your subscription\(version)" : "Not signed in with a subscription: in Terminal run claude, then type /login"
    }

    private func keyDetail(_ doctor: DoctorReport) -> String {
        switch doctor.anthropicKeyStatus {
        case "ok": return "Accepted by Anthropic"
        case "not checked": return "Set (not tested)"
        case .some: return doctor.anthropicKeyMessage ?? "Not working"
        case .none: return doctor.anthropicKey ? "Found" : "Add it under API Keys"
        }
    }

    private func chooseCLI() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true  // .venv is a hidden folder
        panel.message = "Choose the roughcut command (…/Archer-OS/.venv/bin/roughcut)"
        if panel.runModal() == .OK, let url = panel.url {
            model.setCLI(url)
        }
    }
}

struct CheckRow: View {
    let ok: Bool
    let title: String
    let detail: String

    var body: some View {
        LabeledContent {
            Text(detail)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        } label: {
            Label {
                Text(title)
            } icon: {
                Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle")
                    .foregroundStyle(ok ? Color.green : Color.orange)
            }
        }
    }
}

struct KeySettings: View {
    @Environment(AppModel.self) private var model
    @State private var keys = KeyForm()
    @State private var status = ""
    @State private var importing = false

    var body: some View {
        Form {
            Section {
                keyField(.anthropic, prompt: "sk-ant-…")
                keyField(.openai, prompt: "Optional")
            } footer: {
                Text("Stored in your macOS Keychain and handed to roughcut only while it runs. Apps opened from the Dock don't see keys exported in ~/.zshrc, which is why they're stored here.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("Use keys from Terminal") {
                    importFromShell()
                }
                .disabled(importing)
                .help("Reads ANTHROPIC_API_KEY and OPENAI_API_KEY from your shell profile")
                Spacer()
                Text(status)
                    .foregroundStyle(.secondary)
                Button("Save") { save() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .formStyle(.grouped)
        .onAppear {
            keys.load(from: Keychain.store)
        }
    }

    /// One account's box and, under it, what saving did for that account alone.
    @ViewBuilder
    private func keyField(_ account: KeyAccount, prompt: String) -> some View {
        SecureField(account.title, text: Binding(get: { keys[account] }, set: { keys[account] = $0 }), prompt: Text(prompt))
        if let note = keys.note(for: account) {
            Text(note)
                .font(.caption)
                .foregroundStyle(keys.noteIsProblem(for: account) ? Color.orange : Color.secondary)
        }
    }

    private func save() {
        let results = keys.save(to: Keychain.store)
        status = KeyForm.summary(results)
        if results.contains(where: { $0.outcome == .saved || $0.outcome == .removed }) {
            Task { await model.refreshDoctor() }
        }
    }

    private func importFromShell() {
        importing = true
        status = "Reading your shell profile…"
        Task {
            let found = await Task.detached {
                (ShellEnvironment.value(of: KeyAccount.anthropic.rawValue), ShellEnvironment.value(of: KeyAccount.openai.rawValue))
            }.value
            if let key = found.0 { keys[.anthropic] = key }
            if let key = found.1 { keys[.openai] = key }
            status = (found.0 == nil && found.1 == nil) ? "No keys found in your shell profile" : "Found. Click Save."
            importing = false
        }
    }
}

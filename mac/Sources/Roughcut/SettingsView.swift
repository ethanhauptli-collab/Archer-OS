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
        .frame(width: 560, height: 400)
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

            Section("Setup check") {
                if let doctor = model.doctor {
                    CheckRow(ok: true, title: "roughcut \(doctor.version)", detail: "Python \(doctor.python)")
                    CheckRow(ok: doctor.hasFFmpeg, title: "ffmpeg", detail: doctor.ffmpeg ?? "brew install ffmpeg")
                    CheckRow(ok: doctor.transcriber != nil, title: "Transcription", detail: doctor.transcriber ?? "pip install -e '.[mac]'")
                    CheckRow(ok: doctor.anthropicKeyWorks, title: "Claude API key", detail: keyDetail(doctor))
                    CheckRow(ok: doctor.openaiKey && doctor.openaiInstalled, title: "OpenAI (optional)", detail: doctor.openaiInstalled ? (doctor.openaiKey ? "Ready" : "No key") : "Not installed")
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
    @State private var anthropic = ""
    @State private var openai = ""
    @State private var status = ""
    @State private var importing = false

    var body: some View {
        Form {
            Section {
                SecureField("Claude (Anthropic)", text: $anthropic, prompt: Text("sk-ant-…"))
                SecureField("OpenAI or compatible", text: $openai, prompt: Text("Optional"))
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
            anthropic = Keychain.read(.anthropic) ?? ""
            openai = Keychain.read(.openai) ?? ""
        }
    }

    private func save() {
        let ok = Keychain.save(anthropic, for: .anthropic) && Keychain.save(openai, for: .openai)
        status = ok ? "Saved" : "Keychain error"
        Task { await model.refreshDoctor() }
    }

    private func importFromShell() {
        importing = true
        status = "Reading your shell profile…"
        Task {
            let found = await Task.detached {
                (ShellEnvironment.value(of: "ANTHROPIC_API_KEY"), ShellEnvironment.value(of: "OPENAI_API_KEY"))
            }.value
            if let key = found.0 { anthropic = key }
            if let key = found.1 { openai = key }
            status = (found.0 == nil && found.1 == nil) ? "No keys found in your shell profile" : "Found. Click Save."
            importing = false
        }
    }
}

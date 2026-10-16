import AppKit
import RoughcutKit
import SwiftUI

/// The first thing a person sees when something Roughcut needs isn't there yet (and nothing has been dropped): what's ready,
/// what's missing, and how to put it right. It goes away by itself once the checks pass.
struct GetReadyView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        let rows = model.setupRows
        let missing = rows.filter { !$0.ok }
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    HStack(spacing: 18) {
                        Image(nsImage: NSApplication.shared.applicationIconImage)
                            .resizable()
                            .frame(width: 64, height: 64)
                            .accessibilityHidden(true)
                        StepHeader(
                            title: missing.count == 1 ? "One thing before your first cut" : "A few things before your first cut",
                            subtitle: "Roughcut needs a few tools. Here is what's ready and what's still missing."
                        )
                    }
                    StepCard {
                        ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                            if index > 0 {
                                Divider()
                            }
                            SetupCheckRow(row: row)
                        }
                    }
                }
                .frame(maxWidth: 720, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 32)
                .padding(.bottom, 20)
                .frame(maxWidth: .infinity)
            }
            Divider()
            footer(rows)
        }
    }

    private func footer(_ rows: [SetupCheck]) -> some View {
        HStack(spacing: 12) {
            Button("Skip for now") { model.getReadySetAside = true }
                .buttonStyle(.borderless)
                .controlSize(.large)
            Spacer()
            if rows.contains(where: { $0.fix == .signIn }) {
                Button("Use an API key instead") {
                    model.options.choose(provider: .anthropic)
                    openSettings()
                }
                .controlSize(.large)
            }
            if SetupChecks.onlyThePlannerIsMissing(rows) {
                Button("Continue without AI") {
                    model.options.choose(provider: .none)
                }
                .controlSize(.large)
                .help("Roughcut can still cut the dead air. It won't plan the edit, place B-roll or make graphics.")
            }
            Button("Check again") {
                Task { await model.refreshDoctor() }
            }
            .buttonStyle(LinePrimaryButtonStyle())
            .keyboardShortcut(.defaultAction)
            .disabled(model.checkingSetup)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }
}

private struct SetupCheckRow: View {
    let row: SetupCheck

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: row.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .font(.system(size: 22))
                .foregroundStyle(row.ok ? LineTheme.finished : LineTheme.amber)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.title)
                        .font(.system(size: 15, weight: .semibold))
                    Text(row.detail)
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if !row.ok, let fix = row.fix {
                    SetupFixView(fix: fix)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(row.ok ? Color.clear : LineTheme.amber.opacity(0.07))
    }
}

/// What to do about a missing tool: the command for Terminal (with a Copy button), or where in the app to go.
private struct SetupFixView: View {
    let fix: SetupCheck.Fix
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        switch fix {
        case .signIn:
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 12) {
                    Text("1. Open Terminal and run").font(.system(size: 14))
                    CommandChip(command: "claude")
                }
                Text("2. Sign in with your Claude account, not an API key.").font(.system(size: 14))
                Text("Roughcut checks again when you come back to this window.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        case .terminal(let command):
            VStack(alignment: .leading, spacing: 8) {
                Text(command.hasPrefix("pip ") ? "Run this in Terminal, in the Archer-OS folder:" : "Run this in Terminal:")
                    .font(.system(size: 14))
                CommandChip(command: command)
                Text("Roughcut checks again when you come back to this window.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        case .key:
            HStack(spacing: 12) {
                Text("Add it under Settings, API Keys.").font(.system(size: 14))
                Button("Open Settings…") { openSettings() }
            }
        case .settings:
            HStack(spacing: 12) {
                Text("Set where the tool is under Settings.").font(.system(size: 14))
                Button("Open Settings…") { openSettings() }
            }
        }
    }
}

/// A command in a dark chip, with a Copy button.
private struct CommandChip: View {
    let command: String
    @State private var copied = false

    var body: some View {
        HStack(spacing: 10) {
            Text(command)
                .font(.system(size: 13, design: .monospaced))
                .foregroundStyle(Color(white: 0.95))
                .textSelection(.enabled)
                .lineLimit(2)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(command, forType: .string)
                copied = true
            } label: {
                Text(copied ? "Copied" : "Copy")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Color(white: 0.95))
                    .padding(.horizontal, 10)
                    .frame(height: 26)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color(white: 0.25)))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Copy the command")
        }
        .padding(.leading, 12)
        .padding(.trailing, 6)
        .frame(minHeight: 36)
        .background(RoundedRectangle(cornerRadius: 9).fill(Color(red: 0.114, green: 0.114, blue: 0.125)))
    }
}

import RoughcutKit
import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            if !model.needsGetReady {
                SetupBanner()
            }
            switch model.phase {
            case .running:
                RunView()
            case .finished(let result):
                ResultView(result: result)
            case .idle, .failed, .cancelled:
                if model.needsGetReady {
                    GetReadyView()
                } else {
                    FlowView()
                }
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                RecentMenu()
            }
            ToolbarItem(placement: .primaryAction) {
                NewCutButton()
            }
        }
    }
}

// MARK: - Setup banner

struct SetupBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let issues = model.setupIssues
        if !issues.isEmpty && !model.isRunning {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "wrench.and.screwdriver")
                    .foregroundStyle(Color.orange)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(issues, id: \.self) { issue in
                        Text(issue)
                            .textSelection(.enabled)
                    }
                }
                .font(.callout)
                Spacer()
                SettingsLink {
                    Text("Settings…")
                }
                Button("Check again") {
                    Task { await model.refreshDoctor() }
                }
                .disabled(model.checkingSetup)
            }
            .padding(12)
            .background(Color.orange.opacity(0.12))
        }
    }
}

/// "New Cut" in the toolbar while a finished cut is on screen: back to the line with the footage and brief kept.
struct NewCutButton: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Button {
            model.startOver()
        } label: {
            Label("New Cut", systemImage: "plus")
        }
        .help("Back to the start. The footage and brief stay.")
        .disabled(!isFinished)
        .opacity(isFinished ? 1 : 0)
    }

    private var isFinished: Bool {
        if case .finished = model.phase { return true }
        return false
    }
}

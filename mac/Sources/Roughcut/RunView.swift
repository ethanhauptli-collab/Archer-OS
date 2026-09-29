import RoughcutKit
import SwiftUI

struct RunView: View {
    @Environment(AppModel.self) private var model
    @State private var showLog = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(model.isRecut ? "Re-cutting…" : "Building your rough cut…")
                .font(.title2.weight(.semibold))

            VStack(alignment: .leading, spacing: 12) {
                ForEach(model.visibleStages) { stage in
                    StageRow(
                        stage: stage,
                        status: model.status(of: stage),
                        detail: stage == model.activeStage ? model.stageDetail : nil
                    )
                }
            }

            if let total = model.progressTotal, let current = model.progressCurrent, total > 0 {
                ProgressView(value: Double(current), total: Double(total)) {
                    Text("\(current) of \(total)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            DisclosureGroup("Details", isExpanded: $showLog) {
                LogView(lines: model.log)
                    .frame(height: 180)
            }

            Spacer(minLength: 0)

            HStack {
                Text("Transcripts and clip notes are cached, so a second run on the same footage is much faster.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", role: .cancel) {
                    model.cancel()
                }
                .keyboardShortcut(.cancelAction)
            }
        }
        .padding(24)
    }
}

struct StageRow: View {
    let stage: Stage
    let status: StageStatus
    let detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            icon
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(stage.title)
                    .foregroundStyle(status == .pending || status == .skipped ? Color.secondary : Color.primary)
                if let detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
        }
    }

    @ViewBuilder
    private var icon: some View {
        switch status {
        case .pending:
            Image(systemName: stage.symbol).foregroundStyle(.tertiary)
        case .active:
            ProgressView().controlSize(.small)
        case .done:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
        case .skipped:
            Image(systemName: "minus.circle").foregroundStyle(.tertiary)
        }
    }
}

struct LogView: View {
    let lines: [String]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { item in
                        Text(item.element)
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .textSelection(.enabled)
                .padding(8)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .onChange(of: lines.count) {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
            .onAppear {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }
}

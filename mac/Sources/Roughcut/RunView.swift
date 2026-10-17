import RoughcutKit
import SwiftUI

/// A run as the line at work: the machines along the top (Listen, Watch, Plan, Cut, Graphics, Export), the one that is working
/// in the middle with what it is doing, and Cancel.
struct RunView: View {
    @Environment(AppModel.self) private var model
    @State private var showLog = false

    private var nodes: [MachineNode] {
        MachineLine.nodes(
            for: model.runKind,
            options: model.runKind == .build ? model.options : nil,
            stages: model.stageStatus
        )
    }

    private var working: (machine: Machine, number: Int, of: Int)? {
        let list = nodes
        guard let index = list.firstIndex(where: { $0.status == .active }) else { return nil }
        return (list[index].machine, index + 1, list.count)
    }

    var body: some View {
        VStack(spacing: 0) {
            LineStrip(items: stripItems)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    activity
                    finished
                    DisclosureGroup("Details", isExpanded: $showLog) {
                        LogView(lines: model.log)
                            .frame(height: 180)
                    }
                }
                .frame(maxWidth: 720, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 28)
                .padding(.bottom, 20)
                .frame(maxWidth: .infinity)
            }
            Divider()
            HStack(spacing: 24) {
                Text("Transcripts and clip notes are saved, so a second run on the same footage is much faster.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button("Cancel", role: .cancel) {
                    model.cancel()
                }
                .controlSize(.large)
                .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 14)
        }
    }

    private var stripItems: [StripItem] {
        nodes.map { node in
            let state: StationState
            switch node.status {
            case .done: state = .done
            case .active: state = .current
            case .pending, .skipped: state = .upcoming
            }
            return StripItem(
                id: node.id, label: node.machine.label, symbol: node.machine.symbol, state: state,
                summary: node.status == .active ? "Working" : nil, live: node.status == .active
            )
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let working {
                Text("Step \(working.number) of \(working.of)")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.secondary)
                StepHeader(title: working.machine.heading, subtitle: working.machine.explanation)
            } else {
                StepHeader(title: model.runKind.runningTitle, subtitle: "Getting ready…")
            }
        }
    }

    /// What the machine is on right now, as the pipeline reports it, and how far along when it counts.
    @ViewBuilder
    private var activity: some View {
        VStack(alignment: .leading, spacing: 10) {
            if working != nil {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(model.stageDetail.isEmpty ? "Working…" : model.stageDetail)
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .truncationMode(.middle)
                }
            }
            if let total = model.progressTotal, let current = model.progressCurrent, total > 0 {
                ProgressView(value: Double(current), total: Double(total)) {
                    Text("\(current) of \(total)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .tint(LineTheme.amber)
            }
        }
    }

    /// The machines that have finished, in a line each.
    @ViewBuilder
    private var finished: some View {
        let done = nodes.filter { $0.status == .done }
        if !done.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(done) { node in
                    HStack(spacing: 10) {
                        Image(systemName: "checkmark")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(LineTheme.finished)
                        Text(node.machine.doneLine)
                            .font(.system(size: 14))
                            .foregroundStyle(.secondary)
                    }
                }
            }
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

import RoughcutKit
import SwiftUI

struct ResultView: View {
    @Environment(AppModel.self) private var model
    let result: RunResult
    @State private var recutStyle: CutStyle = .tight
    @State private var recutFormat: SequenceFormat = .auto
    @State private var showWarnings = false

    private var subtitle: String {
        if result.fellBack {
            return "The AI step failed, so this is the stringout with dead air removed. The warnings say why."
        }
        return "Planned by \(result.plannedBy)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: result.fellBack ? "exclamationmark.triangle.fill" : "checkmark.seal.fill")
                    .font(.largeTitle)
                    .foregroundStyle(result.fellBack ? Color.orange : Color.green)
                VStack(alignment: .leading, spacing: 4) {
                    Text(result.title)
                        .font(.title2.weight(.semibold))
                        .textSelection(.enabled)
                    Text(subtitle)
                        .foregroundStyle(.secondary)
                }
            }

            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
                GridRow {
                    Text("Rough cut").foregroundStyle(.secondary)
                    Text(TimeText.clock(result.roughSeconds)).monospacedDigit()
                }
                if let stringout = result.stringoutSeconds {
                    GridRow {
                        Text("Stringout").foregroundStyle(.secondary)
                        Text(TimeText.clock(stringout)).monospacedDigit()
                    }
                }
                GridRow {
                    Text("Edits").foregroundStyle(.secondary)
                    Text("\(result.edits) cuts, \(result.broll) B-roll")
                }
                if !result.sections.isEmpty {
                    GridRow(alignment: .top) {
                        Text("Sections").foregroundStyle(.secondary)
                        Text(result.sections.joined(separator: "  ·  "))
                            .lineLimit(4)
                    }
                }
            }

            HStack(spacing: 10) {
                Button {
                    model.openInFinalCut(result)
                } label: {
                    Label("Open in Final Cut Pro", systemImage: "film")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                Button("Show in Finder") { model.revealInFinder(result) }
                    .controlSize(.large)
                Button("Paper Edit") { model.openReport(result) }
                    .controlSize(.large)
            }
            Text("Final Cut opens its import dialog. Choose a library and the event appears with both projects.")
                .font(.caption)
                .foregroundStyle(.secondary)

            GroupBox {
                HStack(spacing: 12) {
                    Picker("Pacing", selection: $recutStyle) {
                        ForEach(CutStyle.allCases) { style in
                            Text(style.title).tag(style)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 240)
                    Picker("Format", selection: $recutFormat) {
                        ForEach(SequenceFormat.allCases) { format in
                            Text(format.title).tag(format)
                        }
                    }
                    .frame(maxWidth: 220)
                    Spacer()
                    Button("Re-cut") {
                        model.recut(result, style: recutStyle, format: recutFormat)
                    }
                }
                .padding(4)
            } label: {
                Text("Re-cut the same plan, no AI call")
            }

            if !result.warnings.isEmpty {
                DisclosureGroup("\(result.warnings.count) warning\(result.warnings.count == 1 ? "" : "s")", isExpanded: $showWarnings) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(Array(result.warnings.enumerated()), id: \.offset) { item in
                                Text("• " + item.element)
                                    .font(.callout)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        .textSelection(.enabled)
                    }
                    .frame(maxHeight: 160)
                }
            }

            Spacer(minLength: 0)

            HStack {
                Spacer()
                Button("New Build") { model.startOver() }
            }
        }
        .padding(24)
        .onAppear {
            recutStyle = model.options.style == .tight ? .medium : .tight
        }
    }
}

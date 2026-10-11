import RoughcutKit
import SwiftUI

struct ResultView: View {
    @Environment(AppModel.self) private var model
    let result: RunResult
    @State private var recutStyle: CutStyle = .tight
    @State private var recutFormat: SequenceFormat = .auto
    @State private var showWarnings = false
    @State private var notes = ""
    @State private var reviseTarget = ""

    private var subtitle: String {
        if result.fellBack {
            return "The AI step failed, so this is the stringout with dead air removed. The warnings say why."
        }
        let version = result.version.map { "Version \($0) · " } ?? ""
        return version + "Planned by \(result.plannedBy)"
    }

    private var importHint: String {
        if let project = result.project, let event = result.event {
            return "In Final Cut Pro choose a library; then open the project “\(project)” in the event “\(event)”."
        }
        return "Final Cut opens its import dialog. Choose a library and the event appears with both projects."
    }

    var body: some View {
        ScrollView {
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
                    if let graphics = result.graphics, graphics.planned > 0 || graphics.note != nil {
                        GridRow(alignment: .top) {
                            Text("Graphics").foregroundStyle(.secondary)
                            Text("\(graphics.rendered) of \(graphics.planned) on the timeline" + (graphics.note.map { " (\($0))" } ?? ""))
                        }
                    }
                    if let visual = result.visualLog, visual.total > 0 {
                        GridRow {
                            Text("Looked at").foregroundStyle(.secondary)
                            Text("\(visual.described) of \(visual.total) clips")
                        }
                    }
                    if let usage = result.usage, usage.calls > 0 {
                        GridRow(alignment: .top) {
                            Text("AI usage").foregroundStyle(.secondary)
                            Text(usage.summaryText)
                        }
                    }
                    if let skipped = result.skipped, !skipped.files.isEmpty {
                        GridRow(alignment: .top) {
                            Text("Left out").foregroundStyle(.secondary)
                            Text("\(skipped.files.count) file\(skipped.files.count == 1 ? "" : "s") roughcut couldn't read: " + skipped.files.prefix(3).joined(separator: ", "))
                                .lineLimit(2)
                        }
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
                Text(importHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)

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

                if !result.fellBack {
                    GroupBox {
                        VStack(alignment: .leading, spacing: 8) {
                            TextEditor(text: $notes)
                                .font(.body)
                                .frame(minHeight: 54, maxHeight: 90)
                                .overlay(alignment: .topLeading) {
                                    if notes.isEmpty {
                                        Text("e.g. tighter hook, drop the second interview line, more drone over the intro")
                                            .foregroundStyle(.tertiary)
                                            .padding(.top, 1)
                                            .padding(.leading, 6)
                                            .allowsHitTesting(false)
                                    }
                                }
                            HStack {
                                TextField("New length", text: $reviseTarget, prompt: Text("Same length"))
                                    .frame(maxWidth: 140)
                                Spacer()
                                Button("Revise") {
                                    model.revise(result, notes: notes, targetLength: reviseTarget)
                                }
                                .disabled(notes.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }
                        .padding(4)
                    } label: {
                        Text("Revise with notes (the AI re-plans; this cut stays as an earlier version)")
                    }
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
        }
        .onAppear {
            recutStyle = model.options.style == .tight ? .medium : .tight
        }
        .onChange(of: result) {
            notes = ""
            reviseTarget = ""
        }
    }
}

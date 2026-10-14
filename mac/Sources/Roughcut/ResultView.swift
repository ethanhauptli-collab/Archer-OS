import RoughcutKit
import SwiftUI

struct ResultView: View {
    @Environment(AppModel.self) private var model
    let result: RunResult
    @State private var recutStyle: CutStyle = .tight
    /// Nil keeps this cut's own format (a vertical cut stays vertical).
    @State private var recutFormat: SequenceFormat? = nil
    /// Nil keeps this cut's punch-ins as they are.
    @State private var recutPunchIn: Bool? = nil
    @State private var showWarnings = false

    /// Revise has something to say: notes or reference pictures ("Redesign the graphics" only changes what they do).
    private var canRevise: Bool {
        ReviseOptions.canStart(
            notes: model.reviseNotes,
            references: ReviseOptions.usableReferences(model.reviseReferences, for: result)
        ) && ReviseOptions.lengthProblem(model.reviseTarget) == nil
    }

    private var subtitle: String {
        if result.fellBack {
            return "The AI step failed, so this is the stringout with dead air removed. The warnings say why."
        }
        let version = result.version.map { "Version \($0) · " } ?? ""
        let unfinished = result.partial == true ? "no graphics yet · " : ""
        return version + unfinished + "Planned by \(result.plannedBy)"
    }

    var body: some View {
        @Bindable var model = model
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let problem = model.runProblem {
                    RunProblemBanner(problem: problem)
                }

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
                            GraphicsRow(result: result, graphics: graphics)
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

                if let changes = result.changes, changes.hasContent {
                    WhatChangedBox(changes: changes)
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
                Text(result.importHint)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                if let captions = result.captionsURL {
                    HStack(spacing: 8) {
                        Text("Captions: \(captions.lastPathComponent). With the project open in Final Cut: File > Import > Captions.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        Button("Show Captions") { model.revealCaptions(result) }
                            .controlSize(.small)
                    }
                }

                GroupBox {
                    HStack(spacing: 12) {
                        Picker("Pacing", selection: $recutStyle) {
                            ForEach(CutStyle.recut) { style in
                                Text(style.title).tag(style)
                            }
                        }
                        .pickerStyle(.segmented)
                        .frame(maxWidth: 240)
                        Picker("Format", selection: $recutFormat) {
                            Text(RecutOptions.sameFormatTitle(result.cut)).tag(Optional<SequenceFormat>.none)
                            ForEach(SequenceFormat.choices(including: result.cut?.sequenceFormat)) { format in
                                Text(format.title).tag(Optional(format))
                            }
                        }
                        .frame(maxWidth: 260)
                        Picker("Punch-ins", selection: $recutPunchIn) {
                            Text(RecutOptions.samePunchInTitle(result.cut)).tag(Optional<Bool>.none)
                            Text("On").tag(Optional(true))
                            Text("Off").tag(Optional(false))
                        }
                        .frame(maxWidth: 240)
                        .help("A 112% zoom alternating at each jump cut no B-roll covers.")
                        Spacer()
                        Button("Re-cut") {
                            model.recut(result, style: recutStyle, format: recutFormat, punchIn: recutPunchIn)
                        }
                    }
                    .padding(4)
                } label: {
                    Text("Re-cut the same plan, no AI call")
                }

                if !result.fellBack {
                    GroupBox {
                        VStack(alignment: .leading, spacing: 8) {
                            TextEditor(text: $model.reviseNotes)
                                .font(.body)
                                .frame(minHeight: 54, maxHeight: 90)
                                .overlay(alignment: .topLeading) {
                                    if model.reviseNotes.isEmpty {
                                        Text("e.g. tighter hook, drop the second interview line, more drone over the intro")
                                            .foregroundStyle(.tertiary)
                                            .padding(.top, 1)
                                            .padding(.leading, 6)
                                            .allowsHitTesting(false)
                                    }
                                }
                            if result.madeWithoutAI {
                                // No model planned this cut, so there's none to reuse: say which one re-plans it.
                                Picker("Re-plan with", selection: $model.reviseProvider) {
                                    ForEach(ReviseOptions.providers(form: model.options)) { provider in
                                        Text(provider.title).tag(provider)
                                    }
                                }
                                .frame(maxWidth: 420)
                            }
                            if ReviseOptions.acceptsReferences(result) {
                                ReferenceImagesWell(
                                    references: $model.reviseReferences,
                                    onAdded: {
                                        // A picture is asked to be matched: the graphics are designed again from it.
                                        if ReviseOptions.canRedesign(result) {
                                            model.reviseRedesign.pictureAdded()
                                        }
                                    },
                                    onRemoved: {
                                        model.reviseRedesign.picturesChanged(remaining: model.reviseReferences.count)
                                    }
                                )
                                Text(ReferenceImages.earlierPicturesHint)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            if ReviseOptions.canRedesign(result) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Toggle(
                                        "Redesign the graphics",
                                        isOn: Binding(get: { model.reviseRedesign.isOn }, set: { model.reviseRedesign.set($0) })
                                    )
                                    Text(ReviseOptions.redesignHint)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            HStack {
                                TextField("New length", text: $model.reviseTarget, prompt: Text("Same length"))
                                    .frame(maxWidth: 140)
                                if let problem = ReviseOptions.lengthProblem(model.reviseTarget) {
                                    Label(problem, systemImage: "exclamationmark.triangle")
                                        .font(.callout)
                                        .foregroundStyle(Color.orange)
                                }
                                Spacer()
                                Button("Revise") {
                                    model.revise(result)
                                }
                                .disabled(!canRevise)
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
            recutStyle = RecutOptions.suggestedStyle(for: result.cut, otherwise: model.options.style)
            recutFormat = nil
            recutPunchIn = nil
            model.reviseProvider = ReviseOptions.planner(model.reviseProvider, form: model.options, doctor: model.doctor)
        }
        .onChange(of: result) {
            recutStyle = RecutOptions.suggestedStyle(for: result.cut, otherwise: model.options.style)
            recutFormat = nil
            recutPunchIn = nil
        }
    }
}

/// The Graphics line of the result: how many are on the timeline, why fewer than planned (the engine's note, wrapped), and
/// Finish graphics when some are still to be made.
struct GraphicsRow: View {
    @Environment(AppModel.self) private var model
    let result: RunResult
    let graphics: GraphicsSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(graphics.rendered) of \(graphics.planned) on the timeline")
            if let note = graphics.note, !note.isEmpty {
                Text(note)
                    .font(.callout)
                    .foregroundStyle(graphics.rendered < graphics.planned || result.canFinishGraphics ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if result.canFinishGraphics {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Button {
                        model.finishGraphics(result)
                    } label: {
                        Label("Finish graphics", systemImage: "wand.and.stars")
                    }
                    .help(RunResult.finishGraphicsCaption)
                    Text(RunResult.finishGraphicsCaption)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// What a revision or re-cut changed from the version before: bullets for the items. When nothing a viewer
/// would see changed, the first line says so in a highlighted (not alarming) style, with what to try.
struct WhatChangedBox: View {
    let changes: RunChanges

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                if let lead = changes.highlighted {
                    Label {
                        Text(lead)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "info.circle.fill")
                            .foregroundStyle(Color.accentColor)
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                }
                ForEach(Array(changes.bullets.enumerated()), id: \.offset) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                        Text(item.element)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .textSelection(.enabled)
            .padding(4)
        } label: {
            HStack(spacing: 8) {
                Text(RunChanges.heading)
                if let comparison = changes.comparison {
                    Text(comparison)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// Why the last re-cut or revision of this cut stopped, and what to do about it.
struct RunProblemBanner: View {
    @Environment(AppModel.self) private var model
    let problem: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label {
                Text(problem).textSelection(.enabled)
            } icon: {
                Image(systemName: "exclamationmark.octagon.fill").foregroundStyle(Color.red)
            }
            if let advice = model.runAdvice {
                Text(advice)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.resumeFolder != nil {
                HStack {
                    Text("The new plan was saved before it stopped.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Finish without AI") { model.finishWithoutAI() }
                        .help("Cuts from the saved plan with its own settings and no model call (roughcut render)")
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }
}

import AppKit
import RoughcutKit
import SwiftUI

/// A finished cut: where it is and how long, the one thing to do next (open it in Final Cut), and "Not quite right?" for changing it.
struct ResultView: View {
    @Environment(AppModel.self) private var model
    let result: RunResult
    @State private var adjusting = false
    @State private var mode: AdjustMode = .quick
    @State private var recutStyle: CutStyle = .tight
    /// Nil keeps this cut's own format (a vertical cut stays vertical).
    @State private var recutFormat: SequenceFormat? = nil
    /// Nil keeps this cut's punch-ins as they are.
    @State private var recutPunchIn: Bool? = nil
    @State private var showWarnings = false
    @State private var showMore = false

    enum AdjustMode {
        case quick, ask
    }

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
        let version = result.version.map { "Version \($0). " } ?? ""
        let unfinished = result.partial == true ? "No graphics yet. " : ""
        return version + unfinished + "Planned by \(result.plannedBy)."
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let problem = model.runProblem {
                        RunProblemBanner(problem: problem, finishGraphicsOffered: result.canFinishGraphics)
                    }
                    if adjusting {
                        adjustContent
                    } else {
                        doneContent
                    }
                }
                .frame(maxWidth: 720, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 28)
                .padding(.bottom, 20)
                .frame(maxWidth: .infinity)
            }
            Divider()
            footer
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

    // MARK: Done

    private var doneContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: result.fellBack ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                    .font(.system(size: 34))
                    .foregroundStyle(result.fellBack ? LineTheme.amberDeep : LineTheme.finished)
                StepHeader(title: result.fellBack ? "Made without the AI" : "Your rough cut is ready", subtitle: subtitle)
            }
            cutCard
            importNote
            HStack(spacing: 10) {
                Button("Show in Finder") { model.revealInFinder(result) }
                    .controlSize(.large)
                Button("Read the edit notes") { model.openReport(result) }
                    .controlSize(.large)
                    .help("The paper edit: what was cut and why, in a page you can read")
            }
            if let changes = result.changes, changes.hasContent {
                WhatChangedBox(changes: changes)
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
        }
    }

    private var cutCard: some View {
        StepCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 16) {
                    Text(result.title)
                        .font(.system(size: 17, weight: .semibold))
                        .lineLimit(2)
                        .textSelection(.enabled)
                    Spacer(minLength: 8)
                    Text(TimeText.clock(result.roughSeconds))
                        .font(.system(size: 28, weight: .bold))
                        .monospacedDigit()
                }
                Divider()
                Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
                    if let stringout = result.stringoutSeconds {
                        GridRow {
                            Text("Dialogue only").foregroundStyle(.secondary)
                            Text(TimeText.clock(stringout)).monospacedDigit()
                        }
                    }
                    GridRow {
                        Text("Edits").foregroundStyle(.secondary)
                        Text("\(result.edits) cuts, \(result.broll) B-roll")
                    }
                    if let graphics = result.graphics, graphics.planned > 0 || graphics.displayNote != nil {
                        GridRow(alignment: .top) {
                            Text("Graphics").foregroundStyle(.secondary)
                            GraphicsRow(result: result, graphics: graphics)
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
                if hasMoreDetails {
                    DisclosureGroup("More about this cut", isExpanded: $showMore) {
                        Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
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
                        }
                        .padding(.top, 6)
                    }
                    .font(.callout)
                }
            }
            .padding(16)
        }
    }

    private var hasMoreDetails: Bool {
        (result.visualLog.map { $0.total > 0 } ?? false)
            || (result.usage.map { $0.calls > 0 } ?? false)
            || (result.skipped.map { !$0.files.isEmpty } ?? false)
    }

    /// Which item in Final Cut is the edit, and where the captions are.
    private var importNote: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "info.circle.fill")
                .foregroundStyle(Color.accentColor)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 6) {
                Text(result.importHint)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if let captions = result.captionsURL {
                    HStack(spacing: 8) {
                        Text("Captions: \(captions.lastPathComponent). With the project open in Final Cut: File > Import > Captions.")
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                        Button("Show Captions") { model.revealCaptions(result) }
                            .controlSize(.small)
                    }
                }
            }
            .font(.callout)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 12) {
            if adjusting {
                Button {
                    adjusting = false
                } label: {
                    Label("Back to your cut", systemImage: "chevron.left")
                }
                .buttonStyle(.borderless)
                .controlSize(.large)
                Spacer()
                if mode == .quick || result.fellBack {
                    Text("Instant. Uses no AI.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Re-cut") {
                        model.recut(result, style: recutStyle, format: recutFormat, punchIn: recutPunchIn)
                    }
                    .buttonStyle(LinePrimaryButtonStyle(large: true))
                } else {
                    Text("The AI re-plans the edit; this cut stays as an earlier version.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Revise with Claude") {
                        model.revise(result)
                    }
                    .buttonStyle(LinePrimaryButtonStyle(large: true))
                    .disabled(!canRevise)
                }
            } else {
                Button("Not quite right? Adjust…") {
                    adjusting = true
                }
                .buttonStyle(.borderless)
                .controlSize(.large)
                Spacer()
                Button {
                    model.openInFinalCut(result)
                } label: {
                    Label("Open in Final Cut Pro", systemImage: "arrow.right")
                }
                .buttonStyle(LinePrimaryButtonStyle(large: true))
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }

    // MARK: Adjust

    private var adjustContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(
                title: "What should change?",
                subtitle: result.version.map { "Version \($0) stays as it is. Your changes become version \($0 + 1)." }
                    ?? "This cut stays as an earlier version. Your changes become a new one."
            )
            if !result.fellBack {
                HStack(spacing: 12) {
                    modeCard(.quick, title: "Quick change", caption: "Pacing or format. Instant, no AI.")
                    modeCard(.ask, title: "Ask Claude", caption: "Describe it. Claude re-plans the edit.")
                }
            }
            if mode == .quick || result.fellBack {
                quickPanel
            } else {
                askPanel
            }
        }
    }

    private func modeCard(_ choice: AdjustMode, title: String, caption: String) -> some View {
        let selected = mode == choice
        return Button {
            mode = choice
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 16, weight: .semibold))
                    Text(caption).font(.system(size: 13)).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 20))
                    .foregroundStyle(selected ? LineTheme.amber : Color.secondary)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? LineTheme.amber.opacity(0.10) : LineTheme.card, in: RoundedRectangle(cornerRadius: 14))
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(selected ? LineTheme.amber : LineTheme.hairline, lineWidth: selected ? 1.5 : 1)
            )
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var quickPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Feel")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.secondary)
                Picker("Pacing", selection: $recutStyle) {
                    ForEach(CutStyle.recut) { style in
                        Text(style.title).tag(style)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                Text(recutStyle.detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Format")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Picker("Format", selection: $recutFormat) {
                        Text(RecutOptions.sameFormatTitle(result.cut)).tag(Optional<SequenceFormat>.none)
                        ForEach(SequenceFormat.choices(including: result.cut?.sequenceFormat)) { format in
                            Text(format.title).tag(Optional(format))
                        }
                    }
                    .labelsHidden()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Punch-ins on jump cuts")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Picker("Punch-ins", selection: $recutPunchIn) {
                        Text(RecutOptions.samePunchInTitle(result.cut)).tag(Optional<Bool>.none)
                        Text("On").tag(Optional(true))
                        Text("Off").tag(Optional(false))
                    }
                    .labelsHidden()
                    .help("A 112% zoom alternating at each jump cut no B-roll covers.")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var askPanel: some View {
        @Bindable var model = model
        return VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 8) {
                Text("What should change?")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.secondary)
                TextEditor(text: $model.reviseNotes)
                    .font(.system(size: 14))
                    .scrollContentBackground(.hidden)
                    .padding(8)
                    .frame(height: 110)
                    .background(LineTheme.card, in: RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(LineTheme.hairline, lineWidth: 1))
                    .overlay(alignment: .topLeading) {
                        if model.reviseNotes.isEmpty {
                            Text("e.g. tighter hook, drop the second interview line, more drone over the intro")
                                .font(.system(size: 14))
                                .foregroundStyle(.tertiary)
                                .padding(.top, 16)
                                .padding(.leading, 14)
                                .allowsHitTesting(false)
                        }
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
                        "Design the graphics again",
                        isOn: Binding(get: { model.reviseRedesign.isOn }, set: { model.reviseRedesign.set($0) })
                    )
                    Text(ReviseOptions.redesignHint)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            HStack {
                TextField("New length", text: $model.reviseTarget, prompt: Text("Same length"))
                    .frame(maxWidth: 160)
                if let problem = ReviseOptions.lengthProblem(model.reviseTarget) {
                    Label(problem, systemImage: "exclamationmark.triangle")
                        .font(.callout)
                        .foregroundStyle(Color.orange)
                }
                Spacer()
            }
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
            if let note = graphics.displayNote {
                Text(note)
                    .font(.callout)
                    .foregroundStyle(graphics.rendered < graphics.planned || result.canFinishGraphics ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if result.canFinishGraphics {
                // The last Finish graphics changed nothing: the same button says it is another try, and why to wait.
                let again = model.finishGraphicsTriedInVain
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Button {
                        model.finishGraphics(result)
                    } label: {
                        Label(result.finishGraphicsTitle(afterEmptyTry: again), systemImage: again ? "arrow.clockwise" : "wand.and.stars")
                    }
                    .help(result.finishGraphicsCaption(afterEmptyTry: again))
                    Text(result.finishGraphicsCaption(afterEmptyTry: again))
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
    /// The cut on screen has graphics to finish: the Graphics row has a Finish graphics button, which the banner points to.
    var finishGraphicsOffered = false

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
                    Text(StoppedRun.finishWithoutAICaption(finishGraphicsOffered: finishGraphicsOffered))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
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

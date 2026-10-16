import AppKit
import RoughcutKit
import SwiftUI
import UniformTypeIdentifiers

/// The build as a line of steps: the strip on top, one step in the middle, Back and Next at the bottom.
struct FlowView: View {
    @Environment(AppModel.self) private var model
    @State private var showImporter = false

    var body: some View {
        VStack(spacing: 0) {
            LineStrip(items: stripItems) { id in
                model.jump(toStation: id)
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    FlowNotice()
                    stepContent
                }
                .frame(maxWidth: 720, alignment: .leading)
                .padding(.horizontal, 48)
                .padding(.top, 24)
                .padding(.bottom, 20)
                .frame(maxWidth: .infinity)
            }
            Divider()
            FlowBar()
        }
        .fileImporter(
            isPresented: $showImporter,
            allowedContentTypes: [.folder, .movie, .audio, .image],
            allowsMultipleSelection: true
        ) { result in
            if case .success(let urls) = result {
                model.addSources(urls)
            }
        }
        .onChange(of: model.options) {
            model.savePreferences()
            model.repairFlow()
        }
    }

    private var stripItems: [StripItem] {
        model.flow.nodes(for: model.options, footage: model.footageSummary).map { node in
            StripItem(
                id: node.id, label: node.label, symbol: node.symbol, state: node.state, summary: node.summary,
                dashed: node.station == .add && node.state == .upcoming
            )
        }
    }

    @ViewBuilder
    private var stepContent: some View {
        switch model.flow.step {
        case .footage:
            FootageStep(showImporter: $showImporter)
        case .brief:
            BriefStep()
        case .add:
            AddStep()
        case .part(let part):
            switch part {
            case .music: MusicStep()
            case .effects: EffectsStep()
            case .graphics: GraphicsStep()
            }
        case .ready:
            ReadyStep()
        case .fineTune:
            FineTuneStep()
        }
    }
}

/// Why the last build stopped, above the step it stopped from (with Finish without AI when its plan was saved).
struct FlowNotice: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if case .failed(let message) = model.phase {
            VStack(alignment: .leading, spacing: 8) {
                Label {
                    Text(message).textSelection(.enabled)
                } icon: {
                    Image(systemName: "exclamationmark.octagon.fill").foregroundStyle(Color.red)
                }
                if model.failureKind == "usage_limit", let advice = model.runKind.limitAdvice {
                    Text(advice)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                finishWithoutAI("The edit plan was saved before it stopped.")
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        } else if model.phase == .cancelled {
            VStack(alignment: .leading, spacing: 8) {
                Label("\(model.runKind.noun) cancelled.", systemImage: "stop.circle")
                finishWithoutAI("The edit plan was saved before you cancelled.")
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        }
    }

    @ViewBuilder
    private func finishWithoutAI(_ caption: String) -> some View {
        if model.resumeFolder != nil {
            HStack {
                Text(caption)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Finish without AI") { model.finishWithoutAI() }
                    .help("Cuts from the saved plan with its own settings and no model call (roughcut render)")
            }
        }
    }
}

/// Back, what Next is waiting for (or what building costs), and the step's main button.
struct FlowBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 12) {
            if model.flow.back(for: model.options) != nil {
                Button {
                    model.goBack()
                } label: {
                    Label("Back", systemImage: "chevron.left")
                }
                .buttonStyle(.borderless)
                .controlSize(.large)
            }
            note
            Spacer()
            primary
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
    }

    @ViewBuilder
    private var note: some View {
        if let text = model.flow.footerNote(for: model.options, footage: model.footageSummary) {
            let isProblem = model.flow.step == .ready && !model.options.problems.isEmpty
            Text(text)
                .font(.callout)
                .foregroundStyle(isProblem ? Color.orange : Color.secondary)
                .lineLimit(2)
        }
    }

    @ViewBuilder
    private var primary: some View {
        switch model.flow.step {
        case .fineTune:
            Button("Done") { model.go(to: .ready) }
                .buttonStyle(LinePrimaryButtonStyle())
                .keyboardShortcut(.defaultAction)
        case .ready:
            Button {
                model.build()
            } label: {
                Label("Build rough cut", systemImage: "bolt.fill")
            }
            .buttonStyle(LinePrimaryButtonStyle(large: true))
            .keyboardShortcut(.return, modifiers: .command)
            .disabled(!model.options.problems.isEmpty)
        default:
            Button {
                model.goNext()
            } label: {
                HStack(spacing: 6) {
                    Text("Next")
                    Image(systemName: "arrow.right")
                }
            }
            .buttonStyle(LinePrimaryButtonStyle())
            .keyboardShortcut(.defaultAction)
            .disabled(!model.flow.canAdvance(for: model.options))
        }
    }
}

/// The rough cuts made before, in the window's toolbar: open one, show it in Finder, or forget it.
struct RecentMenu: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Menu {
            ForEach(model.availableRecents.prefix(8)) { run in
                Menu(Self.title(of: run)) {
                    Button("Open") { model.showRecent(run) }
                    Button("Show in Finder") { model.revealInFinder(run.result) }
                    Button("Remove from Recent") { model.forgetRecent(run) }
                }
            }
        } label: {
            Label("Recent", systemImage: "clock")
        }
        .disabled(model.isRunning || model.availableRecents.isEmpty)
        .help("Rough cuts you made before")
    }

    private static func title(of run: RecentRun) -> String {
        let length = TimeText.clock(run.result.roughSeconds)
        let unfinished = run.result.partial == true ? " · no graphics yet" : ""
        return "\(run.result.title) · \(length)\(unfinished) · \(run.finishedAt.formatted(.relative(presentation: .named)))"
    }
}

import AppKit
import RoughcutKit
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            SetupBanner()
            switch model.phase {
            case .running:
                RunView()
            case .finished(let result):
                ResultView(result: result)
            case .idle, .failed, .cancelled:
                BuildForm()
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

// MARK: - Form

struct BuildForm: View {
    @Environment(AppModel.self) private var model
    @State private var showImporter = false

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            Form {
                if case .failed(let message) = model.phase {
                    Section {
                        Label {
                            Text(message).textSelection(.enabled)
                        } icon: {
                            Image(systemName: "exclamationmark.octagon.fill").foregroundStyle(Color.red)
                        }
                        if model.failureKind == "usage_limit", let advice = model.runKind.limitAdvice {
                            Text(advice)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                        if model.resumeFolder != nil {
                            HStack {
                                Text("The edit plan was saved before it stopped.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button("Finish without AI") { model.finishWithoutAI() }
                                    .help("Cuts from the saved plan with its own settings and no model call (roughcut render)")
                            }
                        }
                    }
                } else if model.phase == .cancelled {
                    Section {
                        Label("\(model.runKind.noun) cancelled.", systemImage: "stop.circle")
                        if model.resumeFolder != nil {
                            HStack {
                                Text("The edit plan was saved before you cancelled.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button("Finish without AI") { model.finishWithoutAI() }
                                    .help("Cuts from the saved plan with its own settings and no model call (roughcut render)")
                            }
                        }
                    }
                }

                Section("Footage") {
                    SourcesDropZone(showImporter: $showImporter)
                }

                if !model.availableRecents.isEmpty {
                    Section("Recent rough cuts") {
                        ForEach(model.availableRecents.prefix(4)) { run in
                            RecentRunRow(run: run)
                        }
                    }
                }

                Section("Brief") {
                    if !model.presets.isEmpty {
                        Picker("Preset", selection: Binding(
                            get: { model.options.presetName },
                            set: { model.applyPreset(named: $0) }
                        )) {
                            Text("None").tag("")
                            ForEach(model.presets) { preset in
                                Text(preset.title).tag(preset.name)
                            }
                        }
                        .help("Fills the brief, length, pacing, format and graphics look from a saved preset. Change anything after.")
                    }
                    TextEditor(text: $model.options.brief)
                        .font(.body)
                        .frame(minHeight: 96)
                        .overlay(alignment: .topLeading) {
                            if model.options.brief.isEmpty {
                                Text("What is this video? Who's it for, what tone, which moments have to stay…")
                                    .foregroundStyle(.tertiary)
                                    .padding(.top, 1)
                                    .padding(.leading, 6)
                                    .allowsHitTesting(false)
                            }
                        }
                    TextField("Target length", text: $model.options.targetLength, prompt: Text("Optional: 8m, 90s, 8:30"))
                    Picker("Pacing", selection: $model.options.style) {
                        ForEach(CutStyle.allCases) { style in
                            Text(style.title).tag(style)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text(model.options.style.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker("Punch-ins", selection: $model.options.punchIn) {
                        ForEach(PunchIn.allCases) { choice in
                            Text(choice.title).tag(choice)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text("A 112% zoom alternating at each jump cut no B-roll covers, so back-to-back pieces of one shot don't jump. Auto: when Claude reads the brief asking for punch-ins at the jump cuts (without AI, when it says so).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("AI") {
                    Picker("Plans the edit", selection: $model.options.provider) {
                        ForEach(ModelProvider.allCases) { provider in
                            Text(provider.title).tag(provider)
                        }
                    }
                    if model.options.provider.usesClaudeModels {
                        Picker("Model", selection: $model.options.model) {
                            ForEach(ClaudeModelChoice.planners) { choice in
                                Text(choice.title).tag(choice.id)
                            }
                        }
                    } else if model.options.provider != .none {
                        TextField("Model", text: $model.options.model, prompt: Text(model.options.provider.modelPlaceholder))
                    }
                    if model.options.provider != .none {
                        if model.options.provider.usesBaseURL {
                            TextField("Server URL", text: $model.options.baseURL, prompt: Text("http://localhost:1234/v1"))
                        }
                        if model.options.provider.usesClaudeModels {
                            Picker("Effort", selection: $model.options.effort) {
                                ForEach(Effort.allCases) { effort in
                                    Text(effort.title).tag(effort)
                                }
                            }
                        }
                        Toggle("Look at the footage (better B-roll picks, costs a little more)", isOn: $model.options.vision)
                        if model.options.provider.usesClaudeModels && model.options.vision {
                            Picker("Describe clips with", selection: $model.options.visionModel) {
                                ForEach(ClaudeModelChoice.describers) { choice in
                                    Text(choice.title).tag(choice.id)
                                }
                            }
                        }
                    }
                }

                Section("Audio") {
                    Picker("Dialogue level", selection: $model.options.dialogueLevel) {
                        ForEach(DialogueLevel.allCases) { level in
                            Text(level.title).tag(level)
                        }
                    }
                    Toggle("Short fades at audio edits (no clicks)", isOn: $model.options.fades)
                    MusicPicker()
                    SoundEffectsPicker()
                }

                Section("Output") {
                    Picker("Format", selection: $model.options.format) {
                        // A preset's own format (720p, 1080x1920@29.97...) shows as its own row.
                        ForEach(SequenceFormat.choices(including: model.options.format)) { format in
                            Text(format.title).tag(format)
                        }
                    }
                    Toggle("Motion graphics (Claude designs lower thirds, stats and captions from the transcript)", isOn: $model.options.graphics)
                        .disabled(model.options.provider == .none)
                    if model.options.graphics && model.options.provider != .none {
                        TextField("Graphics style", text: $model.options.graphicsStyle, prompt: Text("Brand colors, fonts and mood, or pick a preset above"), axis: .vertical)
                            .lineLimit(1...4)
                        LogoPicker()
                        ReferenceImagesWell(references: $model.options.graphicsReferences)
                        if !model.options.graphicsLook.isEmpty {
                            HStack {
                                Text("Matching \(model.options.graphicsLook.count) approved graphic\(model.options.graphicsLook.count == 1 ? "" : "s") from an earlier video")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Button("Don't match") { model.options.graphicsLook = [] }
                            }
                        }
                    }
                    TextField("Project name", text: $model.options.projectName, prompt: Text("Defaults to the folder name"))
                    Toggle("Also build a Stringout (all dialogue, dead air removed)", isOn: $model.options.stringout)
                    Toggle("Keep “um” and “uh”", isOn: $model.options.keepFillers)
                    Toggle("Open in Final Cut Pro when it's done", isOn: $model.options.openInFinalCutWhenDone)
                }
            }
            .formStyle(.grouped)

            Divider()
            BuildBar()
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
        }
        .onChange(of: model.options.provider) {
            // Local and compatible servers are usually text-only models.
            model.options.vision = model.options.provider.usesClaudeModels || model.options.provider == .openai
            // Model names don't carry over between providers.
            model.options.model = ""
            model.options.visionModel = ""
        }
    }
}

struct SourcesDropZone: View {
    @Environment(AppModel.self) private var model
    @Binding var showImporter: Bool
    @State private var isTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.options.sources.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "film.stack")
                        .font(.system(size: 28))
                        .foregroundStyle(.secondary)
                    Text("Drop footage folders or clips here")
                        .font(.headline)
                    Text("Video, audio and stills. Subfolders are included.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button("Choose…") { showImporter = true }
                        .padding(.top, 4)
                }
                .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                ForEach(model.options.sources, id: \.self) { url in
                    HStack(spacing: 8) {
                        Image(systemName: Self.isFolder(url) ? "folder" : "film")
                            .foregroundStyle(.secondary)
                        Text(url.lastPathComponent)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text(url.deletingLastPathComponent().path)
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .truncationMode(.head)
                        Spacer()
                        Button {
                            model.removeSource(url)
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                        }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .help("Remove")
                    }
                }
                Button("Add more…") { showImporter = true }
                    .buttonStyle(.link)
            }
        }
        .padding(10)
        .background {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
                .foregroundStyle(isTargeted ? Color.accentColor : Color.secondary.opacity(0.4))
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.addSources(urls)
            return !urls.isEmpty
        } isTargeted: { targeted in
            isTargeted = targeted
        }
    }

    static func isFolder(_ url: URL) -> Bool {
        (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
    }
}

struct BuildBar: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 12) {
            if let problem = model.options.problems.first, !model.options.sources.isEmpty {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(Color.orange)
                    .lineLimit(2)
            } else if let target = TimeText.seconds(from: model.options.targetLength) {
                Text("Aiming for about \(TimeText.clock(target)).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                model.build()
            } label: {
                Label("Build Rough Cut", systemImage: "scissors")
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .keyboardShortcut(.return, modifiers: .command)
            .disabled(!model.options.problems.isEmpty)
        }
        .padding(12)
    }
}

struct RecentRunRow: View {
    @Environment(AppModel.self) private var model
    let run: RecentRun

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: run.result.fellBack ? "exclamationmark.triangle" : "film")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(run.result.title)
                    .lineLimit(1)
                Text("\(TimeText.clock(run.result.roughSeconds)) · \(run.finishedAt.formatted(.relative(presentation: .named)))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Open") { model.showRecent(run) }
                .disabled(model.isRunning)
        }
        .contextMenu {
            Button("Show in Finder") { model.revealInFinder(run.result) }
            Button("Remove from Recent") { model.forgetRecent(run) }
        }
    }
}

/// Songs to lay under the cut (`--music`).
struct MusicPicker: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Music bed")
                Spacer()
                Button(model.options.music.isEmpty ? "Choose a song…" : "Add another…") {
                    let panel = NSOpenPanel()
                    panel.canChooseFiles = true
                    panel.canChooseDirectories = false
                    panel.allowsMultipleSelection = true
                    panel.allowedContentTypes = [.audio]
                    panel.message = "A song to lay under the cut. It ducks under speech and is timed to end with the cut."
                    if panel.runModal() == .OK {
                        for url in panel.urls where !model.options.music.contains(url) {
                            model.options.music.append(url)
                        }
                    }
                }
            }
            ForEach(model.options.music, id: \.self) { url in
                HStack(spacing: 8) {
                    Image(systemName: "music.note").foregroundStyle(.secondary)
                    Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button {
                        model.options.music.removeAll { $0 == url }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.secondary)
                    .help("Remove")
                }
            }
            if model.options.music.isEmpty {
                Text("Optional. Without one, the AI uses music from your footage folder if there is any.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Sound effects for accents (`--sfx`), and Venture Studio collections (`--sfx-from`) when Studio is on this Mac.
struct SoundEffectsPicker: View {
    @Environment(AppModel.self) private var model
    @State private var collection = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Sound effects")
                Spacer()
                Button(model.options.sfx.isEmpty ? "Choose a folder…" : "Add another…") {
                    let panel = NSOpenPanel()
                    panel.canChooseFiles = true
                    panel.canChooseDirectories = true
                    panel.allowsMultipleSelection = true
                    panel.allowedContentTypes = [.folder, .audio]
                    panel.message = "A folder of sound effects (whooshes, risers, pops, hits), or single sounds. Roughcut only reads them."
                    if panel.runModal() == .OK {
                        for url in panel.urls where !model.options.sfx.contains(url) {
                            model.options.sfx.append(url)
                        }
                    }
                }
            }
            ForEach(model.options.sfx, id: \.self) { url in
                HStack(spacing: 8) {
                    Image(systemName: "waveform").foregroundStyle(.secondary)
                    Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle).help(url.path)
                    Spacer()
                    Button {
                        model.options.sfx.removeAll { $0 == url }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.secondary)
                    .help("Remove")
                }
            }
            ForEach(model.options.sfxFrom, id: \.self) { name in
                HStack(spacing: 8) {
                    Image(systemName: "square.stack").foregroundStyle(.secondary)
                    Text("Venture Studio: \(name)").lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button {
                        model.options.sfxFrom.removeAll { $0 == name }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.secondary)
                    .help("Remove")
                }
            }
            if model.doctor?.ventureStudio?.isInstalled == true {
                HStack {
                    TextField("Venture Studio collection", text: $collection, prompt: Text("A collection's name, e.g. Whooshes"))
                        .onSubmit(addCollection)
                    Button("Add", action: addCollection)
                        .disabled(collection.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            Text("Optional. Whooshes at section changes, pops and hits as graphics land (more when the brief asks for lots), classed by the words in the file names, set under the dialogue on their own role.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func addCollection() {
        let name = collection.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        if !model.options.sfxFrom.contains(name) {
            model.options.sfxFrom.append(name)
        }
        if !model.options.sfxFromTyped.contains(name) {
            model.options.sfxFromTyped.append(name)  // typed now: the build stops if Studio has no such collection
        }
        collection = ""
    }
}

/// A logo graphics may show (`--graphics-logo`).
struct LogoPicker: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack {
            Text("Logo")
            Spacer()
            if model.options.graphicsLogo.isEmpty {
                Text("None").foregroundStyle(.secondary)
            } else {
                Text(URL(fileURLWithPath: model.options.graphicsLogo).lastPathComponent)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Button("Clear") { model.options.graphicsLogo = "" }
            }
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.canChooseFiles = true
                panel.canChooseDirectories = false
                panel.allowsMultipleSelection = false
                panel.allowedContentTypes = [.png, .jpeg, .svg, .webP]
                panel.message = "A logo for title cards and lower thirds (PNG with transparency works best)"
                if panel.runModal() == .OK, let url = panel.url {
                    model.options.graphicsLogo = url.path
                }
            }
        }
    }
}

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
                    }
                } else if model.phase == .cancelled {
                    Section {
                        Label("Build cancelled.", systemImage: "stop.circle")
                    }
                }

                Section("Footage") {
                    SourcesDropZone(showImporter: $showImporter)
                }

                Section("Brief") {
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
                }

                Section("AI") {
                    Picker("Plans the edit", selection: $model.options.provider) {
                        ForEach(ModelProvider.allCases) { provider in
                            Text(provider.title).tag(provider)
                        }
                    }
                    if model.options.provider == .anthropic {
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
                        if model.options.provider == .anthropic {
                            Picker("Effort", selection: $model.options.effort) {
                                ForEach(Effort.allCases) { effort in
                                    Text(effort.title).tag(effort)
                                }
                            }
                        }
                        Toggle("Look at the footage (better B-roll picks, costs a little more)", isOn: $model.options.vision)
                        if model.options.provider == .anthropic && model.options.vision {
                            Picker("Describe clips with", selection: $model.options.visionModel) {
                                ForEach(ClaudeModelChoice.describers) { choice in
                                    Text(choice.title).tag(choice.id)
                                }
                            }
                        }
                    }
                }

                Section("Output") {
                    Picker("Format", selection: $model.options.format) {
                        ForEach(SequenceFormat.allCases) { format in
                            Text(format.title).tag(format)
                        }
                    }
                    TextField("Project name", text: $model.options.projectName, prompt: Text("Defaults to the folder name"))
                    Toggle("Also build a Stringout (all dialogue, dead air removed)", isOn: $model.options.stringout)
                    Toggle("Keep “um” and “uh”", isOn: $model.options.keepFillers)
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
            model.options.vision = model.options.provider == .anthropic || model.options.provider == .openai
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

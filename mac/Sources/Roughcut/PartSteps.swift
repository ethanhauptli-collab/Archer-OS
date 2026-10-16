import AppKit
import RoughcutKit
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Add

/// Step three: the parts a person can add to the line. Each one added becomes a step of its own.
struct AddStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "Add anything else to the line?", subtitle: "Each part adds one step. Skip them all and you still get a full cut.")
            VStack(spacing: 12) {
                ForEach(LinePart.allCases) { part in
                    PartRow(part: part)
                }
            }
            if !model.options.provider.usesAI {
                Text(BuildOptions.graphicsNeedAIHint)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct PartRow: View {
    @Environment(AppModel.self) private var model
    let part: LinePart

    private var added: Bool {
        model.flow.parts(for: model.options).contains(part)
    }

    /// Graphics are designed by the AI: with no model there is nothing to add.
    private var blocked: Bool {
        part == .graphics && !added && !model.options.provider.usesAI
    }

    var body: some View {
        HStack(spacing: 16) {
            IconTile(symbol: part.symbol, tint: added, size: 44)
            VStack(alignment: .leading, spacing: 2) {
                Text(part.title)
                    .font(.system(size: 16, weight: .semibold))
                Text(part.blurb)
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            if added {
                AddedPill()
                Button("Remove") { model.removePart(part) }
                    .buttonStyle(.borderless)
            } else {
                Button {
                    model.addPart(part)
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .controlSize(.large)
                .disabled(blocked)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(added ? LineTheme.amber.opacity(0.10) : LineTheme.card, in: RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(added ? LineTheme.amber : LineTheme.hairline, lineWidth: added ? 1.5 : 1)
        )
    }
}

// MARK: - Music

/// The Music step: songs to lay under the cut (`--music`), chosen, dropped, pasted, or picked from Venture Studio.
struct MusicStep: View {
    @Environment(AppModel.self) private var model
    @State private var showStudio = false
    @State private var isTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "Add a song", subtitle: "It plays under the edit, ducks under speech, and is timed to end on the last frame.")
            if !model.options.music.isEmpty {
                StepCard {
                    ForEach(Array(model.options.music.enumerated()), id: \.element) { index, url in
                        if index > 0 {
                            Divider()
                        }
                        HStack(spacing: 12) {
                            IconTile(symbol: "music.note", tint: true)
                            Text(url.lastPathComponent)
                                .font(.system(size: 15, weight: .semibold))
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer()
                            Button {
                                model.options.music.removeAll { $0 == url }
                            } label: {
                                Image(systemName: "xmark")
                            }
                            .buttonStyle(.borderless)
                            .foregroundStyle(.secondary)
                            .help("Remove")
                            .accessibilityLabel("Remove \(url.lastPathComponent)")
                        }
                        .padding(.horizontal, 16)
                        .padding(.vertical, 12)
                    }
                }
            }
            VStack(spacing: 12) {
                Text(model.options.music.isEmpty ? "Drop a song here" : "Drop another song here")
                    .font(.system(size: 15, weight: .semibold))
                HStack(spacing: 10) {
                    Button(model.options.music.isEmpty ? "Choose a song…" : "Add another…") { choose() }
                        .controlSize(.large)
                    if model.studioInstalled {
                        Button("From Venture Studio…") { showStudio = true }
                            .controlSize(.large)
                            .help("Songs from Venture Studio's catalog snapshot, added here as plain files")
                    }
                    PasteButton(supportedContentTypes: [.fileURL, .plainText]) { providers in
                        paste(providers)
                    }
                    .help("Paste a song copied in Finder or Studio, or its full path")
                }
                if let note = model.musicNote {
                    Text(note)
                        .font(.callout)
                        .foregroundStyle(Color.orange)
                }
                Text(model.options.music.isEmpty
                    ? "Without a song, Claude uses music from your footage folder if there is any."
                    : "Add more than one and Claude picks the best fit.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 150)
            .padding(.vertical, 12)
            .dashedZone(targeted: isTargeted, radius: 16)
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.addMusic(urls)
        } isTargeted: { targeted in
            isTargeted = targeted
        }
        .sheet(isPresented: $showStudio) {
            StudioMusicSheet()
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.audio]
        panel.message = "A song to lay under the cut. It ducks under speech and is timed to end with the cut."
        if panel.runModal() == .OK {
            model.addMusic(panel.urls)
        }
    }

    /// File URLs from the pasteboard (Finder's or Studio's Copy), or one absolute path as text (a copied file path).
    private func paste(_ providers: [NSItemProvider]) {
        let model = self.model
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url else { return }
                    Task { @MainActor in model.addMusic([url]) }
                }
            } else if provider.canLoadObject(ofClass: String.self) {
                _ = provider.loadObject(ofClass: String.self) { text, _ in
                    Task { @MainActor in
                        if let text, let url = MusicFiles.url(fromPastedText: text) {
                            model.addMusic([url])
                        } else {
                            model.musicNote = "Paste a song file copied in Finder or Studio, or its full path (/Volumes/…)."
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Sound effects

/// The Effects step: sound effects for accents (`--sfx`), and Venture Studio collections (`--sfx-from`) when Studio is on this Mac.
struct EffectsStep: View {
    @Environment(AppModel.self) private var model
    @State private var collection = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(
                title: "Where are your sound effects?",
                subtitle: "Pick a folder, or a Venture Studio collection. Whooshes land at section changes, pops and hits as graphics arrive."
            )
            if !model.options.sfx.isEmpty || !model.options.sfxFrom.isEmpty {
                StepCard {
                    ForEach(model.options.sfx, id: \.self) { url in
                        sourceRow(symbol: "folder", title: url.lastPathComponent, help: url.path) {
                            model.options.sfx.removeAll { $0 == url }
                        }
                        if url != model.options.sfx.last || !model.options.sfxFrom.isEmpty {
                            Divider()
                        }
                    }
                    ForEach(model.options.sfxFrom, id: \.self) { name in
                        sourceRow(symbol: "square.stack", title: "Venture Studio: \(name)", help: name) {
                            model.options.sfxFrom.removeAll { $0 == name }
                        }
                        if name != model.options.sfxFrom.last {
                            Divider()
                        }
                    }
                }
            }
            VStack(spacing: 12) {
                Button(model.options.sfx.isEmpty ? "Choose a folder…" : "Add another…") { choose() }
                    .controlSize(.large)
                if model.studioInstalled {
                    HStack {
                        TextField("Venture Studio collection", text: $collection, prompt: Text("A collection's name, e.g. Whooshes"))
                            .textFieldStyle(.roundedBorder)
                            .onSubmit(addCollection)
                        Button("Add", action: addCollection)
                            .disabled(collection.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    .frame(maxWidth: 420)
                }
                Text("Sounds are classed by the words in their file names and set under the dialogue on their own role. Want more or fewer? Say so in your brief.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, minHeight: 150)
            .padding(.vertical, 12)
            .padding(.horizontal, 16)
            .dashedZone(radius: 16)
        }
    }

    private func sourceRow(symbol: String, title: String, help: String, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 12) {
            IconTile(symbol: symbol, tint: true)
            Text(title)
                .font(.system(size: 15, weight: .semibold))
                .lineLimit(1)
                .truncationMode(.middle)
                .help(help)
            Spacer()
            Button(action: remove) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
            .help("Remove")
            .accessibilityLabel("Remove \(title)")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private func choose() {
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

// MARK: - Motion graphics

/// The Graphics step: the look, a logo, pictures of a look wanted (`--graphics-style`, `--graphics-logo`, `--graphics-ref`).
struct GraphicsStep: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        VStack(alignment: .leading, spacing: 18) {
            StepHeader(title: "Motion graphics", subtitle: "Claude designs lower thirds, stats and captions from what people say. They sit above the B-roll.")
            if !model.options.provider.usesAI {
                Text(BuildOptions.graphicsNeedAIHint)
                    .font(.callout)
                    .foregroundStyle(Color.orange)
            }
            StepCard {
                VStack(alignment: .leading, spacing: 8) {
                    Text(lookTitle)
                        .font(.system(size: 16, weight: .semibold))
                    TextField(
                        "Graphics style", text: $model.options.graphicsStyle,
                        prompt: Text("Brand colors, fonts and mood, or pick a preset in the Brief step"), axis: .vertical
                    )
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)
                    Text("Set by the preset you chose, or write your own. Claude follows it for every graphic.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .padding(16)
            }
            StepCard {
                LogoRow()
                    .padding(16)
            }
            StepCard {
                ReferenceImagesWell(references: $model.options.graphicsReferences)
                    .padding(16)
            }
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
    }

    private var lookTitle: String {
        if !model.options.presetName.isEmpty {
            return "\(Preset.displayName(for: model.options.presetName)) look"
        }
        return model.options.graphicsStyle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Claude's own look" : "Your look"
    }
}

/// A logo graphics may show (`--graphics-logo`).
private struct LogoRow: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Logo")
                    .font(.system(size: 15, weight: .semibold))
                Text(model.options.graphicsLogo.isEmpty ? "Optional. Graphics can show it on title cards and lower thirds." : URL(fileURLWithPath: model.options.graphicsLogo).lastPathComponent)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            if !model.options.graphicsLogo.isEmpty {
                Button("Clear") { model.options.graphicsLogo = "" }
            }
            Button("Choose…") { choose() }
        }
    }

    private func choose() {
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

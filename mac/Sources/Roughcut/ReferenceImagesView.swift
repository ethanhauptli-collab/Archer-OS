import AppKit
import RoughcutKit
import SwiftUI
import UniformTypeIdentifiers

/// A list of reference pictures for the motion graphics: thumbnails with a remove button, Add… and Paste,
/// ⌘V while the box is selected, and a drop target for picture files and for pictures dragged out of other apps.
/// The build form and the Revise box each hold their own list.
struct ReferenceImagesWell: View {
    @Binding var references: [URL]
    /// Called after pictures were added (the Revise box turns "Redesign the graphics" on).
    var onAdded: () -> Void = {}
    /// Called after a picture was removed (the Revise box turns it off again when the last one goes).
    var onRemoved: () -> Void = {}

    @State private var note: String?
    @State private var targeted = false
    @FocusState private var focused: Bool

    private let columns = [GridItem(.adaptive(minimum: 72, maximum: 72), spacing: 8, alignment: .top)]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("Reference images")
                if !references.isEmpty {
                    Text("\(references.count) of \(ReferenceImages.maxCount)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Add…") { choose() }
                Button("Paste") { paste() }
                    .help("Paste a copied screenshot, an image, or picture files. ⌘V does the same while the box below is selected.")
            }
            well
            Text(ReferenceImages.caption)
                .font(.caption)
                .foregroundStyle(.secondary)
            if let note {
                Label(note, systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(Color.orange)
                    .textSelection(.enabled)
            }
        }
    }

    private var well: some View {
        Group {
            if references.isEmpty {
                Text("Drop pictures here, or click here and press ⌘V")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 52)
            } else {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 8) {
                    ForEach(references, id: \.self) { url in
                        ReferenceThumbnail(url: url) { remove(url) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(8)
        .background {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
                .foregroundStyle(targeted || focused ? Color.accentColor : Color.secondary.opacity(0.4))
        }
        .contentShape(Rectangle())
        .onTapGesture { focused = true }
        .focusable()
        .focused($focused)
        .focusEffectDisabled()
        .onPasteCommand(of: [.fileURL, .image]) { _ in paste() }
        .onDrop(of: [.fileURL, .image, .item], isTargeted: $targeted) { providers in acceptDrop(providers) }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.image]
        panel.message = "Pictures that show the look you want for the graphics: screenshots, other videos' titles, brand sheets"
        if panel.runModal() == .OK {
            add(panel.urls)
        }
    }

    private func paste() {
        let room = ReferenceImages.room(in: references)
        Task { @MainActor in
            let pasted = await ReferenceFiles.fromPasteboard(room: room)
            add(pasted.urls, message: pasted.note)
        }
    }

    /// Picture files and pictures dragged out of other apps are added; any other item is ignored, with a note.
    private func acceptDrop(_ providers: [NSItemProvider]) -> Bool {
        guard !providers.isEmpty else { return false }
        let room = ReferenceImages.room(in: references)
        Task { @MainActor in
            let dropped = await ReferenceFiles.gather(providers, room: room)
            add(dropped.urls, message: dropped.note)
        }
        return true
    }

    /// Adds what was offered; what couldn't be added is said in a short note under the box.
    private func add(_ offered: [URL], message: String? = nil) {
        let result = ReferenceImages.adding(offered, to: references)
        references = result.list
        let text = [message, result.note].compactMap { $0 }.joined(separator: " ")
        note = text.isEmpty ? nil : text
        if result.added > 0 {
            onAdded()
        }
    }

    private func remove(_ url: URL) {
        references.removeAll { $0 == url }
        note = nil
        onRemoved()
    }
}

/// One reference picture, about 72 pt square, with a remove button. It is read off the main thread, scaled down.
struct ReferenceThumbnail: View {
    let url: URL
    let remove: () -> Void
    @State private var image: NSImage?
    @State private var unreadable = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Group {
                if let image {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFill()
                } else {
                    ZStack {
                        Color.secondary.opacity(0.12)
                        Image(systemName: unreadable ? "exclamationmark.triangle" : "photo")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(width: 72, height: 72)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.secondary.opacity(0.3)))
            .help(unreadable ? "\(url.lastPathComponent): no preview. Roughcut says if it can't use this picture." : url.lastPathComponent)
            Button(action: remove) {
                Image(systemName: "xmark.circle.fill")
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, Color.black.opacity(0.65))
            }
            .buttonStyle(.borderless)
            .padding(3)
            .help("Remove \(url.lastPathComponent)")
        }
        .frame(width: 72, height: 72)
        .task(id: url) {
            image = await ReferenceFiles.thumbnail(of: url, pixels: 160)
            unreadable = image == nil
        }
    }
}

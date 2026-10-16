import RoughcutKit
import SwiftUI

/// Step one: the footage. Drop it, or choose it; what was dropped is listed with what is in it.
struct FootageStep: View {
    @Environment(AppModel.self) private var model
    @Binding var showImporter: Bool
    @State private var isTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if model.options.sources.isEmpty {
                StepHeader(title: "Start with your footage", subtitle: "Drop the folders or clips from your shoot. Subfolders are included.")
                emptyZone
            } else {
                StepHeader(title: "Your footage", subtitle: summary)
                sourceList
                moreZone
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.addSources(urls)
            return !urls.isEmpty
        } isTargeted: { targeted in
            isTargeted = targeted
        }
    }

    private var summary: String {
        if let counted = model.footageSummary {
            return counted.sentence + "."
        }
        let count = model.options.sources.count
        return "\(count) source\(count == 1 ? "" : "s"), still being counted."
    }

    private var emptyZone: some View {
        VStack(spacing: 10) {
            IconTile(symbol: "square.and.arrow.down", size: 56)
            Text("Drop folders or clips here")
                .font(.system(size: 18, weight: .semibold))
            Text("Video, audio and stills")
                .foregroundStyle(.secondary)
            Button("Choose…") { showImporter = true }
                .controlSize(.large)
                .padding(.top, 6)
        }
        .frame(maxWidth: .infinity, minHeight: 280)
        .dashedZone(targeted: isTargeted, radius: 20)
    }

    private var sourceList: some View {
        StepCard {
            ForEach(Array(model.options.sources.enumerated()), id: \.element) { index, url in
                if index > 0 {
                    Divider()
                }
                HStack(spacing: 12) {
                    IconTile(symbol: Self.symbol(for: url))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(url.lastPathComponent)
                            .font(.system(size: 15, weight: .semibold))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Text(detail(of: url))
                            .font(.system(size: 13))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Spacer()
                    Button {
                        model.removeSource(url)
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

    private var moreZone: some View {
        HStack(spacing: 8) {
            Text("Drop more here, or")
                .foregroundStyle(.secondary)
            Button("Choose…") { showImporter = true }
        }
        .frame(maxWidth: .infinity, minHeight: 56)
        .dashedZone(targeted: isTargeted, radius: 14)
    }

    /// What a source holds: its count once read, else what the file is.
    private func detail(of url: URL) -> String {
        if let count = model.footageCounts[url] {
            return count.isEmpty ? "No video, audio or stills in it" : count.sentence
        }
        return Self.isFolder(url) ? "Folder" : "File"
    }

    static func symbol(for url: URL) -> String {
        if isFolder(url) { return "folder" }
        switch MediaKind.of(url) {
        case .video: return "film"
        case .audio: return "waveform"
        case .still: return "photo"
        case nil: return "doc"
        }
    }

    static func isFolder(_ url: URL) -> Bool {
        (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
    }
}

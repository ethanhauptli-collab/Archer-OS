import RoughcutKit
import SwiftUI

/// "From Venture Studio…": Studio's audio as its catalog snapshot lists it (`roughcut studio music --json`, read only),
/// to add to the Music bed as plain files. Shown only when `roughcut doctor` says Studio is installed.
struct StudioMusicSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""
    @State private var collection = ""
    @State private var favoritesOnly = false
    @State private var songsOnly = true
    @State private var selection = Set<String>()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Music from Venture Studio")
                .font(.headline)
            if let asOf = model.studioSongs.first?.asOf, !asOf.isEmpty {
                Text("From \(asOf). Songs added or tagged in Studio since then appear after its next catalog snapshot (in Studio: Back up catalog now).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                TextField("Search names, tags, providers", text: $search)
                    .textFieldStyle(.roundedBorder)
                Picker("Collection", selection: $collection) {
                    Text("All collections").tag("")
                    ForEach(StudioMusic.collections(in: model.studioSongs), id: \.self) { name in
                        Text(name).tag(name)
                    }
                }
                .frame(maxWidth: 240)
            }
            HStack(spacing: 16) {
                Toggle("Favorites only", isOn: $favoritesOnly)
                Toggle("Hide sound effects", isOn: $songsOnly)
                    .help("Leaves out files shorter than 8 seconds, and unmeasured ones whose names say whoosh, hit, riser…")
            }
            list
            HStack {
                if model.studioSongsLoading {
                    ProgressView()
                        .controlSize(.small)
                    Text("Reading Studio's catalog snapshot…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Stop") { model.cancelStudioSongs() }
                } else {
                    Text("\(chosen.count) selected")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") {
                    model.cancelStudioSongs()
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                Button("Add to Music bed") {
                    model.addMusic(chosen.map(\.url))
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(chosen.isEmpty)
            }
        }
        .padding(16)
        .frame(minWidth: 640, minHeight: 460)
        .task { await model.loadStudioSongs() }
    }

    private var shown: [StudioSong] {
        StudioMusic.filter(model.studioSongs, search: search, collection: collection, favoritesOnly: favoritesOnly, songsOnly: songsOnly)
    }

    private var chosen: [StudioSong] {
        model.studioSongs.filter { selection.contains($0.id) && $0.usable }
    }

    @ViewBuilder private var list: some View {
        if let problem = model.studioSongsProblem {
            VStack(alignment: .leading, spacing: 8) {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                Button("Try again") { Task { await model.loadStudioSongs() } }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            List(shown, selection: $selection) { song in
                StudioSongRow(song: song)
                    .selectionDisabled(!song.usable)
            }
            .overlay {
                if shown.isEmpty && !model.studioSongsLoading {
                    Text(model.studioSongs.isEmpty ? "No audio in Studio's catalog snapshot." : "Nothing matches.")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct StudioSongRow: View {
    let song: StudioSong

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: song.favorite ? "star.fill" : "music.note")
                .foregroundStyle(song.favorite ? Color.yellow : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(song.name)
                    .lineLimit(1)
                if !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            Text(song.length)
                .monospacedDigit()
                .foregroundStyle(.secondary)
        }
        .opacity(song.usable ? 1 : 0.5)
        .help(song.usable ? song.path : (song.whyNot ?? "Not a type Roughcut reads"))
    }

    private var detail: String {
        var bits: [String] = []
        if song.guess == "sfx" {
            bits.append("sound effect?")
        }
        if !song.provider.isEmpty {
            bits.append(song.provider)
        }
        if !song.license.isEmpty {
            bits.append(song.license)
        }
        if !song.tags.isEmpty {
            bits.append(song.tags.joined(separator: ", "))
        }
        if !song.usable {
            bits.append(song.whyNot ?? "not usable")
        }
        return bits.joined(separator: " · ")
    }
}

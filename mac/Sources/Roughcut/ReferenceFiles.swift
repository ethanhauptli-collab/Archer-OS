import AppKit
import ImageIO
import RoughcutKit
import UniformTypeIdentifiers

/// Getting reference pictures from the pasteboard and from drops (AppKit). Which files a list takes, how many,
/// and where a bitmap is kept are RoughcutKit's `ReferenceImages`.
enum ReferenceFiles {
    /// What a paste or drop brought in: files to offer the list, and a short note when something wasn't usable.
    struct Gathered {
        var urls: [URL] = []
        var note: String?
    }

    /// What ⌘V or the Paste button brings in: picture files copied in Finder, else a copied bitmap (a screenshot, "Copy Image"
    /// in a browser), kept in the References folder. `room` is how many more pictures the list takes: with none, a bitmap isn't
    /// even saved. The pasteboard is read here; the bitmap is converted and written off the main thread.
    @MainActor
    static func fromPasteboard(room: Int, pasteboard: NSPasteboard = .general) async -> Gathered {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        if let files = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL], !files.isEmpty {
            return Gathered(urls: files)
        }
        guard room > 0 else { return Gathered(note: ReferenceImages.fullNote) }
        guard let data = pasteboard.data(forType: .png) ?? pasteboard.data(forType: .tiff) ?? NSImage(pasteboard: pasteboard)?.tiffRepresentation else {
            return Gathered(note: "There is no picture on the clipboard. Copy a screenshot or an image file first.")
        }
        return await keep([data])
    }

    /// What was dropped: picture files by their path, and pictures dragged out of a web page or another app as bitmaps (as
    /// many as `room` allows, kept in the References folder). Anything else (text, a link) is ignored, and the note says so.
    static func gather(_ providers: [NSItemProvider], room: Int) async -> Gathered {
        var gathered = Gathered()
        var bitmaps: [Data] = []
        var ignored = 0
        var unreadable = 0
        var pastTheLimit = 0
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                if let url = await fileURL(from: provider) {
                    gathered.urls.append(url)
                } else {
                    unreadable += 1
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                if bitmaps.count + gathered.urls.count >= room {
                    pastTheLimit += 1  // not even read: it wouldn't be added
                } else if let data = await imageData(from: provider) {
                    bitmaps.append(data)
                } else {
                    unreadable += 1
                }
            } else {
                ignored += 1
            }
        }
        let kept = await keep(bitmaps)
        gathered.urls += kept.urls
        var notes: [String] = []
        if pastTheLimit > 0 {
            notes.append(ReferenceImages.fullNote)
        }
        if ignored > 0 {
            notes.append(ignored == 1 ? "One dropped item isn't a picture, so it was ignored." : "\(ignored) dropped items aren't pictures, so they were ignored.")
        }
        if unreadable > 0 {
            notes.append(unreadable == 1 ? "One dropped item couldn't be read, so it wasn't added." : "\(unreadable) dropped items couldn't be read, so they weren't added.")
        }
        if let note = kept.note {
            notes.append(note)
        }
        gathered.note = notes.isEmpty ? nil : notes.joined(separator: " ")
        return gathered
    }

    /// Bitmaps written into the References folder, off the main thread (converting a large screenshot takes a moment).
    private static func keep(_ blobs: [Data]) async -> Gathered {
        guard !blobs.isEmpty else { return Gathered() }
        return await Task.detached(priority: .userInitiated) { () -> Gathered in
            var kept = Gathered()
            var notes: [String] = []
            for data in blobs {
                guard let (bytes, ext) = keepable(data) else {
                    notes.append("That picture couldn't be read, so it wasn't added.")
                    continue
                }
                do {
                    kept.urls.append(try ReferenceImages.store(bytes, ext: ext, in: ReferenceImages.storeFolder()))
                } catch let error as ReferenceImages.StoreError {
                    notes.append(error.localizedDescription)
                } catch {
                    notes.append("The picture couldn't be saved (\(error.localizedDescription)), so it wasn't added.")
                }
            }
            kept.note = notes.isEmpty ? nil : notes.joined(separator: " ")
            return kept
        }.value
    }

    /// The bytes to keep and the extension to keep them under: a PNG, JPEG, GIF, WebP, BMP or HEIC as it is (re-encoding a
    /// photograph as a PNG could push it past the size the CLI takes), anything else (a TIFF off the pasteboard is
    /// uncompressed) converted to PNG. Nil when it can't be read as a picture.
    static func keepable(_ data: Data) -> (Data, String)? {
        if let kind = ReferenceImages.pictureKind(of: data), kind != "tiff" {
            return (data, kind)
        }
        guard let bitmap = NSBitmapImageRep(data: data), let png = bitmap.representation(using: .png, properties: [:]) else { return nil }
        return (png, "png")
    }

    private static func fileURL(from provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                continuation.resume(returning: url)
            }
        }
    }

    private static func imageData(from provider: NSItemProvider) async -> Data? {
        guard provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) else { return nil }
        return await withCheckedContinuation { continuation in
            _ = provider.loadDataRepresentation(forTypeIdentifier: UTType.image.identifier) { data, _ in
                continuation.resume(returning: data)
            }
        }
    }

    /// At launch: pasted pictures that no list uses and that are over 30 days old are removed.
    /// Runs off the main thread; only the References folder is looked at.
    static func removeStale(keeping: [URL]) {
        Task.detached(priority: .utility) {
            ReferenceImages.removeStale(in: ReferenceImages.storeFolder(), keeping: keeping)
        }
    }

    /// A picture scaled down for the list (long edge `pixels`), read off the main thread. Nil when it can't be read.
    static func thumbnail(of url: URL, pixels: Int) async -> NSImage? {
        let image: CGImage? = await Task.detached(priority: .utility) { () -> CGImage? in
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: pixels,
            ]
            return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        }.value
        return image.map { NSImage(cgImage: $0, size: .zero) }
    }
}

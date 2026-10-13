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

    /// What ⌘V or the Paste button brings in: picture files copied in Finder, else a copied bitmap
    /// (a screenshot, "Copy Image" in a browser), saved as a PNG in the References folder.
    static func fromPasteboard(_ pasteboard: NSPasteboard = .general) -> Gathered {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        if let files = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL], !files.isEmpty {
            return Gathered(urls: files)
        }
        if let data = pasteboard.data(forType: .png) ?? pasteboard.data(forType: .tiff) {
            return keep(data)
        }
        if let image = NSImage(pasteboard: pasteboard), let tiff = image.tiffRepresentation {
            return keep(tiff)
        }
        return Gathered(note: "There is no picture on the clipboard. Copy a screenshot or an image file first.")
    }

    /// What was dropped: picture files by their path, and a picture dragged out of a web page or another app as a bitmap.
    /// Anything else (text, a link) is ignored, and the note says so.
    static func gather(_ providers: [NSItemProvider]) async -> Gathered {
        var gathered = Gathered()
        var ignored = 0
        var unreadable = 0
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                if let url = await fileURL(from: provider) {
                    gathered.urls.append(url)
                } else {
                    unreadable += 1
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                let kept = await imageData(from: provider).map { keep($0) }
                if let urls = kept?.urls, !urls.isEmpty {
                    gathered.urls += urls
                } else {
                    unreadable += 1
                }
            } else {
                ignored += 1
            }
        }
        var notes: [String] = []
        if ignored > 0 {
            notes.append(ignored == 1 ? "One dropped item isn't a picture, so it was ignored." : "\(ignored) dropped items aren't pictures, so they were ignored.")
        }
        if unreadable > 0 {
            notes.append(unreadable == 1 ? "One dropped item couldn't be read, so it wasn't added." : "\(unreadable) dropped items couldn't be read, so they weren't added.")
        }
        gathered.note = notes.isEmpty ? nil : notes.joined(separator: " ")
        return gathered
    }

    /// A bitmap written as a PNG into the References folder.
    private static func keep(_ data: Data) -> Gathered {
        guard let png = pngData(from: data) else {
            return Gathered(note: "That picture couldn't be read, so it wasn't added.")
        }
        do {
            return Gathered(urls: [try ReferenceImages.store(png: png, in: ReferenceImages.storeFolder())])
        } catch {
            return Gathered(note: "The picture couldn't be saved (\(error.localizedDescription)), so it wasn't added.")
        }
    }

    /// PNG data as it is, any other bitmap (TIFF, JPEG, GIF...) converted.
    static func pngData(from data: Data) -> Data? {
        if data.starts(with: [0x89, 0x50, 0x4E, 0x47]) {
            return data
        }
        guard let bitmap = NSBitmapImageRep(data: data) else { return nil }
        return bitmap.representation(using: .png, properties: [:])
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

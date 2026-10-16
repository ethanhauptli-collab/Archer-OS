// One file in a small window of its own that can be dragged out, as a file in a Finder window can: a real drag session with the
// file's URL on the drag pasteboard, so what the drop lands on gets what Finder would give it. ui-drive's `drag` step moves the
// mouse from this window to the drop.
//   swiftc -O .github/scripts/ui-drag-source.swift -o ui-drag-source
//   ui-drag-source FILE X Y        X, Y: where the window's top left goes on the screen (the same top left origin the clicks use)
// It prints "ready" once the window is up, and says how each drag ended ("taken" when a window accepted the drop, "none" when
// nothing did). It stops by itself after 3 minutes.
import AppKit

final class FileTile: NSView, NSDraggingSource {
    let url: URL
    private var dragging = false

    init(url: URL, frame: NSRect) {
        self.url = url
        super.init(frame: frame)
    }

    required init?(coder: NSCoder) { nil }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemOrange.setFill()
        bounds.fill()
        NSWorkspace.shared.icon(forFile: url.path).draw(in: NSRect(x: 20, y: 24, width: 50, height: 50))
        let name = url.lastPathComponent as NSString
        name.draw(in: NSRect(x: 4, y: 2, width: bounds.width - 8, height: 18), withAttributes: [
            .font: NSFont.systemFont(ofSize: 10),
            .foregroundColor: NSColor.black,
        ])
    }

    override func mouseDown(with event: NSEvent) {
        dragging = false
        print("mouse down in the tile")
        fflush(stdout)
    }

    override func mouseDragged(with event: NSEvent) {
        guard !dragging else { return }
        dragging = true
        let item = NSDraggingItem(pasteboardWriter: url as NSURL)
        item.setDraggingFrame(NSRect(x: 0, y: 0, width: 48, height: 48), contents: NSWorkspace.shared.icon(forFile: url.path))
        beginDraggingSession(with: [item], event: event, source: self)
        print("drag session begun for \(url.lastPathComponent)")
        fflush(stdout)
    }

    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        [.copy, .generic, .link]
    }

    func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) {
        print("drag ended: \(operation.isEmpty ? "none" : "taken") (operation \(operation.rawValue)) at \(Int(screenPoint.x)), \(Int(screenPoint.y))")
        fflush(stdout)
        dragging = false
    }
}

let arguments = CommandLine.arguments
guard arguments.count == 4, let left = Double(arguments[2]), let top = Double(arguments[3]) else {
    FileHandle.standardError.write(Data("usage: ui-drag-source FILE X Y\n".utf8))
    exit(2)
}
let file = URL(fileURLWithPath: arguments[1])
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let size = NSSize(width: 90, height: 90)
let screenHeight = NSScreen.screens.first?.frame.height ?? 768
let window = NSWindow(
    contentRect: NSRect(x: left, y: screenHeight - top - size.height, width: size.width, height: size.height),
    styleMask: [.borderless], backing: .buffered, defer: false
)
window.level = .floating
window.isOpaque = true
window.hasShadow = false
window.contentView = FileTile(url: file, frame: NSRect(origin: .zero, size: size))
window.orderFrontRegardless()
DispatchQueue.main.asyncAfter(deadline: .now() + 180) { exit(0) }
print("ready")
fflush(stdout)
app.run()

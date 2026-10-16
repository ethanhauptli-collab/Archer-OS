// A window with a SwiftUI button, a bordered-prominent one and an AppKit one; each press and every mouse event goes to a log.
import AppKit
import SwiftUI

let logPath = "/tmp/click-target.log"

func log(_ text: String) {
    let line = text + "\n"
    if let handle = FileHandle(forWritingAtPath: logPath) {
        handle.seekToEndOfFile()
        handle.write(Data(line.utf8))
        try? handle.close()
    } else {
        try? line.write(toFile: logPath, atomically: true, encoding: .utf8)
    }
}

struct NativeButton: NSViewRepresentable {
    final class Coordinator: NSObject {
        @objc func pressed() {
            log("native click")
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> NSButton {
        NSButton(title: "Native button", target: context.coordinator, action: #selector(Coordinator.pressed))
    }

    func updateNSView(_ view: NSButton, context: Context) {}
}

struct Content: View {
    var body: some View {
        VStack(spacing: 24) {
            Text("Probe").font(.title)
            Button("SwiftUI button") {
                log("swiftui click")
            }
            Button {
                log("prominent click")
            } label: {
                Text("Prominent")
            }
            .buttonStyle(.borderedProminent)
            NativeButton()
        }
        .padding(60)
        .frame(width: 400, height: 320)
    }
}

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 400, height: 320), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.contentView = NSHostingView(rootView: Content())
        window.title = "Click probe"
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseUp, .mouseEntered]) { event in
            log("event \(event.type.rawValue) clicks \(event.clickCount) key \(event.window?.isKeyWindow ?? false) active \(NSApp.isActive)")
            return event
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            log("became active")
        }
        log("started key \(window.isKeyWindow) active \(NSApp.isActive)")
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.run()

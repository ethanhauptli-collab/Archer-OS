import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare executable (`swift run`, Xcode) so the
        // app gets a Dock icon, a menu bar and keyboard focus.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct RoughcutApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    var body: some Scene {
        Window("Roughcut", id: "main") {
            ContentView()
                .environment(model)
                .frame(minWidth: 620, minHeight: 680)
                .task { await model.refreshDoctor() }
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Build") { model.startOver() }
                    .keyboardShortcut("n", modifiers: .command)
                    .disabled(model.isRunning)
            }
        }

        Settings {
            SettingsView()
                .environment(model)
        }
    }
}

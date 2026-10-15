import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare executable (`swift run`, Xcode) so the
        // app gets a Dock icon, a menu bar and keyboard focus.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        // A drive may have been unplugged or mounted while the app was behind: look again at the recent cuts' files.
        guard let model else { return }
        Task { await model.refreshRecents() }
        // A setup problem is usually fixed in Terminal (Claude Code signed in, ffmpeg installed): look again when the app comes
        // back to the front, so the banner clears without a click on Check again.
        if !model.setupIssues.isEmpty && !model.checkingSetup && !model.isRunning {
            Task { await model.refreshDoctor() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Child processes outlive their parent; stop a build still running.
        model?.cancel()
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
                .alert("Roughcut update", isPresented: Binding(
                    get: { model.updateOutput != nil },
                    set: { if !$0 { model.updateOutput = nil } }
                )) {
                    Button("OK") { model.updateOutput = nil }
                } message: {
                    Text(model.updateOutput ?? "")
                }
                .onAppear { appDelegate.model = model }
                .task {
                    model.tidyReferences()
                    async let recents: Void = model.refreshRecents()
                    await model.refreshDoctor()
                    await recents
                }
        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Build") { model.startOver() }
                    .keyboardShortcut("n", modifiers: .command)
                    .disabled(model.isRunning)
            }
            CommandGroup(after: .appInfo) {
                Button("Update Roughcut…") { model.updateRoughcut() }
                    .disabled(model.isRunning || model.updating)
            }
        }

        Settings {
            SettingsView()
                .environment(model)
        }
    }
}

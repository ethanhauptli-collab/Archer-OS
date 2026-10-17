import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private weak var model: AppModel?
    /// Footage that arrived before the window had its model (`open -a Roughcut FOLDER` starting the app).
    private var waitingFootage: [URL] = []

    /// Folders and clips handed to the app from outside are footage for the next build.
    func application(_ application: NSApplication, open urls: [URL]) {
        waitingFootage.append(contentsOf: urls)
        addWaitingFootage()
    }

    func attach(_ model: AppModel) {
        self.model = model
        addWaitingFootage()
    }

    private func addWaitingFootage() {
        guard UIPreview.screen == nil, let model, !waitingFootage.isEmpty else { return }
        let urls = waitingFootage
        waitingFootage = []
        model.openFootage(urls)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Needed when launched as a bare executable (`swift run`, Xcode) so the
        // app gets a Dock icon, a menu bar and keyboard focus.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        if UIPreview.screen != nil {
            openPreviewWindow()
        }
    }

    /// For the pictures of each screen (see `UIPreview`): the dark look when asked, and a window of a size that shows a whole screen.
    private func openPreviewWindow() {
        if UIPreview.dark {
            NSApp.appearance = NSAppearance(named: .darkAqua)
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard let window = NSApp.windows.first(where: { $0.canBecomeMain }) else { return }
            window.setContentSize(NSSize(width: 860, height: 760))
            window.center()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate()
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard UIPreview.screen == nil else { return }  // made-up content: nothing to look at again
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
                .onAppear { appDelegate.attach(model) }
                .task {
                    if let screen = UIPreview.screen {
                        model.applyPreview(screen)
                        return
                    }
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

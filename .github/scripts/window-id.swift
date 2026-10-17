// Prints the window number of the first on-screen window a process owns, for `screencapture -l`.
//   swiftc -O .github/scripts/window-id.swift -o window-id && ./window-id PID
import CoreGraphics
import Foundation

guard CommandLine.arguments.count > 1, let pid = Int32(CommandLine.arguments[1]) else {
    FileHandle.standardError.write(Data("usage: window-id PID\n".utf8))
    exit(2)
}
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
for window in windows {
    let owner = window[kCGWindowOwnerPID as String] as? Int32
    let layer = window[kCGWindowLayer as String] as? Int
    let bounds = window[kCGWindowBounds as String] as? [String: Any]
    let height = bounds?["Height"] as? Double ?? 0
    // The app's own window: the normal layer, and taller than a toolbar.
    if owner == pid, layer == 0, height > 200, let number = window[kCGWindowNumber as String] as? Int {
        print(number)
        exit(0)
    }
}
exit(1)

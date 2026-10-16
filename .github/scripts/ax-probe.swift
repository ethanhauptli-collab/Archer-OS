import AppKit
import ApplicationServices

func say(_ text: String) {
    print(text)
    fflush(stdout)
}

func attribute(_ element: AXUIElement, _ name: String) -> (CFTypeRef?, AXError) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return (error == .success ? value : nil, error)
}

func text(_ element: AXUIElement, _ name: String) -> String {
    (attribute(element, name).0 as? String) ?? ""
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (attribute(element, kAXChildrenAttribute).0 as? [AXUIElement]) ?? []
}

func frame(_ element: AXUIElement) -> CGRect? {
    var point = CGPoint.zero
    var size = CGSize.zero
    guard let p = attribute(element, kAXPositionAttribute).0, CFGetTypeID(p) == AXValueGetTypeID(),
          AXValueGetValue(p as! AXValue, .cgPoint, &point),
          let s = attribute(element, kAXSizeAttribute).0, CFGetTypeID(s) == AXValueGetTypeID(),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

var dumped = 0
func dump(_ element: AXUIElement, depth: Int = 0) {
    guard depth < 7, dumped < 80 else { return }
    dumped += 1
    let role = text(element, kAXRoleAttribute)
    let title = text(element, kAXTitleAttribute)
    let desc = text(element, kAXDescriptionAttribute)
    say(String(repeating: "  ", count: depth) + "\(role) title=\(title.debugDescription) desc=\(desc.debugDescription)")
    for child in children(element) {
        dump(child, depth: depth + 1)
    }
}

func findButton(_ element: AXUIElement, titled wanted: String, depth: Int = 0) -> AXUIElement? {
    guard depth < 10 else { return nil }
    if text(element, kAXRoleAttribute) == kAXButtonRole as String,
       text(element, kAXTitleAttribute) == wanted || text(element, kAXDescriptionAttribute) == wanted {
        return element
    }
    for child in children(element) {
        if let found = findButton(child, titled: wanted, depth: depth + 1) { return found }
    }
    return nil
}

say("macOS \(ProcessInfo.processInfo.operatingSystemVersionString), user \(NSUserName())")
say("AXIsProcessTrusted: \(AXIsProcessTrusted())")
say("CGPreflightPostEventAccess: \(CGPreflightPostEventAccess())")
say("CGPreflightListenEventAccess: \(CGPreflightListenEventAccess())")
say("CGPreflightScreenCaptureAccess: \(CGPreflightScreenCaptureAccess())")
say("screens: \(NSScreen.screens.count), main display \(CGMainDisplayID())")
if let session = CGSessionCopyCurrentDictionary() as? [String: Any] {
    say("session: onConsole=\(session["kCGSSessionOnConsoleKey"] ?? "?") loginDone=\(session["kCGSessionLoginDoneKey"] ?? "?")")
} else {
    say("session: no window-server session")
}

let textEdit = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
let launched = DispatchSemaphore(value: 0)
var running: NSRunningApplication?
NSWorkspace.shared.openApplication(at: textEdit, configuration: NSWorkspace.OpenConfiguration()) { app, error in
    running = app
    if let error { say("launch error: \(error)") }
    launched.signal()
}
_ = launched.wait(timeout: .now() + 20)
guard let app = running else {
    say("TextEdit did not start")
    exit(0)
}
say("TextEdit pid \(app.processIdentifier)")
Thread.sleep(forTimeInterval: 4)

let element = AXUIElementCreateApplication(app.processIdentifier)
let (windows, windowsError) = attribute(element, kAXWindowsAttribute)
say("AX windows: \((windows as? [AXUIElement])?.count ?? -1), error \(windowsError.rawValue)")
dump(element)

func click(at point: CGPoint) {
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.2)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
}

func windowCount() -> Int {
    (attribute(element, kAXWindowsAttribute).0 as? [AXUIElement])?.count ?? -1
}

if let button = findButton(element, titled: "New Document"), let box = frame(button) {
    say("found New Document at \(box)")
    app.activate()
    Thread.sleep(forTimeInterval: 1)
    let before = windowCount()
    click(at: CGPoint(x: box.midX, y: box.midY))
    Thread.sleep(forTimeInterval: 2)
    let afterClick = windowCount()
    say("real mouse click: windows \(before) -> \(afterClick)")
    if afterClick == before, let again = findButton(element, titled: "New Document") {
        let pressed = AXUIElementPerformAction(again, kAXPressAction as CFString)
        Thread.sleep(forTimeInterval: 2)
        say("AXPress result \(pressed.rawValue): windows \(afterClick) -> \(windowCount())")
    }
} else {
    say("no New Document button (window shape differs)")
}
app.terminate()

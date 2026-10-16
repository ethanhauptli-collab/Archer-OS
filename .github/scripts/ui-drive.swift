// Drives a Mac app the way a person would, on a Mac runner: it reads the app's Accessibility tree, clicks with real mouse
// events, types, waits for text to appear and takes a picture of the window.
//   swiftc -O .github/scripts/ui-drive.swift -o ui-drive
//   ui-drive BUNDLE_ID_OR_PID SCRIPT OUTPUT_FOLDER
// The script is one step a line (# starts a comment; "double quotes" hold a text with spaces):
//   wait-window [SECONDS]      the app has a window
//   see "TEXT" [SECONDS]       something on screen is called TEXT (exact; ^TEXT: begins with it; *TEXT: contains it)
//   gone "TEXT" [SECONDS]      nothing is called TEXT any more
//   press "LABEL" [SECONDS]    a real mouse click in the middle of the enabled button called LABEL (same ways to match; "Add#2"
//                              is the second button with that name)
//   press-ax "LABEL" [SECONDS] the same through Accessibility's press, for a control a click can't reach
//   click "LABEL" [SECONDS]    a real click on anything in view called LABEL (a row in a list), not only a button
//   scroll-to "LABEL"         roll the wheel over the page until something called LABEL is in view
//   focus ROLE                 a click in the first enabled element of that role (AXTextArea, AXTextField), to type into it
//   type "TEXT"                keys, one at a time, into whatever has the keyboard
//   key NAME                   return, escape, tab or space, with cmd- / shift- / opt- in front
//   shot NAME                  OUTPUT_FOLDER/NAME.png, a picture of the app's window
//   dump NAME                  OUTPUT_FOLDER/NAME.ax.txt, the Accessibility tree
//   pause SECONDS
// A step that doesn't happen in time stops the run (exit 1) after saving failure.png and failure.ax.txt.
import AppKit
import ApplicationServices
import Foundation

let buttonRoles: Set<String> = [
    "AXButton", "AXRadioButton", "AXCheckBox", "AXPopUpButton", "AXMenuButton", "AXLink", "AXDisclosureTriangle", "AXMenuItem",
]

var bundleID = ""
var outputFolder = ""
var stepNumber = 0
var appPID: pid_t = 0

func write(_ text: String, to handle: FileHandle) {
    handle.write(Data((text + "\n").utf8))
}

func say(_ text: String) {
    write(text, to: FileHandle.standardOutput)
}

// MARK: Accessibility

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func string(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attribute(element, name) else { return "" }
    if let text = value as? String { return text }
    if let number = value as? NSNumber { return number.stringValue }
    return ""
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (attribute(element, "AXChildren") as? [AXUIElement]) ?? []
}

func frame(of element: AXUIElement) -> CGRect? {
    var origin = CGPoint.zero
    var size = CGSize.zero
    guard let position = attribute(element, "AXPosition"), CFGetTypeID(position) == AXValueGetTypeID(),
          AXValueGetValue(position as! AXValue, .cgPoint, &origin),
          let extent = attribute(element, "AXSize"), CFGetTypeID(extent) == AXValueGetTypeID(),
          AXValueGetValue(extent as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: origin, size: size)
}

/// The words a button shows when it has no title of its own.
func staticTexts(under element: AXUIElement, depth: Int = 0) -> [String] {
    guard depth < 4 else { return [] }
    var texts: [String] = []
    for child in children(element) {
        if string(child, "AXRole") == "AXStaticText" {
            let text = string(child, "AXValue")
            if !text.isEmpty {
                texts.append(text)
            }
        }
        texts += staticTexts(under: child, depth: depth + 1)
    }
    return texts
}

struct Node {
    let element: AXUIElement
    let role: String
    let labels: [String]
    let enabled: Bool
    let frame: CGRect?
    let depth: Int
    /// The visible part of the scroll area the node sits in (nil outside one): a control scrolled out of it can't be clicked.
    let viewport: CGRect?
}

/// The app by its bundle identifier, or, when a process number is given instead, that process.
func runningApp() -> NSRunningApplication? {
    var app: NSRunningApplication?
    if let fixed = Int32(bundleID) {
        app = NSRunningApplication(processIdentifier: fixed)
    } else {
        app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first
    }
    if let app {
        appPID = app.processIdentifier
    }
    return app
}

/// Every element of the app, in reading order (the menu bar left out: it is long and nothing here uses it).
func nodes(limit: Int = 6000) -> [Node] {
    guard let app = runningApp() else { return [] }
    var found: [Node] = []
    func visit(_ element: AXUIElement, _ depth: Int, _ viewport: CGRect?) {
        guard found.count < limit, depth < 60 else { return }
        let role = string(element, "AXRole")
        var labels: [String] = []
        for name in ["AXTitle", "AXDescription", "AXValue", "AXHelp", "AXIdentifier"] {
            let text = string(element, name)
            if !text.isEmpty && !labels.contains(text) {
                labels.append(text)
            }
        }
        if buttonRoles.contains(role) {
            for text in staticTexts(under: element) where !labels.contains(text) {
                labels.append(text)
            }
        }
        let enabled = (attribute(element, "AXEnabled") as? NSNumber)?.boolValue ?? true
        let box = frame(of: element)
        found.append(Node(element: element, role: role, labels: labels, enabled: enabled, frame: box, depth: depth, viewport: viewport))
        if role == "AXMenuBar" {
            return
        }
        let inside = role == "AXScrollArea" ? box : viewport
        for child in children(element) {
            visit(child, depth + 1, inside)
        }
    }
    visit(AXUIElementCreateApplication(app.processIdentifier), 0, nil)
    return found
}

func matches(_ labels: [String], _ pattern: String) -> Bool {
    if pattern.hasPrefix("^") {
        let wanted = String(pattern.dropFirst())
        return labels.contains { $0.hasPrefix(wanted) }
    }
    if pattern.hasPrefix("*") {
        let wanted = String(pattern.dropFirst())
        return labels.contains { $0.contains(wanted) }
    }
    return labels.contains(pattern)
}

// MARK: Pictures and the tree

func windowNumber() -> Int? {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    for window in windows {
        let owner = window[kCGWindowOwnerPID as String] as? Int32
        let layer = window[kCGWindowLayer as String] as? Int
        let bounds = window[kCGWindowBounds as String] as? [String: Any]
        let height = bounds?["Height"] as? Double ?? 0
        if owner == appPID, layer == 0, height > 200, let number = window[kCGWindowNumber as String] as? Int {
            return number
        }
    }
    return nil
}

func savePicture(_ name: String) {
    _ = runningApp()
    let path = outputFolder + "/" + name + ".png"
    var arguments = ["-x", path]
    if let number = windowNumber() {
        arguments = ["-x", "-o", "-l", String(number), path]
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    process.arguments = arguments
    try? process.run()
    process.waitUntilExit()
}

func saveTree(_ name: String) {
    var lines: [String] = []
    for node in nodes() {
        var box = ""
        if let rect = node.frame {
            box = String(format: "(%.0f,%.0f %.0fx%.0f)", Double(rect.origin.x), Double(rect.origin.y), Double(rect.width), Double(rect.height))
        }
        let words = node.labels.map { "\u{201C}" + $0.replacingOccurrences(of: "\n", with: " ") + "\u{201D}" }.joined(separator: " ")
        let indent = String(repeating: "  ", count: node.depth)
        lines.append(indent + node.role + (node.enabled ? "" : " disabled") + " " + box + " " + words)
    }
    try? lines.joined(separator: "\n").write(toFile: outputFolder + "/" + name + ".ax.txt", atomically: true, encoding: .utf8)
}

func treeLines(limit: Int) -> [String] {
    var lines: [String] = []
    for node in nodes() {
        if lines.count >= limit {
            break
        }
        if node.labels.isEmpty && !buttonRoles.contains(node.role) {
            continue
        }
        var box = ""
        if let rect = node.frame {
            box = String(format: "(%.0f,%.0f %.0fx%.0f)", Double(rect.origin.x), Double(rect.origin.y), Double(rect.width), Double(rect.height))
        }
        let words = node.labels.map { "\u{201C}" + $0.replacingOccurrences(of: "\n", with: " ") + "\u{201D}" }.joined(separator: " ")
        lines.append("    " + node.role + (node.enabled ? "" : " disabled") + " " + box + " " + words)
    }
    return lines
}

func failStep(_ message: String) -> Never {
    write("ui-drive: step \(stepNumber) failed: \(message)", to: FileHandle.standardError)
    if let screen = NSScreen.main {
        write("  screen \(Int(screen.frame.width))x\(Int(screen.frame.height))", to: FileHandle.standardError)
    }
    write("  on screen:", to: FileHandle.standardError)
    for line in treeLines(limit: 70) {
        write(line, to: FileHandle.standardError)
    }
    savePicture("failure")
    saveTree("failure")
    exit(1)
}

// MARK: Events

/// An app started from a script is not the active one on a runner (its window stays in the inactive look, and a click on it
/// may only wake it): Accessibility is asked to put it in front, the window is raised, and, when the app still says it is not
/// active, a click on the title bar (which does nothing but make the window the one in front) comes before the real click.
func bringToFront() {
    guard let app = runningApp() else { return }
    let element = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetAttributeValue(element, "AXFrontmost" as CFString, kCFBooleanTrue)
    let window = (attribute(element, "AXWindows") as? [AXUIElement])?.first
    if let window {
        AXUIElementPerformAction(window, "AXRaise" as CFString)
    }
    app.activate()
    Thread.sleep(forTimeInterval: 0.3)
    let active = runningApp()?.isActive ?? false
    say("  active: \(active)")
    if !active, let window, let box = frame(of: window) {
        say("  clicking the title bar to wake the window")
        click(at: CGPoint(x: box.midX, y: box.minY + 10), quiet: true)
        Thread.sleep(forTimeInterval: 0.5)
        say("  active: \(runningApp()?.isActive ?? false)")
    }
}

/// A click as the mouse makes it: the pointer moves there, then down and up, each saying it is click number one (SwiftUI's
/// buttons count clicks, and a press that says zero is not a tap). UI_DRIVE_CLICK picks another way when asked:
/// session, annotated (another place to post to), pid (straight to the app), slow (the pointer walks up to the button and the
/// press is held), double (two clicks).
func click(at point: CGPoint, quiet: Bool = false) {
    let way = ProcessInfo.processInfo.environment["UI_DRIVE_CLICK"] ?? "hid"
    let source = CGEventSource(stateID: way == "combined" ? .combinedSessionState : .hidSystemState)
    func post(_ type: CGEventType, count: Int64 = 1) {
        guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left) else { return }
        if type == .leftMouseDown || type == .leftMouseUp {
            event.setIntegerValueField(.mouseEventClickState, value: count)
        }
        switch way {
        case "session": event.post(tap: .cgSessionEventTap)
        case "annotated": event.post(tap: .cgAnnotatedSessionEventTap)
        case "pid": event.postToPid(appPID)
        default: event.post(tap: .cghidEventTap)
        }
    }
    if way == "slow" {
        for step in stride(from: 8, through: 1, by: -1) {
            let near = CGPoint(x: point.x - CGFloat(step * 6), y: point.y - CGFloat(step * 3))
            CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: near, mouseButton: .left)?.post(tap: .cghidEventTap)
            Thread.sleep(forTimeInterval: 0.03)
        }
    }
    post(.mouseMoved)
    Thread.sleep(forTimeInterval: 0.15)
    if !quiet, let here = CGEvent(source: nil)?.location {
        say("  cursor at (\(Int(here.x)), \(Int(here.y))), way \(way)")
    }
    post(.leftMouseDown)
    Thread.sleep(forTimeInterval: way == "slow" ? 0.2 : 0.06)
    post(.leftMouseUp)
    if way == "double" {
        Thread.sleep(forTimeInterval: 0.08)
        post(.leftMouseDown, count: 2)
        Thread.sleep(forTimeInterval: 0.06)
        post(.leftMouseUp, count: 2)
    }
}

/// What is on screen as one text, to tell whether a click did anything: the toolbar and what animates are left out.
func screenSignature() -> String {
    var skipBelow: Int?
    var parts: [String] = []
    for node in nodes() {
        if let depth = skipBelow {
            if node.depth > depth {
                continue
            }
            skipBelow = nil
        }
        if node.role == "AXToolbar" || node.role == "AXBusyIndicator" || node.role == "AXProgressIndicator" {
            skipBelow = node.depth
            continue
        }
        parts.append(node.role + ":" + node.labels.joined(separator: "|"))
    }
    return parts.joined(separator: "\n")
}

var realClicks = 0
var pressedWithAccessibility = 0

/// Rolls the wheel over a point (negative: the content moves up, as a swipe down the page).
func scrollWheel(at point: CGPoint, by pixels: Int32) {
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: pixels, wheel2: 0, wheel3: 0)
    event?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.25)
}

func typeText(_ text: String) {
    for character in text {
        let units = Array(String(character).utf16)
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)
            event?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
            event?.post(tap: .cghidEventTap)
        }
        Thread.sleep(forTimeInterval: 0.02)
    }
}

func pressKey(_ name: String) {
    var flags = CGEventFlags()
    var key = name
    while true {
        if key.hasPrefix("cmd-") {
            flags.insert(.maskCommand)
            key = String(key.dropFirst(4))
        } else if key.hasPrefix("shift-") {
            flags.insert(.maskShift)
            key = String(key.dropFirst(6))
        } else if key.hasPrefix("opt-") {
            flags.insert(.maskAlternate)
            key = String(key.dropFirst(4))
        } else {
            break
        }
    }
    let codes: [String: CGKeyCode] = ["return": 36, "tab": 48, "space": 49, "escape": 53, "n": 45, "a": 0, "v": 9, "w": 13, "comma": 43]
    guard let code = codes[key] else { failStep("no key called \(name)") }
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)
        event?.flags = flags
        event?.post(tap: .cghidEventTap)
    }
}

func waitFor(timeout: Double, _ condition: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
        if condition() {
            return true
        }
        Thread.sleep(forTimeInterval: 0.3)
    } while Date() < deadline
    return false
}

// MARK: The script

func tokens(_ line: String) -> [String] {
    var result: [String] = []
    var current = ""
    var quoted = false
    var started = false
    for character in line {
        if character == "\"" {
            quoted.toggle()
            started = true
        } else if character == " " && !quoted {
            if started {
                result.append(current)
                current = ""
                started = false
            }
        } else {
            current.append(character)
            started = true
        }
    }
    if started {
        result.append(current)
    }
    return result
}

/// "Add#2": the label and which of the matches is meant (the first when there is no #N at the end).
func splitIndex(_ text: String) -> (label: String, which: Int) {
    if let hash = text.lastIndex(of: "#"), hash != text.startIndex, let which = Int(text[text.index(after: hash)...]), which >= 1 {
        return (String(text[..<hash]), which)
    }
    return (text, 1)
}

func seconds(_ parts: [String], at index: Int, default fallback: Double) -> Double {
    parts.count > index ? (Double(parts[index]) ?? fallback) : fallback
}

let arguments = CommandLine.arguments
guard arguments.count == 4 else {
    write("usage: ui-drive BUNDLE_ID SCRIPT OUTPUT_FOLDER", to: FileHandle.standardError)
    exit(2)
}
bundleID = arguments[1]
outputFolder = arguments[3]
guard let script = try? String(contentsOfFile: arguments[2], encoding: .utf8) else {
    write("ui-drive: can't read \(arguments[2])", to: FileHandle.standardError)
    exit(2)
}
try? FileManager.default.createDirectory(atPath: outputFolder, withIntermediateDirectories: true)
guard AXIsProcessTrusted() else {
    write("ui-drive: this process is not trusted for Accessibility", to: FileHandle.standardError)
    exit(3)
}

for rawLine in script.split(separator: "\n") {
    let line = rawLine.trimmingCharacters(in: .whitespaces)
    if line.isEmpty || line.hasPrefix("#") {
        continue
    }
    let parts = tokens(line)
    stepNumber += 1
    say("[\(stepNumber)] \(line)")
    switch parts[0] {
    case "wait-window":
        let ok = waitFor(timeout: seconds(parts, at: 1, default: 60)) {
            guard let app = runningApp() else { return false }
            let windows = attribute(AXUIElementCreateApplication(app.processIdentifier), "AXWindows") as? [AXUIElement]
            return !(windows ?? []).isEmpty
        }
        if !ok {
            failStep("the app never showed a window")
        }
    case "see":
        let text = parts[1]
        if !waitFor(timeout: seconds(parts, at: 2, default: 30), { nodes().contains { matches($0.labels, text) } }) {
            failStep("nothing on screen is called \u{201C}\(text)\u{201D}")
        }
    case "gone":
        let text = parts[1]
        if !waitFor(timeout: seconds(parts, at: 2, default: 30), { !nodes().contains { matches($0.labels, text) } }) {
            failStep("\u{201C}\(text)\u{201D} is still on screen")
        }
    case "press", "press-ax":
        let (label, which) = splitIndex(parts[1])
        var target: Node?
        let ok = waitFor(timeout: seconds(parts, at: 2, default: 30)) {
            let all = nodes()
            let window = all.first { $0.role == "AXWindow" }?.frame
            let found = all.filter { node in
                guard buttonRoles.contains(node.role), node.enabled, matches(node.labels, label),
                      let box = node.frame, box.width > 2, box.height > 2 else { return false }
                if node.role == "AXMenuItem" {
                    return true
                }
                let centre = CGPoint(x: box.midX, y: box.midY)
                if let window, !window.contains(centre) {
                    return false
                }
                if let viewport = node.viewport, !viewport.contains(centre) {
                    return false
                }
                return true
            }
            target = found.count >= which ? found[which - 1] : nil
            return target != nil
        }
        guard ok, let node = target, let box = node.frame else {
            failStep("no enabled button called \u{201C}\(parts[1])\u{201D}")
        }
        if parts[0] == "press-ax" {
            AXUIElementPerformAction(node.element, "AXPress" as CFString)
        } else {
            bringToFront()
            Thread.sleep(forTimeInterval: 0.3)
            let before = screenSignature()
            say("  clicking at (\(Int(box.midX)), \(Int(box.midY)))")
            click(at: CGPoint(x: box.midX, y: box.midY))
            Thread.sleep(forTimeInterval: 1.0)
            if screenSignature() == before && ProcessInfo.processInfo.environment["UI_DRIVE_NO_FALLBACK"] == nil {
                // Nothing on screen changed: say so, and press the button the way a screen reader does.
                say("  the click changed nothing; pressing with Accessibility")
                AXUIElementPerformAction(node.element, "AXPress" as CFString)
                pressedWithAccessibility += 1
            } else {
                realClicks += 1
            }
        }
        Thread.sleep(forTimeInterval: 0.5)
    case "scroll-to":
        let label = parts[1]
        var arrived = false
        for _ in 0..<30 {
            let all = nodes()
            if let node = all.first(where: { matches($0.labels, label) && $0.frame != nil }), let box = node.frame {
                let centre = CGPoint(x: box.midX, y: box.midY)
                if node.viewport?.contains(centre) ?? true {
                    arrived = true
                    break
                }
            }
            // Down the page: over the biggest scroll area on screen.
            let areas = all.filter { $0.role == "AXScrollArea" }.compactMap { $0.frame }
            guard let area = areas.max(by: { $0.width * $0.height < $1.width * $1.height }) else { break }
            scrollWheel(at: CGPoint(x: area.midX, y: area.midY), by: -160)
        }
        if !arrived {
            failStep("could not scroll \u{201C}\(label)\u{201D} into view")
        }
    case "click":
        let label = parts[1]
        var element: Node?
        let seen = waitFor(timeout: seconds(parts, at: 2, default: 30)) {
            element = nodes().first { node in
                guard matches(node.labels, label), let box = node.frame, box.width > 2, box.height > 2 else { return false }
                return node.viewport?.contains(CGPoint(x: box.midX, y: box.midY)) ?? true
            }
            return element != nil
        }
        guard seen, let found = element, let box = found.frame else {
            failStep("nothing in view called \u{201C}\(label)\u{201D} to click")
        }
        bringToFront()
        click(at: CGPoint(x: box.midX, y: box.midY))
        Thread.sleep(forTimeInterval: 0.5)
    case "focus":
        let role = parts[1]
        var field: Node?
        let found = waitFor(timeout: seconds(parts, at: 2, default: 20)) {
            field = nodes().first { node in
                guard node.role == role, node.enabled, let box = node.frame, box.width > 2, box.height > 2 else { return false }
                return node.viewport?.contains(CGPoint(x: box.midX, y: box.midY)) ?? true
            }
            return field != nil
        }
        guard found, let target = field, let box = target.frame else {
            failStep("no \(role) to put the keyboard in")
        }
        bringToFront()
        click(at: CGPoint(x: box.midX, y: box.midY))
        Thread.sleep(forTimeInterval: 0.4)
    case "type":
        typeText(parts[1])
    case "key":
        pressKey(parts[1])
    case "shot":
        savePicture(parts[1])
    case "dump":
        saveTree(parts[1])
    case "pause":
        Thread.sleep(forTimeInterval: seconds(parts, at: 1, default: 1))
    default:
        failStep("no step called \(parts[0])")
    }
}
say("ui-drive: \(stepNumber) steps done (\(realClicks) real clicks, \(pressedWithAccessibility) presses through Accessibility)")

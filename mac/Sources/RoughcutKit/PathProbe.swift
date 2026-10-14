import Foundation

/// The answer to "is this file there?", asked without a network share being able to freeze the app.
public enum Presence: Equatable, Sendable {
    case there
    case gone
    /// No answer in time: the file may be fine and its drive slow, asleep or gone.
    case silent
}

/// Asks the file system whether a file exists, off the calling thread and with a deadline.
///
/// A mounted network share whose server has stopped answering blocks `stat` in the kernel for as long as the system's own
/// timeout, and no signal reaches the call. The window runs on one thread, so a check made there takes the whole window with
/// it. Here each check runs on its own thread and is given up on after a couple of seconds (`.silent`); a check that never
/// returns is left behind on its thread, and the same path is not asked again while it is still waiting.
public enum PathProbe {
    /// How long one check waits for its drive.
    public static let seconds: TimeInterval = 2

    public static func presence(
        of path: String,
        timeout: TimeInterval = PathProbe.seconds,
        check: @escaping @Sendable (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) async -> Presence {
        guard Flights.begin(path) else { return .silent }
        return await withCheckedContinuation { (continuation: CheckedContinuation<Presence, Never>) in
            let once = OneAnswer(continuation)
            DispatchQueue.global(qos: .userInitiated).async {
                let found = check(path)
                Flights.end(path)
                once.resume(with: found ? .there : .gone)
            }
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + max(0, timeout)) {
                once.resume(with: .silent)
            }
        }
    }

    /// Every path asked at once, so the wait is the slowest answer (at most `timeout`), not their sum.
    public static func presence(
        of paths: [String],
        timeout: TimeInterval = PathProbe.seconds,
        check: @escaping @Sendable (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) async -> [String: Presence] {
        await withTaskGroup(of: (String, Presence).self) { group in
            for path in Set(paths) {
                group.addTask { (path, await presence(of: path, timeout: timeout, check: check)) }
            }
            var answers: [String: Presence] = [:]
            for await (path, answer) in group {
                answers[path] = answer
            }
            return answers
        }
    }
}

/// The paths with a check still waiting on their drive.
private enum Flights {
    private static let lock = NSLock()
    private static var asking: Set<String> = []

    static func begin(_ path: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return asking.insert(path).inserted
    }

    static func end(_ path: String) {
        lock.lock()
        asking.remove(path)
        lock.unlock()
    }
}

/// Resumes a continuation once, whichever of the check and the deadline gets there first.
private final class OneAnswer: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Presence, Never>?

    init(_ continuation: CheckedContinuation<Presence, Never>) {
        self.continuation = continuation
    }

    func resume(with value: Presence) {
        lock.lock()
        let taken = continuation
        continuation = nil
        lock.unlock()
        taken?.resume(returning: value)
    }
}

import Foundation

/// What a running `roughcut` process produces, in order.
public enum RunOutput: Equatable, Sendable {
    case event(ProgressEvent)
    case stdout(String)
    case stderr(String)
    case failedToLaunch(String)
    /// Always the last element unless launch failed.
    case exited(status: Int32, signaled: Bool)
}

/// Runs the CLI and streams its output line by line.
///
/// The stream finishes only after both pipes reach end-of-file, so the final
/// `done` event is never lost to a race with process termination.
public final class CLIProcess: @unchecked Sendable {
    public let executable: URL
    public let arguments: [String]
    public let environment: [String: String]
    private let process = Process()

    public init(executable: URL, arguments: [String], environment: [String: String]) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
    }

    public func run() -> AsyncStream<RunOutput> {
        let process = self.process
        let executable = self.executable
        let arguments = self.arguments
        let environment = self.environment
        return AsyncStream { continuation in
            let out = Pipe()
            let err = Pipe()
            process.executableURL = executable
            process.arguments = arguments
            process.environment = environment
            process.standardOutput = out
            process.standardError = err
            process.standardInput = FileHandle.nullDevice

            let pipesClosed = DispatchGroup()
            let outLines = LineBuffer(separators: [0x0A])
            // tqdm progress bars (model downloads, Whisper) redraw with \r.
            let errLines = LineBuffer(separators: [0x0A, 0x0D])

            pipesClosed.enter()
            out.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty {
                    handle.readabilityHandler = nil
                    if let rest = outLines.flush() {
                        continuation.yield(CLIProcess.classify(rest))
                    }
                    pipesClosed.leave()
                    return
                }
                for line in outLines.append(data) {
                    continuation.yield(CLIProcess.classify(line))
                }
            }

            pipesClosed.enter()
            err.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty {
                    handle.readabilityHandler = nil
                    if let rest = errLines.flush() {
                        continuation.yield(.stderr(rest))
                    }
                    pipesClosed.leave()
                    return
                }
                for line in errLines.append(data) {
                    continuation.yield(.stderr(line))
                }
            }

            continuation.onTermination = { _ in
                if process.isRunning {
                    process.terminate()
                }
            }

            do {
                try process.run()
            } catch {
                out.fileHandleForReading.readabilityHandler = nil
                err.fileHandleForReading.readabilityHandler = nil
                continuation.yield(.failedToLaunch(error.localizedDescription))
                continuation.finish()
                return
            }

            pipesClosed.notify(queue: .global()) {
                process.waitUntilExit()
                continuation.yield(.exited(status: process.terminationStatus, signaled: process.terminationReason == .uncaughtSignal))
                continuation.finish()
            }
        }
    }

    /// Stops the run (SIGTERM). The stream still ends with `.exited`.
    public func cancel() {
        if process.isRunning {
            process.terminate()
        }
    }

    static func classify(_ line: String) -> RunOutput {
        if let event = JSONLines.event(from: line) {
            return .event(event)
        }
        return .stdout(line)
    }
}

/// Splits a byte stream into lines. Each instance is fed from one pipe
/// handler at a time, so it needs no locking.
public final class LineBuffer: @unchecked Sendable {
    private var buffer: [UInt8] = []
    private let separators: Set<UInt8>

    public init(separators: Set<UInt8>) {
        self.separators = separators
    }

    public func append(_ data: Data) -> [String] {
        var lines: [String] = []
        for byte in data {
            if separators.contains(byte) {
                if !buffer.isEmpty {
                    lines.append(String(decoding: buffer, as: UTF8.self))
                    buffer.removeAll(keepingCapacity: true)
                }
            } else {
                buffer.append(byte)
            }
        }
        return lines
    }

    public func flush() -> String? {
        guard !buffer.isEmpty else { return nil }
        defer { buffer.removeAll() }
        return String(decoding: buffer, as: UTF8.self)
    }
}

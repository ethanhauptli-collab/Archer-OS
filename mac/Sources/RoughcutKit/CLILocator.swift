import Foundation

/// Finds the `roughcut` command and builds the environment to run it in.
///
/// Apps launched from Finder don't get your shell's PATH or exported
/// variables, so Homebrew (ffmpeg) and the venv are added explicitly and API
/// keys are passed in from the Keychain.
public enum CLILocator {
    /// UserDefaults key for a path chosen in Settings.
    public static let defaultsKey = "roughcutCLIPath"
    /// Info.plist key written by scripts/build-app.sh.
    public static let infoPlistKey = "RoughcutCLIPath"

    /// The repo this app was built from: mac/Sources/RoughcutKit/CLILocator.swift -> repo root.
    static var repoCheckoutCLI: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // RoughcutKit
            .deletingLastPathComponent()  // Sources
            .deletingLastPathComponent()  // mac
            .deletingLastPathComponent()  // repo root
            .appendingPathComponent(".venv/bin/roughcut")
    }

    /// Places to look, best first.
    public static func candidates(
        defaults: UserDefaults = .standard,
        bundle: Bundle = .main,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> [URL] {
        var list: [URL] = []
        if let chosen = defaults.string(forKey: defaultsKey), !chosen.isEmpty {
            list.append(URL(fileURLWithPath: chosen))
        }
        if let built = bundle.object(forInfoDictionaryKey: infoPlistKey) as? String, !built.isEmpty {
            list.append(URL(fileURLWithPath: built))
        }
        list.append(repoCheckoutCLI)
        for folder in ["Archer-OS", "Developer/Archer-OS", "Projects/Archer-OS", "Code/Archer-OS", "GitHub/Archer-OS", "Documents/GitHub/Archer-OS"] {
            list.append(home.appendingPathComponent(folder).appendingPathComponent(".venv/bin/roughcut"))
        }
        list.append(URL(fileURLWithPath: "/opt/homebrew/bin/roughcut"))
        list.append(URL(fileURLWithPath: "/usr/local/bin/roughcut"))
        list.append(home.appendingPathComponent(".local/bin/roughcut"))
        return list
    }

    public static func locate(defaults: UserDefaults = .standard, bundle: Bundle = .main) -> URL? {
        candidates(defaults: defaults, bundle: bundle).first { FileManager.default.isExecutableFile(atPath: $0.path) }
    }

    /// The inherited environment plus Homebrew/venv on PATH and any non-empty extras (API keys).
    public static func environment(
        for cli: URL,
        extra: [String: String] = [:],
        base: [String: String] = ProcessInfo.processInfo.environment
    ) -> [String: String] {
        var env = base
        let preferred = [cli.deletingLastPathComponent().path, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let existing = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        var seen = Set<String>()
        env["PATH"] = (preferred + existing).filter { !$0.isEmpty && seen.insert($0).inserted }.joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for (key, value) in extra where !value.isEmpty {
            env[key] = value
        }
        return env
    }
}

/// Reads a variable exported in your shell profile (e.g. ANTHROPIC_API_KEY in ~/.zshrc).
public enum ShellEnvironment {
    /// Blocking; call off the main thread. Nil if unset, unreadable, or the shell took too long.
    public static func value(
        of name: String,
        shell: String = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh",
        timeout: TimeInterval = 8
    ) -> String? {
        guard !name.isEmpty, name.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" }) else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        // -i loads ~/.zshrc, -l loads ~/.zprofile; markers survive noisy rc files.
        process.arguments = ["-ilc", "printf '__RC_BEGIN__%s__RC_END__' \"$\(name)\""]
        let out = Pipe()
        process.standardOutput = out
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning {
            process.terminate()
            return nil
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let text = String(decoding: data, as: UTF8.self)
        guard let begin = text.range(of: "__RC_BEGIN__"),
              let end = text.range(of: "__RC_END__", range: begin.upperBound..<text.endIndex) else { return nil }
        let value = String(text[begin.upperBound..<end.lowerBound])
        return value.isEmpty ? nil : value
    }
}

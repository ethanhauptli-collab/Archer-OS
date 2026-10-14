import Foundation

/// The commit the running app was built from, as `mac/scripts/build-app.sh` writes it into Info.plist.
public struct AppStamp: Equatable, Sendable {
    public static let commitKey = "RoughcutAppCommit"
    public static let sourceCommitKey = "RoughcutAppSourceCommit"

    /// The checkout's short commit, `-dirty` after it when `mac/` had uncommitted changes. Nil: not built by build-app.sh
    /// (`swift run`, Xcode) or not from a git checkout.
    public var commit: String?
    /// The last commit that touched `mac/` in that checkout.
    public var sourceCommit: String?

    public init(commit: String? = nil, sourceCommit: String? = nil) {
        self.commit = commit
        self.sourceCommit = sourceCommit
    }

    /// From `Bundle.main.infoDictionary`; "unknown" (build-app.sh outside a git checkout) and empty text are nil.
    public init(infoDictionary: [String: Any]?) {
        func text(_ key: String) -> String? {
            guard let value = (infoDictionary?[key] as? String)?.trimmed, !value.isEmpty, value != "unknown" else { return nil }
            return value
        }
        self.init(commit: text(Self.commitKey), sourceCommit: text(Self.sourceCommitKey))
    }

    /// Built with uncommitted changes in `mac/`: the commit doesn't say what code this app has.
    public var isDirty: Bool { commit?.hasSuffix(VersionStamp.dirtySuffix) ?? false }
}

/// What Settings says about the app's and the engine's versions: "App 3f9c2ab · Engine 3f9c2ab", and a warning when the
/// app may be behind the engine (after `roughcut update`).
public struct VersionStamp: Equatable, Sendable {
    static let dirtySuffix = "-dirty"
    public static let rebuildCommand = "mac/scripts/build-app.sh --install"

    public var line: String
    /// Nil when there is nothing to act on.
    public var warning: String?

    /// `engine` is nil until the setup check has answered.
    public init(app: AppStamp, engine: DoctorReport?) {
        let appText = app.commit ?? "unknown"
        let engineText = engine.map { $0.commit ?? "unknown" } ?? "not checked yet"
        line = "App \(appText) · Engine \(engineText)"
        warning = Self.warningText(app: app, engine: engine)
    }

    /// The commit without `-dirty`.
    static func base(_ commit: String?) -> String? {
        guard var text = commit?.trimmed, !text.isEmpty else { return nil }
        if text.hasSuffix(dirtySuffix) {
            text.removeLast(dirtySuffix.count)
        }
        return text.isEmpty ? nil : text
    }

    /// Whether two abbreviated commits name the same one (git lengthens an abbreviation as a repository grows); nil if either is unknown.
    static func same(_ a: String?, _ b: String?) -> Bool? {
        guard let a = base(a), let b = base(b) else { return nil }
        return a.hasPrefix(b) || b.hasPrefix(a)
    }

    private static func warningText(app: AppStamp, engine: DoctorReport?) -> String? {
        guard app.commit != nil, let engine else { return nil }
        // The code of the app itself is what matters: an engine-only update leaves the app as current as it was. An engine that
        // doesn't say (before this key) leaves the whole commit to compare.
        var compared: (built: String?, now: String?, agree: Bool)?
        if let agree = same(app.sourceCommit, engine.appSourceCommit) {
            compared = (base(app.sourceCommit), base(engine.appSourceCommit), agree)
        } else if let agree = same(app.commit, engine.commit) {
            compared = (base(app.commit), base(engine.commit), agree)
        }
        if let compared, !compared.agree {
            return "This app may be out of date: it was built from \(compared.built ?? "?"), and the roughcut it runs has \(compared.now ?? "?"). Rebuild it with \(rebuildCommand)."
        }
        if app.isDirty {
            return "This app was built with uncommitted changes in mac/, so its commit doesn't say exactly what code it has."
        }
        return nil
    }
}

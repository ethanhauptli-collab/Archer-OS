import Foundation

/// The API keys the app keeps for the CLI. An account is named after the environment variable roughcut reads, and the
/// pair (`service`, account) is the Keychain item `roughcut/keys.py` looks up with `security find-generic-password`:
/// change either and a key saved in the app stops working in Terminal (tests/test_keys.py checks the two agree).
public enum KeyAccount: String, CaseIterable, Sendable {
    case anthropic = "ANTHROPIC_API_KEY"
    case openai = "OPENAI_API_KEY"

    /// The Keychain item's service name.
    public static let service = "com.roughcut.app"

    public var title: String {
        switch self {
        case .anthropic: return "Claude (Anthropic)"
        case .openai: return "OpenAI or compatible"
        }
    }
}

/// Why a key store could not be read or written, in words for the Settings window.
public struct KeyStoreFailure: Error, Equatable, Sendable {
    public var message: String

    public init(_ message: String) {
        self.message = message
    }
}

/// Where keys are kept: the login Keychain in the app (`Keychain.swift`), a dictionary in tests.
public protocol KeyStore {
    /// The stored key; nil when there is none. Throws when the store couldn't be asked (locked, access denied).
    func read(_ account: KeyAccount) throws -> String?
    /// Puts `value` in the account's item: updated where it is when there is one, added when there isn't. The key already
    /// stored is never removed first, so a write that fails leaves it as it was.
    func write(_ value: String, for account: KeyAccount) throws
    /// Removes the account's item; there being none is not a failure.
    func remove(_ account: KeyAccount) throws
}

/// What saving one account did.
public enum KeyOutcome: Equatable, Sendable {
    case saved
    case removed
    /// The box holds what is stored already (or is empty with nothing stored): the store wasn't touched.
    case unchanged
    case failedToSave(String)
    case failedToRemove(String)

    public var failed: Bool {
        switch self {
        case .failedToSave, .failedToRemove: return true
        case .saved, .removed, .unchanged: return false
        }
    }

    /// The line shown under the account's box.
    public var message: String {
        switch self {
        case .saved: return "Saved"
        case .removed: return "Removed"
        case .unchanged: return "No change"
        case .failedToSave(let why): return "Not saved: \(why). A key already stored is unchanged."
        case .failedToRemove(let why): return "Not removed: \(why)."
        }
    }
}

public struct KeySaveResult: Equatable, Sendable {
    public var account: KeyAccount
    public var outcome: KeyOutcome

    public init(account: KeyAccount, outcome: KeyOutcome) {
        self.account = account
        self.outcome = outcome
    }
}

/// The API Keys tab's state: a box per account, and what each box last held in the store, so saving changes only the
/// accounts that were edited and one account's failure never touches another's.
public struct KeyForm: Sendable {
    /// What the store held for an account when the box was last loaded or saved.
    public enum Stored: Equatable, Sendable {
        case value(String)
        case absent
        /// The store couldn't be read: an empty box must not be taken for "remove it".
        case unreadable(String)
    }

    struct Field: Sendable {
        var text = ""
        var stored = Stored.absent
        var result: KeySaveResult?
        var readProblem: String?
    }

    private var fields: [KeyAccount: Field] = [:]

    public init() {}

    /// The box's text. Typing in a box clears the line a save left under it.
    public subscript(account: KeyAccount) -> String {
        get { fields[account]?.text ?? "" }
        set {
            var field = fields[account] ?? Field()
            guard field.text != newValue else { return }
            field.text = newValue
            field.result = nil
            fields[account] = field
        }
    }

    /// Reads every account from the store into its box. An account that can't be read keeps an empty box and says why.
    public mutating func load(from store: KeyStore) {
        for account in KeyAccount.allCases {
            var field = Field()
            do {
                if let value = try store.read(account) {
                    field.text = value
                    field.stored = .value(value.trimmed)
                }
            } catch let failure as KeyStoreFailure {
                field.stored = .unreadable(failure.message)
                field.readProblem = "Couldn't read the key saved in the Keychain: \(failure.message). Leaving this box empty won't remove it."
            } catch {
                field.stored = .unreadable(error.localizedDescription)
                field.readProblem = "Couldn't read the key saved in the Keychain: \(error.localizedDescription). Leaving this box empty won't remove it."
            }
            fields[account] = field
        }
    }

    /// The line under the account's box: what the last save did there, else why it couldn't be read. A save that left an unreadable
    /// account alone ("No change") doesn't hide why.
    public func note(for account: KeyAccount) -> String? {
        guard let field = fields[account] else { return nil }
        if let result = field.result {
            if result.outcome == .unchanged, let problem = field.readProblem {
                return problem
            }
            return result.outcome.message
        }
        return field.readProblem
    }

    public func result(for account: KeyAccount) -> KeySaveResult? {
        fields[account]?.result
    }

    /// The account's line is something to act on (a failed save or removal, an unreadable Keychain), not just a confirmation.
    public func noteIsProblem(for account: KeyAccount) -> Bool {
        guard let field = fields[account] else { return false }
        if let result = field.result {
            return result.outcome.failed || (result.outcome == .unchanged && field.readProblem != nil)
        }
        return field.readProblem != nil
    }

    /// Saves each account on its own: an edited box is written in place, an emptied one removes its item, one that is
    /// as stored is left alone. A failure is that account's result; the others are still saved.
    @discardableResult
    public mutating func save(to store: KeyStore) -> [KeySaveResult] {
        var results: [KeySaveResult] = []
        for account in KeyAccount.allCases {
            var field = fields[account] ?? Field()
            let outcome = Self.saveOne(&field, to: store, account: account)
            let result = KeySaveResult(account: account, outcome: outcome)
            field.result = result
            if outcome == .saved || outcome == .removed {
                field.readProblem = nil
            }
            fields[account] = field
            results.append(result)
        }
        return results
    }

    private static func saveOne(_ field: inout Field, to store: KeyStore, account: KeyAccount) -> KeyOutcome {
        let text = field.text.trimmed
        if text.isEmpty {
            switch field.stored {
            case .absent, .unreadable:
                return .unchanged  // nothing stored, or unknown: never remove what hasn't been seen
            case .value:
                do {
                    try store.remove(account)
                } catch {
                    return .failedToRemove(describe(error))
                }
                field.text = ""
                field.stored = .absent
                return .removed
            }
        }
        if case .value(let current) = field.stored, current == text {
            return .unchanged
        }
        do {
            try store.write(text, for: account)
        } catch {
            return .failedToSave(describe(error))
        }
        field.text = text
        field.stored = .value(text)
        return .saved
    }

    private static func describe(_ error: Error) -> String {
        (error as? KeyStoreFailure)?.message ?? error.localizedDescription
    }

    /// One line for the whole save, beside the button.
    public static func summary(_ results: [KeySaveResult]) -> String {
        let failed = results.filter { $0.outcome.failed }
        if !failed.isEmpty {
            let names = failed.map { $0.account.title }.joined(separator: " and ")
            return "\(names) not saved. See below."
        }
        return results.contains { $0.outcome != .unchanged } ? "Saved" : "No changes to save"
    }
}

import Foundation
import Security

/// API keys live in the login Keychain, never in UserDefaults or on disk.
enum Keychain {
    /// Account names double as the environment variables the CLI reads.
    enum Account: String, CaseIterable {
        case anthropic = "ANTHROPIC_API_KEY"
        case openai = "OPENAI_API_KEY"
    }

    private static let service = "com.roughcut.app"

    private static func baseQuery(_ account: Account) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account.rawValue,
        ]
    }

    static func read(_ account: Account) -> String? {
        var query = baseQuery(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// Saves (or, for an empty value, deletes) the key. Returns false on a Keychain error.
    @discardableResult
    static func save(_ value: String, for account: Account) -> Bool {
        let query = baseQuery(account)
        SecItemDelete(query as CFDictionary)
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return true }
        var add = query
        add[kSecValueData as String] = Data(trimmed.utf8)
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }
}

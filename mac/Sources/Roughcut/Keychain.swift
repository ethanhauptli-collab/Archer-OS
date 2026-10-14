import Foundation
import RoughcutKit
import Security

/// API keys live in the login Keychain, never in UserDefaults or on disk. The items are generic passwords with the service
/// `KeyAccount.service` and the variable's name as the account, which is what `roughcut/keys.py` reads in Terminal.
///
/// What this file does can only be checked on a Mac (RoughcutKitTests cover the logic around it with a fake store):
/// that `SecItemUpdate` keeps the item (and the access the user already granted) and that a first save adds one.
struct SecurityKeyStore: KeyStore {
    private func baseQuery(_ account: KeyAccount) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: KeyAccount.service,
            kSecAttrAccount as String: account.rawValue,
        ]
    }

    private func failure(_ status: OSStatus) -> KeyStoreFailure {
        let text = SecCopyErrorMessageString(status, nil) as String?
        return KeyStoreFailure(text.map { "\($0) (\(status))" } ?? "Keychain error \(status)")
    }

    func read(_ account: KeyAccount) throws -> String? {
        var query = baseQuery(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            guard let data = item as? Data, let text = String(data: data, encoding: .utf8) else {
                throw KeyStoreFailure("The saved item isn't text")
            }
            return text
        case errSecItemNotFound:
            return nil
        default:
            throw failure(status)
        }
    }

    /// Updates the item where it is; adds it only when there is none. The old value is never deleted first, so a refused
    /// write leaves the working key in place.
    func write(_ value: String, for account: KeyAccount) throws {
        let query = baseQuery(account)
        let data = Data(value.utf8)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            status = SecItemAdd(add as CFDictionary, nil)
            if status == errSecDuplicateItem {
                // Added by someone else between the two calls (Terminal's `roughcut key`): update that one.
                status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
            }
        }
        guard status == errSecSuccess else { throw failure(status) }
    }

    func remove(_ account: KeyAccount) throws {
        let status = SecItemDelete(baseQuery(account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw failure(status) }
    }
}

enum Keychain {
    typealias Account = KeyAccount

    static let store = SecurityKeyStore()

    /// For a run's environment: the stored key, or nil when there is none or the Keychain can't be asked.
    static func read(_ account: Account) -> String? {
        try? store.read(account)
    }
}

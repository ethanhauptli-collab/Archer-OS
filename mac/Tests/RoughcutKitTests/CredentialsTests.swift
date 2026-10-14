import XCTest
@testable import RoughcutKit

/// The API Keys tab's logic with a store in memory. Nothing here touches the real Keychain: that the Security calls
/// (`SecItemUpdate` on an existing item, `SecItemAdd` for a first one) do what `FakeKeyStore` stands in for is something only a Mac can show.
final class CredentialsTests: XCTestCase {
    /// A key store that remembers the calls made to it, and can refuse some of them. A refused write leaves the stored value as it was.
    final class FakeKeyStore: KeyStore {
        var values: [KeyAccount: String] = [:]
        var calls: [String] = []
        var failWrites: Set<KeyAccount> = []
        var failRemoves: Set<KeyAccount> = []
        var failReads: Set<KeyAccount> = []

        func read(_ account: KeyAccount) throws -> String? {
            calls.append("read \(account.rawValue)")
            if failReads.contains(account) { throw KeyStoreFailure("The Keychain is locked (-25308)") }
            return values[account]
        }

        func write(_ value: String, for account: KeyAccount) throws {
            calls.append("write \(account.rawValue)")
            if failWrites.contains(account) { throw KeyStoreFailure("User interaction is not allowed (-25308)") }
            values[account] = value
        }

        func remove(_ account: KeyAccount) throws {
            calls.append("remove \(account.rawValue)")
            if failRemoves.contains(account) { throw KeyStoreFailure("Item can't be removed") }
            values[account] = nil
        }
    }

    func testTheItemsAreTheOnesTheCLIReads() {
        // roughcut/keys.py: service com.roughcut.app, account = the variable. tests/test_keys.py reads this file to check the two agree.
        XCTAssertEqual(KeyAccount.service, "com.roughcut.app")
        XCTAssertEqual(KeyAccount.allCases.map(\.rawValue), ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
    }

    func testAFailedWriteNeverRemovesTheKeyThatWorks() {
        let store = FakeKeyStore()
        store.values[.anthropic] = "sk-ant-old"
        store.failWrites = [.anthropic]
        var form = KeyForm()
        form.load(from: store)
        form[.anthropic] = "sk-ant-new"
        let results = form.save(to: store)
        XCTAssertEqual(results.first { $0.account == .anthropic }?.outcome, .failedToSave("User interaction is not allowed (-25308)"))
        XCTAssertEqual(store.values[.anthropic], "sk-ant-old", "the working key is still there")
        XCTAssertFalse(store.calls.contains { $0.hasPrefix("remove") }, "the old value is never deleted to make room: \(store.calls)")
        XCTAssertTrue(form.noteIsProblem(for: .anthropic))
        XCTAssertEqual(form.note(for: .anthropic), "Not saved: User interaction is not allowed (-25308). A key already stored is unchanged.")
        // Try again once the Keychain answers: the box still holds the new key, so it is saved now.
        store.failWrites = []
        XCTAssertEqual(form.save(to: store).first { $0.account == .anthropic }?.outcome, .saved)
        XCTAssertEqual(store.values[.anthropic], "sk-ant-new")
    }

    func testEachAccountSavesOnItsOwn() {
        let store = FakeKeyStore()
        store.failWrites = [.anthropic]
        var form = KeyForm()
        form.load(from: store)
        form[.anthropic] = "sk-ant-key"
        form[.openai] = "  sk-openai-key \n"  // a pasted key brings its whitespace
        let results = form.save(to: store)
        XCTAssertEqual(results.map(\.account), [.anthropic, .openai])
        XCTAssertEqual(results[0].outcome, .failedToSave("User interaction is not allowed (-25308)"))
        XCTAssertEqual(results[1].outcome, .saved, "one account failing doesn't stop the other")
        XCTAssertEqual(store.values[.openai], "sk-openai-key")
        XCTAssertNil(store.values[.anthropic])
        XCTAssertEqual(KeyForm.summary(results), "Claude (Anthropic) not saved. See below.")
        XCTAssertEqual(form.note(for: .openai), "Saved")
        XCTAssertFalse(form.noteIsProblem(for: .openai))
        XCTAssertEqual(form[.openai], "sk-openai-key", "the box shows what was stored")
    }

    func testOnlyEditedBoxesTouchTheStore() {
        let store = FakeKeyStore()
        store.values = [.anthropic: "sk-ant-key", .openai: "sk-openai-key"]
        var form = KeyForm()
        form.load(from: store)
        XCTAssertEqual(form[.anthropic], "sk-ant-key")
        store.calls = []
        let results = form.save(to: store)
        XCTAssertEqual(results.map(\.outcome), [.unchanged, .unchanged])
        XCTAssertEqual(store.calls, [], "nothing to save, nothing written or removed")
        XCTAssertEqual(KeyForm.summary(results), "No changes to save")
        form[.openai] = "sk-openai-key2"
        store.calls = []
        XCTAssertEqual(form.save(to: store).map(\.outcome), [.unchanged, .saved])
        XCTAssertEqual(store.calls, ["write OPENAI_API_KEY"])
        // Saving again changes nothing.
        store.calls = []
        XCTAssertEqual(form.save(to: store).map(\.outcome), [.unchanged, .unchanged])
        XCTAssertEqual(store.calls, [])
    }

    func testEmptyingABoxRemovesOnlyThatKey() {
        let store = FakeKeyStore()
        store.values = [.anthropic: "sk-ant-key", .openai: "sk-openai-key"]
        var form = KeyForm()
        form.load(from: store)
        form[.openai] = "   "
        store.calls = []
        let results = form.save(to: store)
        XCTAssertEqual(results.map(\.outcome), [.unchanged, .removed])
        XCTAssertEqual(store.calls, ["remove OPENAI_API_KEY"])
        XCTAssertEqual(store.values, [.anthropic: "sk-ant-key"])
        XCTAssertEqual(KeyForm.summary(results), "Saved")
        // A removal that fails says so for that account, and the key is still stored.
        store.failRemoves = [.anthropic]
        form[.anthropic] = ""
        let again = form.save(to: store)
        XCTAssertEqual(again.first?.outcome, .failedToRemove("Item can't be removed"))
        XCTAssertEqual(store.values[.anthropic], "sk-ant-key")
        XCTAssertEqual(KeyForm.summary(again), "Claude (Anthropic) not removed. See below.", "a removal is not called a save")
        XCTAssertEqual(form.note(for: .anthropic), "Not removed: Item can't be removed.")
    }

    func testTheSummaryNamesEachFailureInItsOwnWords() {
        let saveFailed = KeySaveResult(account: .anthropic, outcome: .failedToSave("no"))
        let removeFailed = KeySaveResult(account: .openai, outcome: .failedToRemove("no"))
        let fine = KeySaveResult(account: .openai, outcome: .saved)
        XCTAssertEqual(KeyForm.summary([saveFailed, fine]), "Claude (Anthropic) not saved. See below.")
        XCTAssertEqual(KeyForm.summary([saveFailed, removeFailed]), "Claude (Anthropic) not saved; OpenAI or compatible not removed. See below.")
        XCTAssertEqual(KeyForm.summary([KeySaveResult(account: .anthropic, outcome: .removed), fine]), "Saved")
        XCTAssertEqual(KeyForm.summary([KeySaveResult(account: .anthropic, outcome: .unchanged), KeySaveResult(account: .openai, outcome: .unchanged)]), "No changes to save")
    }

    func testAKeychainThatCouldNotBeReadIsNeverTakenForEmpty() {
        let store = FakeKeyStore()
        store.values = [.anthropic: "sk-ant-key"]
        store.failReads = [.anthropic]
        var form = KeyForm()
        form.load(from: store)
        XCTAssertEqual(form[.anthropic], "", "nothing to show")
        XCTAssertTrue(form.noteIsProblem(for: .anthropic))
        XCTAssertEqual(form.note(for: .anthropic), "Couldn't read the key saved in the Keychain: The Keychain is locked (-25308). Leaving this box empty won't remove it.")
        store.calls = []
        XCTAssertEqual(form.save(to: store).first?.outcome, .unchanged)
        XCTAssertEqual(store.calls, [], "an empty box over an unreadable item isn't a request to remove it")
        XCTAssertEqual(store.values[.anthropic], "sk-ant-key")
        // Typing a key still saves it (over whatever is there).
        form[.anthropic] = "sk-ant-new"
        XCTAssertEqual(form.save(to: store).first?.outcome, .saved)
        XCTAssertEqual(store.values[.anthropic], "sk-ant-new")
    }

    func testTypingClearsTheLineASaveLeft() {
        let store = FakeKeyStore()
        var form = KeyForm()
        form.load(from: store)
        form[.openai] = "sk-openai-key"
        form.save(to: store)
        XCTAssertEqual(form.note(for: .openai), "Saved")
        form[.openai] = "sk-openai-key-2"
        XCTAssertNil(form.note(for: .openai))
        XCTAssertNil(form.result(for: .openai))
    }

    func testFirstSaveAddsAndLaterSavesWriteInPlace() {
        // The store decides between adding and updating (SecurityKeyStore: update first, add when there is no item); the form just writes.
        let store = FakeKeyStore()
        var form = KeyForm()
        form.load(from: store)
        form[.anthropic] = "sk-ant-1"
        form.save(to: store)
        form[.anthropic] = "sk-ant-2"
        form.save(to: store)
        XCTAssertEqual(store.calls.filter { $0.hasPrefix("write") }, ["write ANTHROPIC_API_KEY", "write ANTHROPIC_API_KEY"])
        XCTAssertFalse(store.calls.contains { $0.hasPrefix("remove") })
        XCTAssertEqual(store.values[.anthropic], "sk-ant-2")
    }
}

import Foundation
import XCTest

/// Real CLI output kept in `Fixtures/` (tests/test_progress.py checks each still matches the CLI).
enum FixtureFiles {
    static func url(_ name: String, _ ext: String) throws -> URL {
        try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: ext, subdirectory: "Fixtures"))
    }

    static func data(_ name: String, _ ext: String) throws -> Data {
        try Data(contentsOf: url(name, ext))
    }

    /// The non-empty lines of a `.jsonl` stream.
    static func lines(_ name: String) throws -> [String] {
        try String(contentsOf: url(name, "jsonl"), encoding: .utf8).split(separator: "\n").map(String.init)
    }
}

// swift-tools-version:5.9
import PackageDescription

// RoughcutKit is Foundation-only (options -> CLI arguments, progress decoding,
// process runner) so it stays testable anywhere. The SwiftUI app target is
// macOS-only.
var products: [Product] = [
    .library(name: "RoughcutKit", targets: ["RoughcutKit"]),
]
var targets: [Target] = [
    .target(name: "RoughcutKit"),
    .testTarget(
        name: "RoughcutKitTests",
        dependencies: ["RoughcutKit"],
        resources: [.copy("Fixtures")]
    ),
]

#if os(macOS)
products.append(.executable(name: "Roughcut", targets: ["Roughcut"]))
targets.append(.executableTarget(name: "Roughcut", dependencies: ["RoughcutKit"]))
#endif

let package = Package(
    name: "Roughcut",
    platforms: [.macOS(.v14)],
    products: products,
    targets: targets
)

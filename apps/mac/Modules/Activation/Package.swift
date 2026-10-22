// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JarvisActivation",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "JarvisActivation", targets: ["JarvisActivation"]),
        .executable(name: "activation-regression", targets: ["ActivationRegression"]),
        .executable(name: "activation-replay", targets: ["ActivationReplay"])
    ],
    targets: [
        .target(name: "JarvisActivation"),
        .executableTarget(name: "ActivationRegression", dependencies: ["JarvisActivation"], path: "Tests/ActivationRegression"),
        .executableTarget(name: "ActivationReplay", dependencies: ["JarvisActivation"], path: "Tests/ActivationReplay")
    ],
    swiftLanguageVersions: [.v5]
)

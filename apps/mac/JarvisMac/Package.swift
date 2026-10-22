// swift-tools-version: 5.9
import PackageDescription

// Five independent dynamic modules, plus a thin composition-root executable.
let package = Package(
    name: "JarvisMacApp",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "Jarvis", targets: ["JarvisApp"]),
        .library(name: "JarvisCore", type: .dynamic, targets: ["JarvisCore"]),
        .library(name: "JarvisAudio", type: .dynamic, targets: ["JarvisAudio"]),
        .library(name: "JarvisSpeech", type: .dynamic, targets: ["JarvisSpeech"]),
        .library(name: "JarvisTransport", type: .dynamic, targets: ["JarvisTransport"]),
        .library(name: "JarvisUI", type: .dynamic, targets: ["JarvisUI"])
    ],
    dependencies: [
        .package(url: "https://github.com/daltoniam/Starscream.git", exact: "4.0.8")
    ],
    targets: [
        .target(name: "JarvisCore"),
        .target(name: "JarvisAudio", dependencies: ["JarvisCore"],
                linkerSettings: [.linkedFramework("AVFoundation"), .linkedFramework("Accelerate"), .linkedFramework("AudioToolbox")]),
        .target(name: "JarvisSpeech", dependencies: ["JarvisCore"]),
        .target(name: "JarvisTransport", dependencies: ["JarvisCore", .product(name: "Starscream", package: "Starscream")],
                linkerSettings: [.linkedFramework("Security"), .linkedLibrary("z")]),
        .target(name: "JarvisUI", dependencies: ["JarvisCore"],
                linkerSettings: [.linkedFramework("AppKit"), .linkedFramework("AVFoundation"), .linkedFramework("ApplicationServices")]),
        .executableTarget(name: "JarvisApp", dependencies: ["JarvisCore", "JarvisAudio", "JarvisSpeech", "JarvisTransport", "JarvisUI"]),
        .testTarget(name: "JarvisCoreTests", dependencies: ["JarvisCore"])
    ],
    swiftLanguageVersions: [.v5]
)

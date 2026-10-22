// swift-tools-version: 5.9
import PackageDescription
let package = Package(
    name: "JarvisWakeWord",
    platforms: [.macOS(.v13), .iOS(.v16), .watchOS(.v9)],
    products: [.library(name: "JarvisWakeWord", targets: ["JarvisWakeWord"]), .executable(name: "wakeword-replay", targets: ["WakeWordReplay"])],
    dependencies: [.package(path: "../mac/Modules/Activation")],
    targets: [
        .target(name: "JarvisWakeWord", dependencies: [.product(name: "JarvisActivation", package: "Activation", condition: .when(platforms: [.macOS, .iOS]))]),
        .executableTarget(name: "WakeWordReplay", dependencies: ["JarvisWakeWord", .product(name: "JarvisActivation", package: "Activation")])
    ]
)

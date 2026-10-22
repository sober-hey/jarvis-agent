// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "JarvisWebSocket",
    platforms: [.macOS(.v12)],
    products: [.library(name: "JarvisWebSocket", targets: ["JarvisWebSocket"])],
    dependencies: [.package(url: "https://github.com/daltoniam/Starscream.git", exact: "4.0.8")],
    targets: [
        .target(name: "JarvisWebSocket", dependencies: ["Starscream"]),
        .testTarget(name: "JarvisWebSocketTests", dependencies: ["JarvisWebSocket"])
    ]
)

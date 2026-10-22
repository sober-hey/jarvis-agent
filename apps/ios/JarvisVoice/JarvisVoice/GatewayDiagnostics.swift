import Foundation

/// Whitelisted events only: never accept message bodies, URLs with secrets,
/// tokens, or localized network errors. Safe to pull via devicectl.
@MainActor
enum GatewayDiagnostics {
    static let launchID = UUID().uuidString
    static var file: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("gateway-diagnostics.jsonl")
    }
    enum Event: String {
        case launch, tokenImported, tokenAvailable, tokenMissing, tokenImportFailed
        case connecting, upgraded, helloSent, selected, suppressed, disconnected
        case microphoneDenied, backgroundPaused
    }
    static func record(_ event: Event, endpoint: String? = nil, code: Int? = nil) {
        var value: [String: Any] = ["time": ISO8601DateFormatter().string(from: Date()),
                                   "launch_id": launchID, "event": event.rawValue]
        if let endpoint, GatewayEndpoints.addresses.contains(endpoint) { value["endpoint"] = endpoint }
        if let code { value["code"] = code }
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        let url = file
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil,
                    attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
            }
            // Bound diagnostics; contains only operational events.
            if let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
               let size = attrs[.size] as? NSNumber, size.intValue > 512 * 1024 {
                try Data().write(to: url)
            }
            let handle = try FileHandle(forWritingTo: url)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data + Data([10]))
        } catch { /* Diagnostics must not prevent a connection. */ }
    }
}

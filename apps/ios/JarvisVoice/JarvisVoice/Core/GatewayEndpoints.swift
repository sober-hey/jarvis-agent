import Foundation

/// Single source of truth: never stick to a failed address or reorder a cycle.
struct GatewayEndpoints {
    /// Numeric addresses first: the .local name resolves to the Mac's IPv6
    /// address, which the gateway does not listen on.
    static let defaults = [
        "ws://192.168.1.5:8765",
        "ws://100.67.229.1:8765"
    ]
    private static let customKey = "gatewayCustomEndpoint"
    /// User-entered address from Settings; tried first, built-ins remain fallback.
    static var custom: String? {
        get { UserDefaults.standard.string(forKey: customKey).flatMap { $0.isEmpty ? nil : $0 } }
        set {
            if let newValue, !newValue.isEmpty { UserDefaults.standard.set(newValue, forKey: customKey) }
            else { UserDefaults.standard.removeObject(forKey: customKey) }
        }
    }
    static var addresses: [String] {
        guard let custom else { return defaults }
        return [custom] + defaults.filter { $0 != custom }
    }
    private let list: [String]
    private(set) var index = 0
    init() { list = Self.addresses }
    var current: String { list[index] }
    mutating func failed() { index = (index + 1) % list.count }
    mutating func reset() { index = 0 }
}

/// Import is transactional with respect to Keychain: remove only after save.
/// No token values enter errors, diagnostics, defaults, or URLs.
enum GatewayTokenImport {
    @discardableResult
    static func run(file: URL, save: (String) throws -> Void) throws -> Bool {
        guard FileManager.default.fileExists(atPath: file.path) else { return false }
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber, size.intValue > 0, size.intValue <= 4096 else {
            throw ImportError.invalidFile
        }
        let data = try Data(contentsOf: file)
        guard let raw = String(data: data, encoding: .utf8) else { throw ImportError.invalidFile }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, token.utf8.allSatisfy({ $0 >= 33 && $0 <= 126 }) else {
            throw ImportError.invalidFile
        }
        try save(token)
        try FileManager.default.removeItem(at: file)
        return true
    }
    enum ImportError: Error { case invalidFile }
}

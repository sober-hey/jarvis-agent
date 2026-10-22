import Foundation
import Security

public enum GatewayTokenError: Error, LocalizedError {
    case keychain(OSStatus), invalidValue
    public var errorDescription: String? {
        switch self {
        case .keychain(let status): return "网关令牌不可读取（钥匙串状态 \(status)）"
        case .invalidValue: return "网关令牌为空或格式无效"
        }
    }
}

/// Login keychain only. Never reads files, emits secrets, or prompts automatically.
public struct GatewayTokenStore {
    public static let service = "app.jarvis.voice-gateway"
    private let query: (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus
    public init() { query = SecItemCopyMatching }
    init(query: @escaping (CFDictionary, UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus) { self.query = query }
    public func read() throws -> String {
        let request: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service, kSecAttrAccount as String: "default",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
            kSecUseDataProtectionKeychain as String: false]
        var result: CFTypeRef?
        let status = query(request as CFDictionary, &result)
        guard status == errSecSuccess else { throw GatewayTokenError.keychain(status) }
        guard let data = result as? Data, let value = String(data: data, encoding: .utf8),
              !value.isEmpty, !value.contains(where: { $0.isWhitespace || $0.isNewline }) else {
            throw GatewayTokenError.invalidValue
        }
        return value
    }
}

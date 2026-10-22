import Foundation
import Security

/// Optional gateway bearer credential. It is never part of a URL or defaults.
enum GatewayCredential {
    private static let service = "app.jarvis.voice.gateway"
    private static let account = "bearer-token"
    @discardableResult
    static func importPending(documents: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]) throws -> Bool {
        try GatewayTokenImport.run(file: documents.appendingPathComponent("gateway-token"), save: save)
    }
    static func load() -> String {
        var query = identity
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func save(_ value: String) throws {
        let data = Data(value.utf8)
        if value.isEmpty {
            let status = SecItemDelete(identity as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw CredentialError.writeFailed }
            return
        }
        let updates: [String: Any] = [kSecValueData as String: data]
        let existing = SecItemUpdate(identity as CFDictionary, updates as CFDictionary)
        if existing == errSecItemNotFound || existing == errSecParam {
            _ = SecItemDelete(identity as CFDictionary)
            var query = identity
            query[kSecValueData as String] = data
            query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else { throw CredentialError.writeFailed }
        } else if existing != errSecSuccess { throw CredentialError.writeFailed }
    }
    private static var identity: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service, kSecAttrAccount as String: account]
    }
    enum CredentialError: LocalizedError {
        case writeFailed
        var errorDescription: String? { "无法保存网关凭证。" }
    }
}

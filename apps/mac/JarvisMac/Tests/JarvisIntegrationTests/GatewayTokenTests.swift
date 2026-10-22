import Foundation
import Security
import XCTest
@testable import JarvisIntegration

final class GatewayTokenTests: XCTestCase {
    func testMissingKeychainItemReturnsTypedFailureWithoutSecret() {
        let store = GatewayTokenStore(query: { request, _ in
            let fields = request as NSDictionary
            XCTAssertEqual(fields[kSecAttrService] as? String, "app.jarvis.voice-gateway")
            XCTAssertEqual(fields[kSecAttrAccount] as? String, "default")
            return errSecItemNotFound
        })
        XCTAssertThrowsError(try store.read()) { error in
            guard case GatewayTokenError.keychain(let status) = error else { return XCTFail("Expected typed OSStatus") }
            XCTAssertEqual(status, errSecItemNotFound)
        }
    }
    func testKeychainAccessDeniedIsFailureNotEmptyBearer() {
        let store = GatewayTokenStore(query: { _, _ in errSecInteractionNotAllowed })
        XCTAssertThrowsError(try store.read())
    }
    func testInvalidSecretDataIsRejected() {
        let store = GatewayTokenStore(query: { _, output in
            output?.pointee = Data("bad\nvalue".utf8) as CFData
            return errSecSuccess
        })
        XCTAssertThrowsError(try store.read())
    }
    func testValidKeychainValueReturnsVerbatim() throws {
        let store = GatewayTokenStore(query: { _, output in
            output?.pointee = Data("fixture-only-value".utf8) as CFData
            return errSecSuccess
        })
        XCTAssertEqual(try store.read(), "fixture-only-value")
    }
}

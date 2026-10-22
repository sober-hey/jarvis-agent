import Foundation
import Starscream
import JarvisCore

/// Owner: transport worker. Inject URL/token at runtime; never hard-code credentials.
public enum WebSocketFactory {
    public static func make(url: URL, headers: [String: String] = [:]) throws -> WebSocket {
        guard url.scheme == "wss", url.host != nil else {
            throw JarvisError.operationFailed("WebSocket 必须使用 wss URL")
        }
        var request = URLRequest(url: url, timeoutInterval: 15)
        for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
        return WebSocket(request: request)
    }
}
@MainActor
public protocol BrainTransport: AnyObject {
    var onMessage: ((Data) -> Void)? { get set }
    func connect() async throws
    func send(_ data: Data) async throws
    func disconnect()
}

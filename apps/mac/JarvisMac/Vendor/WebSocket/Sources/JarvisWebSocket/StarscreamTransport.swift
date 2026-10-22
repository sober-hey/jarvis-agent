import Foundation
import Starscream

final class StarscreamTransport: SocketTransport {
    var onEvent: ((SocketEvent) -> Void)?
    private let socket: WebSocket
    init(request: URLRequest, callbackQueue: DispatchQueue) {
        // Starscream's default upgrader adds an Origin automatically. The gateway
        // intentionally allows non-browser clients only (origins=[None]). Strip
        // just that synthesized header; do NOT weaken the gateway's origin policy.
        let engine = WSEngine(transport: TCPTransport(), certPinner: FoundationSecurity(),
                              httpHandler: NativeAppHTTPHandler())
        var nativeRequest = request
        nativeRequest.setValue("", forHTTPHeaderField: "Cookie") // No shared browser cookies.
        socket = WebSocket(request: nativeRequest, engine: engine)
        socket.callbackQueue = callbackQueue
        // Default Starscream certificate/hostname validation remains enabled.
        socket.onEvent = { [weak self] event in
            switch event {
            case .connected: self?.onEvent?(.connected)
            case .text(let text): self?.onEvent?(.text(text))
            case .binary(let data): self?.onEvent?(.binary(data))
            case .pong: self?.onEvent?(.pong)
            case .ping: self?.onEvent?(.activity)
            case .disconnected(let reason, let code):
                self?.onEvent?(.disconnected(JarvisWebSocketError.transport("\(reason) (\(code))")))
            case .error(let error):
                if let upgrade = error as? HTTPUpgradeError, case .notAnUpgrade(let status, _) = upgrade, status == 401 || status == 403 {
                    self?.onEvent?(.disconnected(JarvisWebSocketError.unauthorized))
                } else { self?.onEvent?(.disconnected(error)) }
            case .cancelled, .peerClosed: self?.onEvent?(.disconnected(nil))
            case .viabilityChanged, .reconnectSuggested: break
            }
        }
    }
    func connect() { socket.connect() }
    func disconnect() { socket.forceDisconnect() }
    func write(text: String, completion: @escaping () -> Void) { socket.write(string: text, completion: completion) }
    func ping(completion: @escaping () -> Void) { socket.write(ping: Data(), completion: completion) }
}

private final class NativeAppHTTPHandler: HTTPHandler {
    private let underlying = FoundationHTTPHandler()
    func register(delegate: HTTPHandlerDelegate) { underlying.register(delegate: delegate) }
    func parse(data: Data) -> Int { underlying.parse(data: data) }
    func convert(request: URLRequest) -> Data {
        let data = underlying.convert(request: request)
        guard let headers = String(data: data, encoding: .utf8) else { return data }
        let filtered = headers.components(separatedBy: "\r\n").filter {
            !$0.lowercased().hasPrefix("origin:")
        }.joined(separator: "\r\n")
        return Data(filtered.utf8)
    }
}

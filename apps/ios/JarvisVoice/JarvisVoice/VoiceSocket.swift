import Foundation

/// Main-actor FIFO preserves audio_end after its PCM frames. Reconnects never
/// replay text requests or microphone audio whose delivery was uncertain.
@MainActor
final class VoiceSocket {
    var onMessage: (([String: Any]) -> Void)?
    var onConnection: ((Bool) -> Void)?
    var onError: ((String) -> Void)?
    private var socket: URLSessionWebSocketTask?
    private var session: URLSession?
    private var receiver: Task<Void, Never>?
    private var sender: Task<Void, Never>?
    private var heartbeat: Task<Void, Never>?
    private var deadline: Task<Void, Never>?
    private var target: String?
    private var queue: [String] = []
    private var epoch = UUID()
    private(set) var connected = false

    func connect(url: URL, token: String = "") {
        disconnect()
        let current = UUID()
        epoch = current
        target = url.absoluteString
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 5
        configuration.timeoutIntervalForResource = 24 * 60 * 60
        configuration.waitsForConnectivity = false
        let session = URLSession(configuration: configuration)
        self.session = session
        var request = URLRequest(url: url)
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let task = session.webSocketTask(with: request)
        socket = task
        task.maximumMessageSize = 2 * 1024 * 1024
        task.resume()
        deadline = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(6)) } catch { return }
            guard let self, self.epoch == current, !self.connected else { return }
            GatewayDiagnostics.record(.disconnected, endpoint: self.target, code: NSURLErrorTimedOut)
            self.fail("该地址不可达，正在尝试下一个地址。")
        }
        receiver = Task { [weak self] in
            do {
                // A successful ping confirms HTTP upgrade, not just task.resume().
                try await task.sendPing()
                guard let self, self.epoch == current else { return }
                self.deadline?.cancel()
                self.deadline = nil
                GatewayDiagnostics.record(.upgraded, endpoint: self.target)
                self.connected = true
                self.onConnection?(true)
                while !Task.isCancelled {
                    let message = try await task.receive()
                    guard self.epoch == current else { return }
                    let data: Data
                    switch message {
                    case .string(let text): data = Data(text.utf8)
                    case .data(let bytes): data = bytes
                    @unknown default: continue
                    }
                    guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                    self.onMessage?(object)
                }
            } catch {
                guard let self, self.epoch == current else { return }
                GatewayDiagnostics.record(.disconnected, endpoint: self.target, code: (error as NSError).code)
                self.fail("语音连接已断开，正在尝试下一个地址。")
            }
        }
        heartbeat = Task { [weak self] in
            do {
                while !Task.isCancelled {
                    try await Task.sleep(for: .seconds(15))
                    guard let self, self.epoch == current else { return }
                    try await task.sendPing()
                }
            } catch {
                guard let self, self.epoch == current else { return }
                self.fail("语音连接心跳中断。")
            }
        }
    }

    @discardableResult
    func send(_ object: [String: Any]) -> Bool {
        guard connected, let socket,
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return false }
        // At 20 ms/frame this bounds queued capture to two seconds.
        guard queue.count < 100 else {
            fail("网络跟不上录音，已断开连接以免积压。")
            return false
        }
        queue.append(text)
        guard sender == nil else { return true }
        let current = epoch
        sender = Task { [weak self] in
            guard let self else { return }
            defer { if self.epoch == current { self.sender = nil } }
            do {
                while !self.queue.isEmpty, !Task.isCancelled, self.epoch == current {
                    let next = self.queue.removeFirst()
                    try await socket.send(.string(next))
                }
            } catch {
                if self.epoch == current { self.fail("发送失败；不会自动重发已提交的内容。") }
            }
        }
        return true
    }

    private func fail(_ message: String) {
        disconnect()
        onError?(message)
        onConnection?(false)
    }

    func disconnect() {
        epoch = UUID()
        connected = false
        deadline?.cancel()
        deadline = nil
        receiver?.cancel()
        sender?.cancel()
        heartbeat?.cancel()
        receiver = nil
        sender = nil
        heartbeat = nil
        queue.removeAll()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        session?.invalidateAndCancel()
        session = nil
    }
}

private extension URLSessionWebSocketTask {
    func sendPing() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            sendPing { error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
    }
}

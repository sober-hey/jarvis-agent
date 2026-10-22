import Combine
import Foundation
import Security

/// Direct Watch Wi-Fi/cellular transport, using the same voice gateway contract as iOS.
/// Never reconnect/replay an upload with an uncertain outcome.
@MainActor
final class WatchConnection: NSObject, ObservableObject, URLSessionWebSocketDelegate {
    @Published private(set) var ready = false
    @Published private(set) var suppressed = false
    @Published private(set) var state = "未连接"
    var onReply: ((Data, Int) -> Void)?
    var onText: ((String, String) -> Void)?
    var onStopPlayback: (() -> Void)?
    var onSuppressed: (() -> Void)?
    var onError: ((String) -> Void)?
    var onReady: (() -> Void)?
    var deviceState: () -> [String: Any] = {
        ["device_type": "watch", "battery_level": NSNull(), "charging": NSNull(),
         "on_wrist": NSNull(), "wearing": NSNull(), "clap_triggered": false,
         "observed_at": Date().timeIntervalSince1970]
    }
    private var socket: URLSessionWebSocketTask?
    private lazy var session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    private var receiveTask: Task<Void, Never>?
    private var heartbeat: Task<Void, Never>?
    private var responseDeadline: Task<Void, Never>?
    private var pendingID: UUID?
    private var replyBuffer = WatchReplyBuffer()
    private var captureEpoch = 0
    private let credential: () throws -> String?
    let deviceID: String
    var awaitingReply: Bool { pendingID != nil }

    init(credential: @escaping () throws -> String? = WatchCredential.load,
         deviceID: String? = nil) {
        self.credential = credential
        if let deviceID { self.deviceID = deviceID }
        else if let saved = UserDefaults.standard.string(forKey: "gateway_device_id") {
            self.deviceID = saved
        } else {
            let value = "watch-" + UUID().uuidString
            UserDefaults.standard.set(value, forKey: "gateway_device_id")
            self.deviceID = value
        }
        super.init()
    }

    func connect(endpoint: String) {
        disconnect()
        guard let url = WatchWire.validatedEndpoint(endpoint) else {
            onError?("地址须为 ws://局域网或Tailscale地址:8765，公网须 wss://")
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        do {
            if let token = try credential(), !token.isEmpty {
                guard !token.contains("\r"), !token.contains("\n"), token.utf8.count <= 4096 else {
                    throw WatchCredential.CredentialError.invalid
                }
                request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
            }
        } catch {
            onError?("无法读取有效钥匙串令牌；没有发起连接")
            return
        }
        let task = session.webSocketTask(with: request)
        task.maximumMessageSize = WatchWire.maxMessageBytes
        socket = task
        state = "连接中"
        task.resume()
        receiveTask = Task { [weak self, weak task] in
            guard let task else { return }
            while !Task.isCancelled {
                do {
                    let message = try await task.receive()
                    guard let self, self.socket === task else { return }
                    guard case .string(let text) = message else { throw WatchWire.WireError.invalidMessage }
                    try self.receive(text)
                } catch {
                    guard let self, self.socket === task else { return }
                    self.fail("连接或响应失败；未自动重发录音")
                    return
                }
            }
        }
    }

    private func receive(_ text: String) throws {
        switch try WatchWire.event(text) {
        case .text(let text, let role, let generation):
            replyBuffer.observe(generation)
            if !suppressed { onText?(text, role) }
        case .audio(let data, let generation, let final):
            guard !suppressed, pendingID != nil else { return }
            if let complete = try replyBuffer.append(data, generation: generation, final: final) {
                completePending()
                onReply?(complete, generation)
            }
        case .interrupt(let generation):
            replyBuffer.interrupt(generation: generation)
            onStopPlayback?()
        case .suppressed:
            suppressed = true
            captureEpoch += 1
            replyBuffer.setSuppressed(true)
            completePending()
            onStopPlayback?()
            onSuppressed?()
        case .selected:
            suppressed = false
            replyBuffer.setSuppressed(false)
            // Does not resume a cancelled capture/upload. Wait for the next onset.
        case .error(let code):
            completePending()
            replyBuffer.interrupt()
            onStopPlayback?()
            onError?("语音网关错误：" + code)
        case .ignored: break
        }
    }

    /// Local onset invalidates old MP3 before recording, regardless of network latency.
    func beginUtterance() -> Int? {
        guard ready, !suppressed, !awaitingReply else { return nil }
        captureEpoch += 1
        replyBuffer.interrupt()
        onStopPlayback?()
        return captureEpoch
    }

    func sendClip(_ wav: Data, capture: Int) async throws {
        guard ready, !suppressed, let task = socket, pendingID == nil,
              capture == captureEpoch else { throw ConnectionError.notReady }
        let pcm = try WatchWire.pcm(fromWAV: wav)
        let id = UUID() // Local timeout identity only; never invent a wire request ID.
        pendingID = id
        do {
            for start in stride(from: 0, to: pcm.count, by: WatchWire.pcmFrameBytes) {
                guard socket === task, !suppressed, capture == captureEpoch, pendingID == id else {
                    throw ConnectionError.notReady
                }
                let frame = pcm.subdata(in: start..<min(start + WatchWire.pcmFrameBytes, pcm.count))
                try await task.send(.string(try WatchWire.audio(frame)))
                // Match iOS 20ms PCM pacing; never leave a >500ms application gap.
                try await Task.sleep(for: .milliseconds(20))
            }
            guard socket === task, !suppressed, capture == captureEpoch, pendingID == id else {
                throw ConnectionError.notReady
            }
            try await task.send(.string("{\"type\":\"audio_end\"}"))
            guard socket === task, pendingID == id else { return }
            responseDeadline = Task { [weak self] in
                try? await Task.sleep(for: .seconds(30))
                guard !Task.isCancelled, let self, self.pendingID == id else { return }
                self.fail("回复超时，结果未知；未重发。请重新唤醒发起新一轮")
            }
        } catch {
            if socket === task, !suppressed, capture == captureEpoch {
                fail("录音发送结果未知；未自动重试")
            }
            throw error
        }
    }

    func sendStatus() async {
        guard ready, let task = socket else { return }
        do {
            let text = try WatchWire.deviceState(deviceID: deviceID, state: deviceState())
            guard text.utf8.count < 16_384 else { return }
            try await task.send(.string(text))
        } catch { if socket === task { fail("状态发送失败；请手动重连") } }
    }

    func playbackDone(generation: Int) async {
        guard ready, !suppressed, let task = socket else { return }
        do {
            try await task.send(.string(try WatchWire.message([
                "type": "playback_done", "generation": generation])))
        } catch { if socket === task { fail("播放确认失败；未重试") } }
    }

    private func completePending() {
        pendingID = nil
        responseDeadline?.cancel()
        responseDeadline = nil
    }

    func disconnect() {
        let previous = socket
        socket = nil
        ready = false
        suppressed = false
        state = "未连接"
        captureEpoch += 1
        completePending()
        replyBuffer.reset()
        receiveTask?.cancel(); heartbeat?.cancel()
        onStopPlayback?()
        previous?.cancel(with: .goingAway, reason: nil)
    }
    private func fail(_ message: String) {
        disconnect()
        state = "连接中断"
        onError?(message)
    }

    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                              didOpenWithProtocol protocol: String?) {
        Task { @MainActor [self, webSocketTask] in
            guard self.socket === webSocketTask else { return }
            do {
                try await webSocketTask.send(.string(try WatchWire.hello(
                    deviceID: deviceID, state: deviceState())))
            } catch {
                if self.socket === webSocketTask { self.fail("设备握手发送失败；未自动重连") }
                return
            }
            guard self.socket === webSocketTask else { return }
            self.ready = true
            self.state = "已连接"
            self.onReady?()
            self.heartbeat = Task { [weak self, weak webSocketTask] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(15))
                    guard !Task.isCancelled, let self, let webSocketTask,
                          self.socket === webSocketTask else { return }
                    await self.sendStatus()
                    webSocketTask.sendPing { [weak self, weak webSocketTask] error in
                        guard error != nil else { return }
                        Task { @MainActor in
                            guard let self, let webSocketTask, self.socket === webSocketTask else { return }
                            self.fail("网络不可达；请手动重连")
                        }
                    }
                }
            }
        }
    }
    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask,
                               willPerformHTTPRedirection response: HTTPURLResponse,
                               newRequest request: URLRequest,
                               completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Never forward bearer credentials to a redirect.
    }
    nonisolated func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                              didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        Task { @MainActor in
            guard self.socket === webSocketTask else { return }
            self.fail("远端关闭连接；未重发录音")
        }
    }
    enum ConnectionError: Error { case notReady }
}

/// Gateway credential only, never a cloud/model API key, URL token or UserDefaults value.
enum WatchCredential {
    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "app.jarvis.watch.gateway",
         kSecAttrAccount as String: "gateway-token"]
    }
    static func save(_ value: String) throws {
        guard !value.contains("\r"), !value.contains("\n"), value.utf8.count <= 4096 else {
            throw CredentialError.invalid
        }
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw CredentialError.storage }
        guard !value.isEmpty else { return }
        var item = query
        item[kSecValueData as String] = Data(value.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw CredentialError.storage }
    }
    static func load() throws -> String? {
        var item = query
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var value: CFTypeRef?
        let status = SecItemCopyMatching(item as CFDictionary, &value)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = value as? Data,
              let string = String(data: data, encoding: .utf8) else { throw CredentialError.storage }
        return string
    }
    enum CredentialError: Error { case invalid, storage }
}

/// One-shot token import so the watch never needs a typed credential.
///
/// The gateway token is pushed into the app container as Documents/gateway-token
/// (0600 on the Mac, deleted after transfer). The first launch moves it into the
/// keychain and removes the file, so a copied container cannot replay it.
enum WatchTokenImport {
    @discardableResult
    static func importIfPresent() -> Bool {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        guard let documents else { return false }
        let file = documents.appendingPathComponent("gateway-token")
        guard FileManager.default.fileExists(atPath: file.path) else { return false }
        defer { try? FileManager.default.removeItem(at: file) }
        guard let raw = try? String(contentsOf: file, encoding: .utf8) else { return false }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return false }
        do {
            try WatchCredential.save(token)
            return true
        } catch {
            return false
        }
    }
}

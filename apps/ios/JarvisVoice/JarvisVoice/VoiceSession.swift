import AVFoundation
import Combine
import SwiftUI

@MainActor
final class VoiceSession: ObservableObject {
    static let shared = VoiceSession()
    @Published var active = false
    @Published var connected = false
    @Published var speaking = false
    /// True between "utterance uploaded" and "answer text arrived".
    @Published var thinking = false
    @Published var level: Float = 0
    @Published var status = "待命"
    @Published var userText = ""
    @Published var assistantText = ""
    @Published var error: String?
    @Published private(set) var endpoint = GatewayEndpoints().current
    @Published private(set) var credentialReady = false
    @Published var threshold = UserDefaults.standard.double(forKey: "vadThreshold") == 0
        ? 0.018 : UserDefaults.standard.double(forKey: "vadThreshold")
    let deviceID: String
    private let capture = AudioCapture()
    private let playback = MP3Playback()
    private let socket = VoiceSocket()
    private let reporter = DeviceStatusReporter()
    private var observers: [NSObjectProtocol] = []
    private var reconnect: Task<Void, Never>?
    private var restart: Task<Void, Never>?
    private var reconnectAttempt = 0
    private var selected = false
    private var endpoints = GatewayEndpoints()
    private var wantsConnection = false
    private var foreground = true
    private var pendingText: String?
    private var starting = false
    private var interrupted = false
    private var forwarding = UtteranceForwarding()

    private init() {
        if let stored = UserDefaults.standard.string(forKey: "deviceID") { deviceID = stored }
        else {
            deviceID = "iphone-" + UUID().uuidString.lowercased()
            UserDefaults.standard.set(deviceID, forKey: "deviceID")
        }
        playback.output = capture
        capture.onFrame = { [weak self] result in
            // FIFO from the serial capture queue; unordered Tasks could reorder
            // the PCM frame and its audio_end marker.
            DispatchQueue.main.async { self?.consume(result) }
        }
        playback.onPlaying = { [weak self] in self?.speaking = $0 }
        playback.onFinished = { [weak self] generation in
            self?.socket.send(["type": "playback_done", "generation": generation])
        }
        playback.onError = { [weak self] in self?.error = $0 }
        socket.onMessage = { [weak self] in self?.receive($0) }
        socket.onConnection = { [weak self] isConnected in
            guard let self else { return }
            self.connected = isConnected
            self.forwarding.reset()
            if isConnected {
                self.reconnectAttempt = 0
                self.status = self.active ? "说吧，我在听" : "网关已连接"
                self.playback.reset()
                self.error = nil
                if self.socket.send(VoiceWire.hello(deviceID: self.deviceID, state: self.reporter.snapshot())) {
                    GatewayDiagnostics.record(.helloSent, endpoint: self.endpoint)
                }
                if let text = self.pendingText {
                    self.pendingText = nil // Never auto-replay uncertain submissions.
                    self.socket.send(["type": "text", "text": text, "device_id": self.deviceID])
                }
            } else { self.selected = false; self.endpoints.failed(); self.scheduleReconnect() }
        }
        socket.onError = { [weak self] message in
            self?.error = message
            self?.playback.reset()
        }
        reporter.onChange = { [weak self] state in
            guard let self, self.wantsConnection else { return }
            self.socket.send(["type": "device_state", "device_id": self.deviceID, "state": state])
        }
        for name in [AVAudioSession.interruptionNotification, AVAudioSession.routeChangeNotification,
                     AVAudioSession.mediaServicesWereResetNotification, .AVAudioEngineConfigurationChange] {
            observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                Task { @MainActor in self?.audioNotification(note) }
            })
        }
    }

    func saveSettings() {
        UserDefaults.standard.set(threshold, forKey: "vadThreshold")
    }

    /// Settings page: save a user-entered gateway address and/or token, then
    /// reconnect. Empty address restores the built-in LAN/Tailscale list; an
    /// empty token field leaves the stored token untouched.
    @discardableResult
    func updateGateway(address: String, token: String) -> String? {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty, EndpointPolicy.validate(trimmed) == nil {
            return "地址无效：用 ws://局域网或Tailscale地址:8765，或 wss://"
        }
        let secret = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if !secret.isEmpty {
            guard secret.utf8.allSatisfy({ $0 >= 33 && $0 <= 126 }) else { return "令牌含非法字符" }
            do { try GatewayCredential.save(secret) } catch { return "令牌保存失败" }
        }
        GatewayEndpoints.custom = trimmed.isEmpty ? nil : trimmed
        let wasActive = active
        socket.disconnect()
        reconnect?.cancel()
        reconnect = nil
        connected = false
        selected = false
        wantsConnection = false
        endpoints = GatewayEndpoints()
        endpoint = endpoints.current
        error = nil
        bootstrap()
        if wasActive, !wantsConnection { stop() }
        return nil
    }

    func start(text: String? = nil) async {
        if active {
            if let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                sendText(text)
            }
            return
        }
        guard !starting else { return }
        starting = true
        defer { starting = false }
        saveSettings()
        bootstrap()
        guard credentialReady else { return }
        let granted = await AVAudioApplication.requestRecordPermission()
        guard granted else {
            GatewayDiagnostics.record(.microphoneDenied)
            error = "麦克风未授权。请在设置 → 隐私与安全性 → 麦克风中允许 JARVIS。"
            return
        }
        do {
            capture.threshold = Float(threshold)
            try capture.start()
            active = true
            interrupted = false
            error = nil
            status = "连接中"
            pendingText = text?.trimmingCharacters(in: .whitespacesAndNewlines)
            reporter.start()
            if connected {
                status = "正在听您说"
                if let text = pendingText { pendingText = nil; sendText(text) }
            } else { connect() }
        } catch {
            self.error = error.localizedDescription
            stop()
        }
    }

    func stop() {
        active = false
        wantsConnection = false
        forwarding.reset()
        selected = false
        pendingText = nil
        reconnect?.cancel()
        reconnect = nil
        restart?.cancel()
        restart = nil
        capture.stop()
        playback.reset()
        socket.disconnect()
        reporter.stop()
        connected = false
        speaking = false
        thinking = false
        level = 0
        status = "待命"
        // Deactivating on the main thread can stall the UI (AVAudioSession warning).
        DispatchQueue.global(qos: .userInitiated).async {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    func sendText(_ text: String) {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        playback.interrupt(locally: true)
        if socket.send(["type": "text", "text": value, "device_id": deviceID]) { userText = value }
        else { error = "当前未连接，文字没有发送。" }
    }

    /// Foreground startup imports a provisioned token and connects without
    /// microphone access. Permission to capture is requested only by Start.
    func bootstrap() {
        do {
            if try GatewayCredential.importPending() { GatewayDiagnostics.record(.tokenImported) }
        } catch {
            GatewayDiagnostics.record(.tokenImportFailed)
            self.error = "网关凭证导入失败，请重新部署凭证。"
            return
        }
        credentialReady = !GatewayCredential.load().isEmpty
        guard credentialReady else {
            GatewayDiagnostics.record(.tokenMissing)
            error = "等待 Mac 安全配置网关凭证。"
            return
        }
        guard !wantsConnection else { return }
        GatewayDiagnostics.record(.tokenAvailable)
        wantsConnection = true
        endpoints.reset()
        reporter.start()
        connect()
    }

    func foregroundChanged(_ active: Bool) {
        foreground = active
        reporter.setForeground(active)
        if active {
            guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil,
                  NSClassFromString("XCTestCase") == nil else { return }
            bootstrap()
        }
        else if self.active {
            // iOS forbids idle background listening; no silent keepalive.
            stop()
            GatewayDiagnostics.record(.backgroundPaused)
        }
    }

    private func consume(_ result: SpeechGate.Result) {
        guard active else { return }
        level = result.level
        guard !interrupted else { return }
        // Half-duplex while a reply is playing: the speaker's own voice leaking
        // into the mic used to trip the VAD, interrupt playback and cut the
        // reply into fragments. Tap the orb to stop a reply instead.
        let start = result.started && !speaking
        let forward = forwarding.consume(started: start, ended: result.ended,
                                         connected: connected, selected: selected)
        if start && selected {
            // Independent of network: stop device playback immediately on onset.
            playback.interrupt(locally: true)
            status = "正在听您说"
        }
        guard forward else { return }
        for frame in result.frames { socket.send(VoiceWire.audio(PCM16.encode(frame))) }
        if result.ended {
            socket.send(["type": "audio_end"])
            thinking = true
            status = connected ? "正在聆听" : "等待重连"
        }
    }

    private func connect() {
        guard wantsConnection else { return }
        endpoint = endpoints.current
        guard let url = EndpointPolicy.validate(endpoint) else { return }
        let token = GatewayCredential.load()
        guard !token.isEmpty else { return }
        GatewayDiagnostics.record(.connecting, endpoint: endpoint)
        status = "连接中"
        socket.connect(url: url, token: token)
    }

    private func scheduleReconnect() {
        guard wantsConnection, reconnect == nil else { return }
        reconnectAttempt += 1
        let seconds = endpoints.index == 0 ? min(15, 1 << min(reconnectAttempt / 3, 4)) : 0
        status = "等待重连"
        reconnect = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(seconds)) }
            catch { return }
            guard let self, self.wantsConnection else { return }
            self.reconnect = nil
            self.connect()
        }
    }

    private func receive(_ message: [String: Any]) {
        switch message["type"] as? String {
        case "selected", "resumed": GatewayDiagnostics.record(.selected, endpoint: endpoint)
        case "suppressed": GatewayDiagnostics.record(.suppressed, endpoint: endpoint)
        default: break
        }
        switch VoiceWire.decode(message) {
        case .interrupt: playback.interrupt()
        case .text(let text, let role):
            if role == "user" { userText = text }
            else { assistantText = text; thinking = false }
        case .audio(let data, let generation, let final):
            // Always receive audio chunks: dropping them prevents playback_done
            // from firing, which leaves gateway.playing=true and blocks the next turn.
            // selected only gates microphone forwarding, not playback.
            playback.receive(data, generation: generation, final: final)
        case .suppressed:
            selected = false
            playback.interrupt()
            status = "另一台设备正在回应"
        case .selected: selected = true; status = active ? "说吧，我在听" : "网关握手成功"
        case .error(let code): error = "服务端：" + code; thinking = false
        case .invalid: error = "收到不符合语音协议的数据。"
        case .ignored: break
        }
    }

    private func audioNotification(_ notification: Notification) {
        guard active else { return }
        if notification.name == .AVAudioEngineConfigurationChange,
           !capture.ownsEngine(notification.object) { return }
        if notification.name == AVAudioSession.interruptionNotification {
            let info = notification.userInfo ?? [:]
            guard let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            if type == .began {
                interrupted = true
                capture.stop()
                playback.interrupt()
                status = "系统音频中断"
                socket.send(["type": "audio_end"])
                thinking = true
                return
            }
            let options = AVAudioSession.InterruptionOptions(rawValue: info[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            guard options.contains(.shouldResume) else {
                stop()
                error = "通话或系统录音结束；请重新开始语音。"
                return
            }
        }
        restart?.cancel()
        restart = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(300)) }
            catch { return }
            guard let self, self.active else { return }
            self.capture.stop()
            self.playback.reset()
            do {
                try self.capture.start()
                self.interrupted = false
                self.status = self.connected ? "正在聆听" : "等待重连"
            } catch {
                self.stop()
                self.error = "音频设备已变化，请重新开始语音。"
            }
        }
    }
}

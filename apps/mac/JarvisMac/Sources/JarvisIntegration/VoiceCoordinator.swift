import AppKit
import Combine
import Foundation
import AudioAccessibilityModule
import JarvisWebSocket

@MainActor
public final class VoiceCoordinator {
    public enum State: String { case standby = "待机", listening = "聆听", thinking = "思考", speaking = "说话", disconnected = "断线" }
    public let player = AudioPlayer()
    public let capture: MicrophoneCapture
    public private(set) var state: State = .disconnected
    public private(set) var connected = false
    public private(set) var gatewayStatus = "断线"
    public private(set) var suppressed = false
    public private(set) var microphoneRunning = false
    public private(set) var captureSampleRate: Double = 0
    public private(set) var lastError: String?
    /// Non-nil while the microphone is released for a call.
    public private(set) var yieldedForCall: String?
    public private(set) var lastText: String?
    public var onUpdate: (() -> Void)?
    public var onEvent: (([String: Any]) -> Void)?
    private let socket: JarvisWebSocketClient
    private var subscriptions = [UUID: AnyCancellable]()
    private var stateTimer: Timer?
    private var activation: UUID?
    private var playerToken: UUID?
    private var playbackGeneration: Int?
    private var epoch = UUID()
    private var alive = true
    private let deviceID: String
    private let callYield = CallYieldMonitor()
    public init(gateway: URL = URL(string: "ws://127.0.0.1:8765")!) throws {
        capture = try MicrophoneCapture()
        var config = JarvisWebSocketConfiguration(); config.url = gateway
        config.bearerTokenProvider = { try GatewayTokenStore().read() }
        socket = try JarvisWebSocketClient(configuration: config)
        if let stored = UserDefaults.standard.string(forKey: "JarvisDeviceID") { deviceID = stored }
        else { let value = "mac-" + UUID().uuidString; UserDefaults.standard.set(value, forKey: "JarvisDeviceID"); deviceID = value }
        capture.onActivation = { [weak self] id, kind, rate in
            MainActor.assumeIsolated { self?.activated(id: id, kind: kind, rate: rate) }
        }
        capture.onUtterance = { [weak self] id, data, duration, silence in
            MainActor.assumeIsolated { self?.upload(id: id, data: data, duration: duration, silence: silence) }
        }
        capture.onNoSpeech = { [weak self] id in
            MainActor.assumeIsolated {
                guard let self, self.activation == id else { return }
                self.emit("vad_no_speech"); self.activation = nil; self.refreshIdle()
            }
        }
        capture.onError = { [weak self] message in MainActor.assumeIsolated { self?.report(message) } }
        capture.onYield = { [weak self] yielding, reason in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.yieldedForCall = yielding ? (reason ?? "通话中") : nil
                self.emit("microphone_yield", ["yielded": yielding, "reason": reason ?? ""])
                self.onUpdate?()
            }
        }
        capture.onCapture = { [weak self] running, rate in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.microphoneRunning = running; self.captureSampleRate = rate; self.emit("capture", ["running": running, "sample_rate": rate]); self.onUpdate?()
            }
        }
        socket.onConnectionChanged = { [weak self] value in
            MainActor.assumeIsolated { self?.connectionChanged(value) }
        }
        socket.onSelectionChanged = { [weak self] selected in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.suppressed = !selected; self.capture.setAllowed(selected && self.connected)
                if !selected { self.activation = nil; self.playerToken = nil; self.playbackGeneration = nil; self.player.stop() }
                self.emit(selected ? "selected" : "suppressed"); self.refreshIdle()
            }
        }
        socket.onInterrupted = { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                // An interrupt commonly announces the CURRENT response; do not invalidate its token.
                self.player.stop(); self.playerToken = self.player.activationID; self.playbackGeneration = nil
                self.emit("gateway_interrupt")
            }
        }
        socket.onTextReceived = { [weak self] text in MainActor.assumeIsolated {
            self?.lastText = text; self?.emit("assistant_text", ["text": text]); self?.onUpdate?()
        } }
        socket.onTranscriptReceived = { [weak self] text in MainActor.assumeIsolated { self?.emit("user_transcript", ["text": text]) } }
        socket.onError = { [weak self] error in MainActor.assumeIsolated {
            if let failure = error as? JarvisWebSocketError, case .unauthorized = failure { self?.gatewayStatus = "未授权" }
            self?.report(error.localizedDescription)
        } }
        player.onProgress = { [weak self] snapshot in
            guard let self else { return }
            switch snapshot.phase {
            case .playing:
                self.emit("player_playing", ["generation": self.playbackGeneration as Any])
                self.capture.setPlaybackActive(true)
                self.change(.speaking)
            case .finished:
                self.emit("player_finished", ["generation": self.playbackGeneration as Any])
                self.capture.setPlaybackActive(false)
                // Keep the conversation open so the next sentence needs no wake word.
                self.capture.openConversation(25)
                if let generation = self.playbackGeneration {
                    self.socket.playbackDone(generation: generation); self.emit("playback_done", ["generation": generation])
                }
                self.playbackGeneration = nil; self.refreshIdle()
            case .failed:
                self.capture.setPlaybackActive(false)
                self.report(snapshot.errorMessage ?? "播放失败"); self.refreshIdle()
            case .idle:
                if self.state == .speaking { self.playbackGeneration = nil; self.refreshIdle() }
            default: break
            }
            self.onUpdate?()
        }
    }
    public func connect() {
        alive = true; socket.connect()
        // Yield the microphone to calls instead of blocking them.
        callYield.onCallStarted = { [weak self] reason in
            MainActor.assumeIsolated { self?.capture.yieldForCall(reason) }
        }
        callYield.onCallEnded = { [weak self] in
            MainActor.assumeIsolated { self?.capture.resumeAfterCall() }
        }
        callYield.start()
        stateTimer?.invalidate()
        let timer = Timer(timeInterval: 15, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.sendDeviceState(hello: false) }
        }
        RunLoop.main.add(timer, forMode: .common); stateTimer = timer
    }
    public func reconnect() { gatewayStatus = "断线"; socket.disconnect(); socket.connect() }
    public func startMicrophone() { capture.start() }
    public func stopMicrophone() { capture.openConversation(0); capture.stop(); activation = nil; refreshIdle() }
    public func activateManually() { capture.manualActivation() }
    public func stopPlayback() { playerToken = nil; playbackGeneration = nil; capture.setPlaybackActive(false); player.stop(); refreshIdle() }
    public func setGestures(clap: Bool, snap: Bool) { capture.setGestures(clap: clap, snap: snap) }
    public func shutdown() {
        alive = false; stateTimer?.invalidate(); stateTimer = nil
        epoch = UUID(); capture.stop(); callYield.stop(); player.stop(); socket.disconnect(); subscriptions.removeAll()
    }
    private func connectionChanged(_ value: Bool) {
        guard alive else { return }
        connected = value
        if value { gatewayStatus = "已连接" } else if gatewayStatus != "未授权" { gatewayStatus = "断线" }
        if value {
            suppressed = false; sendDeviceState(hello: true); sendDeviceState(hello: false)
        } else {
            epoch = UUID(); activation = nil; playerToken = nil; playbackGeneration = nil; player.stop()
        }
        capture.setAllowed(value && !suppressed)
        emit("connection", ["connected": value, "gateway_status": gatewayStatus]); refreshIdle()
    }
    private func sendDeviceState(hello: Bool) {
        guard connected else { return }
        // Unknown physical facts stay null. App foreground is NOT screen/lock/headphone wearing.
        let state: [String: Any] = ["battery_level": NSNull(), "charging": NSNull(),
            "headphones": NSNull(), "headphones_connected": NSNull(), "headphones_worn": NSNull(),
            "screen_on": NSNull(), "screen_state_source": "unavailable_public_api",
            "app_foreground": NSApp?.isActive ?? false, "face_down": NSNull(),
            "audio_outputs": [], "observed_at": Date().timeIntervalSince1970]
        var message: [String: Any] = ["type": hello ? "hello" : "device_state", "device_id": deviceID, "state": state]
        if hello { message["device_type"] = "mac"; message["sample_rate"] = 16000; message["channels"] = 1; message["encoding"] = "pcm_s16le" }
        socket.sendControl(message); emit(hello ? "hello_sent" : "device_state_sent")
    }
    private func activated(id: UUID, kind: String, rate: Double) {
        guard connected, !suppressed, alive else { return }
        let interrupted = player.snapshot.phase == .playing || player.snapshot.phase == .paused
        activation = id; playerToken = player.activate(); playbackGeneration = nil
        lastError = nil; emit("activation", ["kind": kind, "original_sample_rate": rate, "interrupted_playback": interrupted])
        change(.listening)
    }
    private func upload(id: UUID, data: Data, duration: Double, silence: Double) {
        guard connected, !suppressed, alive, activation == id else { emit("utterance_discarded"); return }
        let requestEpoch = epoch
        change(.thinking); emit("vad_audio_end", ["pcm_bytes": data.count, "duration": duration, "sample_rate": 16000, "silence_seconds": silence])
        subscriptions[id] = socket.sendAudio(data).receive(on: DispatchQueue.main).sink(receiveCompletion: { [weak self] completion in
            self?.subscriptions[id] = nil
            guard let self, self.epoch == requestEpoch, self.activation == id else { return }
            if case .failure(let error) = completion { self.report(error.localizedDescription); self.activation = nil; self.refreshIdle() }
        }, receiveValue: { [weak self] response in
            guard let self, self.epoch == requestEpoch, self.activation == id, !self.suppressed else { self?.emit("stale_response_discarded"); return }
            self.emit("mp3_received", ["bytes": response.audio.count, "generation": response.generation as Any])
            self.playbackGeneration = response.generation
            do {
                if response.audio.isEmpty {
                    if let generation = response.generation { self.socket.playbackDone(generation: generation) }
                    self.refreshIdle()
                } else if self.capture.playViaEngine(response.audio, completion: { [weak self] in
                    self?.finishPlayback()
                }) {
                    self.beginPlayback()
                } else {
                    // 采集引擎未运行（麦克风关闭）时退回独立播放器。
                    try self.player.play(data: response.audio, activation: self.playerToken)
                }
            } catch { self.report(error.localizedDescription); self.refreshIdle() }
        })
    }
    /// 引擎内播放没有 AVPlayer 的进度回调，状态由这里驱动。
    private func beginPlayback() {
        emit("player_playing", ["generation": playbackGeneration as Any])
        capture.setPlaybackActive(true)
        change(.speaking)
    }

    private func finishPlayback() {
        emit("player_finished", ["generation": playbackGeneration as Any])
        capture.setPlaybackActive(false)
        capture.openConversation(25)
        if let generation = playbackGeneration {
            socket.playbackDone(generation: generation)
            emit("playback_done", ["generation": generation])
        }
        playbackGeneration = nil
        refreshIdle()
    }

    private func report(_ message: String) { lastError = message; emit("error", ["message": message]); onUpdate?() }
    private func refreshIdle() { change(connected ? .standby : .disconnected) }
    private func change(_ next: State) {
        if state != next { state = next; emit("state", ["value": next.rawValue]) }
        onUpdate?()
    }
    public func emit(_ name: String, _ fields: [String: Any] = [:]) {
        var event = fields; event["event"] = name; event["uptime"] = ProcessInfo.processInfo.systemUptime
        onEvent?(event)
    }
}

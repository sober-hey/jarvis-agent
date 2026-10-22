import Foundation
import Combine

public struct ResponseData: Equatable {
    public let text: String?
    public let audio: Data
    public let audioFormat: String
    public let generation: Int?
}

public enum JarvisWebSocketError: Error, LocalizedError {
    case invalidConfiguration, invalidAudio, queueFull, disconnected, unauthorized
    case outcomeUnknown, requestTimedOut, reconnectLimitReached, heartbeatTimedOut
    case protocolViolation(String), server(String), transport(String)
    public var errorDescription: String? {
        switch self {
        case .unauthorized: return "网关未授权，请检查登录钥匙串中的网关令牌。"
        case .invalidConfiguration: return "Use wss:// or loopback ws://, with positive bounded limits and timing."
        case .invalidAudio: return "PCM must be nonempty mono 16kHz S16LE, at most 30 seconds."
        case .queueFull: return "The bounded audio upload queue is full."
        case .disconnected: return "Explicitly disconnected."
        case .outcomeUnknown: return "Connection lost after upload began; the server may have processed it. Do not automatically retry."
        case .requestTimedOut: return "The response deadline expired."
        case .reconnectLimitReached: return "The reconnect budget was exhausted."
        case .heartbeatTimedOut: return "The heartbeat or write deadline expired."
        case .protocolViolation(let value): return "Invalid gateway message: \(value)"
        case .server(let value): return "Gateway error: \(value)"
        case .transport(let value): return "WebSocket error: \(value)"
        }
    }
}

public struct JarvisWebSocketConfiguration {
    public var url = URL(string: "ws://localhost:8765")!
    /// Loaded for each handshake; never included in descriptions or logs.
    public var bearerTokenProvider: (() throws -> String)?
    public var reconnectDelay: TimeInterval = 5
    public var maximumReconnectAttempts = 10
    public var heartbeatInterval: TimeInterval = 30
    public var pongTimeout: TimeInterval = 10
    public var connectionTimeout: TimeInterval = 10
    public var writeTimeout: TimeInterval = 15
    public var responseTimeout: TimeInterval = 120
    public var maximumAudioBytes = 960_000 // Gateway: 16kHz * 2 bytes * 30 seconds.
    public var maximumQueuedMessages = 16
    public var maximumQueuedBytes = 8 * 1_024 * 1_024 // Includes Base64/JSON overhead.
    public var maximumIncomingBytes = 2 * 1_024 * 1_024
    public var maximumResponseAudioBytes = 16 * 1_024 * 1_024
    public init() {}
}

enum SocketEvent {
    case connected, text(String), binary(Data), pong, activity
    case disconnected(Error?)
}
protocol SocketTransport: AnyObject {
    var onEvent: ((SocketEvent) -> Void)? { get set }
    func connect()
    func disconnect()
    func write(text: String, completion: @escaping () -> Void)
    func ping(completion: @escaping () -> Void)
}

/// Implements packaging/voice_gateway.py's data/final/generation protocol.
/// One utterance is in flight at a time: the gateway has no request IDs or deduplication.
/// Only NEVER-SENT audio survives a disconnect. An in-flight upload fails outcomeUnknown,
/// never replays a brain operation. The queue is memory-only, bounded and deadline-limited.
public final class JarvisWebSocketClient {
    public typealias Configuration = JarvisWebSocketConfiguration
    private typealias Promise = Future<ResponseData, Error>.Promise
    private struct Upload {
        let id: UUID
        let wire: String
        let bytes: Int
        let deadline: TimeInterval
        let promise: Promise
        var text: String?
        var audio = Data()
        var generation: Int?
    }
    private let config: Configuration
    private let queue = DispatchQueue(label: "ai.jarvis.websocket.state", qos: .userInitiated)
    private let queueKey = DispatchSpecificKey<UInt8>()
    private let callbackQueue: DispatchQueue
    private let factory: (URLRequest, DispatchQueue) -> SocketTransport
    private var socket: SocketTransport?
    private var token = UUID()
    private var connected = false
    private var connecting = false
    private var wantsConnection = false
    private var reconnectAttempts = 0
    private var reconnectAt: TimeInterval?
    private var connectionDeadline: TimeInterval?
    private var writeDeadline: TimeInterval?
    private var pingDeadline: TimeInterval?
    private var pingWriting = false
    private var lastActivity: TimeInterval = 0
    private var timer: DispatchSourceTimer?
    private var waiting: [Upload] = []
    private var active: Upload?
    private var queuedBytes = 0
    private var highestGeneration = -1
    private var completedGeneration = -1
    private var audioHandler: ((Data) -> Void)?
    private var textHandler: ((String) -> Void)?
    private var transcriptHandler: ((String) -> Void)?
    private var errorHandler: ((Error) -> Void)?
    private var interruptHandler: (() -> Void)?
    private var streamEndHandler: (() -> Void)?
    private var connectionHandler: ((Bool) -> Void)?
    private var selectionHandler: ((Bool) -> Void)?
    private var suppressed = false
    public var onSelectionChanged: ((Bool) -> Void)? {
        get { sync { selectionHandler } } set { sync { selectionHandler = newValue } }
    }
    /// Only device negotiation; never queues/replays a brain command.
    public func sendControl(_ value: [String: Any]) {
        guard let kind = value["type"] as? String, ["hello", "device_state"].contains(kind),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), data.count <= 16384 else { return }
        sync {
            guard connected else { return }
            socket?.write(text: String(decoding: data, as: UTF8.self), completion: {})
        }
    }

    public var isConnected: Bool { sync { connected } }
    public var onAudioReceived: ((Data) -> Void)? {
        get { sync { audioHandler } } set { sync { audioHandler = newValue } }
    }
    /// Assistant text only. User ASR goes to onTranscriptReceived.
    public var onTextReceived: ((String) -> Void)? {
        get { sync { textHandler } } set { sync { textHandler = newValue } }
    }
    public var onTranscriptReceived: ((String) -> Void)? {
        get { sync { transcriptHandler } } set { sync { transcriptHandler = newValue } }
    }
    public var onError: ((Error) -> Void)? {
        get { sync { errorHandler } } set { sync { errorHandler = newValue } }
    }
    /// Stop/flush buffered speaker audio when the gateway sends interrupt.
    public var onInterrupted: (() -> Void)? {
        get { sync { interruptHandler } } set { sync { interruptHandler = newValue } }
    }
    public var onAudioStreamFinished: (() -> Void)? {
        get { sync { streamEndHandler } } set { sync { streamEndHandler = newValue } }
    }
    public var onConnectionChanged: ((Bool) -> Void)? {
        get { sync { connectionHandler } } set { sync { connectionHandler = newValue } }
    }

    public convenience init(configuration: Configuration = .init(), callbackQueue: DispatchQueue = .main) throws {
        try self.init(configuration: configuration, callbackQueue: callbackQueue,
                      transportFactory: { StarscreamTransport(request: $0, callbackQueue: $1) })
    }
    init(configuration: Configuration, callbackQueue: DispatchQueue,
         transportFactory: @escaping (URLRequest, DispatchQueue) -> SocketTransport) throws {
        let c = configuration
        let loopback = ["localhost", "127.0.0.1", "::1", "[::1]"].contains(c.url.host?.lowercased() ?? "")
        let intervals = [c.reconnectDelay, c.heartbeatInterval, c.pongTimeout, c.connectionTimeout,
                         c.writeTimeout, c.responseTimeout]
        guard c.url.scheme == "wss" || (c.url.scheme == "ws" && loopback), c.url.host != nil,
              c.url.user == nil, c.url.password == nil, c.url.fragment == nil,
              intervals.allSatisfy({ $0.isFinite && $0 > 0 }), c.maximumReconnectAttempts >= 0,
              (1...960_000).contains(c.maximumAudioBytes), c.maximumQueuedMessages > 0,
              c.maximumQueuedBytes > 0, c.maximumIncomingBytes > 0, c.maximumResponseAudioBytes > 0 else {
            throw JarvisWebSocketError.invalidConfiguration
        }
        config = c
        self.callbackQueue = DispatchQueue(label: "ai.jarvis.websocket.callbacks", target: callbackQueue)
        factory = transportFactory
        queue.setSpecific(key: queueKey, value: 1)
    }
    deinit {
        timer?.cancel()
        socket?.onEvent = nil
        socket?.disconnect()
        let promises = waiting.map(\.promise) + (active.map { [$0.promise] } ?? [])
        callbackQueue.async { promises.forEach { $0(.failure(JarvisWebSocketError.disconnected)) } }
    }
    /// Idempotent. The ten-retry budget resets only on explicit disconnect/connect,
    /// not every handshake; this also bounds a server that repeatedly accepts then closes.
    public func connect() {
        sync {
            guard !wantsConnection else { return }
            wantsConnection = true
            reconnectAttempts = 0
            startTimer()
            open()
        }
    }
    public func disconnect() {
        sync {
            wantsConnection = false
            reconnectAt = nil
            close()
            failActive(JarvisWebSocketError.disconnected)
            failWaiting(JarvisWebSocketError.disconnected)
            stopTimerIfIdle()
        }
    }
    /// One complete utterance, not a microphone chunk: sends audio followed by audio_end.
    /// Queues eagerly before connect. Callbacks/resolution run on callbackQueue; Combine
    /// subscribers attached after Future completion should use receive(on:) for affinity.
    public func sendAudio(_ pcmData: Data) -> Future<ResponseData, Error> {
        Future { [weak self] promise in
            guard let self = self else { promise(.failure(JarvisWebSocketError.disconnected)); return }
            self.sync {
                guard !self.suppressed else { self.reject(promise, JarvisWebSocketError.server("suppressed")); return }
                guard !pcmData.isEmpty, pcmData.count % 2 == 0, pcmData.count <= self.config.maximumAudioBytes else {
                    self.reject(promise, JarvisWebSocketError.invalidAudio); return
                }
                do {
                    let wireData = try JSONSerialization.data(withJSONObject: ["type": "audio", "format": "pcm_s16le",
                        "sample_rate": 16000, "data": pcmData.base64EncodedString()] as [String: Any], options: [.sortedKeys])
                    guard self.waiting.count + (self.active == nil ? 0 : 1) < self.config.maximumQueuedMessages,
                          wireData.count <= self.config.maximumQueuedBytes - self.queuedBytes else {
                        self.reject(promise, JarvisWebSocketError.queueFull); return
                    }
                    let item = Upload(id: UUID(), wire: String(decoding: wireData, as: UTF8.self), bytes: wireData.count,
                                      deadline: self.now + self.config.responseTimeout, promise: promise)
                    self.waiting.append(item)
                    self.queuedBytes += item.bytes
                    self.startTimer()
                    self.flush()
                } catch { self.reject(promise, error) }
            }
        }
    }
    /// Acknowledge actual speaker drain, NOT merely receipt of final MP3.
    /// Supply ResponseData.generation; stale playback completions are ignored.
    public func playbackDone(generation: Int) {
        sync {
            guard connected, active == nil, generation == completedGeneration else { return }
            socket?.write(text: "{\"type\":\"playback_done\",\"generation\":\(generation)}", completion: {})
        }
    }
    private var now: TimeInterval { ProcessInfo.processInfo.systemUptime }
    @discardableResult private func sync<T>(_ work: () -> T) -> T {
        if DispatchQueue.getSpecific(key: queueKey) != nil { return work() }
        return queue.sync(execute: work)
    }
    private func open() {
        token = UUID()
        let current = token
        var request = URLRequest(url: config.url)
        request.timeoutInterval = config.connectionTimeout
        if let provider = config.bearerTokenProvider {
            do {
                let value = try provider()
                guard !value.isEmpty, !value.contains(where: { $0.isWhitespace || $0.isNewline }) else { throw JarvisWebSocketError.unauthorized }
                request.setValue("Bearer " + value, forHTTPHeaderField: "Authorization")
            } catch {
                wantsConnection = false; report(JarvisWebSocketError.unauthorized); stopTimerIfIdle(); return
            }
        }
        let transport = factory(request, queue)
        socket = transport
        connecting = true
        reconnectAt = nil
        connectionDeadline = now + config.connectionTimeout
        transport.onEvent = { [weak self] event in
            self?.sync {
                guard let self = self, self.token == current else { return }
                self.receive(event)
            }
        }
        transport.connect()
    }
    private func receive(_ event: SocketEvent) {
        switch event {
        case .connected:
            guard wantsConnection, connecting else { return }
            connecting = false; connected = true; connectionDeadline = nil
            lastActivity = now; highestGeneration = -1; completedGeneration = -1; suppressed = false
            let handler = connectionHandler
            callbackQueue.async { handler?(true) }
            flush()
        case .text(let value): if connected { lastActivity = now; parse(Data(value.utf8)) }
        case .binary(let value): if connected { lastActivity = now; parse(value) }
        case .pong: if connected { lastActivity = now; pingDeadline = nil; pingWriting = false }
        case .activity: if connected { lastActivity = now }
        case .disconnected(let error): lost(error ?? JarvisWebSocketError.transport("Peer closed."))
        }
    }
    private func close() {
        token = UUID()
        let old = socket
        socket = nil
        old?.onEvent = nil; old?.disconnect()
        connecting = false; connectionDeadline = nil; writeDeadline = nil
        pingDeadline = nil; pingWriting = false
        if connected {
            connected = false
            let handler = connectionHandler
            callbackQueue.async { handler?(false) }
        }
    }
    private func lost(_ error: Error) {
        guard socket != nil else { return }
        close()
        failActive(JarvisWebSocketError.outcomeUnknown)
        report(error)
        guard wantsConnection else { return }
        if let failure = error as? JarvisWebSocketError, case .unauthorized = failure {
            wantsConnection = false; reconnectAt = nil; failWaiting(error); stopTimerIfIdle(); return
        }
        if reconnectAttempts >= config.maximumReconnectAttempts {
            wantsConnection = false; reconnectAt = nil
            let error = JarvisWebSocketError.reconnectLimitReached
            failWaiting(error); report(error); stopTimerIfIdle()
        } else { reconnectAt = now + config.reconnectDelay }
    }
    private func flush() {
        guard connected, !suppressed, active == nil, !waiting.isEmpty, let socket = socket else { return }
        active = waiting.removeFirst()
        let current = token
        let id = active!.id
        writeDeadline = now + config.writeTimeout
        socket.write(text: active!.wire) { [weak self] in
            self?.queue.async { [weak self] in
                guard let self = self, self.token == current, self.active?.id == id else { return }
                self.lastActivity = self.now
                self.socket?.write(text: "{\"type\":\"audio_end\"}") { [weak self] in
                    self?.queue.async { [weak self] in
                        guard let self = self, self.token == current, self.active?.id == id else { return }
                        self.writeDeadline = nil; self.lastActivity = self.now
                    }
                }
            }
        }
    }
    private func parse(_ data: Data) {
        do {
            guard data.count <= config.maximumIncomingBytes else { throw JarvisWebSocketError.protocolViolation("Message too large.") }
            let reply = try JSONDecoder().decode(WireMessage.self, from: data)
            if ["suppressed", "selected", "resumed"].contains(reply.type) {
                suppressed = reply.type == "suppressed"
                if suppressed {
                    writeDeadline = nil
                    failActive(JarvisWebSocketError.server("suppressed"))
                    failWaiting(JarvisWebSocketError.server("suppressed"))
                }
                let selected = !suppressed, handler = selectionHandler
                callbackQueue.async { handler?(selected) }
                return
            }
            if reply.type == "hello" || reply.type == "device_state" { return }
            if reply.type == "interrupt" {
                let handler = interruptHandler
                callbackQueue.async { handler?() }
                return
            }
            if reply.type == "error" { throw JarvisWebSocketError.server(reply.code ?? "unspecified") }
            guard reply.type == "text" || reply.type == "audio", let generation = reply.generation, generation >= 0 else {
                throw JarvisWebSocketError.protocolViolation("Missing type/generation.")
            }
            guard generation >= highestGeneration, generation > completedGeneration else { return }
            if let current = active?.generation, generation != current {
                throw JarvisWebSocketError.protocolViolation("Unexpected generation while an utterance is active.")
            }
            highestGeneration = generation
            active?.generation = generation
            if reply.type == "text" {
                guard let text = reply.text, reply.role == "user" || reply.role == "assistant" else {
                    throw JarvisWebSocketError.protocolViolation("Missing text/role.")
                }
                if reply.role == "assistant" {
                    active?.text = text
                    let handler = textHandler
                    callbackQueue.async { handler?(text) }
                } else {
                    let handler = transcriptHandler
                    callbackQueue.async { handler?(text) }
                }
            } else {
                guard reply.format == "mp3", let encoded = reply.data, let final = reply.final,
                      let audio = Data(base64Encoded: encoded), final || !audio.isEmpty else {
                    throw JarvisWebSocketError.protocolViolation("Invalid MP3 Base64/final.")
                }
                guard audio.count <= config.maximumResponseAudioBytes - (active?.audio.count ?? 0) else {
                    throw JarvisWebSocketError.protocolViolation("Response audio limit exceeded.")
                }
                if !audio.isEmpty {
                    active?.audio.append(audio)
                    let handler = audioHandler
                    callbackQueue.async { handler?(audio) }
                }
                if final {
                    completedGeneration = generation
                    let handler = streamEndHandler
                    callbackQueue.async { handler?() }
                    if let item = active {
                        active = nil; writeDeadline = nil; queuedBytes -= item.bytes
                        let response = ResponseData(text: item.text, audio: item.audio, audioFormat: "mp3", generation: item.generation)
                        callbackQueue.async { item.promise(.success(response)) }
                    }
                    flush()
                }
            }
        } catch {
            // No correlation IDs: after an invalid/error reply, reset the session rather
            // than allowing a late response to be misattributed to the next queued upload.
            failActive(error)
            report(error)
            lost(error)
        }
    }
    private func failActive(_ error: Error) {
        guard let item = active else { return }
        active = nil; queuedBytes -= item.bytes
        callbackQueue.async { item.promise(.failure(error)) }
    }
    private func failWaiting(_ error: Error) {
        let items = waiting
        waiting.removeAll()
        for item in items { queuedBytes -= item.bytes; callbackQueue.async { item.promise(.failure(error)) } }
    }
    private func reject(_ promise: @escaping Promise, _ error: Error) {
        callbackQueue.async { promise(.failure(error)) }; report(error)
    }
    private func report(_ error: Error) {
        let handler = errorHandler
        callbackQueue.async { handler?(error) }
    }
    private func startTimer() {
        guard timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        let step = max(0.005, min(1, config.reconnectDelay / 2, config.heartbeatInterval / 2,
                                  config.pongTimeout / 2, config.responseTimeout / 2, config.writeTimeout / 2,
                                  config.connectionTimeout / 2))
        timer.schedule(deadline: .now() + step, repeating: step)
        timer.setEventHandler { [weak self] in self?.tick() }
        self.timer = timer; timer.resume()
    }
    private func stopTimerIfIdle() {
        if !wantsConnection && waiting.isEmpty && active == nil { timer?.cancel(); timer = nil }
    }
    private func tick() {
        let time = now
        let expired = waiting.filter { $0.deadline <= time }
        waiting.removeAll { $0.deadline <= time }
        for item in expired {
            queuedBytes -= item.bytes
            callbackQueue.async { item.promise(.failure(JarvisWebSocketError.requestTimedOut)) }
        }
        if let item = active, item.deadline <= time {
            failActive(JarvisWebSocketError.requestTimedOut)
            lost(JarvisWebSocketError.requestTimedOut)
        }
        if let deadline = connectionDeadline, time >= deadline { lost(JarvisWebSocketError.transport("Connect timed out.")) }
        if let deadline = writeDeadline, time >= deadline { lost(JarvisWebSocketError.heartbeatTimedOut) }
        if wantsConnection, let deadline = reconnectAt, time >= deadline {
            reconnectAttempts += 1; open()
        }
        if connected {
            if let deadline = pingDeadline, time >= deadline {
                lost(JarvisWebSocketError.heartbeatTimedOut)
            } else if pingDeadline == nil, !pingWriting, time - lastActivity >= config.heartbeatInterval {
                pingWriting = true
                pingDeadline = time + config.writeTimeout // Also bounds a ping stuck in Starscream's write queue.
                let current = token
                socket?.ping { [weak self] in
                    self?.queue.async { [weak self] in
                        guard let self = self, self.token == current, self.pingWriting else { return }
                        self.pingWriting = false
                        self.pingDeadline = self.now + self.config.pongTimeout
                    }
                }
            }
        }
        stopTimerIfIdle()
    }
}
private struct WireMessage: Decodable {
    let type: String
    let text: String?
    let role: String?
    let data: String?
    let format: String?
    let final: Bool?
    let generation: Int?
    let code: String?
}

import Foundation
import Combine
@testable import JarvisWebSocket

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    precondition(value(), message)
}
func eventually(_ message: String, timeout: TimeInterval = 2, _ condition: () -> Bool) {
    let deadline = ProcessInfo.processInfo.systemUptime + timeout
    while !condition(), ProcessInfo.processInfo.systemUptime < deadline { Thread.sleep(forTimeInterval: 0.005) }
    check(condition(), message)
}
final class TestTransport: SocketTransport {
    var onEvent: ((SocketEvent) -> Void)?
    private let lock = NSLock()
    private var messages: [String] = []
    private var pings = 0
    var sent: [String] { lock.lock(); defer { lock.unlock() }; return messages }
    var pingCount: Int { lock.lock(); defer { lock.unlock() }; return pings }
    var started = false
    var autoComplete = true
    func connect() { started = true }
    func disconnect() {}
    func write(text: String, completion: @escaping () -> Void) {
        lock.lock(); messages.append(text); lock.unlock()
        if autoComplete { completion() }
    }
    func ping(completion: @escaping () -> Void) { lock.lock(); pings += 1; lock.unlock(); completion() }
    func emit(_ event: SocketEvent) { onEvent?(event) }
}
func object(_ text: String) throws -> [String: Any] {
    try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
}
func testDuplicateFinalCannotCompleteNextUpload() throws {
    let socket = TestTransport()
    let client = try JarvisWebSocketClient(configuration: .init(), callbackQueue: .global(), transportFactory: { _, _ in socket })
    let first = DispatchSemaphore(value: 0), second = DispatchSemaphore(value: 0)
    let one = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { _ in }, receiveValue: { _ in first.signal() })
    let two = client.sendAudio(Data([1, 0])).sink(receiveCompletion: { _ in }, receiveValue: { _ in second.signal() })
    client.connect(); socket.emit(.connected)
    let final = "{\"type\":\"audio\",\"data\":\"\",\"format\":\"mp3\",\"generation\":1,\"final\":true}"
    socket.emit(.text(final))
    check(first.wait(timeout: .now() + 1) == .success, "first completes")
    socket.emit(.text(final))
    check(second.wait(timeout: .now() + 0.05) == .timedOut, "duplicate final must not complete next upload")
    socket.emit(.text(final.replacingOccurrences(of: "\"generation\":1", with: "\"generation\":2")))
    check(second.wait(timeout: .now() + 1) == .success, "next generation completes")
    client.disconnect(); withExtendedLifetime([one, two]) {}
}

func testDisconnectFailsInflightButPreservesUnsentAudio() throws {
    var config = JarvisWebSocketConfiguration(); config.reconnectDelay = 0.03
    let lock = NSLock(); var sockets: [TestTransport] = []
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in
        let socket = TestTransport(); lock.lock(); sockets.append(socket); lock.unlock(); return socket
    })
    let unknown = DispatchSemaphore(value: 0)
    let one = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.outcomeUnknown = error { unknown.signal() }
    }, receiveValue: { _ in preconditionFailure("lost upload succeeded") })
    let two = client.sendAudio(Data([1, 0])).sink(receiveCompletion: { _ in }, receiveValue: { _ in })
    client.connect(); let first = sockets[0]; first.emit(.connected)
    eventually("first write complete") { first.sent.count == 2 }
    first.emit(.disconnected(nil))
    check(unknown.wait(timeout: .now() + 1) == .success, "unknown outcome is explicit")
    eventually("reconnect") { lock.lock(); defer { lock.unlock() }; return sockets.count == 2 }
    lock.lock(); let second = sockets[1]; lock.unlock()
    second.emit(.connected)
    eventually("only unsent audio flushed") { second.sent.count == 2 }
    let wire = try object(second.sent[0])
    check(wire["data"] as? String == Data([1, 0]).base64EncodedString(), "never replay first operation")
    client.disconnect(); withExtendedLifetime([one, two]) {}
}

func testHeartbeatPongAndMissingPongReconnect() throws {
    var config = JarvisWebSocketConfiguration()
    config.heartbeatInterval = 0.04; config.pongTimeout = 0.05; config.reconnectDelay = 0.03
    let socket = TestTransport()
    let failed = DispatchSemaphore(value: 0)
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in socket })
    client.onError = { error in if case JarvisWebSocketError.heartbeatTimedOut = error { failed.signal() } }
    client.connect(); socket.emit(.connected)
    eventually("idle ping") { socket.pingCount == 1 }
    socket.emit(.pong)
    check(client.isConnected, "pong keeps session")
    eventually("next idle ping") { socket.pingCount == 2 }
    check(failed.wait(timeout: .now() + 1) == .success, "missing pong detected")
    client.disconnect()
}

func testReconnectBudgetIsHardLimitEvenWithFlappingHandshakes() throws {
    var config = JarvisWebSocketConfiguration(); config.reconnectDelay = 0.02; config.maximumReconnectAttempts = 2
    let socket = TestTransport(); let exhausted = DispatchSemaphore(value: 0)
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in socket })
    client.onError = { error in if case JarvisWebSocketError.reconnectLimitReached = error { exhausted.signal() } }
    client.connect(); socket.emit(.connected); socket.emit(.disconnected(nil))
    Thread.sleep(forTimeInterval: 0.06); socket.emit(.connected); check(client.isConnected, "first retry connected")
    socket.emit(.disconnected(nil)); Thread.sleep(forTimeInterval: 0.06); socket.emit(.connected)
    check(client.isConnected, "second retry connected"); socket.emit(.disconnected(nil))
    check(exhausted.wait(timeout: .now() + 1) == .success, "bounded flapping")
    client.disconnect()
}

func testInvalidAudioQueueLimitsAndManualDisconnect() throws {
    var config = JarvisWebSocketConfiguration(); config.maximumQueuedMessages = 1
    let socket = TestTransport()
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in socket })
    let invalid = DispatchSemaphore(value: 0), full = DispatchSemaphore(value: 0), cancelled = DispatchSemaphore(value: 0)
    let zero = client.sendAudio(Data([1])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.invalidAudio = error { invalid.signal() }
    }, receiveValue: { _ in })
    let one = client.sendAudio(Data([1, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.disconnected = error { cancelled.signal() }
    }, receiveValue: { _ in })
    let two = client.sendAudio(Data([2, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.queueFull = error { full.signal() }
    }, receiveValue: { _ in })
    check(invalid.wait(timeout: .now() + 1) == .success, "odd PCM rejected")
    check(full.wait(timeout: .now() + 1) == .success, "bounded queue")
    client.disconnect()
    check(cancelled.wait(timeout: .now() + 1) == .success, "disconnect fails queued Future")
    withExtendedLifetime([zero, one, two]) {}
    config.url = URL(string: "ws://example.com")!
    do { _ = try JarvisWebSocketClient(configuration: config); preconditionFailure("insecure URL accepted") }
    catch JarvisWebSocketError.invalidConfiguration {}
}

func testTimeoutAndMalformedAudioFailFutures() throws {
    var config = JarvisWebSocketConfiguration(); config.responseTimeout = 0.05
    let socket = TestTransport()
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in socket })
    let expired = DispatchSemaphore(value: 0)
    let one = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.requestTimedOut = error { expired.signal() }
    }, receiveValue: { _ in })
    check(expired.wait(timeout: .now() + 1) == .success, "offline timeout")
    let invalid = DispatchSemaphore(value: 0)
    let two = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.protocolViolation = error { invalid.signal() }
    }, receiveValue: { _ in })
    client.connect(); socket.emit(.connected)
    socket.emit(.text("{\"type\":\"audio\",\"data\":\"bad!\",\"format\":\"mp3\",\"generation\":1,\"final\":false}"))
    check(invalid.wait(timeout: .now() + 1) == .success, "malformed audio fails active Future")
    client.disconnect(); withExtendedLifetime([one, two]) {}
}

func testWriteStallCannotBlockForever() throws {
    var config = JarvisWebSocketConfiguration(); config.writeTimeout = 0.04
    let socket = TestTransport(); socket.autoComplete = false
    let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in socket })
    let failed = DispatchSemaphore(value: 0)
    let subscription = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
        if case .failure(let error) = result, case JarvisWebSocketError.outcomeUnknown = error { failed.signal() }
    }, receiveValue: { _ in })
    client.connect(); socket.emit(.connected)
    check(failed.wait(timeout: .now() + 1) == .success, "stalled write disconnects")
    client.disconnect(); withExtendedLifetime(subscription) {}
}

func testGatewayPCMAndStreamingReply() throws {
    let socket = TestTransport()
    let client = try JarvisWebSocketClient(configuration: .init(), callbackQueue: .global(),
                                           transportFactory: { _, _ in socket })
    let done = DispatchSemaphore(value: 0)
    let pcm = Data([0, 0, 255, 127])
    let subscription = client.sendAudio(pcm).sink(receiveCompletion: { completion in
        if case .failure(let error) = completion { preconditionFailure("\(error)") }
    }, receiveValue: { response in
        check(response.text == "reply", "assistant text, not user transcription")
        check(response.audio == Data([1, 2, 3]), "MP3 chunks accumulated")
        check(response.audioFormat == "mp3", "codec metadata")
        done.signal()
    })
    check(socket.sent.isEmpty, "offline queued")
    client.connect(); socket.emit(.connected)
    check(client.isConnected, "connected")
    eventually("audio_end sent") { socket.sent.count == 2 }
    let wire = try object(socket.sent[0])
    check(wire["data"] as? String == pcm.base64EncodedString(), "gateway uses data key")
    check(wire["format"] as? String == "pcm_s16le", "PCM format")
    check(wire["sample_rate"] as? Int == 16000, "sample rate")
    let end = try object(socket.sent[1])
    check(end["type"] as? String == "audio_end", "utterance boundary")
    socket.emit(.text("{\"type\":\"text\",\"role\":\"user\",\"text\":\"question\",\"generation\":1,\"final\":true}"))
    socket.emit(.text("{\"type\":\"text\",\"role\":\"assistant\",\"text\":\"reply\",\"generation\":1,\"final\":true}"))
    socket.emit(.text("{\"type\":\"audio\",\"data\":\"AQID\",\"format\":\"mp3\",\"generation\":1,\"final\":false}"))
    check(done.wait(timeout: .now() + 0.03) == .timedOut, "waits for final MP3 marker")
    socket.emit(.text("{\"type\":\"audio\",\"data\":\"\",\"format\":\"mp3\",\"generation\":1,\"final\":true}"))
    check(done.wait(timeout: .now() + 2) == .success, "Future resolves on empty final chunk")
    client.disconnect(); withExtendedLifetime(subscription) {}
}

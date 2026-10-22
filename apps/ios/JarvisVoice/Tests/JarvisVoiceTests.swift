import XCTest
@testable import JarvisVoice

final class JarvisVoiceTests: XCTestCase {
    func testGatewayFallbackOrderAndCycleReset() {
        GatewayEndpoints.custom = nil
        var endpoints = GatewayEndpoints()
        XCTAssertEqual(endpoints.current, "ws://192.168.1.5:8765")
        endpoints.failed()
        XCTAssertEqual(endpoints.current, "ws://100.64.0.1:8765")
        endpoints.failed()
        XCTAssertEqual(endpoints.current, GatewayEndpoints.addresses[0])
        endpoints.failed(); endpoints.reset()
        XCTAssertEqual(endpoints.current, GatewayEndpoints.addresses[0])
        for endpoint in GatewayEndpoints.addresses { XCTAssertNotNil(EndpointPolicy.validate(endpoint)) }
        XCTAssertNil(EndpointPolicy.validate("ws://192.168.1.6:8765"))
        GatewayEndpoints.custom = "ws://100.80.1.2:8765"
        XCTAssertEqual(GatewayEndpoints().current, "ws://100.80.1.2:8765")
        GatewayEndpoints.custom = nil
    }

    func testGatewayTokenImportDeletesOnlyAfterSuccessfulSave() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("gateway-token")
        try Data("unit-test-token\n".utf8).write(to: file)
        var stored = ""
        XCTAssertTrue(try GatewayTokenImport.run(file: file) { stored = $0 })
        XCTAssertEqual(stored, "unit-test-token")
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertFalse(try GatewayTokenImport.run(file: file) { _ in XCTFail("missing file should not save") })
        try Data("unit-test-token".utf8).write(to: file)
        enum SaveFailure: Error { case denied }
        XCTAssertThrowsError(try GatewayTokenImport.run(file: file) { _ in throw SaveFailure.denied })
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
    }

    func testWireEncodingAndDecoding() throws {
        let hello = VoiceWire.hello(deviceID: "iphone-unit", state: ["headphones": false])
        let bytes = try JSONSerialization.data(withJSONObject: hello)
        let decoded = try XCTUnwrap(JSONSerialization.jsonObject(with: bytes) as? [String: Any])
        XCTAssertEqual(decoded["device_id"] as? String, "iphone-unit")
        XCTAssertEqual(decoded["sample_rate"] as? Int, 16000)
        let pcm = PCM16.encode([-1, 0, 1, .nan])
        XCTAssertEqual(Array(pcm), [0, 128, 0, 0, 255, 127, 0, 0])
        let audio = VoiceWire.audio(pcm)
        XCTAssertEqual(Data(base64Encoded: try XCTUnwrap(audio["data"] as? String)), pcm)
        guard case .audio(let data, let generation, let final) = VoiceWire.decode([
            "type": "audio", "format": "mp3", "data": "AQID", "generation": 2, "final": true
        ]) else { return XCTFail("valid MP3 rejected") }
        XCTAssertEqual(data, Data([1, 2, 3]))
        XCTAssertEqual(generation, 2)
        XCTAssertTrue(final)
    }

    func testVoiceGateAndBoundedUtterance() {
        let silent = [Float](repeating: 0, count: 320)
        let voice = [Float](repeating: 0.1, count: 320)
        var gate = SpeechGate(threshold: 0.02, endSilenceFrames: 2, preRollFrames: 2, maxFrames: 6)
        XCTAssertFalse(gate.process(silent).started)
        let start = gate.process(voice)
        XCTAssertTrue(start.started)
        XCTAssertEqual(start.frames.count, 2)
        XCTAssertFalse(gate.process(silent).ended)
        XCTAssertTrue(gate.process(silent).ended)
        XCTAssertTrue(gate.process(silent).frames.isEmpty)
        XCTAssertFalse(gate.process([.nan, .infinity]).started)
    }

    func testUnknownDeviceStatesAreNull() throws {
        let state = DeviceSnapshot(batteryLevel: nil, charging: false, headphonesConnected: true,
                                   foreground: false, faceDown: nil, audioOutputs: ["BluetoothHFP"],
                                   protectedDataAvailable: false, observedAt: 123).wire
        XCTAssertTrue(state["battery_level"] is NSNull)
        XCTAssertTrue(state["headphones_worn"] is NSNull)
        XCTAssertTrue(state["screen_on"] is NSNull)
        XCTAssertTrue(state["face_down"] is NSNull)
        XCTAssertEqual(state["headphones_connected"] as? Bool, true)
        _ = try JSONSerialization.data(withJSONObject: state)
    }

    func testPlaybackGenerationAndAssembly() {
        var accumulator = PlaybackAccumulator()
        XCTAssertNil(accumulator.append(Data([1, 2]), generation: 2, final: false))
        XCTAssertEqual(accumulator.append(Data([3]), generation: 2, final: true), Data([1, 2, 3]))
        accumulator.interrupt()
        XCTAssertNil(accumulator.append(Data([4]), generation: 2, final: true))
        XCTAssertEqual(accumulator.append(Data([5]), generation: 3, final: true), Data([5]))
    }

    func testEndpointPrivacyPolicy() {
        XCTAssertNotNil(EndpointPolicy.validate("ws://100.64.0.1:8765"))
        XCTAssertNotNil(EndpointPolicy.validate("ws://127.0.0.1:8765"))
        XCTAssertNotNil(EndpointPolicy.validate("wss://voice.example.com"))
        XCTAssertNil(EndpointPolicy.validate("ws://example.com:8765"))
        XCTAssertNil(EndpointPolicy.validate("ws://user:pass@100.80.1.2:8765"))
        XCTAssertNil(EndpointPolicy.validate("http://100.80.1.2:8765"))
    }

    func testReconnectDoesNotSendPartialUtterance() {
        var state = UtteranceForwarding()
        XCTAssertTrue(state.consume(started: true, ended: false, connected: true, selected: true))
        XCTAssertFalse(state.consume(started: false, ended: false, connected: false, selected: true))
        XCTAssertFalse(state.consume(started: false, ended: true, connected: true, selected: true))
        XCTAssertTrue(state.consume(started: true, ended: true, connected: true, selected: true))
        XCTAssertFalse(state.consume(started: true, ended: false, connected: true, selected: false))
    }

    func testMalformedAudioRejected() {
        for payload: [String: Any] in [
            ["type": "audio", "format": "mp3", "data": "not-base64"],
            ["type": "audio", "format": "mp3", "data": "AQID", "generation": true, "final": true],
            ["type": "audio", "format": "mp3", "data": "AQID", "generation": -1, "final": true],
            ["type": "audio", "format": "wav", "data": "AQID", "generation": 1, "final": true]
        ] {
            guard case .invalid = VoiceWire.decode(payload) else { return XCTFail("malformed audio accepted") }
        }
    }

    @MainActor
    func testAppIntentIsForegroundAndVoiceSessionIsIdle() {
        XCTAssertTrue(StartJarvisIntent.openAppWhenRun)
        XCTAssertFalse(VoiceSession.shared.active)
        XCTAssertFalse(VoiceSession.shared.connected)
        XCTAssertEqual(Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String], ["audio"])
        XCTAssertNotNil(Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription"))
    }
}

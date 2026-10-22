import XCTest
import Combine
@testable import JarvisWebSocket

/// Added integration regressions; original module XCTest sources remain intact.
final class MacControlTests: XCTestCase {
    func testSuppressionCancelsInflightAndRejectsNewAudioUntilSelected() throws {
        var config = JarvisWebSocketConfiguration(); config.writeTimeout = 0.05
        let transport = TestTransport()
        let client = try JarvisWebSocketClient(configuration: config, callbackQueue: .global(), transportFactory: { _, _ in transport })
        client.connect(); transport.emit(.connected)
        transport.autoComplete = false
        let cancelled = expectation(description: "suppressed in-flight future")
        let one = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
            if case .failure = result { cancelled.fulfill() }
        }, receiveValue: { _ in XCTFail("suppressed response succeeded") })
        transport.emit(.text("{\"type\":\"suppressed\"}"))
        wait(for: [cancelled], timeout: 1)
        let rejected = expectation(description: "new audio rejected")
        let two = client.sendAudio(Data([0, 0])).sink(receiveCompletion: { result in
            if case .failure = result { rejected.fulfill() }
        }, receiveValue: { _ in XCTFail("suppressed audio accepted") })
        wait(for: [rejected], timeout: 1)
        Thread.sleep(forTimeInterval: 0.12)
        XCTAssertTrue(client.isConnected, "suppression must clear stale write deadline")
        XCTAssertEqual(transport.sent.count, 1)
        transport.autoComplete = true
        transport.emit(.text("{\"type\":\"selected\"}"))
        let done = expectation(description: "next FULL utterance")
        let three = client.sendAudio(Data([1, 0])).sink(receiveCompletion: { _ in }, receiveValue: { _ in done.fulfill() })
        eventually("full utterance sent") { transport.sent.count == 3 }
        transport.emit(.text("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"\",\"final\":true,\"generation\":2}"))
        wait(for: [done], timeout: 1)
        client.disconnect(); withExtendedLifetime([one,two,three]) {}
    }
    func testDeviceControlsNeverAcceptTextCommands() throws {
        let transport = TestTransport()
        let client = try JarvisWebSocketClient(configuration: .init(), callbackQueue: .global(), transportFactory: { _, _ in transport })
        client.connect(); transport.emit(.connected)
        client.sendControl(["type":"hello","device_type":"mac"])
        client.sendControl(["type":"device_state","state":[:]])
        client.sendControl(["type":"text","text":"must not reach brain"])
        XCTAssertEqual(transport.sent.count, 2)
        XCTAssertEqual(try object(transport.sent[0])["device_type"] as? String, "mac")
        client.disconnect()
    }
}

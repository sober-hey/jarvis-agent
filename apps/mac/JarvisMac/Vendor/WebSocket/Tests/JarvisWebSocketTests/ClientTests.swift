import XCTest
@testable import JarvisWebSocket

final class ClientTests: XCTestCase {
    func testGatewayProtocol() throws { try testGatewayPCMAndStreamingReply() }
    func testDuplicateFinalGuard() throws { try testDuplicateFinalCannotCompleteNextUpload() }
    func testNoUnsafeReplay() throws { try testDisconnectFailsInflightButPreservesUnsentAudio() }
    func testIdlePing() throws { try testHeartbeatPongAndMissingPongReconnect() }
    func testBoundedReconnects() throws { try testReconnectBudgetIsHardLimitEvenWithFlappingHandshakes() }
    func testInputAndQueueLimits() throws { try testInvalidAudioQueueLimitsAndManualDisconnect() }
    func testTimeoutAndInvalidReply() throws { try testTimeoutAndMalformedAudioFailFutures() }
    func testStalledWrite() throws { try testWriteStallCannotBlockForever() }
}

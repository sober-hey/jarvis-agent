import XCTest
@testable import JarvisCore

final class RuntimeStateTests: XCTestCase {
    func testListeningToggleAndActivityReset() throws {
        let state = RuntimeState()
        XCTAssertFalse(state.isListening)
        XCTAssertEqual(state.indicator, .off)
        state.setListening(true)
        XCTAssertEqual(state.indicator, .listening)
        try state.setActivity(.recording)
        XCTAssertEqual(state.indicator, .recording)
        try state.setActivity(.processing)
        XCTAssertEqual(state.indicator, .processing)
        try state.setActivity(.playing)
        XCTAssertEqual(state.indicator, .playing)
        state.setListening(false)
        XCTAssertEqual(state.indicator, .off)
        state.setListening(true)
        XCTAssertEqual(state.indicator, .listening)
    }

    func testActivityRequiresListeningAndErrorRecoveryIsExplicit() throws {
        let state = RuntimeState()
        XCTAssertThrowsError(try state.setActivity(.recording))
        XCTAssertEqual(state.indicator, .off)
        state.setListening(true)
        state.report(.permissionDenied("microphone"))
        XCTAssertEqual(state.indicator, .failed)
        XCTAssertNotNil(state.lastError)
        state.clearError()
        XCTAssertEqual(state.indicator, .listening)
        XCTAssertNil(state.lastError)
    }
}

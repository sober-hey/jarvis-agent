import XCTest
@testable import JarvisWatch

final class WatchCoreTests: XCTestCase {
    func testDetectorProtocolAndRecordedReplay() throws {
        let fixture = try XCTUnwrap(Bundle(for: Self.self).url(
            forResource: "wisdm_1600_clapping_excerpt", withExtension: "txt"))
        try WatchCoreSuite.run(fixture: fixture) { name, passed in
            XCTAssertTrue(passed, name)
        }
    }
}

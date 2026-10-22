import XCTest

extension RuntimeStateTests {
    static let allTests = [
        ("testListeningToggleAndActivityReset", testListeningToggleAndActivityReset),
        ("testActivityRequiresListeningAndErrorRecoveryIsExplicit", testActivityRequiresListeningAndErrorRecoveryIsExplicit)
    ]
}
XCTMain([testCase(RuntimeStateTests.allTests)])

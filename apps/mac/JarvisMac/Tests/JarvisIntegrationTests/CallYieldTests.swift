import XCTest
@testable import JarvisIntegration

/// The microphone must step aside for calls. These tests pin the decision, not
/// the AppKit plumbing: the app list is injected so no real call is needed.
final class CallYieldTests: XCTestCase {
    func testNoCallAppsMeansKeepListening() {
        XCTAssertEqual(CallYieldMonitor.callApps(in: ["com.apple.finder", "com.jarvis.macos"]), [])
    }

    func testFaceTimeCountsAsACall() {
        XCTAssertEqual(CallYieldMonitor.callApps(in: ["com.apple.FaceTime", "com.apple.finder"]),
                       ["com.apple.FaceTime"])
        XCTAssertEqual(CallYieldMonitor.reason(for: "com.apple.FaceTime"), "FaceTime 通话中")
    }

    func testPhoneAppsCountAsACall() {
        for bundle in ["com.apple.Phone", "com.apple.MobilePhone"] {
            XCTAssertEqual(CallYieldMonitor.callApps(in: [bundle]), [bundle])
            XCTAssertEqual(CallYieldMonitor.reason(for: bundle), "电话通话中")
        }
    }

    func testMonitorYieldsOnceAndResumesOnce() {
        let monitor = CallYieldMonitor()
        var events: [String] = []
        monitor.onCallStarted = { events.append("start:" + $0) }
        monitor.onCallEnded = { events.append("end") }

        monitor.evaluate(running: ["com.apple.finder"])
        XCTAssertTrue(events.isEmpty)
        XCTAssertFalse(monitor.isYielded)

        monitor.evaluate(running: ["com.apple.FaceTime"])
        monitor.evaluate(running: ["com.apple.FaceTime"])  // still in the same call
        XCTAssertEqual(events, ["start:FaceTime 通话中"])
        XCTAssertTrue(monitor.isYielded)

        monitor.evaluate(running: ["com.apple.finder"])
        monitor.evaluate(running: ["com.apple.finder"])
        XCTAssertEqual(events, ["start:FaceTime 通话中", "end"])
        XCTAssertFalse(monitor.isYielded)
    }
}

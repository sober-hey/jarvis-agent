import XCTest
import AppKit
import ApplicationServices

final class ModuleXcodeTests: XCTestCase {
    @MainActor
    func testRealMP3DecodeAndImmediateActivationInterrupt() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "fixture", withExtension: "mp3"))
        let p = AudioPlayer(); p.volume = 0
        let old = p.activate()
        try p.play(data: Data(contentsOf: url), activation: old)
        XCTAssertEqual(p.snapshot.phase, .playing)
        XCTAssertGreaterThan(p.snapshot.duration, 0.5)
        _ = p.activate()
        XCTAssertEqual(p.snapshot.phase, .idle)
        XCTAssertFalse(try p.play(data: Data(contentsOf: url), activation: old))
    }
    @MainActor
    func testPermissionDeniedIsFriendlyAndPrecedesAX() {
        let h = AccessibilityHelper(trustCheck: { false })
        XCTAssertFalse(h.isPermissionGranted)
        XCTAssertThrowsError(try h.captureFrontmostWindow()) { error in
            XCTAssertEqual(error as? AccessibilityHelper.Failure, .permissionDenied)
            XCTAssertTrue(error.localizedDescription.contains("系统设置"))
        }
    }
    @MainActor
    func testMenuProgressAndDeniedSettingsGuidance() {
        _ = NSApplication.shared
        let p = AudioPlayer(), h = AccessibilityHelper(trustCheck: { false })
        let presenter = PlaybackMenuPresenter(player: p, accessibility: h)
        let menu = NSMenu(); presenter.appendControls(to: menu)
        XCTAssertTrue(menu.items.contains { $0.title.contains("未授权") })
        let display = PlaybackMenuPresenter.display(for: .init(phase: .playing, currentTime: 30, duration: 120, errorMessage: nil))
        XCTAssertEqual(display.title, "▶ 25%")
    }
    /// Explicitly requested by owner: exercise asynchronous macOS system prompt,
    /// never grant/modify TCC or interpret current denial as future approval.
    @MainActor
    func testExplicitSystemPermissionPrompt() {
        let h = AccessibilityHelper()
        let current = h.requestPermissionFromUserAction()
        print("XCODE_PERMISSION_PROMPT_CALLED; TRUST_AT_RETURN=\(current); LIVE_INTERACTION=NOT_RUN")
        XCTAssertEqual(h.isPermissionGranted, AXIsProcessTrusted())
    }
}

import Foundation
import AppKit
import ApplicationServices

@main
@MainActor
struct AccessibilityChecks {
    static var passed = 0
    static var failed = 0
    static func check(_ name: String, _ body: () throws -> Void) {
        do { try body(); passed += 1; print("PASS \(name)") }
        catch { failed += 1; print("FAIL \(name): \(error)") }
    }
    enum Assertion: Error { case failed(String) }
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        if !condition() { throw Assertion.failed(message) }
    }
    static func expectFailure(_ expected: AccessibilityHelper.Failure, _ body: () throws -> Void) throws {
        do { try body(); throw Assertion.failed("expected \(expected)") }
        catch let error as AccessibilityHelper.Failure { try expect(error == expected, "wrong failure \(error)") }
    }
    @MainActor
    final class Fixture {
        var identity = AccessibilityHelper.ProcessIdentity(pid: 741, name: "Fixture", bundleIdentifier: "test.fixture", launchDate: Date(timeIntervalSince1970: 100))
        var window = AXUIElementCreateApplication(741)
        var focused: AXUIElement? = AXUIElementCreateApplication(741)
        var role: String? = kAXTextFieldRole
        var subrole: String? = "AXStandardTextField"
        var secure = false
        var posts: [(pid_t, [UniChar])] = []
        var afterPost: (() -> Void)?
        func helper() -> AccessibilityHelper {
            var backend = AccessibilityHelper.Backend.live
            backend.frontmost = { self.identity }
            backend.window = { _ in self.window }
            backend.focusedElement = { _ in self.focused }
            backend.stringAttribute = { _, name in
                switch name {
                case kAXTitleAttribute: return "Fixture title"
                case kAXRoleAttribute: return self.role
                case kAXSubroleAttribute: return self.subrole
                default: throw Assertion.failed("unexpected read \(name)")
                }
            }
            backend.secureInputEnabled = { self.secure }
            backend.canPostEvents = { true }
            backend.postUnicode = { pid, units in self.posts.append((pid, units)); self.afterPost?() }
            return AccessibilityHelper(trustCheck: { true }, backend: backend)
        }
    }
    static func main() {
        check("permission denial precedes all AX reads") {
            let helper = AccessibilityHelper(trustCheck: { false })
            try expect(!helper.isPermissionGranted, "injected denial ignored")
            try expectFailure(.permissionDenied) { _ = try helper.captureFrontmostWindow() }
        }
        check("snapshot contains only captured app and window title") {
            var backend = AccessibilityHelper.Backend.live
            let identity = AccessibilityHelper.ProcessIdentity(pid: 741, name: "Fixture", bundleIdentifier: "test.fixture", launchDate: Date(timeIntervalSince1970: 100))
            let window = AXUIElementCreateApplication(741)
            backend.frontmost = { identity }
            backend.window = { _ in window }
            backend.focusedElement = { _ in nil }
            backend.stringAttribute = { _, attribute in
                if attribute == kAXTitleAttribute { return "Owned test window" }
                throw Assertion.failed("unexpected content read: \(attribute)")
            }
            let helper = AccessibilityHelper(trustCheck: { true }, backend: backend)
            let snapshot = try helper.captureFrontmostWindow()
            try expect(snapshot.title == "Owned test window", "incorrect title")
            try expect(snapshot.target.pid == 741, "incorrect target pid")
            try expect(snapshot.applicationName == "Fixture", "incorrect app name")
        }
        check("typing rejects changed PID without posting input") {
            let fixture = Fixture()
            let helper = fixture.helper()
            let target = try helper.captureFrontmostWindow().target
            fixture.identity = .init(pid: 742, name: "Other", bundleIdentifier: nil, launchDate: Date())
            try expectFailure(.focusChanged) { try helper.typeText("禁止", into: target) }
            try expect(fixture.posts.isEmpty, "input escaped focus guard")
        }
        check("secure fields and secure event input reject Unicode") {
            let f = Fixture(); let h = f.helper(); let t = try h.captureFrontmostWindow().target
            f.subrole = kAXSecureTextFieldSubrole
            try expectFailure(.secureInput) { try h.typeText("secret", into: t) }
            f.subrole = "AXStandardTextField"; f.secure = true
            try expectFailure(.secureInput) { try h.typeText("secret", into: t) }
            try expect(f.posts.isEmpty, "secure input posted")
        }
        check("Unicode preserves surrogate pairs and PID destination") {
            let f = Fixture(); let h = f.helper(); let t = try h.captureFrontmostWindow().target
            let text = String(repeating: "a", count: 19) + "😀中文"
            try h.typeText(text, into: t)
            try expect(f.posts.flatMap { $0.1 } == Array(text.utf16), "unicode altered")
            try expect(f.posts.allSatisfy { $0.0 == 741 && $0.1.count <= 20 }, "wrong routing")
        }
        check("TextEdit-style text area without optional subrole accepts hello") {
            let f = Fixture(); f.role = kAXTextAreaRole; f.subrole = nil
            let h = f.helper(); let t = try h.captureFrontmostWindow().target
            try h.typeText("hello", into: t)
            try expect(f.posts.flatMap { $0.1 } == Array("hello".utf16), "TextEdit-style input refused")
        }
        check("focus change between chunks stops subsequent input") {
            let f = Fixture(); let h = f.helper(); let t = try h.captureFrontmostWindow().target
            f.afterPost = { f.focused = AXUIElementCreateApplication(999) }
            try expectFailure(.focusChanged) { try h.typeText(String(repeating: "x", count: 50), into: t) }
            try expect(f.posts.count == 1, "sent after change")
        }
        check("denied press never invokes AX") {
            let f = Fixture(); let t = try f.helper().captureFrontmostWindow().target
            let h = AccessibilityHelper(trustCheck: { false })
            try expectFailure(.permissionDenied) { try h.press(AXUIElementCreateApplication(741), in: t) }
        }
        check("live untrusted host returns friendly denial without prompting") {
            if !AXIsProcessTrusted() {
                let h = AccessibilityHelper()
                try expectFailure(.permissionDenied) { _ = try h.captureFrontmostWindow() }
                try expect(AccessibilityHelper.Failure.permissionDenied.localizedDescription.contains("系统设置"), "not actionable")
            } else {
                print("LIVE_DENIED_BRANCH=NOT_APPLICABLE (host currently trusted)")
            }
        }
        print("LIVE_AX_TRUSTED=\(AXIsProcessTrusted()); LIVE_INTERACTION=NOT_RUN (no user window mutation)")
        print("RESULT {\"passed\":\(passed),\"failed\":\(failed)}")
        if failed > 0 { exit(1) }
    }
}

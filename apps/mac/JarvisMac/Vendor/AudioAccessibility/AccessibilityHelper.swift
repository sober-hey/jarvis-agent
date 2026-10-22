import Foundation
import AppKit
@preconcurrency import ApplicationServices
import Carbon.HIToolbox

/// All operations stay on the main actor; no operation runs on initialization.
@MainActor
public final class AccessibilityHelper {
    public enum Failure: Error, Equatable, LocalizedError {
        case permissionDenied, noFrontmostApplication, noWindow, focusChanged
        case secureInput, unsupportedInput, eventPostingDenied, eventCreationFailed, textTooLong
        case accessibility(operation: String, code: Int32)
        public var errorDescription: String? {
            switch self {
            case .permissionDenied:
                return "尚未获得辅助功能权限。请在系统设置 → 隐私与安全性 → 辅助功能中启用实际运行此程序的应用，再重试。"
            case .noFrontmostApplication: return "未找到可确认身份的前台应用，请先选中目标应用。"
            case .noWindow: return "目标应用没有可访问的窗口；请打开普通窗口后重试。"
            case .focusChanged: return "目标应用、窗口或输入焦点已变化，已停止操作；请重新选择目标。"
            case .secureInput: return "当前为密码框或系统安全输入模式，已拒绝输入；请改用普通文本框。"
            case .unsupportedInput: return "无法确认这是普通文本输入框，已拒绝输入。请选中支持辅助功能的文本框。"
            case .eventPostingDenied: return "系统不允许发送键盘事件。请检查辅助功能权限，重新启动应用后再试；本程序不会自动申请或修改权限。"
            case .eventCreationFailed: return "无法创建键盘事件，未发送此段文字。"
            case .textTooLong: return "一次最多输入 4096 个 UTF-16 单元，请分次确认后输入。"
            case let .accessibility(operation, code): return "辅助功能操作失败（\(operation)，代码 \(code)）。请确认目标应用支持辅助功能，并重启应用后重试。"
            }
        }
    }

    /// Opaque, short-lived authorization target captured from the foreground app.
    public struct Target {
        fileprivate let identity: ProcessIdentity
        fileprivate let window: AXUIElement
        fileprivate let focusedElement: AXUIElement?
        public var pid: pid_t { identity.pid }
    }
    public struct WindowSnapshot {
        public let target: Target
        public let applicationName: String
        public let bundleIdentifier: String?
        public let title: String?
        public let capturedAt: Date
    }
    struct ProcessIdentity: Equatable {
        let pid: pid_t
        let name: String
        let bundleIdentifier: String?
        let launchDate: Date
    }
    /// Internal OS seam: tests never send real input or need TCC permission.
    @MainActor
    struct Backend {
        var frontmost: @MainActor () -> ProcessIdentity?
        var window: @MainActor (pid_t) throws -> AXUIElement
        var focusedElement: @MainActor (pid_t) throws -> AXUIElement?
        var stringAttribute: @MainActor (AXUIElement, String) throws -> String?
        var secureInputEnabled: @MainActor () -> Bool = { IsSecureEventInputEnabled() }
        var canPostEvents: @MainActor () -> Bool = { CGPreflightPostEventAccess() }
        var postUnicode: @MainActor (pid_t, [UniChar]) throws -> Void = { pid, units in
            guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
                  let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else {
                throw Failure.eventCreationFailed
            }
            down.flags = []; up.flags = []
            units.withUnsafeBufferPointer { buffer in
                down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
                up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
            }
            // Never use a global event tap: PID routing prevents cross-app delivery.
            down.postToPid(pid); up.postToPid(pid)
        }
        static var live: Backend {
            Backend(frontmost: {
                guard let app = NSWorkspace.shared.frontmostApplication,
                      !app.isTerminated, app.processIdentifier > 0,
                      let launchDate = app.launchDate else { return nil }
                return ProcessIdentity(pid: app.processIdentifier, name: app.localizedName ?? "未知应用", bundleIdentifier: app.bundleIdentifier, launchDate: launchDate)
            }, window: { pid in
                let app = AXUIElementCreateApplication(pid)
                for name in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
                    if let value = try optionalAttribute(app, name), CFGetTypeID(value) == AXUIElementGetTypeID() {
                        return unsafeDowncast(value, to: AXUIElement.self)
                    }
                }
                throw Failure.noWindow
            }, focusedElement: { pid in
                guard let value = try optionalAttribute(AXUIElementCreateApplication(pid), kAXFocusedUIElementAttribute) else { return nil }
                guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
                return unsafeDowncast(value, to: AXUIElement.self)
            }, stringAttribute: { element, name in
                guard let value = try optionalAttribute(element, name) else { return nil }
                guard CFGetTypeID(value) == CFStringGetTypeID() else {
                    throw Failure.accessibility(operation: name, code: AXError.illegalArgument.rawValue)
                }
                return value as? String
            })
        }
    }
    private let trustCheck: () -> Bool
    private let backend: Backend
    public convenience init(trustCheck: @escaping () -> Bool = { AXIsProcessTrusted() }) {
        self.init(trustCheck: trustCheck, backend: .live)
    }
    init(trustCheck: @escaping () -> Bool, backend: Backend) {
        self.trustCheck = trustCheck
        self.backend = backend
    }
    public var isPermissionGranted: Bool { trustCheck() }
    private func requirePermission() throws {
        guard isPermissionGranted else { throw Failure.permissionDenied }
    }
    /// Reads only the selected app's focused/main window title, never AXValue or a text tree.
    public func captureFrontmostWindow() throws -> WindowSnapshot {
        try requirePermission()
        guard let identity = backend.frontmost() else { throw Failure.noFrontmostApplication }
        let window = try backend.window(identity.pid)
        let title = try backend.stringAttribute(window, kAXTitleAttribute)
        let focused = try backend.focusedElement(identity.pid)
        guard backend.frontmost() == identity else { throw Failure.focusChanged }
        return WindowSnapshot(target: Target(identity: identity, window: window, focusedElement: focused), applicationName: identity.name, bundleIdentifier: identity.bundleIdentifier, title: title, capturedAt: Date())
    }
    /// Call only from a user action such as an “授权辅助功能” button.
    @discardableResult
    public func requestPermissionFromUserAction() -> Bool {
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    }
    public static let accessibilitySettingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!
    /// Opens settings only when explicitly invoked by a user-facing action.
    @discardableResult
    public func openPermissionSettingsFromUserAction() -> Bool {
        NSWorkspace.shared.open(Self.accessibilitySettingsURL)
    }
    /// AXPress on an explicitly supplied element; no recursive screen traversal.
    public func press(_ element: AXUIElement, in target: Target) throws {
        try validateTarget(target)
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success, pid == target.pid else { throw Failure.focusChanged }
        guard let value = try Self.optionalAttribute(element, kAXWindowAttribute),
              CFGetTypeID(value) == AXUIElementGetTypeID(), CFEqual(value, target.window) else { throw Failure.focusChanged }
        try validateTarget(target)
        let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
        guard result == .success else { throw Failure.accessibility(operation: "AXPress", code: result.rawValue) }
    }
    /// Sends Unicode only to the captured process. A return means queued, not delivered.
    /// May send a prefix before a later focus/permission change stops subsequent chunks.
    public func typeText(_ text: String, into target: Target) throws {
        try requirePermission()
        guard text.utf16.count <= 4096 else { throw Failure.textTooLong }
        try validateInput(target)
        guard backend.canPostEvents() else { throw Failure.eventPostingDenied }
        let units = Array(text.utf16)
        var start = 0
        while start < units.count {
            var end = min(start + 20, units.count)
            if end < units.count, (0xD800...0xDBFF).contains(units[end - 1]) { end -= 1 }
            try validateInput(target)
            guard backend.canPostEvents() else { throw Failure.eventPostingDenied }
            try backend.postUnicode(target.pid, Array(units[start..<end]))
            start = end
        }
    }
    private func validateTarget(_ target: Target) throws {
        try requirePermission()
        guard backend.frontmost() == target.identity,
              CFEqual(try backend.window(target.pid), target.window),
              backend.frontmost() == target.identity else { throw Failure.focusChanged }
    }
    private func validateInput(_ target: Target) throws {
        try validateTarget(target)
        guard !backend.secureInputEnabled() else { throw Failure.secureInput }
        guard let captured = target.focusedElement,
              let current = try backend.focusedElement(target.pid), CFEqual(captured, current) else { throw Failure.focusChanged }
        let role = try backend.stringAttribute(current, kAXRoleAttribute)
        let subrole = try backend.stringAttribute(current, kAXSubroleAttribute)
        guard subrole != kAXSecureTextFieldSubrole else { throw Failure.secureInput }
        // AXTextArea (e.g. TextEdit) may omit its optional subrole. Other unknown
        // metadata fails closed; explicit secure subroles were already rejected.
        let supportedSubrole = subrole == "AXStandardTextField" || subrole == "AXSearchField" || subrole == ""
            || (role == kAXTextAreaRole && subrole == nil)
        guard [kAXTextFieldRole, kAXTextAreaRole, kAXComboBoxRole].contains(role ?? ""), supportedSubrole else {
            throw Failure.unsupportedInput
        }
        guard !backend.secureInputEnabled(), backend.frontmost() == target.identity else { throw Failure.focusChanged }
    }
    private static func optionalAttribute(_ element: AXUIElement, _ name: String) throws -> CFTypeRef? {
        var value: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
        if error == .noValue || error == .attributeUnsupported { return nil }
        guard error == .success else { throw Failure.accessibility(operation: name, code: error.rawValue) }
        return value
    }
}

import Foundation

public enum Activity: String, Sendable, CaseIterable { case idle, recording, processing, playing }
public enum Indicator: String, Sendable, CaseIterable {
    case off, listening, recording, processing, playing, failed

    public var label: String {
        switch self {
        case .off: return "监听关闭"
        case .listening: return "监听就绪"
        case .recording: return "录音中"
        case .processing: return "处理中"
        case .playing: return "播放中"
        case .failed: return "发生错误"
        }
    }
}

public enum JarvisError: LocalizedError, Sendable {
    case permissionDenied(String)
    case invalidState(String)
    case moduleUnavailable(String)
    case operationFailed(String)

    public var errorDescription: String? {
        switch self {
        case .permissionDenied(let permission): return "尚未授权：\(permission)"
        case .invalidState(let detail): return "状态不允许：\(detail)"
        case .moduleUnavailable(let name): return "模块尚未接入：\(name)"
        case .operationFailed(let detail): return "操作失败：\(detail)"
        }
    }
    public var recoverySuggestion: String? {
        switch self {
        case .permissionDenied: return "在系统设置的隐私与安全性中授权，然后重试。"
        case .invalidState: return "先启用监听，再改变活动状态。"
        case .moduleUnavailable: return "此版本仅提供工程骨架；请接入对应模块。"
        case .operationFailed: return "请查看日志；关闭监听后可重新启用。"
        }
    }
}

public extension Notification.Name {
    static let jarvisStateDidChange = Notification.Name("JarvisCore.stateDidChange")
}

/// UI-owned state. Call mutators on the main thread when connected to AppKit.
/// Listening is an intent; the shell does not claim a hardware capture session exists.
public final class RuntimeState {
    public private(set) var isListening = false
    public private(set) var activity: Activity = .idle
    public private(set) var lastError: JarvisError?
    public var indicator: Indicator {
        if lastError != nil { return .failed }
        guard isListening else { return .off }
        switch activity {
        case .idle: return .listening
        case .recording: return .recording
        case .processing: return .processing
        case .playing: return .playing
        }
    }
    public init() {}
    public func setListening(_ enabled: Bool) {
        isListening = enabled
        activity = .idle
        lastError = nil
        notify()
    }
    public func setActivity(_ value: Activity) throws {
        guard isListening || value == .idle else {
            throw JarvisError.invalidState("监听关闭时不能进入 \(value.rawValue)")
        }
        activity = value
        notify()
    }
    public func report(_ error: JarvisError) {
        lastError = error
        activity = .idle
        notify()
    }
    public func clearError() { lastError = nil; notify() }
    private func notify() {
        NotificationCenter.default.post(name: .jarvisStateDidChange, object: self)
    }
}

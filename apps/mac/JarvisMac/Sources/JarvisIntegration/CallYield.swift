import AppKit
import Foundation

/// Steps aside so a phone or video call can take the microphone.
///
/// Holding an AVAudioEngine tap on the default input forever makes macOS treat
/// JARVIS as the app using the microphone, and Continuity/Phone/FaceTime calls
/// then fail or come up muted. Wake-word listening has to keep the input open,
/// so the only correct behaviour is to yield the moment a call begins and take
/// it back when the call ends.
///
/// Two independent signals, because neither is complete on its own:
///  1. A telephony app launches or terminates (fast, but a call can also arrive
///     inside an app that is already running, e.g. WeChat).
///  2. The audio engine reports a configuration change (the call has taken the
///     device), which previously was reported as a hard capture failure.
public final class CallYieldMonitor {
    /// Apps whose launch means "a call is about to need the microphone".
    /// Only apps that are calls by nature belong here; adding WeChat would yield
    /// for ordinary chat use.
    public static let telephonyBundleIDs: Set<String> = [
        "com.apple.FaceTime",
        "com.apple.Phone",
        "com.apple.MobilePhone",
        "com.apple.InCallService",
        "com.apple.avconferenced",
        "com.apple.TelephonyUtilities",
    ]

    /// Bundle identifiers of the running applications that look like a call UI.
    /// Injected for tests; production reads NSWorkspace.
    public static func callApps(in running: [String]) -> [String] {
        running.filter { telephonyBundleIDs.contains($0) }.sorted()
    }

    private var observers: [NSObjectProtocol] = []
    private var timer: Timer?
    private var yieldedFor: String?

    /// Called on the main thread with a human-readable reason.
    public var onCallStarted: ((String) -> Void)?
    public var onCallEnded: (() -> Void)?

    public init() {}

    public func start() {
        guard observers.isEmpty else { return }
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didLaunchApplicationNotification,
                     NSWorkspace.didTerminateApplicationNotification] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.evaluate()
            })
        }
        // A call can also arrive while the app is already running, and a missed
        // terminate notification must not strand the microphone forever.
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.evaluate() }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        evaluate()
    }

    public func stop() {
        for observer in observers { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
        observers.removeAll()
        timer?.invalidate()
        timer = nil
    }

    deinit { stop() }

    /// Pure decision step, also used by tests.
    public func evaluate(running: [String]? = nil) {
        let identifiers = running ?? NSWorkspace.shared.runningApplications.compactMap(\.bundleIdentifier)
        let active = Self.callApps(in: identifiers)
        if let first = active.first {
            guard yieldedFor == nil else { return }
            yieldedFor = first
            onCallStarted?(Self.reason(for: first))
        } else if yieldedFor != nil {
            yieldedFor = nil
            onCallEnded?()
        }
    }

    public var isYielded: Bool { yieldedFor != nil }

    public static func reason(for bundleID: String) -> String {
        switch bundleID {
        case "com.apple.FaceTime": return "FaceTime 通话中"
        case "com.apple.Phone", "com.apple.MobilePhone": return "电话通话中"
        default: return "通话中"
        }
    }
}

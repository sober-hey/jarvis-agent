import AVFoundation
import UIKit

@MainActor
final class DeviceStatusReporter {
    var onChange: (([String: Any]) -> Void)?
    private var tokens: [NSObjectProtocol] = []
    private var timer: Timer?
    private var foreground = true
    private var orientation: UIDeviceOrientation = .unknown

    func start() {
        guard timer == nil else { return }
        UIDevice.current.isBatteryMonitoringEnabled = true
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()
        orientation = UIDevice.current.orientation
        let names: [Notification.Name] = [
            UIDevice.batteryLevelDidChangeNotification,
            UIDevice.batteryStateDidChangeNotification,
            UIDevice.orientationDidChangeNotification,
            AVAudioSession.routeChangeNotification,
            UIApplication.didBecomeActiveNotification,
            UIApplication.didEnterBackgroundNotification,
            UIApplication.protectedDataDidBecomeAvailableNotification,
            UIApplication.protectedDataWillBecomeUnavailableNotification
        ]
        for name in names {
            tokens.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] notification in
                Task { @MainActor in
                    guard let self else { return }
                    if notification.name == UIApplication.didBecomeActiveNotification { self.foreground = true }
                    if notification.name == UIApplication.didEnterBackgroundNotification { self.foreground = false }
                    if notification.name == UIDevice.orientationDidChangeNotification {
                        self.orientation = UIDevice.current.orientation
                    }
                    self.publish()
                }
            })
        }
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.publish() }
        }
        publish()
    }

    func snapshot() -> [String: Any] {
        let device = UIDevice.current
        let route = AVAudioSession.sharedInstance().currentRoute
        let ports: Set<AVAudioSession.Port> = [.headphones, .bluetoothA2DP, .bluetoothHFP, .bluetoothLE]
        let headphones = route.outputs.contains { ports.contains($0.portType) }
        let level = device.batteryLevel
        return DeviceSnapshot(
            batteryLevel: level >= 0 ? Double(level) : nil,
            charging: device.batteryState == .charging || device.batteryState == .full,
            headphonesConnected: headphones,
            foreground: foreground,
            faceDown: orientation == .unknown ? nil : orientation == .faceDown,
            audioOutputs: route.outputs.map { $0.portType.rawValue },
            protectedDataAvailable: UIApplication.shared.isProtectedDataAvailable,
            observedAt: Date().timeIntervalSince1970
        ).wire
    }

    private func publish() { onChange?(snapshot()) }

    func setForeground(_ active: Bool) { foreground = active; publish() }

    func stop() {
        timer?.invalidate()
        timer = nil
        tokens.forEach { NotificationCenter.default.removeObserver($0) }
        tokens.removeAll()
        UIDevice.current.endGeneratingDeviceOrientationNotifications()
        UIDevice.current.isBatteryMonitoringEnabled = false
    }
}

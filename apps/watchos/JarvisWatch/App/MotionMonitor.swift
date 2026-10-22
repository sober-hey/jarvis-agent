import Combine
import CoreMotion
import Foundation

@MainActor
final class MotionMonitor: ObservableObject {
    @Published private(set) var running = false
    @Published private(set) var peakG = 0.0
    @Published private(set) var diagnosticURL: URL?
    private let manager = CMMotionManager()
    private var impacts = DoubleImpactDetector()
    private var raise = WristRaiseDetector()
    private var diagnostic: FileHandle?
    private var diagnosticDeadline: TimeInterval?
    var onImpact: (() -> Void)?
    var onRaise: (() -> Void)?
    var onError: ((String) -> Void)?
    var detectRaises = false

    func start() {
        guard !running else { return }
        guard manager.isAccelerometerAvailable else { onError?("此设备无加速度计"); return }
        impacts.reset(); raise.reset()
        manager.accelerometerUpdateInterval = 0.01
        manager.deviceMotionUpdateInterval = 0.04
        running = true
        manager.startAccelerometerUpdates(to: .main) { [weak self] data, error in
            // .main operation queue owns the detector state; no per-sample Task backlog.
            MainActor.assumeIsolated {
                guard let self, self.running else { return }
                if let error { self.onError?("运动传感器：\(error.localizedDescription)"); self.stop(); return }
                guard let data else { return }
                let a = data.acceleration
                let g = sqrt(a.x*a.x + a.y*a.y + a.z*a.z)
                if g > self.peakG { self.peakG = g }
                self.writeDiagnostic(t: data.timestamp, x: a.x, y: a.y, z: a.z)
                if self.impacts.consume(timestamp: data.timestamp, x: a.x, y: a.y, z: a.z) {
                    self.onImpact?()
                }
            }
        }
        if manager.isDeviceMotionAvailable {
            manager.startDeviceMotionUpdates(to: .main) { [weak self] data, _ in
                MainActor.assumeIsolated {
                    guard let self, self.running, self.detectRaises, let data else { return }
                    let r = data.rotationRate
                    if self.raise.consume(timestamp: data.timestamp, gravityZ: data.gravity.z,
                                          rotation: sqrt(r.x*r.x + r.y*r.y + r.z*r.z)) {
                        self.onRaise?()
                    }
                }
            }
        }
    }

    func stop() {
        running = false
        manager.stopAccelerometerUpdates(); manager.stopDeviceMotionUpdates()
        impacts.reset(); raise.reset()
        finishDiagnostic()
    }

    /// User-started, local-only 20s CSV capture for real Series 8 replay.
    /// Retrieve via Xcode Devices & Simulators -> Download Container.
    func captureDiagnostic() throws {
        guard running else { return }
        finishDiagnostic()
        let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("MotionCaptures", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent(UUID().uuidString + ".csv")
        try Data("timestamp,x_g,y_g,z_g\n".utf8).write(to: url, options: .atomic)
        diagnostic = try FileHandle(forWritingTo: url)
        try diagnostic?.seekToEnd()
        diagnosticURL = url
        diagnosticDeadline = ProcessInfo.processInfo.systemUptime + 20
        // Bound disk usage to latest five captures; never upload sensor traces.
        let files = try FileManager.default.contentsOfDirectory(at: directory,
            includingPropertiesForKeys: [.creationDateKey])
            .sorted {
                let lhs = (try? $0.resourceValues(forKeys: [.creationDateKey]))?.creationDate ?? .distantPast
                let rhs = (try? $1.resourceValues(forKeys: [.creationDateKey]))?.creationDate ?? .distantPast
                return lhs < rhs
            }
        for old in files.dropLast(5) { try? FileManager.default.removeItem(at: old) }
    }
    private func writeDiagnostic(t: Double, x: Double, y: Double, z: Double) {
        guard let diagnostic else { return }
        if ProcessInfo.processInfo.systemUptime >= (diagnosticDeadline ?? 0) { finishDiagnostic(); return }
        do { try diagnostic.write(contentsOf: Data("\(t),\(x),\(y),\(z)\n".utf8)) }
        catch { finishDiagnostic(); onError?("本机轨迹写入失败") }
    }
    private func finishDiagnostic() {
        try? diagnostic?.close(); diagnostic = nil; diagnosticDeadline = nil
    }
}

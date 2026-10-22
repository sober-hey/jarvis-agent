import Foundation
import ServiceManagement

@MainActor
final class LoginLaunchController {
    private(set) var error: String?
    var enabled: Bool { SMAppService.mainApp.status == .enabled }
    var requiresApproval: Bool { SMAppService.mainApp.status == .requiresApproval }
    func applyDefault() {
        // Acceptance instances must never register a DerivedData app for login.
        guard Bundle.main.bundleURL.standardizedFileURL.path.hasSuffix("/Applications/JARVIS.app") else { return }
        if UserDefaults.standard.object(forKey: "LaunchAtLogin") == nil { UserDefaults.standard.set(true, forKey: "LaunchAtLogin") }
        if UserDefaults.standard.bool(forKey: "LaunchAtLogin"), !enabled, !requiresApproval { setEnabled(true) }
    }
    func setEnabled(_ value: Bool) {
        do {
            if value { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            UserDefaults.standard.set(value, forKey: "LaunchAtLogin"); error = nil
        } catch { self.error = "登录启动设置失败：\(error.localizedDescription)" }
    }
}

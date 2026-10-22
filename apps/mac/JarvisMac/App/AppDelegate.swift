import AppKit
import AVFoundation
import ServiceManagement
import JarvisIntegration
import AudioAccessibilityModule

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem?
    private var coordinator: VoiceCoordinator?
    private var presenter: PlaybackMenuPresenter?
    private var acceptance: ReplayAcceptance?
    private let status = NSMenuItem(title: "JARVIS · 断线", action: nil, keyEquivalent: "")
    private let detail = NSMenuItem(title: "麦克风未启动", action: nil, keyEquivalent: "")
    private let microphone = NSMenuItem(title: "开启麦克风与激活检测", action: #selector(toggleMicrophone), keyEquivalent: "")
    private let clap = NSMenuItem(title: "击掌两下", action: #selector(toggleClap), keyEquivalent: "")
    private let snap = NSMenuItem(title: "响指", action: #selector(toggleSnap), keyEquivalent: "")
    private var clapEnabled = true, snapEnabled = true, wakeEnabled = true
    private let gatewayStatus = NSMenuItem(title: "网关：断线", action: nil, keyEquivalent: "")
    private let wake = NSMenuItem(title: "唤醒词 Jarvis", action: #selector(toggleWakeWord), keyEquivalent: "")
    private let login = NSMenuItem(title: "登录时启动", action: #selector(toggleLogin), keyEquivalent: "")
    private let loginController = LoginLaunchController()
    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let arguments = ProcessInfo.processInfo.arguments
            func argument(_ key: String) -> String? { guard let i = arguments.firstIndex(of: key), i + 1 < arguments.count else { return nil }; return arguments[i + 1] }
            let gateway = URL(string: argument("--gateway") ?? "ws://127.0.0.1:8765")!
            let c = try VoiceCoordinator(gateway: gateway); coordinator = c
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            let menu = NSMenu(); menu.delegate = self; menu.autoenablesItems = false
            status.isEnabled = false; detail.isEnabled = false
            menu.addItem(status); menu.addItem(gatewayStatus); gatewayStatus.isEnabled = false
            menu.addItem(detail); menu.addItem(.separator())
            microphone.target = self; menu.addItem(microphone)
            let activate = NSMenuItem(title: "开始说话", action: #selector(activateManually), keyEquivalent: ""); activate.target = self; menu.addItem(activate)
            clap.target = self; snap.target = self; clap.state = .on; snap.state = .on
            menu.addItem(clap); menu.addItem(snap)
            wake.target = self; wake.isEnabled = c.capture.wakeWordAvailable; wake.state = wake.isEnabled ? .on : .off; menu.addItem(wake)
            login.target = self; menu.addItem(login)
            let reconnect = NSMenuItem(title: "重新连接网关", action: #selector(reconnectGateway), keyEquivalent: ""); reconnect.target = self; menu.addItem(reconnect)
            menu.addItem(.separator())
            let p = PlaybackMenuPresenter(player: c.player, accessibility: AccessibilityHelper()); presenter = p
            p.appendControls(to: menu)
            // Presenter refreshes progress/AX controls first; coordinator.onUpdate renders
            // the final status button so playback percentages cannot erase the state icon.
            p.attach(to: item)
            menu.addItem(.separator())
            let about = NSMenuItem(title: "关于 JARVIS", action: #selector(showAbout), keyEquivalent: ""); about.target = self; menu.addItem(about)
            let quit = NSMenuItem(title: "退出 JARVIS", action: #selector(quitApplication), keyEquivalent: "q"); quit.target = self; menu.addItem(quit)
            item.menu = menu; statusItem = item
            c.onUpdate = { [weak self] in self?.render() }; render()
            // Events used to go nowhere, so "no reaction" had zero evidence.
            // The app is sandboxed: ~/.jarvis is not writable, use the container.
            let logDir = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Logs/JARVIS", isDirectory: true)
            try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
            let eventLog = logDir.appendingPathComponent("mac-events.jsonl")
            FileManager.default.createFile(atPath: eventLog.path, contents: nil)
            let handle = try? FileHandle(forWritingTo: eventLog)
            handle?.seekToEndOfFile()
            c.onEvent = { event in
                guard let handle,
                      let data = try? JSONSerialization.data(withJSONObject: event),
                      var line = String(data: data, encoding: .utf8) else { return }
                line += "\n"
                try? handle.write(contentsOf: Data(line.utf8))
            }
            if let path = argument("--replay-wav"), let result = argument("--evidence-json") {
                let replayURL: URL
                if path.hasPrefix("bundle:") {
                    guard let resource = Bundle.main.url(forResource: String(path.dropFirst(7)), withExtension: nil) else {
                        NSLog("Replay fixture missing from app bundle"); NSApp.terminate(nil); return
                    }
                    replayURL = resource
                } else { replayURL = URL(fileURLWithPath: path) }
                let runner = ReplayAcceptance(coordinator: c, wav: replayURL, output: URL(fileURLWithPath: result), scenario: argument("--scenario") ?? "single")
                acceptance = runner; runner.start()
            } else {
                loginController.applyDefault()
                c.connect()
                // Ask for the microphone up front instead of waiting for a menu click:
                // a notDetermined status silently disables the wake word, which looks
                // like "JARVIS never answers". The user still approves the dialog.
                switch AVCaptureDevice.authorizationStatus(for: .audio) {
                case .authorized: c.startMicrophone()
                case .notDetermined:
                    AVCaptureDevice.requestAccess(for: .audio) { granted in
                        DispatchQueue.main.async { if granted { c.startMicrophone() } }
                    }
                default: break
                }
                if arguments.contains("--status-probe") {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                        self?.render(); NSApp.terminate(nil)
                    }
                }
            }
        } catch { status.title = "JARVIS · 初始化失败：\(error.localizedDescription)"; NSLog("JARVIS initialization failed: %@", error.localizedDescription) }
    }
    func menuWillOpen(_ menu: NSMenu) { presenter?.refreshPermissionStatus(); render() }
    private func render() {
        guard let c = coordinator, let button = statusItem?.button else { return }
        status.title = "JARVIS · \(c.state.rawValue)\(c.suppressed ? "（另一台设备响应）" : "")"
        gatewayStatus.title = "网关：\(c.gatewayStatus)"
        wake.isEnabled = c.capture.wakeWordAvailable
        wake.title = c.capture.wakeWordAvailable ? "唤醒词 Jarvis" : "唤醒词 Jarvis：未就绪"
        login.state = loginController.enabled ? .on : .off
        login.title = loginController.requiresApproval ? "登录时启动：需要在系统设置允许" : "登录时启动"
        if let yielded = c.yieldedForCall {
            detail.title = "已让出麦克风（\(yielded)），通话结束后自动恢复"
        } else {
            detail.title = c.lastError ?? loginController.error ?? (c.microphoneRunning ? "麦克风已开启 · 激活后录音 · 静音 1 秒结束" : "麦克风未启动；从菜单开启")
        }
        microphone.title = c.microphoneRunning ? "关闭麦克风" : "开启麦克风与激活检测"
        let snapAvailable = !c.microphoneRunning || c.captureSampleRate >= 24000
        snap.isEnabled = snapAvailable
        snap.title = snapAvailable ? "响指" : "响指：当前麦克风采样率不足 24kHz"
        let symbol: String
        switch c.state { case .standby: symbol = "waveform"; case .listening: symbol = "mic.fill"; case .thinking: symbol = "ellipsis.bubble"; case .speaking: symbol = "speaker.wave.2.fill"; case .disconnected: symbol = "wifi.slash" }
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: status.title); image?.isTemplate = true
        button.image = image
        button.imagePosition = .imageLeading
        // Always show the name: a lone symbol is easy to lose in a crowded menu bar.
        button.title = "JARVIS"
        button.toolTip = status.title; button.setAccessibilityValue(status.title)
        let runtime: [String: Any] = ["gateway_status": c.gatewayStatus, "wake_available": c.capture.wakeWordAvailable,
            "wake_enabled": wakeEnabled, "login_status": SMAppService.mainApp.status.rawValue,
            "microphone_running": c.microphoneRunning, "microphone_permission": AVCaptureDevice.authorizationStatus(for: .audio).rawValue,
            "microphone_yielded_for": c.yieldedForCall ?? "",
            "bundle_path": Bundle.main.bundleURL.path, "pid": ProcessInfo.processInfo.processIdentifier]
        if let data = try? JSONSerialization.data(withJSONObject: runtime, options: [.sortedKeys]),
           let cache = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first {
            try? FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
            try? data.write(to: cache.appendingPathComponent("jarvis-runtime-status.json"), options: .atomic)
        }
        if ProcessInfo.processInfo.arguments.contains("--status-probe") {
            print("JARVIS_STATUS " + String(decoding: (try? JSONSerialization.data(withJSONObject: runtime)) ?? Data(), as: UTF8.self)); fflush(stdout)
        }
        c.emit("ui_render", ["state": c.state.rawValue, "symbol": symbol, "menu_title": status.title, "gateway_status": c.gatewayStatus, "wake_available": c.capture.wakeWordAvailable,
            "clap_enabled": clap.state == .on, "snap_enabled": snap.state == .on,
            "wake_unready": statusItem?.menu?.items.contains(where: { $0.title.contains("未就绪") && !$0.isEnabled }) ?? false])
    }
    @objc private func toggleMicrophone() { guard let c = coordinator else { return }; if c.microphoneRunning { c.stopMicrophone() } else { c.startMicrophone() } }
    @objc private func activateManually() { coordinator?.activateManually() }
    @objc private func toggleClap() { clapEnabled.toggle(); clap.state = clapEnabled ? .on : .off; coordinator?.setGestures(clap: clapEnabled, snap: snapEnabled) }
    @objc private func toggleSnap() { snapEnabled.toggle(); snap.state = snapEnabled ? .on : .off; coordinator?.setGestures(clap: clapEnabled, snap: snapEnabled) }
    @objc private func toggleWakeWord() { wakeEnabled.toggle(); wake.state = wakeEnabled ? .on : .off; coordinator?.capture.setWakeWord(enabled: wakeEnabled); render() }
    @objc private func toggleLogin() { loginController.setEnabled(!loginController.enabled); render() }
    @objc private func reconnectGateway() { coordinator?.reconnect() }
    @objc private func showAbout() { NSApp.activate(ignoringOtherApps: true); NSApp.orderFrontStandardAboutPanel(nil) }
    @objc private func quitApplication() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        coordinator?.shutdown(); presenter?.detach()
        if let statusItem { NSStatusBar.system.removeStatusItem(statusItem) }
    }
}

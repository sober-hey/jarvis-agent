import AppKit
import AVFoundation
import ApplicationServices
import JarvisCore
import OSLog

/// Menu shell only. Toggling changes intent; audio is owned by the integration layer.
@MainActor
public final class StatusBarController: NSObject {
    public private(set) var statusItem: NSStatusItem?
    public let state: RuntimeState
    private let logger = Logger(subsystem: "com.jarvis.macos", category: "MenuBar")

    public init(state: RuntimeState) { self.state = state; super.init() }

    public func install() {
        guard statusItem == nil else { return }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        NotificationCenter.default.addObserver(self, selector: #selector(stateChanged(_:)),
                                               name: .jarvisStateDidChange, object: state)
        refresh()
    }
    public func uninstall() {
        NotificationCenter.default.removeObserver(self)
        if let item = statusItem { NSStatusBar.system.removeStatusItem(item) }
        statusItem = nil
    }
    @objc public func toggleListening(_ sender: Any?) {
        state.setListening(!state.isListening)
    }
    @objc private func stateChanged(_ notification: Notification) { refresh() }

    private func refresh() {
        guard let item = statusItem, let button = item.button else { return }
        button.image = icon(for: state.indicator)
        button.title = ""
        button.toolTip = "JARVIS · \(state.indicator.label)（工程骨架，未连接音频）"
        button.setAccessibilityLabel("JARVIS")
        button.setAccessibilityValue(state.indicator.label)
        let menu = NSMenu()
        let status = NSMenuItem(title: "JARVIS · \(state.indicator.label)", action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        let scope = NSMenuItem(title: "工程骨架 · 未连接音频", action: nil, keyEquivalent: "")
        scope.isEnabled = false
        menu.addItem(scope)
        menu.addItem(.separator())
        let toggle = action(state.isListening ? "关闭监听" : "开启监听", #selector(toggleListening(_:)), key: "l")
        toggle.state = state.isListening ? .on : .off
        menu.addItem(toggle)
        menu.addItem(action("授权麦克风…", #selector(requestMicrophone(_:))))
        menu.addItem(action("授权辅助功能…", #selector(requestAccessibility(_:))))
        menu.addItem(action("权限状态…", #selector(showPermissions(_:))))
        if state.lastError != nil {
            menu.addItem(action("查看错误…", #selector(showError(_:))))
            menu.addItem(action("清除错误", #selector(clearError(_:))))
        }
        let previewItem = NSMenuItem(title: "状态预览（不录音）", action: nil, keyEquivalent: "")
        let preview = NSMenu()
        for indicator in Indicator.allCases {
            let entry = action(indicator.label, #selector(previewState(_:)))
            entry.representedObject = indicator.rawValue
            preview.addItem(entry)
        }
        previewItem.submenu = preview
        menu.addItem(previewItem)
        menu.addItem(.separator())
        menu.addItem(action("退出 JARVIS", #selector(quit(_:)), key: "q"))
        item.menu = menu
    }
    private func action(_ title: String, _ selector: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        return item
    }
    private func icon(for indicator: Indicator) -> NSImage {
        let color: NSColor
        switch indicator {
        case .off: color = .systemGray
        case .listening: color = .systemGreen
        case .recording: color = .systemRed
        case .processing: color = .systemOrange
        case .playing: color = .systemBlue
        case .failed: color = .systemPurple
        }
        let image = NSImage(size: NSSize(width: 20, height: 18), flipped: false) { _ in
            color.setFill()
            NSBezierPath(ovalIn: NSRect(x: 3, y: 2, width: 14, height: 14)).fill()
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.boldSystemFont(ofSize: 10), .foregroundColor: NSColor.white
            ]
            ("J" as NSString).draw(at: NSPoint(x: 7, y: 3), withAttributes: attributes)
            return true
        }
        image.isTemplate = false
        return image
    }
    @objc private func requestMicrophone(_ sender: Any?) {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if !granted { self.state.report(.permissionDenied("microphone")) }
            }
        }
    }
    @objc private func requestAccessibility(_ sender: Any?) {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }
    @objc private func showPermissions(_ sender: Any?) {
        let microphone = AVCaptureDevice.authorizationStatus(for: .audio).rawValue
        let accessibility = AXIsProcessTrusted() ? "已授权" : "未授权"
        alert("权限状态", "麦克风：\(microphone)\n辅助功能：\(accessibility)\n\n监听开关只改变意图；此骨架不会采集或上传音频。")
    }
    @objc private func showError(_ sender: Any?) {
        guard let error = state.lastError else { return }
        alert("JARVIS 错误", "\(error.localizedDescription)\n\(error.recoverySuggestion ?? "")")
    }
    @objc private func clearError(_ sender: Any?) { state.clearError() }
    @objc private func previewState(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let indicator = Indicator(rawValue: raw) else { return }
        state.setListening(indicator != .off)
        do {
            switch indicator {
            case .recording: try state.setActivity(.recording)
            case .processing: try state.setActivity(.processing)
            case .playing: try state.setActivity(.playing)
            case .failed: state.report(.moduleUnavailable("状态预览"))
            case .off, .listening: break
            }
        } catch { state.report(.operationFailed(error.localizedDescription)) }
    }
    private func alert(_ title: String, _ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSAlert()
        panel.messageText = title
        panel.informativeText = message
        panel.runModal()
    }
    @objc private func quit(_ sender: Any?) { NSApp.terminate(nil) }
}

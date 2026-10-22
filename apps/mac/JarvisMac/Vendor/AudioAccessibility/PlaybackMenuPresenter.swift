import AppKit
import Combine

/// Attaches playback progress to an EXISTING status item. Does not create another menu bar app.
/// Keep this object alive and call detach() before removing/replacing the host's item.
@MainActor
public final class PlaybackMenuPresenter: NSObject {
    public struct Display: Equatable {
        public let title: String
        public let detail: String
        public let accessibilityValue: String
    }
    public static func display(for snapshot: AudioPlayer.Snapshot) -> Display {
        let elapsed = clock(snapshot.currentTime), duration = clock(snapshot.duration)
        switch snapshot.phase {
        case .playing, .paused:
            let percent = Int((snapshot.fraction * 100).rounded())
            let label = snapshot.phase == .playing ? "播放中" : "已暂停"
            return Display(title: "\(snapshot.phase == .playing ? "▶" : "Ⅱ") \(percent)%", detail: "语音\(label) · \(elapsed) / \(duration)", accessibilityValue: "\(label)，\(percent)%，\(elapsed) / \(duration)")
        case .finished:
            return Display(title: "", detail: "语音播放完成 · \(duration)", accessibilityValue: "语音播放完成")
        case .failed:
            return Display(title: "!", detail: snapshot.errorMessage ?? "语音播放失败", accessibilityValue: "语音播放失败")
        case .idle:
            return Display(title: "", detail: "语音待机", accessibilityValue: "语音待机")
        }
    }
    private static func clock(_ value: TimeInterval) -> String {
        let seconds = Int(min(86_400, max(0, value.isFinite ? value : 0)))
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
    private weak var item: NSStatusItem?
    private var subscription: AnyCancellable?
    private let player: AudioPlayer
    private let accessibility: AccessibilityHelper
    private let progressItem = NSMenuItem(title: "语音待机", action: nil, keyEquivalent: "")
    private let permissionItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var originalTitle = ""
    private var originalTooltip: String?
    public init(player: AudioPlayer, accessibility: AccessibilityHelper) {
        self.player = player; self.accessibility = accessibility
        super.init()
        progressItem.isEnabled = false; permissionItem.isEnabled = false
    }
    /// The host retains menu ownership. If its refresh rebuilds the menu, append controls again.
    public func appendControls(to menu: NSMenu) {
        for entry in [progressItem, permissionItem] where entry.menu != nil { entry.menu?.removeItem(entry) }
        menu.addItem(progressItem)
        menu.addItem(action("停止语音", #selector(stopPlayback)))
        menu.addItem(action("授权辅助功能…", #selector(requestPermission)))
        menu.addItem(action("打开辅助功能设置…", #selector(openSettings)))
        menu.addItem(permissionItem)
        update(player.snapshot)
    }
    public func attach(to item: NSStatusItem) {
        detach()
        self.item = item
        originalTitle = item.button?.title ?? ""; originalTooltip = item.button?.toolTip
        subscription = player.$snapshot.sink { [weak self] value in self?.update(value) }
        update(player.snapshot)
    }
    public func detach() {
        subscription?.cancel(); subscription = nil
        item?.button?.title = originalTitle; item?.button?.toolTip = originalTooltip
        item?.button?.setAccessibilityValue(nil)
        item = nil
    }
    public func refreshPermissionStatus() {
        permissionItem.title = accessibility.isPermissionGranted ? "辅助功能：已授权" : "辅助功能：未授权，请手动在系统设置中启用"
    }
    private func update(_ snapshot: AudioPlayer.Snapshot) {
        let display = Self.display(for: snapshot)
        item?.button?.title = display.title
        item?.button?.toolTip = "JARVIS · \(display.detail)"
        item?.button?.setAccessibilityValue(display.accessibilityValue)
        progressItem.title = display.detail
        refreshPermissionStatus()
    }
    private func action(_ title: String, _ selector: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self; return item
    }
    @objc private func stopPlayback() { player.stop() }
    @objc private func requestPermission() {
        _ = accessibility.requestPermissionFromUserAction()
        refreshPermissionStatus()
    }
    @objc private func openSettings() { _ = accessibility.openPermissionSettingsFromUserAction() }
}

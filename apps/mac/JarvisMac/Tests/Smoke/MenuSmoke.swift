import AppKit
import JarvisCore
import JarvisUI

@main
struct MenuSmoke {
    @MainActor static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let state = RuntimeState()
        let controller = StatusBarController(state: state)
        controller.install()
        precondition(controller.statusItem?.button != nil, "status button exists")
        precondition(controller.statusItem?.button?.image != nil, "status icon exists")
        precondition(controller.statusItem?.menu?.item(withTitle: "开启监听") != nil)
        controller.toggleListening(nil)
        precondition(state.isListening && state.indicator == .listening)
        precondition(controller.statusItem?.menu?.item(withTitle: "关闭监听") != nil)
        for activity in [Activity.recording, .processing, .playing] {
            try state.setActivity(activity)
            precondition(controller.statusItem?.button?.toolTip?.contains(state.indicator.label) == true)
        }
        controller.toggleListening(nil)
        precondition(!state.isListening && state.indicator == .off)
        controller.uninstall()
        precondition(controller.statusItem == nil)
        print("Menu smoke: status item, icon, menu actions and 5 indicators PASS")
    }
}

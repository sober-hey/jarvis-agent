import AppKit
import JarvisCore
import JarvisAudio
import JarvisSpeech
import JarvisTransport
import JarvisUI
import OSLog

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let state = RuntimeState()
    private var menuBar: StatusBarController?
    private let logger = Logger(subsystem: "com.jarvis.macos", category: "App")

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menuBar = StatusBarController(state: state)
        self.menuBar = menuBar
        menuBar.install()
        logger.info("Menu bar shell started; capture and transport are not connected")
        if CommandLine.arguments.contains("--self-test") {
            do { try selfTest(menuBar) }
            catch {
                logger.error("Shell self-test failed")
                fputs("SELF_TEST_FAIL: \(error)\n", stderr)
                exit(1)
            }
            NSApp.terminate(nil)
        }
    }
    func applicationWillTerminate(_ notification: Notification) {
        state.setListening(false)
        menuBar?.uninstall()
    }
    private func selfTest(_ menuBar: StatusBarController) throws {
        guard menuBar.statusItem?.button?.image != nil else {
            throw JarvisError.operationFailed("status image missing")
        }
        var states = [state.indicator.rawValue]
        menuBar.toggleListening(nil)
        states.append(state.indicator.rawValue)
        guard state.isListening else { throw JarvisError.invalidState("toggle on failed") }
        for activity in [Activity.recording, .processing, .playing] {
            try state.setActivity(activity)
            states.append(state.indicator.rawValue)
            guard menuBar.statusItem?.button?.toolTip?.contains(state.indicator.label) == true else {
                throw JarvisError.operationFailed("indicator did not update")
            }
        }
        menuBar.toggleListening(nil)
        guard !state.isListening && state.indicator == .off else {
            throw JarvisError.invalidState("toggle off failed")
        }
        let rms = AudioDSP.rootMeanSquare([1, -1])
        guard rms == 1 else { throw JarvisError.operationFailed("Accelerate probe failed") }
        _ = try WebSocketFactory.make(url: URL(string: "wss://example.invalid/voice")!)
        state.report(.moduleUnavailable("self-test"))
        guard state.indicator == .failed else { throw JarvisError.invalidState("error not shown") }
        state.clearError()
        print("SELF_TEST_PASS: statusItem=true icon=true toggle=true states=\(states.joined(separator: ",")) Accelerate=true Starscream=true errorRecovery=true; no audio or network used")
    }
}

@main
struct JarvisMain {
    @MainActor static func main() {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let delegate = AppDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}

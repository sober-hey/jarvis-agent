import AppKit
import Foundation

/// Opt-in local fixture runner. Does not request or open the microphone.
@MainActor
public final class ReplayAcceptance {
    private let coordinator: VoiceCoordinator
    private let wav: URL, output: URL, scenario: String
    private var events = [[String: Any]]()
    private var started = false, replayComplete = false, secondStarted = false
    private var timer: Timer?
    private var startTime = ProcessInfo.processInfo.systemUptime
    private var hasEnded = false
    public init(coordinator: VoiceCoordinator, wav: URL, output: URL, scenario: String) {
        self.coordinator = coordinator; self.wav = wav; self.output = output; self.scenario = scenario
    }
    public func start() {
        coordinator.player.volume = 0 // Native MP3 decoder/playback clock still exercised; no test noise.
        coordinator.onEvent = { [weak self] event in
            guard let self else { return }
            self.events.append(event)
            self.flush()
        }
        let timer = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(timer, forMode: .common); self.timer = timer
        coordinator.onUpdate?()
        coordinator.connect()
    }
    private func tick() {
        guard !hasEnded else { return }
        if !started && coordinator.connected && !coordinator.suppressed && ProcessInfo.processInfo.systemUptime - startTime > 0.5 {
            started = true; replayComplete = false
            // Selection is delivered on MainActor while capture admission is queued on
            // its worker. Re-assert the admitted state immediately before this explicit
            // replay hook; suppression still wins because of the guard above.
            coordinator.capture.setAllowed(true)
            coordinator.capture.replay(files: [wav]) { [weak self] in self?.replayComplete = true }
        }
        if scenario == "interrupt" && started && replayComplete && !secondStarted && coordinator.state == .speaking {
            secondStarted = true; replayComplete = false
            coordinator.capture.replay(files: [wav]) { [weak self] in self?.replayComplete = true }
        }
        let done = events.filter { $0["event"] as? String == "playback_done" }.count
        let interruptions = events.filter { $0["event"] as? String == "activation" && $0["interrupted_playback"] as? Bool == true }.count
        let suppressed = events.contains { $0["event"] as? String == "suppressed" }
        let activations = events.filter { $0["event"] as? String == "activation" }.count
        if scenario == "suppressed" && started && replayComplete && suppressed { finish(activations == 0, reason: "suppression_probe") }
        else if scenario == "interrupt" && replayComplete && secondStarted && interruptions > 0 && done >= 1 { finish(true, reason: "activation_interrupt_and_native_drain") }
        else if scenario == "single" && replayComplete && done >= 1 { finish(true, reason: "native_mp3_drain") }
        else if ProcessInfo.processInfo.systemUptime - startTime > 180 { finish(false, reason: "timeout") }
    }
    private func flush(result: [String: Any]? = nil) {
        var value: [String: Any] = ["scenario": scenario, "events": events, "microphone_opened": false, "playback_volume": 0]
        if let result { value["result"] = result }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            if output.lastPathComponent == "-" {
                if result != nil { print("JARVIS_ACCEPTANCE " + String(decoding: data, as: UTF8.self)); fflush(stdout) }
            } else { try? data.write(to: output, options: .atomic) }
        }
    }
    private func finish(_ success: Bool, reason: String) {
        hasEnded = true; timer?.invalidate(); timer = nil
        flush(result: ["passed": success, "reason": reason])
        coordinator.onEvent = nil; coordinator.shutdown()
        NSApp.terminate(nil)
    }
}

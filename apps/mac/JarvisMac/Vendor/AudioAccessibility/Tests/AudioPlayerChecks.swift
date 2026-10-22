import Foundation
import AppKit

@main @MainActor
struct AudioPlayerChecks {
    enum Assertion: Error { case failed(String) }
    static var passed = 0
    static var failed = 0
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
        if !value() { throw Assertion.failed(message) }
    }
    static func check(_ name: String, _ body: () throws -> Void) {
        do { try body(); passed += 1; print("PASS \(name)") }
        catch { failed += 1; print("FAIL \(name): \(error)") }
    }
    static func spin(_ seconds: TimeInterval) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }
    static func main() throws {
        guard CommandLine.arguments.count == 3 else { fatalError("pass MP3 and WAV fixture paths") }
        let mp3URL = URL(fileURLWithPath: CommandLine.arguments[1])
        let data = try Data(contentsOf: mp3URL)
        let wav = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        check("real AVAudioPlayer decodes MP3 response bytes, advances and completes") {
            let player = AudioPlayer(); player.volume = 0
            var callbacks: [AudioPlayer.Snapshot] = []
            player.onProgress = { callbacks.append($0) }
            try player.play(data: data)
            try expect(player.snapshot.phase == .playing && player.snapshot.duration > 0.5, "MP3 not started")
            spin(0.32)
            try expect(player.snapshot.currentTime > 0.1 && callbacks.count >= 3, "progress timer stalled")
            spin(1.1)
            try expect(player.snapshot.phase == .finished && player.snapshot.fraction == 1, "completion missing")
            print("NATIVE_MP3 duration=\(player.snapshot.duration) progressCallbacks=\(callbacks.count)")
        }
        check("WAV bytes decode and local MP3 file plays") {
            let p = AudioPlayer(); p.volume = 0
            try p.play(data: wav); try expect(p.snapshot.phase == .playing, "WAV failed")
            try p.play(fileURL: mp3URL); try expect(p.snapshot.phase == .playing, "file failed")
            p.stop()
        }
        check("volume clamps finite, non-finite and persists across clips") {
            let p = AudioPlayer(); p.volume = -1; try expect(p.volume == 0, "negative volume")
            p.volume = 2; try expect(p.volume == 1, "high volume")
            p.volume = .nan; try expect(p.volume == 0, "NaN volume")
            try p.play(data: data); try expect(p.volume == 0, "lost volume")
            p.stop()
        }
        check("new activation interrupts immediately; stale response cannot resume") {
            let p = AudioPlayer(); p.volume = 0
            let old = p.activate(); try p.play(data: data, activation: old)
            let new = p.activate()
            try expect(p.snapshot.phase == .idle, "activation did not stop")
            let accepted = try p.play(data: data, activation: old)
            try expect(!accepted && p.snapshot.phase == .idle, "stale response played")
            try p.play(data: data, activation: new)
            try expect(p.snapshot.phase == .playing, "current response failed")
            p.stop(); spin(0.12)
            try expect(p.snapshot.phase == .idle, "stale completion revived playback")
        }
        check("pause holds position; seek clamps; resume restarts progress") {
            let p = AudioPlayer(); p.volume = 0; try p.play(data: data)
            spin(0.15); p.pause()
            let frozen = p.snapshot.currentTime
            spin(0.15)
            try expect(p.snapshot.phase == .paused && p.snapshot.currentTime == frozen, "pause not held")
            p.seek(to: -1); try expect(p.snapshot.currentTime == 0, "negative seek")
            try p.resume(); spin(0.15)
            try expect(p.snapshot.phase == .playing && p.snapshot.currentTime > 0, "resume failed")
            p.stop()
        }
        check("empty and malformed response fail with Chinese diagnostic") {
            let p = AudioPlayer(); p.volume = 0
            for invalid in [Data(), Data("{\"error\":\"not audio\"}".utf8)] {
                do { try p.play(data: invalid); throw Assertion.failed("bad bytes accepted") }
                catch is AudioPlayer.Failure {}
                try expect(p.snapshot.phase == .failed && p.snapshot.errorMessage != nil, "missing failed state")
            }
        }
        check("file errors stop old clip and publish actionable failed state") {
            let p = AudioPlayer(); p.volume = 0; try p.play(data: data)
            do { try p.play(fileURL: URL(string: "https://example.invalid/voice.mp3")!); throw Assertion.failed("network URL accepted") }
            catch is AudioPlayer.Failure {}
            try expect(p.snapshot.phase == .failed && p.snapshot.errorMessage != nil, "missing file error state")
        }
        check("new play interrupts preceding clip and stop cancels timer") {
            let p = AudioPlayer(); p.volume = 0
            try p.play(data: data); let old = p.activationID
            try p.play(data: wav)
            try expect(p.activationID != old && p.snapshot.phase == .playing, "not replaced")
            p.stop(); var ticks = 0; p.onProgress = { _ in ticks += 1 }; spin(0.22)
            try expect(ticks == 0 && p.snapshot.phase == .idle, "post-stop progress")
        }
        check("menu text uses playback fraction and accessible elapsed time") {
            let d = PlaybackMenuPresenter.display(for: .init(phase: .playing, currentTime: 30, duration: 120, errorMessage: nil))
            try expect(d.title == "▶ 25%" && d.detail.contains("0:30 / 2:00"), "incorrect display")
            try expect(d.accessibilityValue.contains("25%"), "missing accessible value")
        }
        check("permission-denied menu provides manual settings instruction") {
            _ = NSApplication.shared
            let p = AudioPlayer(); let h = AccessibilityHelper(trustCheck: { false })
            let presenter = PlaybackMenuPresenter(player: p, accessibility: h)
            let menu = NSMenu(); presenter.appendControls(to: menu)
            try expect(menu.items.contains { $0.title.contains("未授权") && $0.title.contains("系统设置") }, "missing denial guidance")
            try expect(menu.items.contains { $0.title == "停止语音" }, "missing stop")
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            defer { presenter.detach(); NSStatusBar.system.removeStatusItem(item); p.stop() }
            presenter.attach(to: item)
            p.volume = 0; try p.play(data: data)
            try expect(item.button?.title.hasPrefix("▶") == true, "status button did not render playback")
            try expect(menu.items.contains { $0.title.contains("播放中") && $0.title.contains("/") }, "menu progress not rendered")
        }
        print("RESULT {\"passed\":\(passed),\"failed\":\(failed)}")
        if failed > 0 { exit(1) }
    }
}

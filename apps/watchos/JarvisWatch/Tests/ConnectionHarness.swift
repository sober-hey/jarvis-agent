import Foundation

/// Real URLSession WebSocket using the Watch app's unmodified transport and core.
/// Synthetic PCM/MP3 fixture, no model, microphone, playback or real Keychain access.
@main
struct ConnectionHarness {
    @MainActor
    static func main() async {
        let connection = WatchConnection(credential: { "watch-loopback-fixture-token" }, deviceID: "watch-fixture")
        var received: [Int] = []
        var texts: [String] = []
        var problem: String?
        var stopped = 0
        let expected = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        let wav = WatchCoreSuite.makeWAV(Data((0..<1280).map { UInt8($0 % 251) }), junk: true)
        connection.deviceState = {
            ["device_type":"watch", "battery_level":0.6, "charging":false,
             "clap_triggered":true, "wearing":NSNull(), "on_wrist":NSNull(),
             "observed_at":1792450015]
        }
        connection.onReply = { data, generation in
            if data != expected { problem = "MP3 does not match fixture" }
            received.append(generation)
        }
        connection.onText = { text, role in texts.append(role + ":" + text) }
        connection.onStopPlayback = { stopped += 1 }
        connection.onError = { message in problem = message }
        func wait(_ predicate: () -> Bool) async {
            for _ in 0..<200 {
                if predicate() || problem != nil { return }
                try? await Task.sleep(for: .milliseconds(25))
            }
        }
        func require(_ condition: Bool, _ message: String) {
            if !condition { print("FAIL " + message + " " + (problem ?? "")); exit(1) }
        }
        connection.connect(endpoint: CommandLine.arguments[1])
        await wait { connection.ready }
        require(connection.ready, "connection")
        await connection.sendStatus()
        let capture = connection.beginUtterance()!
        do { try await connection.sendClip(wav, capture: capture) }
        catch { print("FAIL send \(error)"); exit(1) }
        await wait { received.count == 1 }
        require(received == [1] && !connection.awaitingReply && problem == nil, "first MP3")
        let staleCapture = connection.beginUtterance()!
        await connection.playbackDone(generation: 1)
        await wait { connection.suppressed }
        require(connection.suppressed && connection.beginUtterance() == nil, "suppressed must gate capture")
        await connection.sendStatus()
        await wait { !connection.suppressed }
        require(!connection.suppressed, "selected")
        do {
            try await connection.sendClip(wav, capture: staleCapture)
            require(false, "capture spanning suppression must not resume")
        } catch { print("PASS old capture rejected after suppressed -> selected") }
        let next = connection.beginUtterance()!
        do { try await connection.sendClip(wav, capture: next) }
        catch { print("FAIL second send \(error)"); exit(1) }
        await wait { received.count == 2 }
        require(received == [1, 2] && problem == nil && !connection.awaitingReply, "next full utterance MP3")
        require(texts == ["user:fixture heard", "assistant:fixture reply", "user:fixture heard", "assistant:fixture reply"], "text handling")
        require(stopped >= 4, "interrupt stops local playback")
        await connection.playbackDone(generation: 2)
        connection.disconnect()
        try? await Task.sleep(for: .milliseconds(100))
        require(!connection.ready, "disconnect")
        print("PASS hello -> device_state -> raw PCM/audio_end -> text -> complete MP3 -> playback_done")
        print("PASS interrupt/suppressed/selected; stale capture discarded; next full onset succeeds; no replay")
    }
}

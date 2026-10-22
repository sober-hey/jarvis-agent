import Foundation
#if canImport(JarvisWatch)
@testable import JarvisWatch
#endif

/// Shared suite runs unchanged in a macOS Swift harness and watchOS XCTest.
/// Only the WISDM replay is recorded sensor data. Boundary vectors are synthetic.
struct WatchCoreSuite {
    static func makeWAV(_ pcm: Data, junk: Bool = false) -> Data {
        var body = Data("WAVEfmt ".utf8)
        func le(_ value: Int, _ width: Int) -> Data {
            Data((0..<width).map { UInt8((value >> (8*$0)) & 255) })
        }
        body.append(le(16,4)); body.append(le(1,2)); body.append(le(1,2))
        body.append(le(16000,4)); body.append(le(32000,4)); body.append(le(2,2)); body.append(le(16,2))
        if junk { body.append(Data("JUNK".utf8)); body.append(le(3,4)); body.append(Data([1,2,3,0])) }
        body.append(Data("data".utf8)); body.append(le(pcm.count,4)); body.append(pcm)
        return Data("RIFF".utf8) + le(body.count,4) + body
    }
    static func run(fixture: URL, check: (String, Bool) -> Void) throws {
        func events(_ samples: [(Double, Double)]) -> [Double] {
            var detector = DoubleImpactDetector()
            return samples.compactMap { t, g in
                detector.consume(timestamp: t, x: g, y: 0, z: 0) ? t : nil
            }
        }
        for interval in [0.2, 0.3, 0.45, 0.6] {
            check("interval \(interval) accepted", events([(0,1),(0.1,3),(0.12,1),(0.1+interval,3)]).count == 1)
        }
        for interval in [0.001, 0.1, 0.199, 0.601, 0.7, 1.0] {
            check("interval \(interval) rejected", events([(0,1),(0.1,3),(0.1001,1),(0.1+interval,3)]).isEmpty)
        }
        check("threshold is strictly above 2.5g", events([(0,1),(0.1,2.5),(0.12,1),(0.4,2.5)]).isEmpty)
        check("single impact not enough", events([(0,1),(0.1,4),(0.3,1)]).isEmpty)
        check("continuous wide pulse not counted twice", events([(0,1),(0.1,4),(0.4,4),(0.5,3)]).isEmpty)
        check("hysteresis needs low valley", events([(0,1),(0.1,4),(0.2,2),(0.4,4)]).isEmpty)
        check("early ringing does not replace first", events([(0,1),(0.1,4),(0.11,1),(0.15,4),(0.17,1),(0.32,4)]).count == 1)
        check("stale first expires", events([(0,1),(0.1,4),(0.2,1),(0.9,4)]).isEmpty)
        check("expired second becomes first", events([(0,1),(0.1,4),(0.2,1),(0.9,4),(1.0,1),(1.2,4)]).count == 1)
        check("cooldown suppresses extra pair", events([(0,1),(0.1,4),(0.2,1),(0.4,4),(0.5,1),(0.7,4),(0.8,1),(1.0,4)]).count == 1)
        check("new pair after cooldown", events([(0,1),(0.1,4),(0.2,1),(0.4,4),(0.5,1),(1.8,4),(1.9,1),(2.1,4)]).count == 2)
        var d = DoubleImpactDetector()
        check("rotation independent magnitude first", !d.consume(timestamp: 1, x: 2, y: 2, z: 0))
        _ = d.consume(timestamp: 1.1, x: 0, y: 0, z: 1)
        check("rotation independent magnitude second", d.consume(timestamp: 1.3, x: -2, y: 0, z: -2))
        for value in [Double.nan, Double.infinity, -Double.infinity] {
            check("invalid value ignored \(value)", !d.consume(timestamp: 2, x: value, y: 0, z: 1))
        }
        check("duplicate timestamp rejected", events([(1,3),(1.1,1),(1,3)]).isEmpty)
        d.reset()
        _ = d.consume(timestamp: 4, x: 3, y: 0, z: 0)
        d.reset()
        check("reset clears pending first", !d.consume(timestamp: 4.3, x: 3, y: 0, z: 0))
        var raise = WristRaiseDetector()
        check("starts face up not raise", !raise.consume(timestamp: 1, gravityZ: -1, rotation: 0))
        _ = raise.consume(timestamp: 2, gravityZ: 0, rotation: 0)
        _ = raise.consume(timestamp: 2.1, gravityZ: -1, rotation: 0)
        check("stable face up heuristic", raise.consume(timestamp: 2.3, gravityZ: -1, rotation: 0))
        check("held up not repeat", !raise.consume(timestamp: 3, gravityZ: -1, rotation: 0))
        for url in ["wss://jarvis.example/watch", "ws://localhost:8765/watch", "ws://127.0.0.1:8765/watch", "ws://100.64.0.1:8765", "ws://192.168.1.5:8765"] {
            check("endpoint accepted \(url)", WatchWire.validatedEndpoint(url) != nil)
        }
        for url in ["http://example.com", "ws://example.com", "wss://x?token=secret", "wss://u:p@x", "wss://x/#y", "", "wss:///bad"] {
            check("endpoint rejected \(url)", WatchWire.validatedEndpoint(url) == nil)
        }
        let pcm = Data((0..<1280).map { UInt8($0 % 251) })
        let wav = makeWAV(pcm)
        check("WAV container stripped without sending RIFF", try WatchWire.pcm(fromWAV: wav) == pcm)
        check("WAV metadata skipped, not assumed 44 bytes", try WatchWire.pcm(fromWAV: makeWAV(pcm, junk: true)) == pcm)
        let frame = try WatchWire.audio(pcm.prefix(640))
        let obj = try JSONSerialization.jsonObject(with: Data(frame.utf8)) as! [String: Any]
        check("gateway uses audio type", obj["type"] as? String == "audio")
        check("gateway uses raw pcm_s16le", obj["format"] as? String == "pcm_s16le")
        check("audio rate is 16kHz", obj["sample_rate"] as? Int == 16000)
        check("audio data is raw PCM", Data(base64Encoded: obj["data"] as! String) == pcm.prefix(640))
        check("no legacy ID or audio_base64", obj["id"] == nil && obj["audio_base64"] == nil)
        let snapshot: [String: Any] = ["device_type":"watch", "battery_level":0.6,
            "charging":false, "clap_triggered":true, "wearing":NSNull(), "on_wrist":NSNull()]
        let hello = try WatchWire.hello(deviceID: "watch-fixture", state: snapshot)
        let h = try JSONSerialization.jsonObject(with: Data(hello.utf8)) as! [String: Any]
        let state = h["state"] as! [String: Any]
        check("hello identifies watch and stable device", h["type"] as? String == "hello" && h["device_type"] as? String == "watch" && h["device_id"] as? String == "watch-fixture")
        check("hello negotiates 16k mono PCM", h["sample_rate"] as? Int == 16000 && h["channels"] as? Int == 1 && h["encoding"] as? String == "pcm_s16le")
        check("hello retains clap battery and unknown wear", state["clap_triggered"] as? Bool == true && state["battery_level"] as? Double == 0.6 && state["wearing"] is NSNull && state["on_wrist"] is NSNull)
        let status = try JSONSerialization.jsonObject(with: Data(WatchWire.deviceState(deviceID: "watch-fixture", state: snapshot).utf8)) as! [String: Any]
        check("device_state matches iOS envelope", status["type"] as? String == "device_state" && status["device_id"] as? String == "watch-fixture" && status["state"] is [String: Any])
        func rejects(_ name: String, _ action: () throws -> Void) {
            do { try action(); check(name, false) } catch { check(name, true) }
        }
        rejects("malformed JSON rejected") { _ = try WatchWire.event("garbage") }
        rejects("binary WAV chunk not accepted as MP3") { _ = try WatchWire.event("{\"type\":\"audio\",\"format\":\"wav_pcm_s16le\",\"data\":\"AA==\",\"generation\":1,\"final\":true}") }
        for generation in ["true", "1.5", "-1", "\"1\"", "null"] {
            rejects("invalid generation \(generation)") { _ = try WatchWire.event("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"AA==\",\"generation\":\(generation),\"final\":true}") }
        }
        rejects("numeric final rejected") { _ = try WatchWire.event("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"AA==\",\"generation\":1,\"final\":1}") }
        rejects("bad base64 rejected") { _ = try WatchWire.event("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"!\",\"generation\":1,\"final\":true}") }
        rejects("MP3 without generation rejected") { _ = try WatchWire.event("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"AA==\",\"final\":true}") }
        rejects("oversized socket frame rejected") { _ = try WatchWire.event(String(repeating: " ", count: WatchWire.maxMessageBytes+1)) }
        rejects("oversized PCM frame rejected") { _ = try WatchWire.audio(Data(repeating: 0, count: 642)) }
        rejects("odd PCM frame rejected") { _ = try WatchWire.audio(Data([1])) }
        rejects("empty PCM frame rejected") { _ = try WatchWire.audio(Data()) }
        rejects("oversized WAV rejected") { _ = try WatchWire.pcm(fromWAV: makeWAV(Data(repeating: 0, count: WatchWire.maxAudioBytes+2))) }
        var wrongRate = wav; wrongRate[24] = 0
        rejects("wrong WAV sample rate rejected") { _ = try WatchWire.pcm(fromWAV: wrongRate) }
        var stereo = wav; stereo[22] = 2
        rejects("stereo WAV rejected") { _ = try WatchWire.pcm(fromWAV: stereo) }
        rejects("truncated WAV rejected") { _ = try WatchWire.pcm(fromWAV: wav.dropLast()) }
        var hugeChunk = wav
        hugeChunk.replaceSubrange(40..<44, with: [255,255,255,255])
        rejects("WAV uint32 chunk length cannot overflow watchOS Int") { _ = try WatchWire.pcm(fromWAV: hugeChunk) }
        var hugeContainer = wav
        hugeContainer.replaceSubrange(4..<8, with: [255,255,255,255])
        rejects("WAV uint32 RIFF length cannot overflow watchOS Int") { _ = try WatchWire.pcm(fromWAV: hugeContainer) }
        if case .audio(Data([0]), generation: 2, final: true) = try WatchWire.event("{\"type\":\"audio\",\"format\":\"mp3\",\"data\":\"AA==\",\"generation\":2,\"final\":true}") {
            check("gateway MP3 audio event parsed", true)
        } else { check("gateway MP3 audio event parsed", false) }
        if case .text("heard", role: "user", generation: 2) = try WatchWire.event("{\"type\":\"text\",\"role\":\"user\",\"text\":\"heard\",\"generation\":2}") {
            check("user transcript handled", true)
        } else { check("user transcript handled", false) }
        if case .interrupt(nil) = try WatchWire.event("{\"type\":\"interrupt\"}") { check("legacy gateway interrupt without generation", true) }
        else { check("legacy gateway interrupt without generation", false) }
        if case .suppressed = try WatchWire.event("{\"type\":\"suppressed\"}") { check("suppressed handled", true) }
        else { check("suppressed handled", false) }
        if case .selected = try WatchWire.event("{\"type\":\"resumed\"}") { check("resumed handled", true) }
        else { check("resumed handled", false) }
        var buffer = WatchReplyBuffer()
        let mp3 = Data([73,68,51,1,2,3])
        check("partial MP3 not played", try buffer.append(mp3.prefix(3), generation: 1, final: false) == nil)
        check("MP3 assembled until empty final", try buffer.append(mp3.suffix(3), generation: 1, final: false) == nil && buffer.append(Data(), generation: 1, final: true) == mp3)
        check("duplicate final not replayed", try buffer.append(mp3, generation: 1, final: true) == nil)
        _ = try buffer.append(mp3, generation: 2, final: false)
        buffer.interrupt()
        check("local interruption rejects old in-flight MP3", try buffer.append(mp3, generation: 2, final: true) == nil)
        check("new generation accepted after local interruption", try buffer.append(mp3, generation: 3, final: true) == mp3)
        _ = try buffer.append(mp3, generation: 4, final: false)
        buffer.interrupt(generation: 5)
        check("interrupt rejects older generation", try buffer.append(mp3, generation: 4, final: true) == nil)
        check("interrupt preserves the announced new generation", try buffer.append(mp3, generation: 5, final: true) == mp3)
        buffer.setSuppressed(true)
        check("suppressed never plays audio", try buffer.append(mp3, generation: 6, final: true) == nil)
        buffer.setSuppressed(false)
        check("selection permits next full generation", try buffer.append(mp3, generation: 6, final: true) == mp3)
        buffer.reset()
        check("new connection resets generation", try buffer.append(mp3, generation: 1, final: true) == mp3)
        buffer.reset()
        _ = try buffer.append(Data([1]), generation: 1, final: false)
        check("new generation discards incomplete old buffer", try buffer.append(mp3, generation: 2, final: true) == mp3)
        buffer.reset()
        _ = try buffer.append(Data(repeating: 0, count: WatchWire.maxReplyBytes), generation: 1, final: false)
        rejects("assembled MP3 bounded to 24 MiB") { _ = try buffer.append(Data([1]), generation: 1, final: true) }

        // Genuine recorded Android smartwatch clapping excerpt, never synthesized.
        let lines = try String(contentsOf: fixture, encoding: .utf8).split(separator: "\n")
        check("recorded fixture has 800 samples", lines.count == 800)
        let start = Double(lines[0].split(separator: ",")[2])!
        var replay = DoubleImpactDetector()
        var hits: [Double] = []
        for line in lines {
            let parts = line.split(separator: ",")
            let t = (Double(parts[2])! - start) / 1e9
            if replay.consume(timestamp: t, x: Double(parts[3])! / 9.80665,
                              y: Double(parts[4])! / 9.80665, z: Double(parts[5])! / 9.80665) { hits.append(t) }
        }
        check("recorded replay regression detects one pair", hits.count == 1)
        check("recorded replay time 39.12006018", abs((hits.first ?? 0) - 39.12006018) < 0.000001)
        print("Recorded replay: \(lines.count) samples, \(hits.count) detected pairs, times=\(hits)")
    }
}

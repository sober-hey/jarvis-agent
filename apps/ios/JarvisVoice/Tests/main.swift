import Foundation

var failures = 0
var checks = 0
func expect(_ condition: @autoclosure () -> Bool, _ name: String) {
    checks += 1
    if !condition() { failures += 1; print("FAIL: \(name)") }
}

// Tracer bullet: real wire encoding must be accepted by the same gateway as Mac.
let hello = VoiceWire.hello(deviceID: "iphone-test", state: ["battery_level": 0.5])
expect(hello["type"] as? String == "hello", "hello type")
expect(hello["device_id"] as? String == "iphone-test", "stable identity")
expect(hello["sample_rate"] as? Int == 16000, "16 kHz negotiation")
let encoded = try JSONSerialization.data(withJSONObject: hello)
let decoded = try JSONSerialization.jsonObject(with: encoded)
expect(decoded is [String: Any], "JSON round trip")

// A voice frame opens one bounded utterance; silence stays on-device.
var gate = SpeechGate(threshold: 0.02, endSilenceFrames: 3, preRollFrames: 2, maxFrames: 8)
let quiet = [Float](repeating: 0, count: 320)
let speech = [Float](repeating: 0.1, count: 320)
expect(!gate.process(quiet).started, "silence does not start")
let onset = gate.process(speech)
expect(onset.started && onset.frames.count == 2, "speech includes preroll")
expect(!gate.process(quiet).ended, "short pause held")
_ = gate.process(quiet)
expect(gate.process(quiet).ended, "silence commits utterance")
expect(gate.process(quiet).frames.isEmpty, "idle silence never sent")
let pcm = PCM16.encode([-1, 0, 1, .nan, .infinity])
expect(Array(pcm) == [0, 128, 0, 0, 255, 127, 0, 0, 0, 0], "clamped little endian PCM16")
let audio = VoiceWire.audio(pcm)
expect(audio["format"] as? String == "pcm_s16le", "gateway PCM format")
expect(Data(base64Encoded: audio["data"] as! String) == pcm, "audio base64 round trip")
expect(EndpointPolicy.validate("ws://100.80.1.2:8765") != nil, "Tailscale allowed")
expect(EndpointPolicy.validate("ws://example.com:8765") == nil, "public plaintext refused")
expect(EndpointPolicy.validate("wss://voice.example.com") != nil, "TLS allowed")
expect(EndpointPolicy.validate("ws://user:secret@100.80.1.2:8765") == nil, "credentials not in URL")
var playback = PlaybackAccumulator()
expect(playback.append(Data([1, 2]), generation: 1, final: false) == nil, "partial MP3 not played")
expect(playback.append(Data([3]), generation: 1, final: true) == Data([1, 2, 3]), "MP3 joined in order")
playback.interrupt()
expect(playback.append(Data([4]), generation: 1, final: true) == nil, "interrupted generation discarded")
expect(playback.append(Data([5]), generation: 2, final: true) == Data([5]), "new generation plays")

let state = DeviceSnapshot(batteryLevel: nil, charging: false, headphonesConnected: true,
                           foreground: false, faceDown: nil, audioOutputs: ["BluetoothHFP"],
                           protectedDataAvailable: false, observedAt: 123).wire
expect(state["screen_on"] is NSNull, "background screen unknown not off")
expect(state["battery_level"] is NSNull, "unknown battery is not zero")
expect(state["headphones_worn"] is NSNull, "route is not wearing sensor")
let fgState = DeviceSnapshot(batteryLevel: 0.6, charging: true, headphonesConnected: false,
                             foreground: true, faceDown: true, audioOutputs: [],
                             protectedDataAvailable: true, observedAt: 124).wire
expect(fgState["screen_on"] as? Bool == true, "foreground visible")
expect(fgState["face_down"] as? Bool == true, "face down retained")
let event = VoiceWire.decode(["type": "audio", "format": "mp3", "data": "AQID", "generation": 4, "final": true])
if case .audio(let data, let generation, let final) = event {
    expect(data == Data([1, 2, 3]) && generation == 4 && final, "MP3 event decoded")
} else { expect(false, "MP3 event decoded") }
if case .invalid = VoiceWire.decode(["type": "audio", "format": "mp3", "data": "bad="]) {
    expect(true, "bad MP3 base64 rejected")
} else { expect(false, "bad MP3 base64 rejected") }
if case .invalid = VoiceWire.decode(["type": "audio", "format": "mp3", "data": "AQID", "generation": true]) {
    expect(true, "boolean generation refused")
} else { expect(false, "boolean generation refused") }

var forwarding = UtteranceForwarding()
expect(!forwarding.consume(started: false, ended: false, connected: true, selected: true), "no mid-utterance forwarding")
expect(forwarding.consume(started: true, ended: false, connected: true, selected: true), "connected onset forwards")
expect(!forwarding.consume(started: false, ended: false, connected: false, selected: true), "disconnect drops current utterance")
expect(!forwarding.consume(started: false, ended: false, connected: true, selected: true), "reconnect cannot send tail alone")
expect(forwarding.consume(started: true, ended: true, connected: true, selected: true), "next complete utterance forwards")
expect(!forwarding.consume(started: true, ended: false, connected: true, selected: false), "suppressed device never forwards")

var bounded = SpeechGate(threshold: 0.02, endSilenceFrames: 5, preRollFrames: 1, maxFrames: 3)
_ = bounded.process(speech)
_ = bounded.process(speech)
expect(bounded.process(speech).ended, "utterance hard cap")
expect(bounded.process([]).frames.isEmpty, "empty frame safe")
var nonfiniteGate = SpeechGate()
expect(!nonfiniteGate.process([.nan, .infinity]).started, "nonfinite power cannot trigger")
let stateJSON = try JSONSerialization.data(withJSONObject: state)
expect(String(data: stateJSON, encoding: .utf8)!.contains("null"), "unknown state JSON encodes null")
let endedMP3 = VoiceWire.decode(["type": "audio", "format": "mp3", "data": "", "generation": 4, "final": true])
if case .audio(let data, _, let final) = endedMP3 {
    expect(data.isEmpty && final, "empty final MP3 marker accepted")
} else { expect(false, "empty final MP3 marker accepted") }
if case .suppressed = VoiceWire.decode(["type": "suppressed"]) { expect(true, "suppression decoded") }
else { expect(false, "suppression decoded") }

print("\(checks) checks, \(failures) failures")
exit(failures == 0 ? 0 : 1)

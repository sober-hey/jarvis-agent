import Foundation
import Combine
import JarvisWebSocket

// Test peer only; fixtures verify transport bytes, not ASR/TTS inference or MP3 playback.
let mode = CommandLine.arguments.dropFirst().first ?? "exchange"
let callbacks = DispatchQueue(label: "integration.callbacks")
let finished = DispatchSemaphore(value: 0)
var subscriptions = Set<AnyCancellable>()
var connections = 0
var assistantTexts = 0
var audioChunks = 0
var responses = 0
let client = try JarvisWebSocketClient(callbackQueue: callbacks)
client.onTextReceived = { text in
    precondition(text == "transport fixture reply")
    assistantTexts += 1
}
client.onAudioReceived = { data in precondition(data == Data([1, 2, 3])); audioChunks += 1 }
func upload() {
    client.sendAudio(Data([0, 0, 255, 127])).sink(receiveCompletion: { completion in
        if case .failure(let error) = completion { fatalError("unexpected upload failure: \(error)") }
    }, receiveValue: { response in
        precondition(response.text == "transport fixture reply")
        precondition(response.audio == Data([1, 2, 3]))
        responses += 1
        print("RESPONSE \(responses)", terminator: "\n")
        fflush(stdout)
        if mode == "exchange" || (mode == "outage" && responses == 2) { finished.signal() }
    }).store(in: &subscriptions)
}
client.onConnectionChanged = { connected in
    print("CONNECTION \(connected)"); fflush(stdout)
    if connected {
        connections += 1
        if mode != "heartbeat", connections == 1 { upload() }
    } else if mode == "outage", responses == 1 { upload() } // Never sent until retry succeeds.
}
client.onError = { error in print("ERROR \(error.localizedDescription)"); fflush(stdout) }
client.connect()
if mode == "heartbeat" {
    Thread.sleep(forTimeInterval: 33)
    precondition(client.isConnected, "default 30s heartbeat must keep session alive")
} else {
    precondition(finished.wait(timeout: .now() + 55) == .success, "loopback integration timed out")
    callbacks.sync { precondition(assistantTexts == responses && audioChunks == responses) }
}
client.disconnect()
print("PASS integration \(mode)")

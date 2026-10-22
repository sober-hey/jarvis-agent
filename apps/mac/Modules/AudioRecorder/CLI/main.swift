import AVFoundation
import Foundation

let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--help") {
    print("Usage: jarvis-audio [--seconds 10] [--output recording.pcm] [--permission-status]")
    exit(0)
}
if args.contains("--permission-status") {
    print("microphone_authorization=\(AudioRecorder.permissionStatus.rawValue)")
    exit(0)
}
func value(_ option: String, _ fallback: String) -> String {
    guard let i = args.firstIndex(of: option), i + 1 < args.count else { return fallback }
    return args[i + 1]
}
guard let seconds = Double(value("--seconds", "10")), seconds.isFinite, (0.1...3600).contains(seconds) else {
    fputs("--seconds must be 0.1...3600\n", stderr); exit(2)
}
let destination = URL(fileURLWithPath: value("--output", "recording.pcm"))
var finished = false
var exitCode: Int32 = 0
var peak: Float = 0
var volumeEvents = 0
var recorded: AudioRecorder.Recording?
var receivedSamples = 0
var stopping = false
let targetSamples = Int64((seconds * 16000).rounded())
let callbacks = AudioRecorder.Callbacks(onVolume: { level in
    volumeEvents += 1; peak = max(peak, level)
}, onPCM: { data in
    receivedSamples += data.count / 2
    if receivedSamples >= targetSamples { finish() }
}, onState: { print("state=\($0.rawValue)") }, onError: { error in
    fputs("recording_error=\(error.localizedDescription)\n", stderr)
    exitCode = 1; finished = true
})
let recorder = AudioRecorder(configuration: try .init(bufferDuration: 10), callbacks: callbacks)
func finish() {
    guard !stopping else { return }
    stopping = true
    recorder.stop { result in
        switch result {
        case .success(let recording): recorded = recording
        case .failure(let error): fputs("stop_error=\(error.localizedDescription)\n", stderr); exitCode = 1
        }
        finished = true
    }
}
signal(SIGINT, SIG_IGN)
let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
interrupt.setEventHandler { finish() }; interrupt.resume()
recorder.start(outputURL: destination) { result in
    switch result {
    case .success:
        print("Recording for \(seconds) seconds; microphone data stays on this Mac.")
        // Stop after actual captured samples, not ten wall-clock seconds from engine start.
        // Hardware startup can delay the first tap by a buffer period.
    case .failure(let error):
        fputs("start_error=\(error.localizedDescription)\n", stderr); exitCode = 1; finished = true
    }
}
// A native RunLoop is required for TCC and main-queue permission callbacks.
let overallDeadline = Date().addingTimeInterval(seconds + 60)
while !finished && Date() < overallDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
if !finished {
    fputs("permission_or_recording_timeout\n", stderr); exitCode = 3
    finish()
    let cleanupDeadline = Date().addingTimeInterval(5)
    while !finished && Date() < cleanupDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
}
if let recording = recorded {
    // Tap boundaries can overshoot. Trim only excess real captured samples, never pad silence.
    let savedSamples = min(recording.sampleCount, targetSamples)
    let handle = try FileHandle(forWritingTo: destination)
    try handle.truncate(atOffset: UInt64(savedSamples * 2)); try handle.synchronize(); try handle.close()
    let metadata: [String: Any] = ["sample_rate": recording.sampleRate, "channels": recording.channels,
        "bit_depth": recording.bitDepth, "encoding": "s16le", "sample_count": savedSamples,
        "captured_sample_count": recording.sampleCount,
        "duration_seconds": Double(savedSamples) / 16000, "byte_count": savedSamples * 2,
        "buffered_byte_count": recording.pcm.count, "peak_rms": peak, "volume_callbacks": volumeEvents,
        "source": "real_microphone", "output": destination.path]
    let data = try JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: destination.appendingPathExtension("json"), options: .atomic)
    print(String(decoding: data, as: UTF8.self))
    print("Playback: ffplay -f s16le -ar 16000 -ch_layout mono -autoexit '\(destination.path)'")
}
exit(exitCode)

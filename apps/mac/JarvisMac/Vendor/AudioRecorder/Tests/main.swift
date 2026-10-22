import AVFoundation
import Foundation

var failures = 0
func check(_ condition: @autoclosure () -> Bool, _ name: String) {
    if condition() { print("PASS \(name)") } else { print("FAIL \(name)"); failures += 1 }
}
// First vertical slice: buffer bounds are part of the public contract.
do {
    _ = try AudioRecorder.Configuration(bufferDuration: 2)
    check(false, "reject a buffer shorter than three seconds")
} catch {
    check(true, "reject a buffer shorter than three seconds")
}
check((try? AudioRecorder.Configuration(bufferDuration: 10)) != nil, "accept ten-second buffer")
check((try? AudioRecorder.Configuration(bufferDuration: .nan)) == nil, "reject non-finite duration")

let ring = PCMBufferRing(capacityBytes: 6)
ring.append(Data([1, 2, 3, 4]))
ring.append(Data([5, 6, 7, 8]))
check(ring.snapshot() == Data([3, 4, 5, 6, 7, 8]), "ring retains newest bytes in order")
ring.append(Data([9, 10, 11, 12, 13, 14, 15, 16]))
check(ring.snapshot() == Data([11, 12, 13, 14, 15, 16]), "oversized input stays bounded")
ring.reset()
check(ring.snapshot().isEmpty, "ring resets between sessions")

let format48 = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
let converter = try PCMConverter(inputFormat: format48)
var converted = Data()
for chunk in 0..<100 {
    let input = AVAudioPCMBuffer(pcmFormat: format48, frameCapacity: 4800)!
    input.frameLength = 4800
    for f in 0..<4800 {
        let v = Float(sin(Double(chunk * 4800 + f) * 2 * .pi * 440 / 48000) * 0.5)
        input.floatChannelData![0][f] = v
        input.floatChannelData![1][f] = v
    }
    let output = try converter.convert(input)
    converted.append(output.data)
    check(output.level >= 0 && output.level <= 1, "RMS normalized \(chunk)")
}
converted.append(try converter.finish().data)
print("CONVERTED samples=\(converted.count / 2)")
check(abs(converted.count / 2 - 160000) <= 1, "48k stereo converts to 160000 mono samples in ten seconds")
check(converted.count % 2 == 0, "16-bit samples are byte aligned")
let firstSamples = converted.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
check(firstSamples.map { abs(Int($0)) }.max()! > 14000, "conversion preserves non-silent signal")

final class FakePermission: MicrophonePermission {
    var status: AVAuthorizationStatus
    var pending: ((Bool) -> Void)?
    init(_ status: AVAuthorizationStatus) { self.status = status }
    func request(_ completion: @escaping (Bool) -> Void) { pending = completion }
}
final class FakeInput: AudioInput {
    var receive: ((AVAudioPCMBuffer, @escaping () -> Void) -> Void)?
    var fail: ((AudioRecorder.RecorderError) -> Void)?
    var stopped = false
    func start(onBuffer: @escaping (AVAudioPCMBuffer, @escaping () -> Void) -> Void,
               onError: @escaping (AudioRecorder.RecorderError) -> Void) throws -> AVAudioFormat {
        receive = onBuffer; fail = onError
        return format48
    }
    func stop() { stopped = true }
    func feed() {
        let b = AVAudioPCMBuffer(pcmFormat: format48, frameCapacity: 4800)!
        b.frameLength = 4800
        for c in 0..<2 { for i in 0..<4800 { b.floatChannelData![c][i] = 0.25 } }
        receive?(b, {})
    }
}
let testQueue = DispatchQueue(label: "tests.callbacks")
func awaitResult<T>(_ start: (@escaping (T) -> Void) -> Void) -> T {
    let sem = DispatchSemaphore(value: 0)
    var result: T?
    start { result = $0; sem.signal() }
    if sem.wait(timeout: .now() + 5) == .timedOut { fatalError("Test callback timed out") }
    return result!
}
let fake = FakeInput()
let recorder = AudioRecorder(configuration: try .init(), callbackQueue: testQueue,
                             permission: FakePermission(.authorized), input: fake)
let started: Result<Void, AudioRecorder.RecorderError> = awaitResult { recorder.start(completion: $0) }
check((try? started.get()) != nil, "start authorized recorder")
fake.feed()
let paused: Result<Void, AudioRecorder.RecorderError> = awaitResult { recorder.pause(completion: $0) }
check((try? paused.get()) != nil, "pause recording")
fake.feed()
let resumed: Result<Void, AudioRecorder.RecorderError> = awaitResult { recorder.resume(completion: $0) }
check((try? resumed.get()) != nil, "resume recording")
fake.feed()
let stopped: Result<AudioRecorder.Recording, AudioRecorder.RecorderError> = awaitResult { recorder.stop(completion: $0) }
check((try? stopped.get().sampleCount) == 3200, "paused samples excluded and converter drains on stop")
check(fake.stopped, "stop releases input")
let denied = AudioRecorder(configuration: try .init(), callbackQueue: testQueue,
                           permission: FakePermission(.denied), input: FakeInput())
let refused: Result<Void, AudioRecorder.RecorderError> = awaitResult { denied.start(completion: $0) }
if case .failure(.permissionDenied) = refused { check(true, "denied permission fails explicitly") }
else { check(false, "denied permission fails explicitly") }

let pendingPermission = FakePermission(.notDetermined)
let waiting = AudioRecorder(configuration: try .init(), callbackQueue: testQueue,
                            permission: pendingPermission, input: FakeInput())
let startCancelled = DispatchSemaphore(value: 0)
let cancelledExistingURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"]!).appendingPathComponent("audio-cancel-preserve-\(UUID()).pcm")
try Data("permission-pending-must-preserve".utf8).write(to: cancelledExistingURL)
waiting.start(outputURL: cancelledExistingURL) { result in
    if case .failure(.cancelled) = result { check(true, "pending permission start cancelled") }
    else { check(false, "pending permission start cancelled") }
    startCancelled.signal()
}
let cancelledStop: Result<AudioRecorder.Recording, AudioRecorder.RecorderError> = awaitResult { waiting.stop(completion: $0) }
if case .failure(.cancelled) = cancelledStop { check(true, "permission cancellation has no recording to finalize") }
else { check(false, "permission cancellation has no recording to finalize") }
check(startCancelled.wait(timeout: .now() + 2) == .success, "cancelled start completion delivered")
pendingPermission.pending?(true)
let lateState: AudioRecorder.State = awaitResult { waiting.currentState($0) }
check(lateState == .stopped, "late permission approval cannot restart stopped recorder")
let cancelledExistingBytes = try Data(contentsOf: cancelledExistingURL)
check(cancelledExistingBytes == Data("permission-pending-must-preserve".utf8), "cancelled permission leaves existing file unchanged")
try FileManager.default.removeItem(at: cancelledExistingURL)

let timeoutSem = DispatchSemaphore(value: 0)
let timeoutInput = FakeInput()
let timeoutRecorder = AudioRecorder(configuration: try .init(inputTimeout: 0.15), callbackQueue: testQueue,
    callbacks: .init(onError: { error in
        check(error == .inputTimedOut, "silent backend produces timeout error"); timeoutSem.signal()
    }), permission: FakePermission(.authorized), input: timeoutInput)
let timeoutStart: Result<Void, AudioRecorder.RecorderError> = awaitResult { timeoutRecorder.start(completion: $0) }
check((try? timeoutStart.get()) != nil, "timeout test starts")
check(timeoutSem.wait(timeout: .now() + 3) == .success && timeoutInput.stopped, "timeout cleans up engine")

let deviceSem = DispatchSemaphore(value: 0)
let deviceInput = FakeInput()
let deviceRecorder = AudioRecorder(configuration: try .init(), callbackQueue: testQueue,
    callbacks: .init(onError: { error in
        check(error == .deviceDisconnected, "device disconnect propagated"); deviceSem.signal()
    }), permission: FakePermission(.authorized), input: deviceInput)
let deviceStart: Result<Void, AudioRecorder.RecorderError> = awaitResult { deviceRecorder.start(completion: $0) }
deviceInput.fail?(.deviceDisconnected)
check(deviceSem.wait(timeout: .now() + 3) == .success && deviceInput.stopped, "device failure cleans up engine")
let duplicateStop: Result<AudioRecorder.Recording, AudioRecorder.RecorderError> = awaitResult { deviceRecorder.stop(completion: $0) }
if case .failure(.invalidState) = duplicateStop { check(true, "stop after failure returns error") }
else { check(false, "stop after failure returns error") }

let tinyFormat = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
for length in [1, 2, 3, 6, 7] {
    let tiny = AVAudioPCMBuffer(pcmFormat: tinyFormat, frameCapacity: AVAudioFrameCount(length))!
    tiny.frameLength = AVAudioFrameCount(length)
    for i in 0..<length { tiny.floatChannelData![0][i] = 0.5 }
    let tinyConverter = try PCMConverter(inputFormat: tinyFormat)
    let tinyChunk = try tinyConverter.convert(tiny)
    check(abs(tinyChunk.level - 0.5) < 0.001, "inline Data RMS pointer lifetime \(length)")
}
let scratch = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"]!).appendingPathComponent("audio-no-overwrite-\(UUID()).pcm")
try Data("preserve-existing".utf8).write(to: scratch)
let exclusive = AudioRecorder(configuration: try .init(), callbackQueue: testQueue,
                              permission: FakePermission(.authorized), input: FakeInput())
let overwrite: Result<Void, AudioRecorder.RecorderError> = awaitResult { exclusive.start(outputURL: scratch, completion: $0) }
if case .failure(.fileIO) = overwrite { check(true, "existing recording rejected") }
else { check(false, "existing recording rejected") }
let preserved = try Data(contentsOf: scratch)
check(preserved == Data("preserve-existing".utf8), "existing bytes unchanged")
try FileManager.default.removeItem(at: scratch)
for rate in [16000.0, 24000, 44100, 48000] {
    let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
    let c = try PCMConverter(inputFormat: format)
    var bytes = Data()
    for _ in 0..<10 {
        let frameCount = AVAudioFrameCount(rate / 10)
        let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)!
        b.frameLength = frameCount
        for i in 0..<Int(frameCount) { b.floatChannelData![0][i] = 0.1 }
        bytes.append(try c.convert(b).data)
    }
    bytes.append(try c.finish().data)
    check(bytes.count == 32000, "one-second conversion length at \(rate) Hz")
}
let shortRing = PCMBufferRing(capacityBytes: 3 * 16000 * 2)
shortRing.append(converted)
check(shortRing.snapshot().count == 96000, "three-second ring remains bounded with ten seconds input")
print("TEST_FAILURES=\(failures)")
exit(failures == 0 ? 0 : 1)

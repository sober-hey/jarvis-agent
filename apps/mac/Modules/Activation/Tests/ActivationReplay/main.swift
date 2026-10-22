import AVFoundation
import Foundation
#if canImport(JarvisActivation)
import JarvisActivation
#endif

struct ReplayManifest: Decodable {
    struct Clip: Decodable {
        let path: String
        let label: String
        let expectedKind: ActivationKind?
        /// For double clap, onset of the SECOND clap; for snap, impulse onset.
        let triggerTime: Double?
    }
    let synthetic: Bool
    let clips: [Clip]
}
let arguments = CommandLine.arguments
func option(_ key: String) -> String? {
    guard let index = arguments.firstIndex(of: key), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}
guard let manifestPath = option("--manifest"), let reportPath = option("--report") else {
    print("Usage: activation-replay --manifest clips.json --report results.json [--coreml model.mlmodel] [--fallback]")
    exit(2)
}
let manifestURL = URL(fileURLWithPath: manifestPath)
let manifest = try JSONDecoder().decode(ReplayManifest.self, from: Data(contentsOf: manifestURL))
var configuration = ActivationConfiguration()
if arguments.contains("--fallback") { configuration.wakeWord.mode = .energyFallback }
var scorer: WakeWordScoring?
if let path = option("--coreml") {
    scorer = try CoreMLWakeWordScorer(modelURL: URL(fileURLWithPath: path),
        sampleRate: Double(option("--model-sample-rate") ?? "16000") ?? 16000,
        windowSize: Int(option("--model-window") ?? "16000") ?? 16000,
        inputName: option("--model-input") ?? "audio", outputName: option("--model-output") ?? "confidence")
}
let detector = try ActivationDetector(configuration: configuration, scorer: scorer)
var results = [[String: Any]]()
var positiveCount = 0, matchedCount = 0, negativeCount = 0, falsePositiveCount = 0
var latencyMS = [Double]()
var processingMS = [Double]()
var anyErrors = 0
for clip in manifest.clips {
    detector.reset()
    var events = [ActivationEvent]()
    var callbackAudioTimes = [Double]()
    var deliveryAudioTime: Double = 0
    var errors = [String]()
    detector.onActivation = { events.append($0); callbackAudioTimes.append(deliveryAudioTime) }
    detector.onError = { errors.append(String(describing: $0)) }
    let url = manifestURL.deletingLastPathComponent().appendingPathComponent(clip.path)
    let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatFloat32, interleaved: false)
    let format = file.processingFormat
    let blockSize = AVAudioFrameCount(max(1, Int(format.sampleRate * 0.01)))
    guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: blockSize) else { exit(2) }
    var samplesRead = 0
    while file.framePosition < file.length {
        try file.read(into: buffer, frameCount: blockSize)
        deliveryAudioTime = Double(samplesRead + Int(buffer.frameLength)) / format.sampleRate
        let start = DispatchTime.now().uptimeNanoseconds
        try detector.process(buffer: buffer, timestamp: Double(samplesRead) / format.sampleRate)
        processingMS.append(Double(DispatchTime.now().uptimeNanoseconds - start) / 1e6)
        samplesRead += Int(buffer.frameLength)
    }
    anyErrors += errors.count
    let matched = clip.expectedKind.map { kind in events.filter { $0.kind == kind } } ?? []
    if clip.expectedKind != nil {
        positiveCount += 1
        if matched.count == 1 { matchedCount += 1 }
    } else {
        negativeCount += 1
        if !events.isEmpty { falsePositiveCount += 1 }
    }
    var row: [String: Any] = ["path": clip.path, "label": clip.label,
        "sampleRate": format.sampleRate, "frames": samplesRead,
        "expectedKind": clip.expectedKind?.rawValue ?? "none",
        "events": events.map { ["kind": $0.kind.rawValue, "timestamp": $0.timestamp] as [String: Any] },
        "modelErrors": errors]
    if let trigger = clip.triggerTime,
       let index = events.firstIndex(where: { $0.kind == clip.expectedKind }) {
        let latency = (callbackAudioTimes[index] - trigger) * 1000
        latencyMS.append(latency); row["audioLatencyMS"] = latency
        row["analysisFrameLatencyMS"] = (events[index].timestamp - trigger) * 1000
    }
    results.append(row)
}
let report: [String: Any] = [
    "synthetic": manifest.synthetic, "modelSupplied": scorer != nil,
    "wakeStatus": String(describing: detector.wakeWordDetector.status),
    "positiveClips": positiveCount, "exactlyOneHitClips": matchedCount,
    "hitRate": positiveCount == 0 ? NSNull() : Double(matchedCount) / Double(positiveCount) as Any,
    "negativeClips": negativeCount, "falsePositiveClips": falsePositiveCount,
    "clipFalsePositiveRate": negativeCount == 0 ? NSNull() : Double(falsePositiveCount) / Double(negativeCount) as Any,
    "maxAudioLatencyMS": latencyMS.max() as Any? ?? NSNull(), "maxProcessingBlockMS": processingMS.max() as Any? ?? NSNull(),
    "modelErrorCount": anyErrors, "results": results,
    "limitations": "Synthetic replay accuracy is not measured human/microphone accuracy. No semantic Jarvis accuracy without a trained model. False positive denominator is negative clips, not audio frames."
]
try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    .write(to: URL(fileURLWithPath: reportPath))
print("WAV replay: \(matchedCount)/\(positiveCount) exactly-one hits; \(falsePositiveCount)/\(negativeCount) false-positive clips; max audio latency \(latencyMS.max() ?? 0) ms")
let falsePositiveRate = negativeCount == 0 ? 0 : Double(falsePositiveCount) / Double(negativeCount)
if matchedCount != positiveCount || anyErrors > 0 || falsePositiveRate >= 0.05 { exit(1) }

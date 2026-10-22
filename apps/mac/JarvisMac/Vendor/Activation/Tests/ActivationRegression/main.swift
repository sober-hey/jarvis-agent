import Foundation
#if canImport(JarvisActivation)
import JarvisActivation
#endif

var failures = [String]()
var passes = [String]()
func check(_ name: String, _ condition: Bool) {
    if condition { passes.append(name) } else { failures.append(name); print("FAIL: \(name)") }
}
func tone(_ frequency: Double, amplitude: Float = 0.5, n: Int = 2048, rate: Double = 48000) -> [Float] {
    (0..<n).map { amplitude * Float(sin(2 * Double.pi * frequency * Double($0) / rate)) }
}
func feature(_ time: Double, clap: Float = -100, snap: Float = -100,
             clapFraction: Float = 0, snapFraction: Float = 0, crest: Float = 5) -> AudioFeatures {
    AudioFeatures(timestamp: time, duration: 0.01, rmsDBFS: -20,
                  clapDBFS: clap, snapDBFS: snap, clapEnergyFraction: clapFraction,
                  snapEnergyFraction: snapFraction, crestFactor: crest)
}

let fft = try FFTAnalyzer(size: 2048)
let spectrum = try fft.analyze(tone(3000), sampleRate: 48000)
check("FFT calibrated sine RMS", abs(spectrum.bandDBFS(1000...5000) - (-9.0309)) < 0.2)
check("FFT rejects out-of-band", spectrum.bandDBFS(8000...12000) < -65)
check("FFT invalid rate", (try? fft.analyze(tone(1000), sampleRate: 0)) == nil)
check("FFT invalid length", (try? fft.analyze([0], sampleRate: 48000)) == nil)
check("FFT non-power-of-two", (try? FFTAnalyzer(size: 1000)) == nil)

let gestures = GestureDetector()
_ = gestures.process(feature(0))
check("one clap is not double clap", gestures.process(feature(0.1, clap: -15, clapFraction: 0.8)).isEmpty)
_ = gestures.process(feature(0.15))
let clap = gestures.process(feature(0.4, clap: -15, clapFraction: 0.8))
check("double clap 300ms", clap.map(\.kind) == [.doubleClap])
check("clap not also snap", !clap.contains { $0.kind == .fingerSnap })
gestures.reset()
_ = gestures.process(feature(0))
_ = gestures.process(feature(0.1, clap: -15, clapFraction: 0.8))
_ = gestures.process(feature(0.16))
check("clap too close", gestures.process(feature(0.18, clap: -15, clapFraction: 0.8)).isEmpty)
gestures.reset()
_ = gestures.process(feature(0))
_ = gestures.process(feature(0.1, clap: -15, clapFraction: 0.8))
_ = gestures.process(feature(0.2))
check("clap too far", gestures.process(feature(1.2, clap: -15, clapFraction: 0.8)).isEmpty)
gestures.reset()
_ = gestures.process(feature(0))
check("snap rising edge", gestures.process(feature(0.1, snap: -15, snapFraction: 0.8)).map(\.kind) == [.fingerSnap])
check("sustained snap band no repeat", gestures.process(feature(0.12, snap: -15, snapFraction: 0.8)).isEmpty)
gestures.reset()
_ = gestures.process(feature(0))
check("quiet snap rejected", gestures.process(feature(0.1, snap: -36, snapFraction: 0.8)).isEmpty)
check("low crest music rejected", gestures.process(feature(0.2, snap: -10, snapFraction: 0.8, crest: 1.4)).isEmpty)

final class FakeScorer: WakeWordScoring {
    let sampleRate: Double = 16000
    let windowSize: Int = 1600
    var confidence: Float = 0.6
    var calls = 0
    func score(samples: [Float]) throws -> Float { calls += 1; return confidence }
}
let scorer = FakeScorer()
let wake = WakeWordDetector(scorer: scorer)
check("wake below threshold", try wake.process(samples: tone(800, n: 1600, rate: 16000), sampleRate: 16000, timestamp: 0).isEmpty)
scorer.confidence = 0.7
check("wake confidence accepted", try wake.process(samples: tone(800, n: 1600, rate: 16000), sampleRate: 16000, timestamp: 0.1).map(\.kind) == [.wakeWord])
check("wake cooldown", try wake.process(samples: tone(800, n: 1600, rate: 16000), sampleRate: 16000, timestamp: 0.2).isEmpty)
check("wake wrong rate fails closed", (try? wake.process(samples: [0], sampleRate: 48000, timestamp: 0.3)) == nil)
let fallback = WakeWordDetector()
check("no model no fake Jarvis", try fallback.process(samples: tone(500, n: 48000), sampleRate: 48000, timestamp: 0).isEmpty)
check("missing model status explicit", fallback.status == .unavailable)
let energy = WakeWordDetector(configuration: .init(mode: .energyFallback, energyDuration: 3))
check("fallback needs 3s", try energy.process(samples: tone(500, n: 96000), sampleRate: 48000, timestamp: 0).isEmpty)
check("fallback not semantic confidence", try energy.process(samples: tone(500, n: 48000), sampleRate: 48000, timestamp: 2).first?.kind == .voiceActivityFallback)

// Real Accelerate path: tone bursts with sharp envelopes, not recorded human audio.
func burst(_ frequency: Double, length: Int = 384, amplitude: Float = 0.9) -> [Float] {
    (0..<length).map { amplitude * Float(sin(2 * Double.pi * frequency * Double($0) / 48000)) * exp(-Float($0) / 180) }
}
let pipeline = try ActivationDetector()
var events = [ActivationEvent]()
pipeline.onActivation = { events.append($0) }
var pcm = [Float](repeating: 0, count: 48000)
for start in [4800, 19200] {
    for (i, value) in burst(3000).enumerated() { pcm[start + i] = value }
}
for offset in stride(from: 0, to: pcm.count, by: 480) {
    try pipeline.process(samples: Array(pcm[offset..<min(offset + 480, pcm.count)]), sampleRate: 48000,
                         timestamp: Double(offset) / 48000)
}
check("PCM double clap integration", events.map(\.kind) == [.doubleClap])
pipeline.reset(); events.removeAll()
pcm = [Float](repeating: 0, count: 24000)
for (i, value) in burst(10000).enumerated() { pcm[4800 + i] = value }
for offset in stride(from: 0, to: pcm.count, by: 480) {
    try pipeline.process(samples: Array(pcm[offset..<min(offset + 480, pcm.count)]), sampleRate: 48000,
                         timestamp: Double(offset) / 48000)
}
check("PCM snap integration", events.map(\.kind) == [.fingerSnap])
check("PCM snap timestamp latency", events.first.map { $0.timestamp - 0.1 < 0.1 } ?? false)

pipeline.reset(); events.removeAll()
let start = DispatchTime.now().uptimeNanoseconds
var worst: UInt64 = 0
for i in 0..<500 {
    let before = DispatchTime.now().uptimeNanoseconds
    try pipeline.process(samples: tone(3000, n: 480), sampleRate: 48000, timestamp: Double(i) * 0.01)
    worst = max(worst, DispatchTime.now().uptimeNanoseconds - before)
}
let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start) / 1e6
check("steady tone no false gestures", events.isEmpty)
check("processing under 500ms", Double(worst) / 1e6 < 500)

// Boundary and state regressions (not human examples).
for interval in [0.2, 0.5] {
    gestures.reset()
    _ = gestures.process(feature(0))
    _ = gestures.process(feature(0.1, clap: -15, clapFraction: 0.8))
    _ = gestures.process(feature(0.15))
    check("clap interval boundary \(interval)", gestures.process(feature(0.1 + interval, clap: -15, clapFraction: 0.8)).map(\.kind) == [.doubleClap])
}
gestures.reset()
_ = gestures.process(feature(0))
let ambiguous = gestures.process(feature(0.1, clap: -15, snap: -15, clapFraction: 0.4, snapFraction: 0.6))
check("ambiguous impulse only one kind", ambiguous.map(\.kind) == [.fingerSnap])
check("NaN timestamp rejected", gestures.process(feature(.nan, snap: -10, snapFraction: 0.9)).isEmpty)

var badConfig = ActivationConfiguration()
badConfig.wakeWord.confidenceThreshold = 0.4
check("threshold below 0.5 rejected", (try? ActivationDetector(configuration: badConfig)) == nil)
check("NaN input rejected", (try? pipeline.process(samples: [.nan], sampleRate: 48000, timestamp: 5)) == nil)
check("FFT NaN input rejected", (try? fft.analyze([Float](repeating: .nan, count: 2048), sampleRate: 48000)) == nil)
let lowRate = try ActivationDetector()
try lowRate.process(samples: [Float](repeating: 0, count: 1600), sampleRate: 16000, timestamp: 0)
check("16kHz reports no snap", !lowRate.snapAvailable)

let resampleA = try StreamingResampler(inputRate: 48000, outputRate: 16000)
let resampleB = try StreamingResampler(inputRate: 48000, outputRate: 16000)
let sine = tone(1000, n: 48000)
let once = resampleA.process(sine)
var chunks = [Float]()
for offset in stride(from: 0, to: sine.count, by: 479) {
    chunks += resampleB.process(Array(sine[offset..<min(offset + 479, sine.count)]))
}
check("resampler arbitrary chunk invariance", once == chunks)
check("resampler streaming output count", once.count >= 15990 && once.count <= 16000)
let passPower = once.dropFirst(100).reduce(Float(0)) { $0 + $1 * $1 } / Float(once.count - 100)
check("resampler preserves pass band", abs(PowerSpectrum.dbfs(power: passPower) - (-9.0309)) < 0.3)
resampleA.reset()
let aboveNyquist = resampleA.process(tone(10000, n: 48000))
let stopPower = aboveNyquist.dropFirst(100).reduce(Float(0)) { $0 + $1 * $1 } / Float(aboveNyquist.count - 100)
check("resampler rejects alias", PowerSpectrum.dbfs(power: stopPower) < -50)

// Unified callback independence: a model success must not cancel simultaneous gestures.
let alwaysWake = FakeScorer(); alwaysWake.confidence = 0.9
let combined = try ActivationDetector(scorer: alwaysWake)
var combinedEvents = [ActivationEvent]()
combined.onActivation = { combinedEvents.append($0) }
for offset in stride(from: 0, to: pcm.count, by: 480) {
    try combined.process(samples: Array(pcm[offset..<min(offset + 480, pcm.count)]), sampleRate: 48000, timestamp: Double(offset) / 48000)
}
check("model and snap parallel", Set(combinedEvents.map(\.kind)) == Set([.wakeWord, .fingerSnap]))
final class FailingScorer: WakeWordScoring {
    let sampleRate: Double = 16000
    let windowSize = 1600
    func score(samples: [Float]) throws -> Float { throw ActivationError.invalidConfidence }
}
let isolated = try ActivationDetector(scorer: FailingScorer())
var isolatedEvents = [ActivationEvent]()
var modelErrors = 0
isolated.onActivation = { isolatedEvents.append($0) }
isolated.onError = { _ in modelErrors += 1 }
for offset in stride(from: 0, to: pcm.count, by: 480) {
    try isolated.process(samples: Array(pcm[offset..<min(offset + 480, pcm.count)]), sampleRate: 48000, timestamp: Double(offset) / 48000)
}
check("model errors reported", modelErrors > 0)
check("model failure does not suppress snap", isolatedEvents.map(\.kind) == [.fingerSnap])
scorer.confidence = .nan
wake.reset()
check("NaN model confidence rejected", (try? wake.process(samples: tone(800, n: 1600, rate: 16000), sampleRate: 16000, timestamp: 0)) == nil)

let report: [String: Any] = ["passed": passes, "failed": failures, "passedCount": passes.count,
                           "failedCount": failures.count, "syntheticOnly": true,
                           "processing500BlocksMS": elapsed, "worstBlockMS": Double(worst) / 1e6,
                           "realMicrophoneAcceptance": "NOT_RUN",
                           "humanFalsePositiveRate": "NOT_MEASURED"]
if let index = CommandLine.arguments.firstIndex(of: "--report"), CommandLine.arguments.count > index + 1 {
    try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        .write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
}
print("\(passes.count) passed, \(failures.count) failed; worst block \(Double(worst) / 1e6) ms")
if !failures.isEmpty { exit(1) }

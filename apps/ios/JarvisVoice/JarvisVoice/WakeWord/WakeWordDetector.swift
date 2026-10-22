import Foundation

struct WakeWordConfiguration {
    let confidenceThreshold: Float = 0.8
    let inferenceHop: TimeInterval = 0.08
    let cooldown: TimeInterval = 1.5
}

/// Stateful detector copied from the existing Activation implementation.
/// This app integration deliberately exposes model-backed events only.
final class WakeWordDetector {
    let configuration: WakeWordConfiguration
    let scorer: WakeWordScoring
    private var pending = [Float]()
    private var pendingStart: TimeInterval?
    private var expectedStart: TimeInterval?
    private var lastEvent = -Double.infinity

    init(scorer: WakeWordScoring, configuration: WakeWordConfiguration = .init()) {
        self.scorer = scorer
        self.configuration = configuration
    }

    func reset() {
        pending.removeAll(keepingCapacity: true)
        pendingStart = nil
        expectedStart = nil
        lastEvent = -.infinity
    }

    func process(samples: [Float], sampleRate: Double, timestamp: TimeInterval) throws -> [ActivationEvent] {
        guard sampleRate.isFinite, sampleRate > 0, timestamp.isFinite else { throw ActivationError.invalidSampleRate }
        guard samples.allSatisfy(\.isFinite) else { throw ActivationError.nonFiniteAudio }
        guard !samples.isEmpty else { return [] }
        if let expected = expectedStart, abs(timestamp - expected) > 2 / sampleRate { reset() }
        expectedStart = timestamp + Double(samples.count) / sampleRate
        guard abs(sampleRate - scorer.sampleRate) < 0.01 else { throw ActivationError.sampleRateMismatch }
        if pendingStart == nil { pendingStart = timestamp }
        pending.append(contentsOf: samples)
        let hop = min(scorer.windowSize, max(1, Int(configuration.inferenceHop * sampleRate)))
        var events = [ActivationEvent]()
        while pending.count >= scorer.windowSize {
            let end = pendingStart! + Double(scorer.windowSize) / sampleRate
            let window = Array(pending.prefix(scorer.windowSize))
            pending.removeFirst(hop)
            pendingStart! += Double(hop) / sampleRate
            let confidence = try scorer.score(samples: window)
            guard confidence.isFinite, (0...1).contains(confidence) else { throw ActivationError.invalidConfidence }
            if confidence >= configuration.confidenceThreshold, end - lastEvent >= configuration.cooldown {
                lastEvent = end
                events.append(ActivationEvent(kind: .wakeWord, timestamp: end, confidence: confidence))
            }
        }
        return events
    }
}

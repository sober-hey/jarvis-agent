import CoreML
import Foundation

/// Inject an offline CoreML scorer or an openWakeWord adapter with this contract.
/// Input must be finite mono PCM [-1, 1], at sampleRate, windowSize samples.
public protocol WakeWordScoring: AnyObject {
    var sampleRate: Double { get }
    var windowSize: Int { get }
    func score(samples: [Float]) throws -> Float
}

/// Explicit model contract: float32 raw PCM tensor -> Jarvis probability.
/// Feature/mel/stateful models require their own WakeWordScoring adapter.
public final class CoreMLWakeWordScorer: WakeWordScoring {
    public let sampleRate: Double
    public let windowSize: Int
    private let model: MLModel
    private let inputName: String
    private let outputName: String
    private let outputIndex: Int
    private let shape: [NSNumber]

    public init(modelURL: URL, sampleRate: Double = 16000, windowSize: Int = 16000,
                inputName: String = "audio", outputName: String = "confidence", outputIndex: Int = 0) throws {
        guard sampleRate.isFinite, sampleRate > 0, windowSize > 0, windowSize <= 192000,
              outputIndex >= 0 else { throw ActivationError.modelSchema("Invalid model dimensions.") }
        let compiledURL = modelURL.pathExtension == "mlmodelc" ? modelURL : try MLModel.compileModel(at: modelURL)
        let config = MLModelConfiguration()
        config.computeUnits = .cpuOnly
        model = try MLModel(contentsOf: compiledURL, configuration: config)
        guard let input = model.modelDescription.inputDescriptionsByName[inputName],
              input.type == .multiArray, let constraint = input.multiArrayConstraint,
              constraint.dataType == .float32,
              constraint.shape.allSatisfy({ $0.intValue > 0 }),
              constraint.shape.reduce(1, { $0 * $1.intValue }) == windowSize,
              model.modelDescription.outputDescriptionsByName[outputName] != nil,
              model.modelDescription.inputDescriptionsByName.count == 1 else {
            throw ActivationError.modelSchema("Expected one fixed float32 PCM tensor and a probability output. Check names/shape/preprocessing.")
        }
        self.sampleRate = sampleRate; self.windowSize = windowSize
        self.inputName = inputName; self.outputName = outputName; self.outputIndex = outputIndex
        shape = constraint.shape
    }
    public func score(samples: [Float]) throws -> Float {
        guard samples.count == windowSize else { throw ActivationError.invalidFrameLength }
        guard samples.allSatisfy(\.isFinite) else { throw ActivationError.nonFiniteAudio }
        let array = try MLMultiArray(shape: shape, dataType: .float32)
        for (index, value) in samples.enumerated() { array[index] = NSNumber(value: value) }
        let input = try MLDictionaryFeatureProvider(dictionary: [inputName: array])
        let output = try model.prediction(from: input)
        guard let value = output.featureValue(for: outputName) else {
            throw ActivationError.modelSchema("Missing wake probability output.")
        }
        let probability: Float
        if value.type == .double, outputIndex == 0 {
            probability = Float(value.doubleValue)
        } else if let vector = value.multiArrayValue, outputIndex < vector.count {
            probability = vector[outputIndex].floatValue
        } else { throw ActivationError.modelSchema("Expected scalar double or indexed probability tensor.") }
        guard probability.isFinite, (0...1).contains(probability) else { throw ActivationError.invalidConfidence }
        return probability
    }
}

public struct WakeWordConfiguration: Sendable {
    public enum Mode: Sendable { case model, energyFallback, disabled }
    public var mode: Mode
    public var confidenceThreshold: Float
    public var inferenceHop: TimeInterval
    public var cooldown: TimeInterval
    public var energyThresholdDBFS: Float
    public var energyDuration: TimeInterval
    public init(mode: Mode = .model, confidenceThreshold: Float = 0.8,
                inferenceHop: TimeInterval = 0.08, cooldown: TimeInterval = 1.5,
                energyThresholdDBFS: Float = -40, energyDuration: TimeInterval = 3) {
        self.mode = mode; self.confidenceThreshold = confidenceThreshold
        self.inferenceHop = inferenceHop; self.cooldown = cooldown
        self.energyThresholdDBFS = energyThresholdDBFS; self.energyDuration = energyDuration
    }
    public func validate() throws {
        guard confidenceThreshold.isFinite, confidenceThreshold >= 0.8, confidenceThreshold <= 1,
              inferenceHop.isFinite, inferenceHop > 0, inferenceHop < 0.5,
              cooldown.isFinite, cooldown >= 0, energyThresholdDBFS.isFinite,
              energyDuration.isFinite, (3...5).contains(energyDuration) else {
            throw ActivationError.invalidConfiguration("Wake threshold must be >=0.8; hop <500ms; fallback duration 3...5s.")
        }
    }
}

/// No default loudness-as-keyword behavior. Missing model is explicit/unavailable.
/// `energyFallback` is diagnostic voice activity only, never .wakeWord or confidence 0.8.
public final class WakeWordDetector {
    public enum Status: Equatable, Sendable { case modelReady, energyFallback, unavailable, disabled }
    public let configuration: WakeWordConfiguration
    public let scorer: WakeWordScoring?
    public var status: Status {
        switch configuration.mode {
        case .disabled: return .disabled
        case .energyFallback: return .energyFallback
        case .model: return scorer == nil ? .unavailable : .modelReady
        }
    }
    private var pending = [Float]()
    private var pendingStart: TimeInterval?
    private var expectedStart: TimeInterval?
    private var loudDuration: TimeInterval = 0
    private var lastEvent = -Double.infinity
    public init(scorer: WakeWordScoring? = nil, configuration: WakeWordConfiguration = .init()) {
        self.scorer = scorer; self.configuration = configuration
    }
    public func reset() {
        pending.removeAll(keepingCapacity: true); pendingStart = nil; expectedStart = nil
        loudDuration = 0; lastEvent = -.infinity
    }
    /// timestamp is the start of this audio block, not wall clock time.
    public func process(samples: [Float], sampleRate: Double, timestamp: TimeInterval) throws -> [ActivationEvent] {
        try configuration.validate()
        guard sampleRate.isFinite, sampleRate > 0, timestamp.isFinite else { throw ActivationError.invalidSampleRate }
        guard samples.allSatisfy(\.isFinite) else { throw ActivationError.nonFiniteAudio }
        guard !samples.isEmpty else { return [] }
        if let expected = expectedStart, abs(timestamp - expected) > 2 / sampleRate { reset() }
        expectedStart = timestamp + Double(samples.count) / sampleRate
        switch status {
        case .disabled, .unavailable: return []
        case .energyFallback:
            var events = [ActivationEvent]()
            // 10ms chunks avoid treating arbitrary caller block sizes as a whole utterance.
            let count = max(1, Int(sampleRate * 0.01))
            for offset in stride(from: 0, to: samples.count, by: count) {
                let stop = min(offset + count, samples.count)
                let power = samples[offset..<stop].reduce(Float(0)) { $0 + $1 * $1 } / Float(stop - offset)
                let db = PowerSpectrum.dbfs(power: power)
                loudDuration = db > configuration.energyThresholdDBFS ? loudDuration + Double(stop - offset) / sampleRate : 0
                let end = timestamp + Double(stop) / sampleRate
                if loudDuration + 1e-6 >= configuration.energyDuration, end - lastEvent >= configuration.cooldown {
                    events.append(.init(kind: .voiceActivityFallback, timestamp: end, energyDBFS: db))
                    loudDuration = 0; lastEvent = end
                }
            }
            return events
        case .modelReady:
            guard let scorer = scorer else { return [] }
            guard abs(sampleRate - scorer.sampleRate) < 0.01 else { throw ActivationError.sampleRateMismatch }
            guard scorer.windowSize > 0, scorer.windowSize <= 192000 else {
                throw ActivationError.modelSchema("Invalid scorer window size.")
            }
            if pendingStart == nil { pendingStart = timestamp }
            pending.append(contentsOf: samples)
            let hop = min(scorer.windowSize, max(1, Int(configuration.inferenceHop * sampleRate)))
            var events = [ActivationEvent]()
            while pending.count >= scorer.windowSize {
                let end = pendingStart! + Double(scorer.windowSize) / sampleRate
                // Advance before scoring: a throwing model cannot leave unbounded stale input.
                let window = Array(pending.prefix(scorer.windowSize))
                pending.removeFirst(hop); pendingStart! += Double(hop) / sampleRate
                let confidence = try scorer.score(samples: window)
                guard confidence.isFinite, (0...1).contains(confidence) else { throw ActivationError.invalidConfidence }
                if confidence >= configuration.confidenceThreshold, end - lastEvent >= configuration.cooldown {
                    lastEvent = end
                    events.append(.init(kind: .wakeWord, timestamp: end, confidence: confidence))
                }
            }
            return events
        }
    }
}

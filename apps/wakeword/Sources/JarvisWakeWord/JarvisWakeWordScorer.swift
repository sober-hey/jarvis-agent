import CoreML
import Foundation
#if !os(watchOS)
import JarvisActivation
#else
// Existing Activation's generic loader calls compileModel(at:), unavailable on Watch.
// Keep the same PCM contract without importing that Mac/iOS-only implementation.
public protocol WakeWordScoring: AnyObject {
    var sampleRate: Double { get }
    var windowSize: Int { get }
    func score(samples: [Float]) throws -> Float
}
public enum ActivationError: Error {
    case modelSchema(String), invalidFrameLength, nonFiniteAudio, invalidConfidence
}
#endif

/// openWakeWord frontend + a Jarvis-only trained head. No ASR/network/VAD.
/// One serial worker, 1280 NON-overlapping mono Float32 samples @16kHz per score.
/// Configure WakeWordConfiguration(inferenceHop: 0.08); never overlapping windows.
/// Call reset() on capture restart/discontinuity alongside WakeWordDetector.reset().
public final class JarvisWakeWordScorer: WakeWordScoring {
    public let sampleRate: Double = 16000
    public let windowSize = 1280
    private let melModel: MLModel
    private let embeddingModel: MLModel
    private let headModel: MLModel
    private var tail = [Float](repeating: 0, count: 480)
    private var mel = [Float](repeating: 1, count: 76 * 32)
    private var features = [Float](repeating: 0, count: 16 * 96)
    private var frames = 0

    public init(modelsDirectory: URL, computeUnits: MLComputeUnits = .cpuOnly) throws {
        func load(_ name: String) throws -> MLModel {
            let compiled = modelsDirectory.appendingPathComponent(name + ".mlmodelc")
            let url: URL
            if FileManager.default.fileExists(atPath: compiled.path) { url = compiled }
            else {
                #if os(watchOS)
                throw ActivationError.modelSchema("Watch requires bundled precompiled .mlmodelc assets; runtime compilation is unavailable")
                #else
                url = try MLModel.compileModel(at: modelsDirectory.appendingPathComponent(name + ".mlpackage"))
                #endif
            }
            let config = MLModelConfiguration(); config.computeUnits = computeUnits
            return try MLModel(contentsOf: url, configuration: config)
        }
        melModel = try load("Melspectrogram")
        embeddingModel = try load("SpeechEmbedding")
        headModel = try load("JarvisClassifier")
        try validate(melModel, input: "audio", count: 1760, output: "mel", outputCount: 256)
        try validate(embeddingModel, input: "mel", count: 2432, output: "embedding", outputCount: 96)
        try validate(headModel, input: "features", count: 1536, output: "confidence", outputCount: 1)
    }
    private func validate(_ model: MLModel, input: String, count: Int, output: String, outputCount: Int) throws {
        guard model.modelDescription.inputDescriptionsByName.count == 1,
              let c = model.modelDescription.inputDescriptionsByName[input]?.multiArrayConstraint,
              c.dataType == .float32, c.shape.reduce(1, { $0 * $1.intValue }) == count,
              let out = model.modelDescription.outputDescriptionsByName[output]?.multiArrayConstraint,
              out.shape.reduce(1, { $0 * $1.intValue }) == outputCount else {
            throw ActivationError.modelSchema("Wakeword model schema mismatch: \(input)/\(output)")
        }
    }
    public func reset() {
        tail = [Float](repeating: 0, count: 480)
        mel = [Float](repeating: 1, count: 76 * 32)
        features = [Float](repeating: 0, count: 16 * 96)
        frames = 0
    }
    private func predict(_ model: MLModel, input: String, values: [Float], output: String) throws -> [Float] {
        guard let shape = model.modelDescription.inputDescriptionsByName[input]?.multiArrayConstraint?.shape else {
            throw ActivationError.modelSchema("Missing input shape")
        }
        let array = try MLMultiArray(shape: shape, dataType: .float32)
        let pointer = array.dataPointer.bindMemory(to: Float.self, capacity: values.count)
        values.withUnsafeBufferPointer { source in pointer.update(from: source.baseAddress!, count: values.count) }
        let result = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: [input: array]))
        guard let out = result.featureValue(for: output)?.multiArrayValue else {
            throw ActivationError.modelSchema("Missing tensor output")
        }
        return (0..<out.count).map { out[$0].floatValue }
    }
    public func score(samples: [Float]) throws -> Float {
        guard samples.count == windowSize else { throw ActivationError.invalidFrameLength }
        guard samples.allSatisfy({ $0.isFinite && abs($0) <= 1 }) else { throw ActivationError.nonFiniteAudio }
        // Match upstream int16 PCM scale, including saturation at +1.0.
        let pcm = samples.map { Float(Int16(max(-32768, min(32767, ($0 * 32768).rounded(.towardZero))))) }
        let raw = tail + pcm
        tail = Array(raw.suffix(480))
        let newestMel = try predict(melModel, input: "audio", values: raw, output: "mel")
        mel.removeFirst(256); mel.append(contentsOf: newestMel)
        let newestEmbedding = try predict(embeddingModel, input: "mel", values: mel, output: "embedding")
        features.removeFirst(96); features.append(contentsOf: newestEmbedding)
        let probability = try predict(headModel, input: "features", values: features, output: "confidence")[0]
        guard probability.isFinite, (0...1).contains(probability) else { throw ActivationError.invalidConfidence }
        frames += 1
        return frames < 5 ? 0 : probability
    }
}

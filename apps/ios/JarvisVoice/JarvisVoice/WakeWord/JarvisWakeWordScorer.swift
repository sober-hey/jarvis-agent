import CoreML
import Foundation

/// The existing offline openWakeWord-derived frontend and Jarvis classifier.
/// Models are loaded only from the host application's bundled resources.
final class JarvisWakeWordScorer: WakeWordScoring {
    let sampleRate: Double = 16000
    let windowSize = 1280
    private let melModel: MLModel
    private let embeddingModel: MLModel
    private let headModel: MLModel
    private var tail = [Float](repeating: 0, count: 480)
    private var mel = [Float](repeating: 1, count: 76 * 32)
    private var features = [Float](repeating: 0, count: 16 * 96)
    private var frames = 0

    init(modelsDirectory: URL, computeUnits: MLComputeUnits = .cpuOnly) throws {
        func resource(_ name: String, extension: String) -> URL? {
            let expected = name + "." + `extension`
            guard let root = FileManager.default.enumerator(
                at: modelsDirectory,
                includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]
            ) else { return nil }
            for case let candidate as URL in root where candidate.lastPathComponent == expected {
                return candidate
            }
            return nil
        }
        func load(_ name: String) throws -> MLModel {
            let compiled = resource(name, extension: "mlmodelc")
            let package = resource(name, extension: "mlpackage")
            let url: URL
            if let compiled { url = compiled }
            else if let package { url = try MLModel.compileModel(at: package) }
            else { throw ActivationError.modelSchema("Missing bundled wake-word model: \(name)") }
            let config = MLModelConfiguration()
            config.computeUnits = computeUnits
            return try MLModel(contentsOf: url, configuration: config)
        }
        melModel = try load("Melspectrogram")
        embeddingModel = try load("SpeechEmbedding")
        headModel = try load("JarvisClassifier")
        try validate(melModel, input: "audio", count: 1760, output: "mel", outputCount: 256)
        try validate(embeddingModel, input: "mel", count: 2432, output: "embedding", outputCount: 96)
        try validate(headModel, input: "features", count: 1536, output: "confidence", outputCount: 1)
    }

    func reset() {
        tail = [Float](repeating: 0, count: 480)
        mel = [Float](repeating: 1, count: 76 * 32)
        features = [Float](repeating: 0, count: 16 * 96)
        frames = 0
    }

    private func validate(_ model: MLModel, input: String, count: Int, output: String, outputCount: Int) throws {
        guard model.modelDescription.inputDescriptionsByName.count == 1,
              let c = model.modelDescription.inputDescriptionsByName[input]?.multiArrayConstraint,
              c.dataType == .float32, c.shape.reduce(1, { $0 * $1.intValue }) == count,
              let out = model.modelDescription.outputDescriptionsByName[output]?.multiArrayConstraint,
              out.shape.reduce(1, { $0 * $1.intValue }) == outputCount else {
            throw ActivationError.modelSchema("Wake-word model schema mismatch: \(input)/\(output)")
        }
    }

    private func predict(_ model: MLModel, input: String, values: [Float], output: String) throws -> [Float] {
        guard let shape = model.modelDescription.inputDescriptionsByName[input]?.multiArrayConstraint?.shape else {
            throw ActivationError.modelSchema("Missing wake-word tensor input: \(input)")
        }
        let array = try MLMultiArray(shape: shape, dataType: .float32)
        let pointer = array.dataPointer.bindMemory(to: Float.self, capacity: values.count)
        values.withUnsafeBufferPointer { source in
            pointer.update(from: source.baseAddress!, count: values.count)
        }
        let result = try model.prediction(from: MLDictionaryFeatureProvider(dictionary: [input: array]))
        guard let out = result.featureValue(for: output)?.multiArrayValue else {
            throw ActivationError.modelSchema("Missing wake-word tensor output: \(output)")
        }
        return (0..<out.count).map { out[$0].floatValue }
    }

    func score(samples: [Float]) throws -> Float {
        guard samples.count == windowSize else { throw ActivationError.invalidFrameLength }
        guard samples.allSatisfy({ $0.isFinite && abs($0) <= 1 }) else { throw ActivationError.nonFiniteAudio }
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

import Foundation

protocol WakeWordScoring: AnyObject {
    var sampleRate: Double { get }
    var windowSize: Int { get }
    func score(samples: [Float]) throws -> Float
}

enum ActivationKind { case wakeWord }

struct ActivationEvent {
    let kind: ActivationKind
    let timestamp: TimeInterval
    let confidence: Float?
}

enum ActivationError: Error, LocalizedError {
    case invalidSampleRate
    case invalidFrameLength
    case nonFiniteAudio
    case invalidConfiguration(String)
    case modelSchema(String)
    case invalidConfidence
    case sampleRateMismatch

    var errorDescription: String? {
        switch self {
        case .invalidSampleRate: return "Wake-word sample rate must be finite and positive."
        case .invalidFrameLength: return "Incorrect wake-word frame length."
        case .nonFiniteAudio: return "Wake-word audio contains NaN or infinity."
        case .invalidConfiguration(let message), .modelSchema(let message): return message
        case .invalidConfidence: return "Wake-word confidence must be a finite probability in 0...1."
        case .sampleRateMismatch: return "Wake-word input must be resampled to 16 kHz."
        }
    }
}

import Foundation

public enum ActivationKind: String, Codable, Sendable {
    case wakeWord, doubleClap, fingerSnap
    /// Diagnostic opt-in only. Not proof that "Jarvis" was spoken.
    case voiceActivityFallback
}

public struct ActivationEvent: Codable, Equatable, Sendable {
    public let kind: ActivationKind
    /// End-of-analysis time in the caller's monotonic audio clock, in seconds.
    public let timestamp: TimeInterval
    /// Only model-backed wake words carry semantic confidence.
    public let confidence: Float?
    public let energyDBFS: Float?
    public init(kind: ActivationKind, timestamp: TimeInterval, confidence: Float? = nil, energyDBFS: Float? = nil) {
        self.kind = kind; self.timestamp = timestamp
        self.confidence = confidence; self.energyDBFS = energyDBFS
    }
}

public typealias ActivationCallback = (ActivationEvent) -> Void

public enum ActivationError: Error, LocalizedError {
    case invalidFFTSize, invalidSampleRate, invalidFrameLength, nonFiniteAudio
    case invalidConfiguration(String), modelSchema(String), invalidConfidence, sampleRateMismatch
    public var errorDescription: String? {
        switch self {
        case .invalidFFTSize: return "FFT size must be a power of two, at least 64."
        case .invalidSampleRate: return "Sample rate must be finite and positive."
        case .invalidFrameLength: return "Incorrect FFT frame length."
        case .nonFiniteAudio: return "Audio contains NaN or infinity."
        case .invalidConfiguration(let message), .modelSchema(let message): return message
        case .invalidConfidence: return "Model confidence must be a finite probability in 0...1."
        case .sampleRateMismatch: return "Feed the wake model its declared sample rate; resample before processing."
        }
    }
}

public struct AudioFeatures: Sendable {
    public let timestamp: TimeInterval
    public let duration: TimeInterval
    public let rmsDBFS: Float
    public let clapDBFS: Float
    public let snapDBFS: Float
    public let clapEnergyFraction: Float
    public let snapEnergyFraction: Float
    public let crestFactor: Float
    public init(timestamp: TimeInterval, duration: TimeInterval, rmsDBFS: Float,
                clapDBFS: Float, snapDBFS: Float, clapEnergyFraction: Float,
                snapEnergyFraction: Float, crestFactor: Float) {
        self.timestamp = timestamp; self.duration = duration; self.rmsDBFS = rmsDBFS
        self.clapDBFS = clapDBFS; self.snapDBFS = snapDBFS
        self.clapEnergyFraction = clapEnergyFraction; self.snapEnergyFraction = snapEnergyFraction
        self.crestFactor = crestFactor
    }
}

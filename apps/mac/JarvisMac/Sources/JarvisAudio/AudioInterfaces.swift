import AVFoundation
import Accelerate
import JarvisCore

/// Owner: audio capture / VAD / DSP worker. No microphone starts at import time.
@MainActor
public protocol AudioCaptureService: AnyObject {
    var isRunning: Bool { get }
    var onBuffer: ((AVAudioPCMBuffer, AVAudioTime) -> Void)? { get set }
    func start() throws
    func stop()
}

@MainActor
public protocol AudioPlaybackService: AnyObject {
    func enqueue(_ buffer: AVAudioPCMBuffer) throws
    func stop()
}

/// Tiny real Accelerate probe; FFT implementation belongs to the audio worker.
public enum AudioDSP {
    public static func rootMeanSquare(_ samples: [Float]) -> Float {
        guard !samples.isEmpty else { return 0 }
        var value: Float = 0
        vDSP_rmsqv(samples, 1, &value, vDSP_Length(samples.count))
        return value
    }
}

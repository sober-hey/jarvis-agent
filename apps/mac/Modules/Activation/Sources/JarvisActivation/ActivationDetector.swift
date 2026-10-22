import AVFoundation
import Foundation

public struct ActivationConfiguration: Sendable {
    public var fftSize: Int = 2048
    public var hopSize: Int = 480
    public var gestures: GestureConfiguration = .init()
    public var wakeWord: WakeWordConfiguration = .init()
    public init() {}
}

/// Feed the existing audio capture stream here: this class never installs a second mic tap.
/// All calls and callback assignment must be confined to one serial processing queue.
/// Do not run FFT or CoreML on AVAudioEngine's realtime render/tap thread; copy PCM first.
public final class ActivationDetector {
    public let configuration: ActivationConfiguration
    public let wakeWordDetector: WakeWordDetector
    public let gestureDetector: GestureDetector
    public var onActivation: ActivationCallback?
    /// Wake-model failures are reported here without suppressing gesture processing.
    public var onError: ((Error) -> Void)?
    public private(set) var snapAvailable = false
    private let fft: FFTAnalyzer
    private var fifo = [Float]()
    private var frameStart: TimeInterval?
    private var expectedStart: TimeInterval?
    private var currentRate: Double?
    private var resampler: StreamingResampler?
    private var wakeOrigin: TimeInterval?
    private var wakeSamples = 0

    public init(configuration: ActivationConfiguration = .init(), scorer: WakeWordScoring? = nil) throws {
        try configuration.gestures.validate()
        try configuration.wakeWord.validate()
        guard configuration.hopSize > 0, configuration.hopSize <= configuration.fftSize else {
            throw ActivationError.invalidConfiguration("FFT hop must be in 1...fftSize.")
        }
        self.configuration = configuration
        fft = try FFTAnalyzer(size: configuration.fftSize)
        wakeWordDetector = WakeWordDetector(scorer: scorer, configuration: configuration.wakeWord)
        gestureDetector = GestureDetector(configuration: configuration.gestures)
    }
    public func reset() {
        fifo.removeAll(keepingCapacity: true); frameStart = nil; expectedStart = nil
        currentRate = nil; resampler = nil; wakeOrigin = nil; wakeSamples = 0
        snapAvailable = false; wakeWordDetector.reset(); gestureDetector.reset()
    }

    /// Normalized mono float PCM; timestamp is the block start in a monotonic audio clock.
    public func process(samples: [Float], sampleRate: Double, timestamp: TimeInterval) throws {
        guard sampleRate.isFinite, (8000...192000).contains(sampleRate), timestamp.isFinite else {
            throw ActivationError.invalidSampleRate
        }
        guard samples.allSatisfy(\.isFinite) else { throw ActivationError.nonFiniteAudio }
        guard !samples.isEmpty else { return }
        if currentRate != sampleRate || expectedStart.map({ abs($0 - timestamp) > 2 / sampleRate }) == true { reset() }
        if currentRate == nil {
            currentRate = sampleRate; frameStart = timestamp; wakeOrigin = timestamp
            snapAvailable = sampleRate / 2 >= configuration.gestures.snapBand.upperBound
            if let scorer = wakeWordDetector.scorer {
                resampler = try StreamingResampler(inputRate: sampleRate, outputRate: scorer.sampleRate)
            }
        }
        expectedStart = timestamp + Double(samples.count) / sampleRate
        var events = [ActivationEvent]()
        // Process arbitrary caller blocks in bounded batches; queued data never grows with recording duration.
        for offset in stride(from: 0, to: samples.count, by: 4096) {
            let chunk = Array(samples[offset..<min(offset + 4096, samples.count)])
            fifo.append(contentsOf: chunk)
            while fifo.count >= configuration.fftSize {
                let frame = Array(fifo.prefix(configuration.fftSize))
                let end = frameStart! + Double(configuration.fftSize) / sampleRate
                var features = try fft.features(frame, sampleRate: sampleRate, timestamp: end,
                    clapBand: configuration.gestures.clapBand, snapBand: configuration.gestures.snapBand)
                if !snapAvailable {
                    features = AudioFeatures(timestamp: features.timestamp, duration: features.duration,
                        rmsDBFS: features.rmsDBFS, clapDBFS: features.clapDBFS, snapDBFS: -120,
                        clapEnergyFraction: features.clapEnergyFraction, snapEnergyFraction: 0,
                        crestFactor: features.crestFactor)
                }
                events.append(contentsOf: gestureDetector.process(features))
                fifo.removeFirst(configuration.hopSize)
                frameStart! += Double(configuration.hopSize) / sampleRate
            }
            do {
                let wakeRate = wakeWordDetector.scorer?.sampleRate ?? sampleRate
                let wake = resampler?.process(chunk) ?? chunk
                if !wake.isEmpty {
                    let start = wakeOrigin! + Double(wakeSamples) / wakeRate
                    wakeSamples += wake.count
                    events.append(contentsOf: try wakeWordDetector.process(samples: wake, sampleRate: wakeRate, timestamp: start))
                }
            } catch { onError?(error) }
        }
        // Independent per-kind cooldowns live in the detectors, not in this fan-out.
        for event in events.sorted(by: { $0.timestamp < $1.timestamp }) { onActivation?(event) }
    }

    /// Float32 AVFoundation capture convenience. Downmix all channels, rather than dropping the right channel.
    /// Buffer must remain valid for this synchronous call. Enqueue a copy outside the realtime tap.
    public func process(buffer: AVAudioPCMBuffer, timestamp: TimeInterval) throws {
        guard buffer.format.commonFormat == .pcmFormatFloat32,
              let channels = buffer.floatChannelData, buffer.format.channelCount > 0 else {
            throw ActivationError.invalidConfiguration("Expected float32 AVAudioPCMBuffer.")
        }
        let channelCount = Int(buffer.format.channelCount)
        var mono = [Float](repeating: 0, count: Int(buffer.frameLength))
        for index in mono.indices {
            var sum: Float = 0
            for channel in 0..<channelCount {
                sum += buffer.format.isInterleaved ? channels[0][index * channelCount + channel] : channels[channel][index]
            }
            mono[index] = sum / Float(channelCount)
        }
        try process(samples: mono, sampleRate: buffer.format.sampleRate, timestamp: timestamp)
    }
}

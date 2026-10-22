import Foundation

public struct GestureConfiguration: Sendable {
    public var clapBand: ClosedRange<Double> = 1000...5000
    public var snapBand: ClosedRange<Double> = 8000...12000
    // Real hand claps measured quieter and more spaced out than the original
    // thresholds assumed: two claps 0.2–0.5s apart at -30dBFS rarely matched.
    public var clapThresholdDBFS: Float = -38
    public var snapThresholdDBFS: Float = -36
    public var clapInterval: ClosedRange<TimeInterval> = 0.15...0.9
    // An 8ms snap spans two 10ms hops, so it only rises ~4.5dB frame-to-frame;
    // a 6dB rise gate made snaps undetectable even at -28dBFS.
    public var minimumRiseDB: Float = 4
    public var releaseHysteresisDB: Float = 6
    public var minimumCrestFactor: Float = 2.0
    public var minimumClapEnergyFraction: Float = 0.25
    public var minimumSnapEnergyFraction: Float = 0.25
    public var peakRefractory: TimeInterval = 0.08
    public var cooldown: TimeInterval = 0.7
    public init() {}
    public func validate() throws {
        let finite = [clapThresholdDBFS, snapThresholdDBFS, minimumRiseDB, releaseHysteresisDB,
                      minimumCrestFactor, minimumClapEnergyFraction, minimumSnapEnergyFraction]
        guard finite.allSatisfy(\.isFinite), minimumRiseDB > 0, releaseHysteresisDB > 0,
              minimumCrestFactor >= 1, (0...1).contains(minimumClapEnergyFraction),
              (0...1).contains(minimumSnapEnergyFraction),
              clapInterval.lowerBound > 0, clapInterval.upperBound.isFinite,
              peakRefractory > 0, peakRefractory.isFinite, cooldown >= 0, cooldown.isFinite,
              clapBand.lowerBound > 0, clapBand.upperBound.isFinite,
              snapBand.lowerBound > 0, snapBand.upperBound.isFinite else {
            throw ActivationError.invalidConfiguration("Invalid gesture configuration.")
        }
    }
}

/// Pure per-frame state machine; no microphone tap, networking, or shared cooldown.
/// Call on the same serial queue as FFTAnalyzer. Set configuration at initialization.
public final class GestureDetector {
    public let configuration: GestureConfiguration
    private var previousClap: Float = -120
    private var previousSnap: Float = -120
    private var clapArmed = true
    private var snapArmed = true
    private var lastClapPeak = -Double.infinity
    private var lastSnapPeak = -Double.infinity
    private var firstClap: TimeInterval?
    private var lastClapEvent = -Double.infinity
    private var lastSnapEvent = -Double.infinity
    private var lastTime: TimeInterval?

    public init(configuration: GestureConfiguration = .init()) { self.configuration = configuration }
    public func reset() {
        previousClap = -120; previousSnap = -120; clapArmed = true; snapArmed = true
        lastClapPeak = -.infinity; lastSnapPeak = -.infinity; firstClap = nil
        lastClapEvent = -.infinity; lastSnapEvent = -.infinity; lastTime = nil
    }
    public func process(_ frame: AudioFeatures) -> [ActivationEvent] {
        guard frame.timestamp.isFinite, frame.duration.isFinite, frame.duration > 0,
              frame.clapDBFS.isFinite, frame.snapDBFS.isFinite, frame.crestFactor.isFinite,
              frame.clapEnergyFraction.isFinite, frame.snapEnergyFraction.isFinite,
              (try? configuration.validate()) != nil else { return [] }
        if let last = lastTime, frame.timestamp <= last { reset() }
        lastTime = frame.timestamp
        if let first = firstClap, frame.timestamp - first > configuration.clapInterval.upperBound { firstClap = nil }
        if frame.clapDBFS < configuration.clapThresholdDBFS - configuration.releaseHysteresisDB { clapArmed = true }
        if frame.snapDBFS < configuration.snapThresholdDBFS - configuration.releaseHysteresisDB { snapArmed = true }
        if ProcessInfo.processInfo.environment["JARVIS_GESTURE_DEBUG"] != nil,
           frame.crestFactor >= 1.8, frame.clapDBFS > -60 || frame.snapDBFS > -60 {
            FileHandle.standardError.write(
                String(format: "t=%.3f clap=%.1f snap=%.1f cf=%.2f sf=%.2f crest=%.1f armed=%d/%d\n",
                       frame.timestamp, frame.clapDBFS, frame.snapDBFS,
                       frame.clapEnergyFraction, frame.snapEnergyFraction,
                       frame.crestFactor, clapArmed ? 1 : 0, snapArmed ? 1 : 0)
                    .data(using: .utf8)!)
        }
        defer { previousClap = frame.clapDBFS; previousSnap = frame.snapDBFS }
        let impulsive = frame.crestFactor >= configuration.minimumCrestFactor
        let snapPeak = snapArmed && impulsive && frame.snapDBFS > configuration.snapThresholdDBFS
            && frame.snapDBFS - previousSnap >= configuration.minimumRiseDB
            && frame.snapEnergyFraction >= configuration.minimumSnapEnergyFraction
            && frame.timestamp - lastSnapPeak >= configuration.peakRefractory
        let clapPeak = clapArmed && impulsive && frame.clapDBFS > configuration.clapThresholdDBFS
            && frame.clapDBFS - previousClap >= configuration.minimumRiseDB
            && frame.clapEnergyFraction >= configuration.minimumClapEnergyFraction
            && frame.timestamp - lastClapPeak >= configuration.peakRefractory
        // A broadband impulse can qualify for both. Pick the dominant normalized band,
        // and clear a partial clap pair if a distinct snap wins; never emit twice for one impulse.
        let snapWins = snapPeak && (!clapPeak || frame.snapEnergyFraction > frame.clapEnergyFraction)
        if snapWins {
            snapArmed = false; lastSnapPeak = frame.timestamp
            clapArmed = false; previousClap = frame.clapDBFS; firstClap = nil
            if frame.timestamp - lastSnapEvent >= configuration.cooldown {
                lastSnapEvent = frame.timestamp
                return [.init(kind: .fingerSnap, timestamp: frame.timestamp, energyDBFS: frame.snapDBFS)]
            }
        } else if clapPeak {
            clapArmed = false; lastClapPeak = frame.timestamp
            snapArmed = false
            if let first = firstClap, configuration.clapInterval.contains(frame.timestamp - first) {
                firstClap = nil
                if frame.timestamp - lastClapEvent >= configuration.cooldown {
                    lastClapEvent = frame.timestamp
                    return [.init(kind: .doubleClap, timestamp: frame.timestamp, energyDBFS: frame.clapDBFS)]
                }
            } else {
                firstClap = frame.timestamp
            }
        }
        return []
    }
}

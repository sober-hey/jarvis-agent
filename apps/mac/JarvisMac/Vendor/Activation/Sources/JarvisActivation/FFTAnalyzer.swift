import Accelerate
import Foundation

public struct PowerSpectrum: Sendable {
    /// Single-sided mean-square power per bin. Sum == window-normalized RMS².
    public let power: [Float]
    public let sampleRate: Double
    public let fftSize: Int
    public var totalPower: Float { power.reduce(0, +) }
    public func bandPower(_ range: ClosedRange<Double>) -> Float {
        guard range.lowerBound.isFinite, range.upperBound.isFinite else { return 0 }
        let binWidth = sampleRate / Double(fftSize)
        let lower = max(0, range.lowerBound / binWidth)
        let upper = min(Double(power.count - 1), range.upperBound / binWidth)
        guard lower <= upper else { return 0 }
        let low = Int(ceil(lower))
        let high = Int(floor(upper))
        guard low <= high else { return 0 }
        return power[low...high].reduce(0, +)
    }
    public func bandDBFS(_ range: ClosedRange<Double>) -> Float {
        Self.dbfs(power: bandPower(range))
    }
    public static func dbfs(power: Float) -> Float { 10 * log10(max(power, 1e-12)) }
}

/// Reuses an Accelerate FFT plan. Confine one instance to one processing queue.
public final class FFTAnalyzer {
    public let size: Int
    private let log2Size: vDSP_Length
    private let setup: FFTSetup
    private var window: [Float]
    private let normalization: Float

    public init(size: Int = 2048) throws {
        guard size >= 64, size <= 65536, size.nonzeroBitCount == 1 else { throw ActivationError.invalidFFTSize }
        self.size = size
        log2Size = vDSP_Length(size.trailingZeroBitCount)
        guard let plan = vDSP_create_fftsetup(log2Size, FFTRadix(kFFTRadix2)) else {
            throw ActivationError.invalidFFTSize
        }
        setup = plan
        window = [Float](repeating: 0, count: size)
        vDSP_hann_window(&window, vDSP_Length(size), Int32(vDSP_HANN_NORM))
        normalization = Float(size) * window.reduce(0) { $0 + $1 * $1 }
    }
    deinit { vDSP_destroy_fftsetup(setup) }

    public func analyze(_ samples: [Float], sampleRate: Double) throws -> PowerSpectrum {
        guard sampleRate.isFinite, sampleRate > 0 else { throw ActivationError.invalidSampleRate }
        guard samples.count == size else { throw ActivationError.invalidFrameLength }
        guard samples.allSatisfy(\.isFinite) else { throw ActivationError.nonFiniteAudio }
        var real = zip(samples, window).map(*)
        var imag = [Float](repeating: 0, count: size)
        real.withUnsafeMutableBufferPointer { r in
            imag.withUnsafeMutableBufferPointer { i in
                var split = DSPSplitComplex(realp: r.baseAddress!, imagp: i.baseAddress!)
                vDSP_fft_zip(setup, &split, 1, log2Size, FFTDirection(FFT_FORWARD))
            }
        }
        let power: [Float] = (0...size / 2).map { index in
            let twoSided = real[index] * real[index] + imag[index] * imag[index]
            return twoSided * ((index == 0 || index == size / 2) ? 1 : 2) / normalization
        }
        return PowerSpectrum(power: power, sampleRate: sampleRate, fftSize: size)
    }

    public func features(_ samples: [Float], sampleRate: Double, timestamp: TimeInterval,
                         clapBand: ClosedRange<Double> = 1000...5000,
                         snapBand: ClosedRange<Double> = 8000...12000) throws -> AudioFeatures {
        let spectrum = try analyze(samples, sampleRate: sampleRate)
        let total = max(spectrum.totalPower, 1e-12)
        let meanSquare = samples.reduce(Float(0)) { $0 + $1 * $1 } / Float(size)
        let peak = samples.reduce(Float(0)) { max($0, abs($1)) }
        return AudioFeatures(timestamp: timestamp, duration: Double(size) / sampleRate,
            rmsDBFS: PowerSpectrum.dbfs(power: meanSquare),
            clapDBFS: spectrum.bandDBFS(clapBand), snapDBFS: spectrum.bandDBFS(snapBand),
            clapEnergyFraction: spectrum.bandPower(clapBand) / total,
            snapEnergyFraction: spectrum.bandPower(snapBand) / total,
            crestFactor: peak / sqrt(max(meanSquare, 1e-12)))
    }
}

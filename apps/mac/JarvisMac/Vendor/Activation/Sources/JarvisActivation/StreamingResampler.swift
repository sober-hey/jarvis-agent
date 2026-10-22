import Foundation

/// Small streaming windowed-sinc resampler with anti-aliasing. One serial queue.
/// Preserves fractional phase across input blocks; 24 samples of look-ahead.
public final class StreamingResampler {
    public let inputRate: Double
    public let outputRate: Double
    private let radius = 24
    private var input = [Float]()
    private var inputBase = 0
    private var totalInput = 0
    private var outputIndex = 0
    public init(inputRate: Double, outputRate: Double) throws {
        guard inputRate.isFinite, outputRate.isFinite, inputRate >= 8000, inputRate <= 192000,
              outputRate >= 8000, outputRate <= 192000 else { throw ActivationError.invalidSampleRate }
        self.inputRate = inputRate; self.outputRate = outputRate
    }
    public func reset() { input.removeAll(keepingCapacity: true); inputBase = 0; totalInput = 0; outputIndex = 0 }
    public func process(_ samples: [Float]) -> [Float] {
        if abs(inputRate - outputRate) < 0.01 { return samples }
        input.append(contentsOf: samples); totalInput += samples.count
        let cutoff = 0.45 * min(1, outputRate / inputRate)
        var output = [Float]()
        while true {
            let position = Double(outputIndex) * inputRate / outputRate
            let center = Int(floor(position))
            guard center + radius < totalInput else { break }
            var value: Double = 0
            var weight: Double = 0
            for index in (center - radius + 1)...(center + radius) {
                let delta = Double(index) - position
                let x = 2 * cutoff * delta
                let sinc = abs(x) < 1e-10 ? 1 : sin(Double.pi * x) / (Double.pi * x)
                let window = 0.5 + 0.5 * cos(Double.pi * delta / Double(radius))
                let coefficient = 2 * cutoff * sinc * window
                let source: Float = index >= 0 && index >= inputBase ? input[index - inputBase] : 0
                value += Double(source) * coefficient; weight += coefficient
            }
            output.append(Float(value / max(weight, 1e-12)))
            outputIndex += 1
        }
        let nextCenter = Int(floor(Double(outputIndex) * inputRate / outputRate))
        let discard = max(0, min(input.count, nextCenter - radius - inputBase))
        if discard > 0 { input.removeFirst(discard); inputBase += discard }
        return output
    }
}

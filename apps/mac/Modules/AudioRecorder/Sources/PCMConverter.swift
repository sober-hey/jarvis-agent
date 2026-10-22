import AVFoundation
import Foundation

/// AVAudioConverter maintains its resampling history across tap buffers.
final class PCMConverter {
    struct Chunk { let data: Data; let level: Float }
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private let output: AVAudioPCMBuffer
    private let inputRate: Double
    private var inputFrames: Int64 = 0
    private var emittedFrames: Int64 = 0
    init(inputFormat: AVAudioFormat) throws {
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000,
                                         channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: inputFormat, to: format),
              let output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 32768) else {
            throw AudioRecorder.RecorderError.conversionFailed("Unsupported input format")
        }
        converter.primeMethod = .none
        self.converter = converter
        self.outputFormat = format
        self.output = output
        self.inputRate = inputFormat.sampleRate
    }
    func convert(_ input: AVAudioPCMBuffer) throws -> Chunk {
        inputFrames += Int64(input.frameLength)
        return try drain(input)
    }
    func finish() throws -> Chunk { try drain(nil) }
    private func drain(_ input: AVAudioPCMBuffer?) throws -> Chunk {
        var supplied = false
        var data = Data()
        for _ in 0..<32 {
            output.frameLength = 0
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, state in
                if let input = input, !supplied {
                    supplied = true
                    state.pointee = .haveData
                    return input
                }
                state.pointee = input == nil ? .endOfStream : .noDataNow
                return nil
            }
            guard status != .error, error == nil else {
                throw AudioRecorder.RecorderError.conversionFailed(error?.localizedDescription ?? "Audio conversion failed")
            }
            if output.frameLength > 0 {
                let buffers = UnsafeMutableAudioBufferListPointer(output.mutableAudioBufferList)
                guard let bytes = buffers[0].mData else {
                    throw AudioRecorder.RecorderError.conversionFailed("Missing PCM data")
                }
                data.append(bytes.assumingMemoryBound(to: UInt8.self), count: Int(output.frameLength) * 2)
            }
            if status == .inputRanDry || status == .endOfStream || output.frameLength == 0 {
                // AVAudioConverter may emit a fractional-filter tail on EOS.
                // Do not lengthen the recording beyond its source-frame duration.
                let limit = Int64((Double(inputFrames) * 16000 / inputRate).rounded(.down))
                let frames = min(Int64(data.count / 2), max(0, limit - emittedFrames))
                data = Data(data.prefix(Int(frames) * 2))
                emittedFrames += frames
                let level: Float = data.withUnsafeBytes { raw in
                    let samples = raw.bindMemory(to: Int16.self)
                    let sum = samples.reduce(0.0) { $0 + pow(Double($1) / 32768, 2) }
                    return samples.isEmpty ? 0 : Float(sqrt(sum / Double(samples.count)))
                }
                return Chunk(data: data, level: min(1, max(0, level)))
            }
        }
        throw AudioRecorder.RecorderError.conversionFailed("Converter failed to drain")
    }
}

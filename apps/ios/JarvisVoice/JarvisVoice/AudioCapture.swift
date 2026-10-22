import AVFoundation
import Foundation

/// AVAudioEngine owns continuous, user-started capture. Only VAD utterances leave
/// this device. The tap uses the hardware format; AVAudioConverter resamples it.
final class AudioCapture {
    var onFrame: ((SpeechGate.Result) -> Void)?
    var threshold: Float = 0.018

    /// The owner must set this to false whenever the app is not foreground-active.
    /// Access is serialized with the same queue that owns capture processing.
    private var engine: AVAudioEngine?
    /// Replies play through the same engine as the microphone so the
    /// voice-processing echo canceller hears them as its own output. Playing
    /// through a separate AVAudioPlayer made the canceller treat replies as
    /// "other audio": it ducked them (quiet, pumping volume) and leaked them
    /// back into the mic, which cut replies off mid-sentence.
    private var player: AVAudioPlayerNode?
    private var playerFormat: AVAudioFormat?
    private var playbackToken = 0
    var isRunning: Bool { engine != nil }
    private let processing = DispatchQueue(label: "app.jarvis.ios.capture")
    private let processingKey = DispatchSpecificKey<Void>()
    private var converter: AVAudioConverter?
    private var pending: [Float] = []
    private var gate = SpeechGate()

    init() {
        processing.setSpecific(key: processingKey, value: ())
    }

    func start() throws {
        guard engine == nil else { return }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .voiceChat,
                                options: [.defaultToSpeaker, .allowBluetoothHFP])
        try? session.overrideOutputAudioPort(.speaker)
        try session.setPreferredSampleRate(16000)
        try session.setPreferredIOBufferDuration(0.02)
        try session.setActive(true)

        let newEngine = AVAudioEngine()
        let input = newEngine.inputNode
        try input.setVoiceProcessingEnabled(true)
        // Do not duck other audio while listening.
        input.voiceProcessingOtherAudioDuckingConfiguration =
            .init(enableAdvancedDucking: false, duckingLevel: .min)
        let node = AVAudioPlayerNode()
        newEngine.attach(node)
        let replyFormat = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1)
        newEngine.connect(node, to: newEngine.mainMixerNode, format: replyFormat)
        newEngine.mainMixerNode.outputVolume = 1
        let source = input.outputFormat(forBus: 0)
        guard source.sampleRate > 0, source.channelCount > 0,
              let target = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                         sampleRate: 16000, channels: 1, interleaved: false),
              let conversion = AVAudioConverter(from: source, to: target) else {
            throw CaptureError.unavailable
        }
        converter = conversion
        processing.sync {
            pending.removeAll(keepingCapacity: true)
            gate = SpeechGate(threshold: threshold)
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: source) { [weak self] buffer, _ in
            guard let self else { return }
            // The tap buffer belongs to AVAudioEngine: conversion must finish
            // before the callback returns. The callback below owns copied floats.
            self.processing.sync {
                let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 16000 / source.sampleRate)) + 64
                guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
                var supplied = false
                var conversionError: NSError?
                conversion.convert(to: output, error: &conversionError) { _, status in
                    if supplied { status.pointee = .noDataNow; return nil }
                    supplied = true
                    status.pointee = .haveData
                    return buffer
                }
                guard conversionError == nil, let channel = output.floatChannelData?[0] else { return }
                self.pending.append(contentsOf: UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
                while self.pending.count >= 320 {
                    let frame = Array(self.pending.prefix(320))
                    self.pending.removeFirst(320)
                    self.onFrame?(self.gate.process(frame))
                }
            }
        }
        do {
            newEngine.prepare()
            try newEngine.start()
            engine = newEngine
            player = node
            playerFormat = replyFormat
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
    }

    func ownsEngine(_ object: Any?) -> Bool {
        guard let changed = object as? AVAudioEngine, let engine else { return false }
        return changed === engine
    }

    /// Decode a complete MP3 reply and play it through the capture engine.
    /// `done` runs on the main queue once the audio has actually played out.
    func play(_ mp3: Data, done: @escaping () -> Void) throws {
        guard let engine, let player else { throw CaptureError.unavailable }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString).appendingPathExtension("mp3")
        try mp3.write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        let file = try AVAudioFile(forReading: url)
        let format = file.processingFormat
        guard file.length > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(file.length))
        else { throw CaptureError.unavailable }
        try file.read(into: buffer)
        playbackToken += 1
        let token = playbackToken
        player.stop()
        if playerFormat != format {
            engine.disconnectNodeOutput(player)
            engine.connect(player, to: engine.mainMixerNode, format: format)
            playerFormat = format
        }
        player.scheduleBuffer(buffer, at: nil, options: [], completionCallbackType: .dataPlayedBack) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.playbackToken == token else { return }
                done()
            }
        }
        player.play()
    }

    func stopPlayback() {
        playbackToken += 1
        player?.stop()
    }

    func stop() {
        guard let engine else { return }
        stopPlayback()
        player = nil
        playerFormat = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
        processing.sync {
            converter = nil
            pending.removeAll()
            gate = SpeechGate(threshold: threshold)
        }
    }

    enum CaptureError: LocalizedError {
        case unavailable
        var errorDescription: String? {
            switch self {
            case .unavailable: return "麦克风当前不可用，请检查音频设备。"
            }
        }
    }
}

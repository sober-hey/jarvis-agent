import AVFoundation
import Foundation

protocol MicrophonePermission {
    var status: AVAuthorizationStatus { get }
    func request(_ completion: @escaping (Bool) -> Void)
}
struct SystemMicrophonePermission: MicrophonePermission {
    var status: AVAuthorizationStatus { AVCaptureDevice.authorizationStatus(for: .audio) }
    func request(_ completion: @escaping (Bool) -> Void) {
        AVCaptureDevice.requestAccess(for: .audio, completionHandler: completion)
    }
}
protocol AudioInput: AnyObject {
    func start(onBuffer: @escaping (AVAudioPCMBuffer, @escaping () -> Void) -> Void,
               onError: @escaping (AudioRecorder.RecorderError) -> Void) throws -> AVAudioFormat
    func stop()
}

/// Only the bounded copy runs on the audio tap. Resampling and disk I/O never do.
public final class EngineMicrophoneInput: AudioInput {
    public init() {}
    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    private var playerNode: AVAudioPlayerNode?
    public private(set) var voiceProcessing = false
    public func start(onBuffer: @escaping (AVAudioPCMBuffer, @escaping () -> Void) -> Void,
               onError: @escaping (AudioRecorder.RecorderError) -> Void) throws -> AVAudioFormat {
        try startTimed(onBuffer: { buffer, _, release in onBuffer(buffer, release) }, onError: onError)
    }
    /// Original capture clock; composition root fans this one tap out before resampling.
    public func startTimed(onBuffer: @escaping (AVAudioPCMBuffer, TimeInterval, @escaping () -> Void) -> Void,
                           onError: @escaping (AudioRecorder.RecorderError) -> Void) throws -> AVAudioFormat {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        // 回声消除：VPIO 的参考信号取自引擎输出总线，因此回复必须也从这条总线播放
        // （见 playMP3）。只开采集不开播放时 AEC 抵不掉自己的声音。
        var voiceProcessing = false
        do {
            try input.setVoiceProcessingEnabled(true)
            voiceProcessing = true
        } catch {
            // 设备不支持时保持原样：能听不能说被自我回声干扰，也不能聋。
            voiceProcessing = false
        }
        self.voiceProcessing = voiceProcessing
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioRecorder.RecorderError.deviceUnavailable
        }
        let pool = try TapBufferPool(format: format, count: 8, capacity: 16384)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, time in
            guard buffer.frameLength > 0 else { return }
            guard let slot = pool.acquire() else {
                if pool.markFailed() { onError(.processingOverrun) }
                return
            }
            guard buffer.frameLength <= slot.frameCapacity else {
                pool.release(slot)
                if pool.markFailed() { onError(.processingOverrun) }
                return
            }
            slot.frameLength = buffer.frameLength
            let src = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            let dst = UnsafeMutableAudioBufferListPointer(slot.mutableAudioBufferList)
            for i in 0..<src.count {
                guard let s = src[i].mData, let d = dst[i].mData,
                      src[i].mDataByteSize <= dst[i].mDataByteSize else {
                    pool.release(slot)
                    if pool.markFailed() { onError(.processingOverrun) }
                    return
                }
                memcpy(d, s, Int(src[i].mDataByteSize))
                dst[i].mDataByteSize = src[i].mDataByteSize
            }
            let timestamp = time.isSampleTimeValid ? Double(time.sampleTime) / format.sampleRate : ProcessInfo.processInfo.systemUptime
            onBuffer(slot, timestamp, { pool.release(slot) })
        }
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { _ in onError(.deviceDisconnected) }
        self.engine = engine
        do { engine.prepare(); try engine.start() }
        catch { stop(); throw AudioRecorder.RecorderError.engineStartFailed(error.localizedDescription) }
        return format
    }
    /// 回复音频必须走采集引擎的输出总线，VPIO 才能把它当自身输出抵掉。
    public func playMP3(_ data: Data, completion: @escaping () -> Void) -> Bool {
        guard let engine else { return false }
        let node: AVAudioPlayerNode
        if let existing = playerNode { node = existing }
        else {
            node = AVAudioPlayerNode()
            engine.attach(node)
            playerNode = node
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString).appendingPathExtension("mp3")
        guard (try? data.write(to: url)) != nil,
              let file = try? AVAudioFile(forReading: url),
              file.length > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                            frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: buffer)) != nil else {
            try? FileManager.default.removeItem(at: url)
            return false
        }
        try? FileManager.default.removeItem(at: url)
        engine.disconnectNodeOutput(node)
        engine.connect(node, to: engine.mainMixerNode, format: file.processingFormat)
        engine.mainMixerNode.outputVolume = 1
        node.stop()
        node.scheduleBuffer(buffer, at: nil, options: [], completionCallbackType: .dataPlayedBack) { _ in
            DispatchQueue.main.async(execute: completion)
        }
        node.play()
        return true
    }

    public func stopPlayback() { playerNode?.stop() }

    public func stop() {
        if let observer = observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
    }
    deinit { stop() }
}

private final class TapBufferPool {
    private final class Slot {
        let buffer: AVAudioPCMBuffer
        let available = DispatchSemaphore(value: 1)
        init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }
    }
    private let slots: [Slot]
    private let failureGate = DispatchSemaphore(value: 1)
    init(format: AVAudioFormat, count: Int, capacity: AVAudioFrameCount) throws {
        var slots: [Slot] = []
        for _ in 0..<count {
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else {
                throw AudioRecorder.RecorderError.deviceUnavailable
            }
            slots.append(Slot(buffer))
        }
        self.slots = slots
    }
    func acquire() -> AVAudioPCMBuffer? {
        // All waits have zero timeout; the render thread never waits for the worker.
        for slot in slots where slot.available.wait(timeout: .now()) == .success { return slot.buffer }
        return nil
    }
    func release(_ buffer: AVAudioPCMBuffer) {
        if let slot = slots.first(where: { $0.buffer === buffer }) { slot.available.signal() }
    }
    func markFailed() -> Bool { failureGate.wait(timeout: .now()) == .success }
}

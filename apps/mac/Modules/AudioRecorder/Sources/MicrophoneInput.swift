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
final class EngineMicrophoneInput: AudioInput {
    private var engine: AVAudioEngine?
    private var observer: NSObjectProtocol?
    func start(onBuffer: @escaping (AVAudioPCMBuffer, @escaping () -> Void) -> Void,
               onError: @escaping (AudioRecorder.RecorderError) -> Void) throws -> AVAudioFormat {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw AudioRecorder.RecorderError.deviceUnavailable
        }
        let pool = try TapBufferPool(format: format, count: 8, capacity: 16384)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
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
            onBuffer(slot, { pool.release(slot) })
        }
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { _ in onError(.deviceDisconnected) }
        self.engine = engine
        do { engine.prepare(); try engine.start() }
        catch { stop(); throw AudioRecorder.RecorderError.engineStartFailed(error.localizedDescription) }
        return format
    }
    func stop() {
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

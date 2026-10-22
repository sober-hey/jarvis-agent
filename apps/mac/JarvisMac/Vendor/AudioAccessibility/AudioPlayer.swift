import Foundation
@preconcurrency import AVFoundation
import Combine

private final class AudioCompletionRelay: NSObject, AVAudioPlayerDelegate {
    let completed: @Sendable (Bool, String?) -> Void
    init(completed: @escaping @Sendable (Bool, String?) -> Void) { self.completed = completed }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        completed(flag, flag ? nil : "音频未完整播放，请检查文件或输出设备。")
    }
    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        completed(false, error?.localizedDescription ?? "音频解码失败。")
    }
}

/// Completed MP3/WAV response bodies, not arbitrary network or raw PCM chunks.
/// Create and operate on the main actor; construction never starts audio.
@MainActor
public final class AudioPlayer: ObservableObject {
    public enum Phase: String, Sendable { case idle, playing, paused, finished, failed }
    public struct Snapshot: Equatable, Sendable {
        public let phase: Phase
        public let currentTime: TimeInterval
        public let duration: TimeInterval
        public let errorMessage: String?
        public var fraction: Double { duration > 0 ? min(1, max(0, currentTime / duration)) : 0 }
    }
    public enum Failure: Error, LocalizedError {
        case emptyData, dataTooLarge, unsupportedFileURL, invalidAudio(String), cannotStart
        public var errorDescription: String? {
            switch self {
            case .emptyData: return "服务端返回的音频为空，请重试。"
            case .dataTooLarge: return "音频超过 32 MiB 限制，请缩短语音后重试。"
            case .unsupportedFileURL: return "只接受本地音频文件；网络响应请先获取完整 MP3/WAV 数据。"
            case let .invalidAudio(detail): return "无法解码音频，请确认服务端返回 MP3/WAV，而不是 JSON、HTML 或裸 PCM。\(detail)"
            case .cannotStart: return "无法开始播放，请检查系统声音输出设备及静音设置。"
            }
        }
    }
    @Published public private(set) var snapshot = Snapshot(phase: .idle, currentTime: 0, duration: 0, errorMessage: nil)
    /// Called on the main actor at ~10 Hz and on each state change.
    public var onProgress: ((Snapshot) -> Void)?
    public private(set) var activationID = UUID()
    public var volume: Float {
        get { storedVolume }
        set { storedVolume = newValue.isFinite ? min(1, max(0, newValue)) : 0; player?.volume = storedVolume }
    }
    private var storedVolume: Float = 1
    private var player: AVAudioPlayer?
    private var relay: AudioCompletionRelay?
    private var timer: Timer?
    private var playbackID: UUID?
    public init() {}

    /// Call immediately on a new wake/activation, BEFORE waiting for its response.
    /// The returned token prevents a late response from an older activation playing.
    @discardableResult
    public func activate() -> UUID {
        activationID = UUID()
        stopPlayback()
        publish(.idle, current: 0, duration: 0)
        return activationID
    }
    public func stop() { _ = activate() }

    /// With a token: stale data is ignored without disturbing current playback.
    /// Without a token: implicitly creates a new activation, interrupting the old clip.
    @discardableResult
    public func play(data: Data, activation: UUID? = nil) throws -> Bool {
        if let activation, activation != activationID { return false }
        if activation == nil { _ = activate() } else { stopPlayback() }
        do {
            guard !data.isEmpty else { throw Failure.emptyData }
            guard data.count <= 32 * 1024 * 1024 else { throw Failure.dataTooLarge }
            let native: AVAudioPlayer
            do { native = try AVAudioPlayer(data: data) }
            catch { throw Failure.invalidAudio(error.localizedDescription) }
            guard native.duration.isFinite, native.duration > 0 else { throw Failure.invalidAudio("") }
            let id = UUID()
            let completion = AudioCompletionRelay { [weak self] success, message in
                Task { @MainActor [weak self] in self?.complete(id, success: success, message: message) }
            }
            native.delegate = completion
            native.volume = storedVolume
            guard native.prepareToPlay(), native.play() else { native.stop(); throw Failure.cannotStart }
            player = native; relay = completion; playbackID = id
            startTimer()
            publish(.playing, current: native.currentTime, duration: native.duration)
            return true
        } catch {
            stopPlayback()
            publish(.failed, current: 0, duration: 0, error: error.localizedDescription)
            throw error
        }
    }
    @discardableResult
    public func play(fileURL: URL, activation: UUID? = nil) throws -> Bool {
        if let activation, activation != activationID { return false }
        let token = activation ?? activate()
        stopPlayback()
        do {
            guard fileURL.isFileURL else { throw Failure.unsupportedFileURL }
            // Bounded mapped read; the caller must not pass untrusted paths.
            let size = try fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 32 * 1024 * 1024 else { throw Failure.dataTooLarge }
            let data = try Data(contentsOf: fileURL, options: .mappedIfSafe)
            return try play(data: data, activation: token)
        } catch {
            stopPlayback()
            publish(.failed, current: 0, duration: 0, error: error.localizedDescription)
            throw error
        }
    }
    public func pause() {
        guard let player, snapshot.phase == .playing else { return }
        player.pause(); timer?.invalidate(); timer = nil
        publish(.paused, current: player.currentTime, duration: player.duration)
    }
    public func resume() throws {
        guard let player, snapshot.phase == .paused else { return }
        guard player.play() else {
            stopPlayback(); publish(.failed, current: 0, duration: 0, error: Failure.cannotStart.localizedDescription)
            throw Failure.cannotStart
        }
        startTimer(); publish(.playing, current: player.currentTime, duration: player.duration)
    }
    public func seek(to time: TimeInterval) {
        guard time.isFinite, let player else { return }
        player.currentTime = min(player.duration, max(0, time))
        publish(snapshot.phase, current: player.currentTime, duration: player.duration)
    }
    private func startTimer() {
        timer?.invalidate()
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] timer in
            guard self != nil else { timer.invalidate(); return }
            MainActor.assumeIsolated {
                guard let self else { return }
                guard let player = self.player, self.snapshot.phase == .playing else { return }
                self.publish(.playing, current: player.currentTime, duration: player.duration)
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }
    private func complete(_ id: UUID, success: Bool, message: String?) {
        guard playbackID == id, let player else { return }
        let duration = player.duration, current = success ? duration : player.currentTime
        stopPlayback()
        publish(success ? .finished : .failed, current: current, duration: duration, error: message)
    }
    private func stopPlayback() {
        timer?.invalidate(); timer = nil
        player?.delegate = nil; player?.stop(); player = nil; relay = nil; playbackID = nil
    }
    private func publish(_ phase: Phase, current: TimeInterval, duration: TimeInterval, error: String? = nil) {
        snapshot = Snapshot(phase: phase, currentTime: current, duration: duration, errorMessage: error)
        onProgress?(snapshot)
    }
}

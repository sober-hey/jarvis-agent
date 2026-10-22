import AVFoundation
import Combine
import Foundation

@MainActor
final class WatchAudio: NSObject, ObservableObject, AVAudioRecorderDelegate, AVAudioPlayerDelegate {
    @Published private(set) var recording = false
    @Published private(set) var playing = false
    @Published private(set) var route = "未启用"
    /// 0…1 的实时输入电平，供首页球体随声音起伏；无录音时为 0。
    @Published private(set) var level: Float = 0
    private var recorder: AVAudioRecorder?
    private var player: AVAudioPlayer?
    private var recordingURL: URL?
    private var finish: ((Result<Data, Error>) -> Void)?
    private var deadline: Task<Void, Never>?
    private var silenceTask: Task<Void, Never>?
    private var notification: NSObjectProtocol?
    var onInterrupted: (() -> Void)?
    var onSilence: (() -> Void)?
    var onPlaybackFinished: (() -> Void)?
    enum AudioError: Error { case denied, cannotRecord, invalidFormat, cannotPlay }

    override init() {
        super.init()
        notification = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.cancel()
                self?.stopPlayback()
                self?.onInterrupted?()
            }
        }
    }

    func requestPermission() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
    }

    func record(completion: @escaping (Result<Data, Error>) -> Void) throws {
        guard !recording, recorder == nil else { throw AudioError.cannotRecord }
        stopPlayback()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default, options: [])
        try session.setActive(true)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".wav")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000.0,
            AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false
        ]
        do {
            let recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder.delegate = self
            recorder.isMeteringEnabled = true
            guard recorder.prepareToRecord(), recorder.record() else { throw AudioError.cannotRecord }
            self.recorder = recorder
            self.recordingURL = url
            finish = completion
            recording = true
            route = session.currentRoute.inputs.map(\.portName).joined(separator: ", ")
            // Hard cap avoids indefinitely holding microphone/background audio.
            deadline = Task { [weak self] in
                try? await Task.sleep(for: .seconds(12))
                guard !Task.isCancelled else { return }
                self?.stopRecording()
            }
            silenceTask = Task { [weak self] in
                let start = Date()
                var speechSeen = false
                var lastSpeech = Date()
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(100))
                    guard let self, self.recording else { return }
                    self.recorder?.updateMeters()
                    // -60 dB…0 dB 映射到 0…1，保留一点底噪让人声听得出层次。
                    let power = self.recorder?.averagePower(forChannel: 0) ?? -160
                    let normalised = max(0, min(1, (power + 60) / 60))
                    if self.level != normalised { self.level = normalised }
                    if power > -38 {
                        speechSeen = true
                        lastSpeech = Date()
                    }
                    // Energy gate, not speech recognition; quiet wake abandons after 4s.
                    if (speechSeen && Date().timeIntervalSince(lastSpeech) > 1.1)
                        || (!speechSeen && Date().timeIntervalSince(start) > 4) {
                        if speechSeen { self.stopRecording() }
                        else { self.cancel(); self.onSilence?() }
                        return
                    }
                }
            }
        } catch {
            try? FileManager.default.removeItem(at: url)
            try? session.setActive(false)
            throw error
        }
    }

    func stopRecording() {
        guard recording else { return }
        recording = false
        level = 0
        deadline?.cancel(); silenceTask?.cancel()
        recorder?.stop()
    }

    func cancel() {
        finish = nil
        deadline?.cancel(); silenceTask?.cancel()
        recorder?.delegate = nil
        recorder?.stop(); recorder = nil
        recording = false
        level = 0
        if let url = recordingURL { try? FileManager.default.removeItem(at: url) }
        recordingURL = nil
        try? AVAudioSession.sharedInstance().setActive(false)
    }

    nonisolated func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        let url = recorder.url
        Task { @MainActor in self.completeRecording(url: url, success: flag) }
    }
    nonisolated func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: Error?) {
        let url = recorder.url
        Task { @MainActor in self.completeRecording(url: url, success: false) }
    }
    private func completeRecording(url: URL, success: Bool) {
        // Late delegate calls for an old recorder must not complete a newer capture.
        guard url == recordingURL else { return }
        deadline?.cancel(); silenceTask?.cancel()
        recording = false
        level = 0
        let callback = finish
        finish = nil; recordingURL = nil; recorder = nil
        defer {
            try? FileManager.default.removeItem(at: url)
            try? AVAudioSession.sharedInstance().setActive(false)
        }
        do {
            guard success else { throw AudioError.cannotRecord }
            let file = try AVAudioFile(forReading: url)
            guard file.fileFormat.sampleRate == 16000, file.fileFormat.channelCount == 1 else {
                throw AudioError.invalidFormat
            }
            let data = try Data(contentsOf: url)
            guard data.count <= WatchWire.maxAudioBytes else { throw AudioError.invalidFormat }
            callback?(.success(data))
        } catch { callback?(.failure(error)) }
    }

    func play(_ data: Data) throws {
        cancel()
        stopPlayback()
        let session = AVAudioSession.sharedInstance()
        // .default route policy allows the built-in speaker for short responses.
        // .longFormAudio on Series 8 instead requires a Bluetooth audio route.
        try session.setCategory(.playback, mode: .default, policy: .default, options: [])
        try session.setActive(true)
        do {
            let player = try AVAudioPlayer(data: data, fileTypeHint: AVFileType.mp3.rawValue)
            player.delegate = self
            guard player.prepareToPlay(), player.play() else { throw AudioError.cannotPlay }
            self.player = player
            playing = true
            route = session.currentRoute.outputs.map(\.portName).joined(separator: ", ")
        } catch {
            try? session.setActive(false)
            throw error
        }
    }
    func stopPlayback() {
        player?.stop(); player = nil; playing = false
        if !recording { try? AVAudioSession.sharedInstance().setActive(false) }
    }
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let identity = ObjectIdentifier(player)
        Task { @MainActor in
            guard self.player.map(ObjectIdentifier.init) == identity else { return }
            self.stopPlayback()
            self.onPlaybackFinished?()
        }
    }
    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        let identity = ObjectIdentifier(player)
        Task { @MainActor in
            guard self.player.map(ObjectIdentifier.init) == identity else { return }
            self.stopPlayback()
        }
    }
}

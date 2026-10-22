import AVFoundation
import Foundation

@MainActor
final class MP3Playback: NSObject, AVAudioPlayerDelegate {
    var onFinished: ((Int) -> Void)?
    var onError: ((String) -> Void)?
    var onPlaying: ((Bool) -> Void)?
    /// Set by VoiceSession; when its engine runs, replies play through it.
    weak var output: AudioCapture?
    private var accumulator = PlaybackAccumulator()
    private var enginePlaying = false
    private var player: AVAudioPlayer?
    private var minimumGeneration = 0

    func receive(_ data: Data, generation: Int, final: Bool) {
        guard generation >= minimumGeneration,
              let complete = accumulator.append(data, generation: generation, final: final) else { return }
        if let output, output.isRunning {
            player?.stop()
            player = nil
            let generation = accumulator.generation
            do {
                try output.play(complete) { [weak self] in
                    guard let self, self.enginePlaying else { return }
                    self.enginePlaying = false
                    self.onPlaying?(false)
                    self.onFinished?(generation)
                }
                enginePlaying = true
                onPlaying?(true)
            } catch {
                enginePlaying = false
                onPlaying?(false)
                onError?("收到的音频无法播放。")
                onFinished?(generation)
            }
            return
        }
        do {
            player?.stop()
            let next = try AVAudioPlayer(data: complete, fileTypeHint: AVFileType.mp3.rawValue)
            next.delegate = self
            guard next.prepareToPlay(), next.play() else {
                throw PlaybackError.cannotPlay
            }
            player = next
            onPlaying?(true)
        } catch {
            player = nil
            onPlaying?(false)
            onError?("收到的音频无法播放。")
            onFinished?(accumulator.generation)
        }
    }

    /// Drop queued MP3 immediately on local voice onset. The gateway increases
    /// generation for every new utterance, even when there was no active playback.
    func interrupt(locally: Bool = false) {
        player?.stop()
        player = nil
        output?.stopPlayback()
        enginePlaying = false
        if locally { minimumGeneration = max(minimumGeneration, accumulator.generation + 1) }
        accumulator.interrupt()
        onPlaying?(false)
    }

    func reset() {
        interrupt()
        accumulator.reset()
        minimumGeneration = 0
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            guard self.player === player else { return }
            self.player = nil
            self.onPlaying?(false)
            self.onFinished?(self.accumulator.generation)
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in
            guard self.player === player else { return }
            self.player = nil
            self.onPlaying?(false)
            self.onError?("音频解码失败。")
            self.onFinished?(self.accumulator.generation)
        }
    }

    private enum PlaybackError: Error { case cannotPlay }
}

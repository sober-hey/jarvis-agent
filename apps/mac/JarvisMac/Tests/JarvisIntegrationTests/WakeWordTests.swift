import AVFoundation
import XCTest
@testable import JarvisActivation
@testable import JarvisIntegration

final class WakeWordTests: XCTestCase {
    private final class FixedScorer: WakeWordScoring {
        let sampleRate = 16_000.0
        let windowSize = 1_280
        var calls = 0
        func score(samples: [Float]) throws -> Float {
            calls += 1
            return calls == 5 ? 0.95 : 0.01
        }
    }

    func testFakeScoringEmitsOneWakeWordAtConfiguredHop() throws {
        let scorer = FixedScorer()
        let detector = WakeWordDetector(scorer: scorer,
                                        configuration: WakeWordConfiguration(inferenceHop: 0.08,
                                                                              cooldown: 1.5))
        var events: [ActivationEvent] = []
        for offset in stride(from: 0, to: 10_240, by: 160) {
            events += try detector.process(samples: [Float](repeating: 0, count: 160),
                                           sampleRate: 16_000,
                                           timestamp: Double(offset) / 16_000)
        }
        XCTAssertEqual(scorer.calls, 8, "1280-sample windows must advance by exactly 1280 samples")
        XCTAssertEqual(events.map(\.kind), [.wakeWord])
        XCTAssertEqual(events[0].confidence, 0.95)
    }

    func testMissingModelsAreUnavailableWithoutFailingCaptureInitialization() throws {
        let missing = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
        let capture = try MicrophoneCapture(modelDirectory: missing)
        XCTAssertFalse(capture.wakeWordAvailable)
        XCTAssertNotNil(capture.wakeWordError)
        capture.setWakeWord(enabled: false)
        capture.stop()
    }

    func testActualModelScoresSyntheticJarvisClip() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let models = root.appendingPathComponent("Resources/WakeWord")
        let fixture = root.appendingPathComponent("Tests/ReplayFixtures/jarvis_bare_synthetic_holdout.wav")
        let scorer = try JarvisWakeWordScorer(modelsDirectory: models)
        let file = try AVAudioFile(forReading: fixture)
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                                     frameCapacity: AVAudioFrameCount(file.length)))
        try file.read(into: buffer)
        let pcm = Array(UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength)))
        scorer.reset()
        var maxScore: Float = 0
        for offset in stride(from: 0, through: pcm.count - scorer.windowSize, by: scorer.windowSize) {
            maxScore = max(maxScore, try scorer.score(samples: Array(pcm[offset ..< offset + scorer.windowSize])))
        }
        XCTAssertGreaterThan(maxScore, 0.8)
    }

    func testReplayCallsCaptureActivationWithWakeWordKind() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let models = root.appendingPathComponent("Resources/WakeWord")
        let fixture = root.appendingPathComponent("Tests/ReplayFixtures/jarvis_bare_synthetic_holdout.wav")
        let capture = try MicrophoneCapture(modelDirectory: models)
        XCTAssertTrue(capture.wakeWordAvailable)
        let activation = expectation(description: "wake-word activation")
        capture.onActivation = { _, kind, _ in
            if kind == ActivationKind.wakeWord.rawValue { activation.fulfill() }
        }
        capture.setAllowed(true)
        capture.replay(files: [fixture]) { }
        wait(for: [activation], timeout: 8)
        capture.stop()
    }
}

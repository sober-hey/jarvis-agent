import Foundation
import JarvisCore

/// Owner: wake word / ASR / TTS worker. Implementations must not create another brain.
public struct SpeechUtterance: Sendable, Equatable {
    public let text: String
    public let isFinal: Bool
    public init(text: String, isFinal: Bool) { self.text = text; self.isFinal = isFinal }
}
@MainActor
public protocol SpeechRecognitionService: AnyObject {
    var onUtterance: ((SpeechUtterance) -> Void)? { get set }
    func start() async throws
    func stop()
}
@MainActor
public protocol WakeWordService: AnyObject {
    var onWake: (() -> Void)? { get set }
    func start() throws
    func stop()
}

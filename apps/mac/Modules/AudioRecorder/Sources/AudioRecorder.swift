import AVFoundation
import Darwin
import Foundation

/// Independent macOS capture; s16le, 16kHz, mono. All methods are asynchronous.
/// Callbacks run on callbackQueue (main by default); never block that queue waiting.
public final class AudioRecorder {
    public struct Configuration {
        public let bufferDuration: TimeInterval
        public let inputTimeout: TimeInterval
        public init(bufferDuration: TimeInterval = 10, inputTimeout: TimeInterval = 3) throws {
            guard bufferDuration.isFinite, (3...10).contains(bufferDuration),
                  inputTimeout.isFinite, inputTimeout > 0 else { throw RecorderError.invalidConfiguration }
            self.bufferDuration = bufferDuration
            self.inputTimeout = inputTimeout
        }
    }
    public enum State: String { case idle, requestingPermission, recording, paused, stopped, failed }
    public enum RecorderError: Error, LocalizedError, Equatable {
        case invalidConfiguration, missingUsageDescription, permissionDenied, permissionRestricted
        case invalidState, cancelled, deviceUnavailable, deviceDisconnected, inputTimedOut, processingOverrun
        case conversionFailed(String), engineStartFailed(String), fileIO(String)
        public var errorDescription: String? {
            switch self {
            case .invalidConfiguration: return "Buffer must be 3–10 seconds and timeout must be positive."
            case .missingUsageDescription: return "Host Info.plist needs NSMicrophoneUsageDescription."
            case .permissionDenied: return "Microphone permission denied. Enable it in System Settings → Privacy & Security → Microphone."
            case .permissionRestricted: return "Microphone access is restricted by system policy."
            case .invalidState: return "Operation is not valid in the current recording state."
            case .cancelled: return "Recording start was cancelled."
            case .deviceUnavailable: return "No usable microphone input is available."
            case .deviceDisconnected: return "Audio device or format changed; recording stopped."
            case .inputTimedOut: return "Microphone stopped delivering audio."
            case .processingOverrun: return "Audio processing fell behind; recording stopped rather than dropping samples."
            case .conversionFailed(let m), .engineStartFailed(let m), .fileIO(let m): return m
            }
        }
    }
    public struct Recording {
        /// Only the newest bufferDuration seconds are retained in RAM.
        public let pcm: Data
        public let sampleCount: Int64
        public let fileURL: URL?
        public var duration: TimeInterval { Double(sampleCount) / 16000 }
        public let sampleRate = 16000
        public let channels = 1
        public let bitDepth = 16
    }
    public struct Callbacks {
        public var onVolume: ((Float) -> Void)?
        /// Bounded, coalesced PCM delivery. For lossless storage supply outputURL to start().
        public var onPCM: ((Data) -> Void)?
        public var onState: ((State) -> Void)?
        public var onError: ((RecorderError) -> Void)?
        public init(onVolume: ((Float) -> Void)? = nil, onPCM: ((Data) -> Void)? = nil,
                    onState: ((State) -> Void)? = nil, onError: ((RecorderError) -> Void)? = nil) {
            self.onVolume = onVolume; self.onPCM = onPCM; self.onState = onState; self.onError = onError
        }
    }
    private let configuration: Configuration
    private let callbackQueue: DispatchQueue
    private let callbacks: Callbacks
    private let permission: MicrophonePermission
    private let input: AudioInput
    private let work = DispatchQueue(label: "jarvis.audio.processing", qos: .userInitiated)
    private var state: State = .idle
    private var generation = UUID()
    private var converter: PCMConverter?
    private let ring: PCMBufferRing
    private var file: FileHandle?
    private var outputURL: URL?
    private var sampleCount: Int64 = 0
    private var watchdog: DispatchSourceTimer?
    private var lastInput = ProcessInfo.processInfo.systemUptime
    private var pendingStart: ((Result<Void, RecorderError>) -> Void)?
    private var notificationPending = false
    private var pendingPCM = Data()
    private var pendingVolume: Float = 0

    public convenience init(configuration: Configuration, callbackQueue: DispatchQueue = .main,
                            callbacks: Callbacks = .init()) {
        self.init(configuration: configuration, callbackQueue: callbackQueue, callbacks: callbacks,
                  permission: SystemMicrophonePermission(), input: EngineMicrophoneInput())
    }
    init(configuration: Configuration, callbackQueue: DispatchQueue, callbacks: Callbacks = .init(),
         permission: MicrophonePermission, input: AudioInput) {
        self.configuration = configuration; self.callbackQueue = callbackQueue; self.callbacks = callbacks
        self.permission = permission; self.input = input
        ring = PCMBufferRing(capacityBytes: Int(configuration.bufferDuration * 16000) * 2)
    }
    public static var permissionStatus: AVAuthorizationStatus { AVCaptureDevice.authorizationStatus(for: .audio) }
    public func currentState(_ completion: @escaping (State) -> Void) {
        work.async { let value = self.state; self.callbackQueue.async { completion(value) } }
    }
    public func start(outputURL: URL? = nil, completion: @escaping (Result<Void, RecorderError>) -> Void) {
        work.async { [self] in
            guard [.idle, .stopped, .failed].contains(self.state) else {
                self.deliver(completion, .failure(.invalidState)); return
            }
            self.generation = UUID(); let id = self.generation
            self.ring.reset(); self.sampleCount = 0; self.pendingPCM.removeAll(); self.converter = nil
            self.outputURL = outputURL; self.pendingStart = completion
            switch self.permission.status {
            case .authorized: self.begin(id)
            case .denied: self.fail(.permissionDenied)
            case .restricted: self.fail(.permissionRestricted)
            case .notDetermined:
                // Missing privacy strings abort the process in AVFoundation. Check before asking.
                if self.permission is SystemMicrophonePermission,
                   (Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String)?.isEmpty != false {
                    self.fail(.missingUsageDescription); return
                }
                self.setState(.requestingPermission)
                self.permission.request { [weak self] granted in
                    guard let self = self else { return }
                    self.work.async {
                        guard self.generation == id, self.state == .requestingPermission else { return }
                        if granted { self.begin(id) } else { self.fail(.permissionDenied) }
                    }
                }
            @unknown default: self.fail(.permissionRestricted)
            }
        }
    }
    private func begin(_ id: UUID) {
        do {
            if let url = outputURL {
                // Never silently overwrite existing recordings.
                let fd = Darwin.open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600))
                guard fd >= 0 else { throw RecorderError.fileIO("Cannot exclusively create recording file: \(String(cString: strerror(errno)))") }
                file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            }
            let format = try input.start(onBuffer: { [weak self] buffer, release in
                guard let self = self else { release(); return }
                self.work.async {
                    defer { release() }
                    guard self.generation == id, [.recording, .paused].contains(self.state) else { return }
                    self.lastInput = ProcessInfo.processInfo.systemUptime
                    guard self.state == .recording else { return }
                    do { if let chunk = try self.converter?.convert(buffer) { try self.consume(chunk) } }
                    catch { self.fail(self.typed(error)) }
                }
            }, onError: { [weak self] error in
                guard let self = self else { return }
                self.work.async { if self.generation == id && [.recording, .paused].contains(self.state) { self.fail(error) } }
            })
            converter = try PCMConverter(inputFormat: format)
            lastInput = ProcessInfo.processInfo.systemUptime
            let timer = DispatchSource.makeTimerSource(queue: work)
            timer.schedule(deadline: .now() + 0.1, repeating: 0.1)
            timer.setEventHandler { [weak self] in
                guard let self = self, self.state == .recording else { return }
                if ProcessInfo.processInfo.systemUptime - self.lastInput > self.configuration.inputTimeout {
                    self.fail(.inputTimedOut)
                }
            }
            watchdog = timer; timer.resume()
            setState(.recording)
            if let completion = pendingStart { pendingStart = nil; deliver(completion, .success(())) }
        } catch { fail(typed(error)) }
    }
    public func pause(completion: @escaping (Result<Void, RecorderError>) -> Void = { _ in }) {
        work.async {
            guard self.state == .recording else { self.deliver(completion, .failure(.invalidState)); return }
            self.setState(.paused); self.queueNotification(data: Data(), level: 0)
            self.deliver(completion, .success(()))
        }
    }
    public func resume(completion: @escaping (Result<Void, RecorderError>) -> Void = { _ in }) {
        work.async {
            guard self.state == .paused else { self.deliver(completion, .failure(.invalidState)); return }
            self.lastInput = ProcessInfo.processInfo.systemUptime; self.setState(.recording)
            self.deliver(completion, .success(()))
        }
    }
    public func bufferedPCM(_ completion: @escaping (Data) -> Void) {
        work.async { let data = self.ring.snapshot(); self.callbackQueue.async { completion(data) } }
    }
    public func stop(completion: @escaping (Result<Recording, RecorderError>) -> Void) {
        work.async {
            if self.state == .requestingPermission {
                self.generation = UUID()
                if let pending = self.pendingStart { self.pendingStart = nil; self.deliver(pending, .failure(.cancelled)) }
                self.outputURL = nil
                self.setState(.stopped); self.deliver(completion, .failure(.cancelled)); return
            }
            guard [.recording, .paused].contains(self.state) else {
                self.deliver(completion, .failure(.invalidState)); return
            }
            self.input.stop(); self.watchdog?.cancel(); self.watchdog = nil
            do {
                if let chunk = try self.converter?.finish() { try self.consume(chunk) }
                try self.file?.synchronize(); try self.file?.close(); self.file = nil
                self.converter = nil; self.setState(.stopped)
                self.queueNotification(data: Data(), level: 0)
                self.deliver(completion, .success(self.recording()))
            } catch { let e = self.typed(error); self.fail(e); self.deliver(completion, .failure(e)) }
        }
    }
    private func recording() -> Recording { Recording(pcm: ring.snapshot(), sampleCount: sampleCount, fileURL: outputURL) }
    private func consume(_ chunk: PCMConverter.Chunk) throws {
        if !chunk.data.isEmpty {
            do { try file?.write(contentsOf: chunk.data) } catch { throw RecorderError.fileIO(error.localizedDescription) }
            ring.append(chunk.data); sampleCount += Int64(chunk.data.count / 2)
        }
        queueNotification(data: chunk.data, level: chunk.level)
    }
    private func queueNotification(data: Data, level: Float) {
        pendingPCM.append(data)
        if pendingPCM.count > 320000 { pendingPCM = Data(pendingPCM.suffix(320000)) }
        pendingVolume = level
        guard !notificationPending else { return }
        notificationPending = true
        callbackQueue.async { [weak self] in
            guard let self = self else { return }
            let payload: (Data, Float) = self.work.sync {
                let value = (self.pendingPCM, self.pendingVolume)
                self.pendingPCM.removeAll(keepingCapacity: true); self.notificationPending = false
                return value
            }
            self.callbacks.onVolume?(payload.1)
            if !payload.0.isEmpty { self.callbacks.onPCM?(payload.0) }
        }
    }
    private func fail(_ error: RecorderError) {
        input.stop(); watchdog?.cancel(); watchdog = nil; converter = nil
        try? file?.close(); file = nil
        setState(.failed); queueNotification(data: Data(), level: 0)
        if let completion = pendingStart { pendingStart = nil; deliver(completion, .failure(error)) }
        callbackQueue.async { self.callbacks.onError?(error) }
    }
    private func setState(_ value: State) {
        state = value; callbackQueue.async { self.callbacks.onState?(value) }
    }
    private func typed(_ error: Error) -> RecorderError { (error as? RecorderError) ?? .fileIO(error.localizedDescription) }
    private func deliver<T>(_ completion: @escaping (Result<T, RecorderError>) -> Void, _ result: Result<T, RecorderError>) {
        callbackQueue.async { completion(result) }
    }
    deinit { input.stop(); watchdog?.cancel(); try? file?.close() }
}

import AVFoundation
import Foundation
import AudioRecorderModule
import JarvisActivation

/// One real engine/tap, eight bounded copied buffers. Both consumers run on this worker.
/// No FFT, conversion, VAD or network work executes on the realtime tap.
public final class MicrophoneCapture {
    private let work = DispatchQueue(label: "com.jarvis.mac.capture", qos: .userInitiated)
    private let input = EngineMicrophoneInput()
    private let detector: ActivationDetector
    private let wakeDetector: WakeWordDetector
    private let wakeScorer: JarvisWakeWordScorer?
    private var converter: PCMConverter?
    private var running = false
    private var runID = UUID()
    private var allowed = false
    private var clap = true, snap = true
    private var recordingID: UUID?
    private var pcm = Data()
    private var elapsed: Double = 0, lastSpeech: Double = 0, voiced: Double = 0
    private var skipFrames = 0
    /// After a reply the conversation stays open: speech starts a new utterance
    /// without saying "Jarvis" again. 0 = closed.
    private var conversationUntil: Double = 0
    /// Real-room impulse log so gesture thresholds can be set from measurements.
    private var impulseLog: FileHandle?
    private var impulseLines = 0
    private var impulseWindowStart = Date()
    private var playbackActive = false
    private var playbackGuard: DispatchWorkItem?
    /// 统一的一句话切分逻辑：噪声自适应、思考停顿、立刻补充、语气词丢弃。
    private var utterance = UtteranceGate()
    /// 开口前 200ms 的音频，避免第一个字被切掉。
    private var preRoll: [Data] = []
    private var onsetFrames = 0
    private var lastRate: Double?
    private var permissionToken = UUID()
    private let permissionLock = NSLock()
    private var lastInput = ProcessInfo.processInfo.systemUptime
    private var expectedAudioTimestamp: Double?
    private var wakeTimestamp: Double = 0
    private var wakeWordEnabled = true
    private var watchdog: DispatchSourceTimer?
    public var onActivation: ((UUID, String, Double) -> Void)?
    public var onUtterance: ((UUID, Data, Double, Double) -> Void)?
    public var onNoSpeech: ((UUID) -> Void)?
    public var onError: ((String) -> Void)?
    public var onCapture: ((Bool, Double) -> Void)?
    /// true = the microphone was released for a call; false = taken back.
    public var onYield: ((Bool, String?) -> Void)?
    public private(set) var yielded = false
    private var wanted = false
    private var yieldReason: String?
    private var resumeRetry: DispatchWorkItem?
    public private(set) var wakeWordAvailable = false
    public private(set) var wakeWordError: String?

    /// `modelDirectory` is injectable for tests; production loads `Bundle.main/WakeWord`.
    /// A missing or invalid model is an unavailable optional feature, not an app failure.
    public init(modelDirectory: URL? = nil) throws {
        var scorer: JarvisWakeWordScorer?
        var loadError: String?
        let directory = modelDirectory ?? Bundle.main.resourceURL
        if let directory {
            do { scorer = try JarvisWakeWordScorer(modelsDirectory: directory) }
            catch { loadError = error.localizedDescription; scorer = nil }
        } else {
            loadError = "WakeWord resources are not present in the application bundle."
            scorer = nil
        }
        // Gestures intentionally remain on the original-rate branch. Wake processing
        // is fed below from PCMConverter's stateful 16 kHz output instead.
        detector = try ActivationDetector()
        wakeScorer = scorer
        wakeDetector = WakeWordDetector(scorer: scorer,
                                        configuration: WakeWordConfiguration(mode: .model,
                                                                              inferenceHop: 0.08))
        wakeWordAvailable = scorer != nil
        wakeWordError = loadError
        detector.onImpulse = { [weak self] features in
            guard let self else { return }
            self.impulseWindowStart = self.impulseWindowStart
            let now = Date()
            if now.timeIntervalSince(self.impulseWindowStart) > 1 {
                self.impulseWindowStart = now; self.impulseLines = 0
            }
            guard self.impulseLines < 40 else { return }
            self.impulseLines += 1
            let path = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".jarvis/mac-gestures.jsonl")
            if self.impulseLog == nil {
                FileManager.default.createFile(atPath: path.path, contents: nil)
                self.impulseLog = try? FileHandle(forWritingTo: path)
                self.impulseLog?.seekToEndOfFile()
            }
            let line = String(format: "{\"t\":%.3f,\"clap\":%.1f,\"snap\":%.1f,\"cf\":%.2f,\"sf\":%.2f,\"crest\":%.1f}\n",
                              features.timestamp, features.clapDBFS, features.snapDBFS,
                              features.clapEnergyFraction, features.snapEnergyFraction,
                              features.crestFactor)
            self.impulseLog?.write(line.data(using: .utf8) ?? Data())
        }
        detector.onActivation = { [weak self] event in
            guard let self, self.running, self.allowed else { return }
            let enabled = (event.kind == .doubleClap && self.clap) || (event.kind == .fingerSnap && self.snap) || event.kind == .wakeWord
            guard enabled else { return }
            self.begin(kind: event.kind.rawValue, rate: self.lastRate ?? 0)
        }
    }
    public func setAllowed(_ value: Bool) {
        work.async { self.allowed = value; if !value { self.cancelRecording() } }
    }
    public func setGestures(clap: Bool, snap: Bool) { work.async { self.clap = clap; self.snap = snap } }
    public func setWakeWord(enabled: Bool) {
        work.async { self.wakeWordEnabled = enabled; self.resetWakeState() }
    }
    public func manualActivation() { work.async { if self.running && self.allowed { self.begin(kind: "manual", rate: self.lastRate ?? 0) } } }
    /// Keep listening for follow-up speech for `seconds`; 0 closes the window.
    public func openConversation(_ seconds: TimeInterval) {
        work.async { self.conversationUntil = seconds > 0 ? ProcessInfo.processInfo.systemUptime + seconds : 0 }
    }
    /// Replies must not be recorded as a new utterance (speaker leaks into the mic).
    /// 回复音频走采集引擎的输出总线，VPIO 才能把它当自身输出抵掉。
    public func playViaEngine(_ mp3: Data, completion: @escaping () -> Void) -> Bool {
        var started = false
        work.sync { started = running ? input.playMP3(mp3, completion: completion) : false }
        return started
    }

    public func stopEnginePlayback() { work.async { self.input.stopPlayback() } }

    public func setPlaybackActive(_ value: Bool) {
        work.async {
            self.playbackActive = value
            self.onsetFrames = 0
            self.playbackGuard?.cancel()
            self.playbackGuard = nil
            guard value else { return }
            // A lost completion callback must never leave the microphone muted.
            let guardItem = DispatchWorkItem { [weak self] in self?.playbackActive = false }
            self.playbackGuard = guardItem
            self.work.asyncAfter(deadline: .now() + 90, execute: guardItem)
        }
    }
    public var conversationOpen: Bool {
        var value = false
        work.sync { value = conversationUntil > ProcessInfo.processInfo.systemUptime }
        return value
    }
    private func begin(kind: String, rate: Double, keepPreRoll: Bool = false) {
        guard recordingID == nil else { return }
        utterance.forceStart()
        // 手势/唤醒词开录时丢掉预卷（里面可能是敲键盘的杂音）；
        // 由 UtteranceGate 判定的开口要保留预卷，否则第一个字会被切掉。
        if !keepPreRoll { preRoll.removeAll(keepingCapacity: true) }
        let id = UUID(); recordingID = id; pcm.removeAll(keepingCapacity: true)
        elapsed = 0; lastSpeech = 0; voiced = 0; skipFrames = 1920 // 120ms gesture release, at 16kHz.
        conversationUntil = ProcessInfo.processInfo.systemUptime + 25
        DispatchQueue.main.async { self.onActivation?(id, kind, rate) }
    }
    private func cancelRecording() {
        recordingID = nil
        pcm.removeAll(keepingCapacity: true)
        elapsed = 0
        voiced = 0
        preRoll.removeAll(keepingCapacity: true)
        utterance.resetAfterUtterance()
    }
    public func start() {
        work.async { self.wanted = true }
        permissionLock.lock(); permissionToken = UUID(); let token = permissionToken; permissionLock.unlock()
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: startAuthorized(token: token)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                if granted { self.startAuthorized(token: token) } else { self.error("麦克风未授权，请在系统设置中启用。") }
            }
        default: error("麦克风未授权，请在系统设置 → 隐私与安全性 → 麦克风中启用 JARVIS。")
        }
    }
    private func startAuthorized(token: UUID) {
        work.async {
            self.permissionLock.lock(); let valid = self.permissionToken == token; self.permissionLock.unlock()
            guard valid, !self.running else { return }
            self.runID = UUID(); let id = self.runID
            self.running = true
            do {
                let format = try self.input.startTimed(onBuffer: { [weak self] buffer, timestamp, release in
                    guard let self else { release(); return }
                    self.work.async {
                        defer { release() }
                        guard self.running, self.runID == id else { return }
                        do { try self.consume(buffer, timestamp: timestamp) } catch { self.fail(error.localizedDescription) }
                    }
                }, onError: { [weak self] error in
                    self?.work.async {
                        guard let self else { return }
                        // A configuration change is usually a call taking the
                        // device; yielding keeps the app out of the way instead
                        // of declaring the microphone broken.
                        if case .deviceDisconnected = error { self.yieldOnWorker("音频设备被占用（可能是通话）") }
                        else { self.fail(error.localizedDescription) }
                    }
                })
                self.converter = try PCMConverter(inputFormat: format); self.lastRate = format.sampleRate
                self.detector.reset(); self.resetWakeState(); self.lastInput = ProcessInfo.processInfo.systemUptime
                let watchdog = DispatchSource.makeTimerSource(queue: self.work)
                watchdog.schedule(deadline: .now() + 1, repeating: 1)
                watchdog.setEventHandler { [weak self] in
                    guard let self, self.running else { return }
                    if ProcessInfo.processInfo.systemUptime - self.lastInput > 3 { self.fail("麦克风超过 3 秒未送达音频，请重新开启。") }
                }
                self.watchdog = watchdog; watchdog.resume()
                if self.yielded {
                    // The device came back on a retry: take it over cleanly.
                    self.yielded = false
                    DispatchQueue.main.async { self.onYield?(false, nil) }
                }
                DispatchQueue.main.async { self.onCapture?(true, format.sampleRate) }
            } catch {
                // While yielded, a failed probe only means the call still owns the
                // device: stay out of the way and retry later, do not report an error.
                if self.yielded { self.stopOnWorker() } else { self.fail(error.localizedDescription) }
            }
        }
    }
    public func stop() {
        work.async { self.wanted = false; self.resumeRetry?.cancel(); self.resumeRetry = nil }
        permissionLock.lock(); permissionToken = UUID(); permissionLock.unlock()
        work.async { self.stopOnWorker() }
    }
    private func stopOnWorker() {
        running = false; runID = UUID(); watchdog?.cancel(); watchdog = nil
        input.stop(); converter = nil; detector.reset(); resetWakeState(); cancelRecording()
        DispatchQueue.main.async { self.onCapture?(false, self.lastRate ?? 0) }
    }
    private func fail(_ message: String) { stopOnWorker(); error(message) }

    /// Release the input device so a phone or video call can use it.
    ///
    /// The intent to listen is kept: the microphone is taken back automatically
    /// when the call ends, and a retry keeps trying in case the call app is not
    /// observable (for example a call inside an already-running app).
    public func yieldForCall(_ reason: String) {
        work.async { self.yieldOnWorker(reason) }
    }
    public func resumeAfterCall() {
        work.async { self.resumeOnWorker() }
    }
    private func yieldOnWorker(_ reason: String) {
        guard wanted else { return }
        yieldReason = reason
        if running { stopOnWorker() }
        yieldReason = reason
        yielded = true
        resumeRetry?.cancel()
        scheduleResumeCheck(attempt: 0)
        DispatchQueue.main.async { self.onYield?(true, reason) }
    }
    private func resumeOnWorker() {
        resumeRetry?.cancel(); resumeRetry = nil
        yieldReason = nil
        guard yielded else { return }
        yielded = false
        DispatchQueue.main.async { self.onYield?(false, nil) }
        guard wanted, !running else { return }
        permissionLock.lock(); permissionToken = UUID(); let token = permissionToken; permissionLock.unlock()
        startAuthorized(token: token)
    }
    private func scheduleResumeCheck(attempt: Int) {
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            guard self.yielded else { return }
            guard self.wanted else { return }
            // Still yielded: probe whether the device is usable again. A failed
            // probe keeps the app out of the way and tries again later.
            let token = UUID()
            self.permissionLock.lock(); self.permissionToken = token; self.permissionLock.unlock()
            self.startAuthorized(token: token)
            self.scheduleResumeCheck(attempt: attempt + 1)
        }
        resumeRetry = work
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + (attempt > 6 ? 30 : 5), execute: work)
    }
    private func error(_ message: String) { DispatchQueue.main.async { self.onError?(message) } }
    private func consume(_ buffer: AVAudioPCMBuffer, timestamp: Double) throws {
        lastInput = ProcessInfo.processInfo.systemUptime
        if let expected = expectedAudioTimestamp,
           abs(expected - timestamp) > 2 / buffer.format.sampleRate {
            resetWakeState()
            detector.reset()
            converter = try PCMConverter(inputFormat: buffer.format)
            wakeTimestamp = 0
        }
        if let rate = lastRate, abs(rate - buffer.format.sampleRate) > 0.01 {
            resetWakeState()
            detector.reset()
            converter = try PCMConverter(inputFormat: buffer.format)
            wakeTimestamp = 0
        }
        lastRate = buffer.format.sampleRate
        expectedAudioTimestamp = timestamp + Double(buffer.frameLength) / buffer.format.sampleRate
        // This order is a contract: 8–12kHz snap detector MUST see original 44.1/48kHz.
        try detector.process(buffer: buffer, timestamp: timestamp)
        let chunk = try converter!.convert(buffer)
        if wakeWordEnabled, wakeWordAvailable, !chunk.data.isEmpty {
            let samples: [Float] = chunk.data.withUnsafeBytes { raw in
                raw.bindMemory(to: Int16.self).map { Float($0) / 32768.0 }
            }
            do {
                let events = try wakeDetector.process(samples: samples, sampleRate: 16000,
                                                       timestamp: wakeTimestamp)
                wakeTimestamp += Double(samples.count) / 16000
                for event in events where event.kind == .wakeWord {
                    guard running, allowed else { continue }
                    begin(kind: event.kind.rawValue, rate: buffer.format.sampleRate)
                }
            } catch {
                wakeWordError = error.localizedDescription
                // A model error must not take down microphone capture or gestures.
            }
        }
        guard allowed, !chunk.data.isEmpty else { return }
        // Half-duplex: while JARVIS is speaking, the microphone must not start a new
        // utterance. Otherwise the user's first sentence kills the reply mid-word.
        if playbackActive { return }
        let data = chunk.data
        let dt = Double(data.count / 2) / 16000
        let rms: Double = data.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            guard !samples.isEmpty else { return 0 }
            return sqrt(samples.reduce(0.0) { $0 + pow(Double($1) / 32768, 2) } / Double(samples.count))
        }
        if recordingID == nil {
            // Conversation window: follow-up speech needs no wake word or gesture.
            guard conversationUntil > ProcessInfo.processInfo.systemUptime else { return }
            switch utterance.process(.init(rms: Float(rms), seconds: dt)) {
            case .started:
                begin(kind: "followup", rate: buffer.format.sampleRate, keepPreRoll: true)
                skipFrames = 0
            default:
                preRoll.append(data)
                if preRoll.count > 15 { preRoll.removeFirst() }
                return
            }
        }
        guard let id = recordingID else { return }
        var body = data
        if skipFrames > 0 {
            let skipped = min(skipFrames, body.count / 2); skipFrames -= skipped
            body = Data(body.dropFirst(skipped * 2))
        }
        guard !body.isEmpty else { return }
        if !preRoll.isEmpty {
            for buffered in preRoll { pcm.append(buffered) }
            preRoll.removeAll(keepingCapacity: true)
        }
        elapsed += dt; pcm.append(body)
        if rms >= Double(utterance.threshold) { lastSpeech = elapsed; voiced += dt }
        switch utterance.process(.init(rms: Float(rms), seconds: dt)) {
        case .ended:
            let payload = pcm, duration = elapsed, silence = max(0, elapsed - lastSpeech)
            cancelRecording()
            DispatchQueue.main.async { self.onUtterance?(id, payload, duration, silence) }
        case .discarded:
            cancelRecording()
            DispatchQueue.main.async { self.onNoSpeech?(id) }
        default:
            break
        }
    }
    private final class Replay {
        let files: [URL], completion: () -> Void
        var index = 0, tail = 120
        var file: AVAudioFile?
        var timestamp = 0.0
        init(_ files: [URL], _ completion: @escaping () -> Void) { self.files = files; self.completion = completion }
    }
    /// Explicit no-microphone hook; SAME original-rate detector/converter/VAD path.
    /// Paced one block per worker turn, so suppression/stop never waits for an entire clip.
    public func replay(files: [URL], completion: @escaping () -> Void) {
        work.async {
            guard !self.running else { self.error("请先停止麦克风再回放。"); return }
            self.running = true; self.runID = UUID(); self.detector.reset(); self.cancelRecording()
            self.resetWakeState()
            self.replayStep(Replay(files, completion), id: self.runID)
        }
    }
    private func replayStep(_ session: Replay, id: UUID) {
        guard running, runID == id else { DispatchQueue.main.async { session.completion() }; return }
        do {
            if session.file == nil && session.index < session.files.count {
                let file = try AVAudioFile(forReading: session.files[session.index]); session.index += 1
                guard file.processingFormat.sampleRate >= 16000 else {
                    throw NSError(domain: "JarvisReplay", code: 1, userInfo: [NSLocalizedDescriptionKey: "回放要求至少 16kHz WAV"])
                }
                session.file = file; lastRate = file.processingFormat.sampleRate
                converter = try PCMConverter(inputFormat: file.processingFormat)
                DispatchQueue.main.async { self.onCapture?(true, file.processingFormat.sampleRate) }
            }
            let format = session.file?.processingFormat ?? AVAudioFormat(standardFormatWithSampleRate: lastRate ?? 48000, channels: 1)!
            let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(format.sampleRate / 100))!
            if let file = session.file {
                try file.read(into: b, frameCount: b.frameCapacity)
                if file.framePosition >= file.length { session.file = nil }
            } else if session.tail > 0 {
                session.tail -= 1; b.frameLength = b.frameCapacity
                memset(b.floatChannelData![0], 0, Int(b.frameLength) * 4)
            } else {
                running = false; converter = nil; DispatchQueue.main.async { session.completion() }; return
            }
            if converter == nil { converter = try PCMConverter(inputFormat: format) }
            try consume(b, timestamp: session.timestamp)
            let dt = Double(b.frameLength) / format.sampleRate; session.timestamp += dt
            work.asyncAfter(deadline: .now() + dt) { self.replayStep(session, id: id) }
        } catch { fail(error.localizedDescription); DispatchQueue.main.async { session.completion() } }
    }

    private func resetWakeState() {
        wakeDetector.reset()
        wakeScorer?.reset()
        wakeTimestamp = 0
        expectedAudioTimestamp = nil
    }
}

import Foundation

/// 判断一段话何时开始、何时结束，以及这段值不值得回答。
///
/// 抽成纯逻辑是为了能被仿真测试覆盖：说话方式（停顿、拖音、小声、噪声、
/// 抢话、长独白）不该靠现场试错来调参。
///
/// 设计要点，逐条对应真实说话方式：
/// - 噪声自适应：门限取「静音底噪的 2.5 倍」，不再写死 0.012，
///   小声说话能进来、嘈杂环境不误触发。
/// - 思考停顿：默认 0.9 秒静音才结束，短句（说完就等回答）放宽到 1.4 秒，
///   长独白收紧到 0.7 秒，避免一个人说 20 秒还等你 1.4 秒。
/// - 立刻补充：判定结束后留 0.35 秒观察窗，这期间再出声算同一句，
///   不切成两次提问。
/// - 语气词：只有「嗯/呃/啊」且不足 0.35 秒的片段不单独送出去。
/// - 硬上限 30 秒（网关上限），超过就截断提交。
public struct UtteranceGate: Sendable {
    public struct Frame: Sendable {
        public let rms: Float
        public let seconds: Double
        public init(rms: Float, seconds: Double) {
            self.rms = rms
            self.seconds = seconds
        }
    }

    public enum Decision: Sendable, Equatable {
        case idle
        case recording
        /// 说话开始（携带原因，便于日志区分）
        case started
        /// 一句话结束，附这段时间的语音长度
        case ended(voiced: Double)
        /// 判定为无语音（只有噪声/气声），不上传
        case discarded
    }

    public struct Configuration: Sendable {
        /// 绝对下限：低于这个永远不算说话，防止数字静音被放大。
        public var floorRMS: Float = 0.004
        /// 门限 = 静音底噪 × 这个倍数。
        public var noiseMultiplier: Float = 2.0
        /// 启动后先只听不判，用这段音频标定房间噪声（否则一开机就把底噪当人声）。
        public var calibrateSeconds: Double = 0.5
        /// 连续多少秒超过门限才算开口（防键盘敲击、短促噪声）。
        public var onsetSeconds: Double = 0.06
        /// 至少要有这么多秒的语音才值得上传。
        public var minimumVoiced: Double = 0.10
        /// 常规结束静音。
        public var silenceToEnd: Double = 0.9
        /// 语音不足这个长度时用的结束静音（说完短句在等回答）。
        public var shortUtteranceLimit: Double = 1.2
        public var silenceToEndShort: Double = 1.4
        /// 语音超过这个长度后收紧结束静音（长独白）。
        public var longUtteranceLimit: Double = 8.0
        public var silenceToEndLong: Double = 0.8
        /// 结束判定后的观察窗：期间再出声算同一句。
        /// 豆包语义 VAD 用「句末标点 300ms / 逗号 400ms / 无法判定 700ms」，
        /// 我们做不了语义判定，取 0.5 秒覆盖逗号级停顿。
        public var continuationWindow: Double = 0.5
        /// 硬上限。
        public var maximumSeconds: Double = 29.0
        /// 一直没说话就放弃。
        public var emptyGiveUpSeconds: Double = 6.0
        public init() {}
    }

    public let configuration: Configuration
    private var noiseFloor: Float = 0.004
    private var armed: Double = 0
    private var recording = false
    private var voiced: Double = 0
    private var elapsed: Double = 0
    private var silence: Double = 0
    private var pendingEnd: Double?
    private var lastVoiceAt: Double = 0
    private var quietFrames = 0
    private var calibrating: Double = 0
    private var calibrationSum: Float = 0
    private var calibrationFrames = 0

    public init(configuration: Configuration = Configuration()) {
        self.configuration = configuration
    }

    public mutating func reset() {
        self = UtteranceGate(configuration: configuration)
        noiseFloor = configuration.floorRMS / 2
    }

    /// 手势/唤醒词/手动开录时，让状态机认为「已经在说话」，
    /// 避免它把这轮当真空气再判一次结束。
    public mutating func forceStart() {
        recording = true
        voiced = configuration.minimumVoiced
        elapsed = 0
        silence = 0
        pendingEnd = nil
        armed = 0
        lastVoiceAt = 0
    }

    /// 一句话结束/取消后清计数器，但保留已标定好的噪声底。
    public mutating func resetAfterUtterance() {
        recording = false
        resetCounters()
    }

    /// 重新标定房间噪声（换环境时由调用方触发）。
    public mutating func recalibrate() {
        calibrating = 0
        calibrationSum = 0
        calibrationFrames = 0
        armed = 0
    }

    /// 当前生效的门限，便于日志核对。
    public var threshold: Float {
        max(configuration.floorRMS, noiseFloor * configuration.noiseMultiplier)
    }

    public mutating func process(_ frame: Frame) -> Decision {
        let dt = frame.seconds
        guard dt > 0, frame.rms.isFinite else { return .idle }
        if calibrating < configuration.calibrateSeconds {
            calibrating += dt
            calibrationSum += frame.rms
            calibrationFrames += 1
            noiseFloor = max(configuration.floorRMS / 2, calibrationSum / Float(calibrationFrames))
            return .idle
        }
        let threshold = self.threshold
        let voicedFrame = frame.rms >= threshold

        if !recording {
            // 静音期持续跟踪底噪，慢慢收敛到真实的房间噪声。
            if !voicedFrame {
                quietFrames += 1
                if quietFrames >= 3 {
                    noiseFloor = noiseFloor * 0.95 + frame.rms * 0.05
                }
            }
            armed = voicedFrame ? armed + dt : 0
            guard armed >= configuration.onsetSeconds else { return .idle }
            // 开口
            recording = true
            armed = 0
            voiced = 0
            elapsed = 0
            silence = 0
            pendingEnd = nil
            quietFrames = 0
            lastVoiceAt = 0
            // 本帧计入语音
            elapsed += dt
            voiced += dt
            return .started
        }

        elapsed += dt
        if voicedFrame {
            voiced += dt
            silence = 0
            lastVoiceAt = elapsed
            pendingEnd = nil
            return .recording
        }
        silence += dt

        // 结束判定：先标记，再等一个观察窗；观察窗内重新出声就不算结束。
        let limit = endSilenceLimit()
        if pendingEnd == nil, voiced >= configuration.minimumVoiced, silence >= limit {
            pendingEnd = elapsed
        }
        if let pending = pendingEnd, elapsed - pending >= configuration.continuationWindow {
            pendingEnd = nil
            recording = false
            let spoken = voiced
            resetCounters()
            if spoken < configuration.minimumVoiced { return .discarded }
            return .ended(voiced: spoken)
        }
        if elapsed >= configuration.maximumSeconds {
            recording = false
            let spoken = voiced
            resetCounters()
            return spoken >= configuration.minimumVoiced ? .ended(voiced: spoken) : .discarded
        }
        if voiced < configuration.minimumVoiced, elapsed >= configuration.emptyGiveUpSeconds {
            recording = false
            resetCounters()
            return .discarded
        }
        return .recording
    }

    private func endSilenceLimit() -> Double {
        if elapsed >= configuration.longUtteranceLimit { return configuration.silenceToEndLong }
        if elapsed <= configuration.shortUtteranceLimit { return configuration.silenceToEndShort }
        return configuration.silenceToEnd
    }

    private mutating func resetCounters() {
        voiced = 0
        elapsed = 0
        silence = 0
        pendingEnd = nil
        armed = 0
        lastVoiceAt = 0
    }
}

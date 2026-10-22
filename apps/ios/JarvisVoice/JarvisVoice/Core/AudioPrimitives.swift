import Foundation

struct SpeechGate {
    struct Result {
        var started = false
        var ended = false
        var frames: [[Float]] = []
        var level: Float = 0
    }
    var threshold: Float = 0.018
    // 900ms：中文思考停顿常在 0.6–1.0 秒之间，600ms 会把一句话切成两半。
    var endSilenceFrames = 45 // 900 ms at 20 ms/frame
    var preRollFrames = 10
    var maxFrames = 1250 // 25 seconds; server limit is 30 seconds
    private var preRoll: [[Float]] = []
    private var active = false
    private var silence = 0
    private var count = 0

    mutating func process(_ samples: [Float]) -> Result {
        guard !samples.isEmpty else { return Result() }
        let power = samples.reduce(Float(0)) { $0 + ($1.isFinite ? $1 * $1 : 0) }
        let rms = sqrt(power / Float(samples.count))
        var result = Result(level: rms)
        let voiced = rms >= threshold
        if !active {
            preRoll.append(samples)
            if preRoll.count > preRollFrames { preRoll.removeFirst() }
            guard voiced else { return result }
            result.started = true
            result.frames = preRoll
            preRoll.removeAll(keepingCapacity: true)
            active = true
            count = result.frames.count
            silence = 0
        } else {
            result.frames = [samples]
            count += 1
            silence = voiced ? 0 : silence + 1
        }
        if silence >= endSilenceFrames || count >= maxFrames {
            result.ended = true
            active = false
            count = 0
            silence = 0
        }
        return result
    }
}

enum PCM16 {
    static func encode(_ samples: [Float]) -> Data {
        var bytes = Data(capacity: samples.count * 2)
        for sample in samples {
            let finite = sample.isFinite ? min(1, max(-1, sample)) : 0
            let integer = finite <= -1 ? Int16.min : Int16(finite * Float(Int16.max))
            let raw = UInt16(bitPattern: integer)
            bytes.append(UInt8(truncatingIfNeeded: raw))
            bytes.append(UInt8(truncatingIfNeeded: raw >> 8))
        }
        return bytes
    }
}

enum EndpointPolicy {
    static func validate(_ value: String) -> URL? {
        guard let url = URL(string: value), let host = url.host,
              url.user == nil, url.password == nil, url.fragment == nil,
              url.scheme == "ws" || url.scheme == "wss" else { return nil }
        if url.scheme == "wss" { return url }
        let numbers = host.split(separator: ".").compactMap { Int($0) }
        let tailscale = numbers.count == 4 && numbers.allSatisfy { (0...255).contains($0) }
            && numbers[0] == 100 && (64...127).contains(numbers[1])
        let loopback = host == "127.0.0.1" || host == "localhost" || host == "::1" || host == "[::1]"
        let configuredHost = ["SoberdeMacBook-Air.local", "192.168.1.5", "100.67.229.1"].contains(host)
        return tailscale || loopback || configuredHost ? url : nil
    }
}

struct UtteranceForwarding {
    private var sending = false
    mutating func reset() { sending = false }
    mutating func consume(started: Bool, ended: Bool, connected: Bool, selected: Bool) -> Bool {
        if !connected || !selected { sending = false; return false }
        if started { sending = true }
        let forward = sending
        if ended { sending = false }
        return forward
    }
}

struct PlaybackAccumulator {
    private(set) var generation = -1
    private var discardedThrough = -1
    private var data = Data()
    static let maximumBytes = 24 * 1024 * 1024

    mutating func interrupt() {
        discardedThrough = max(discardedThrough, generation)
        data.removeAll(keepingCapacity: false)
    }

    mutating func reset() { self = PlaybackAccumulator() }

    mutating func append(_ chunk: Data, generation incoming: Int, final: Bool) -> Data? {
        guard incoming > discardedThrough, incoming >= generation else { return nil }
        if incoming != generation {
            generation = incoming
            data.removeAll(keepingCapacity: true)
        }
        guard data.count + chunk.count <= Self.maximumBytes else {
            interrupt()
            return nil
        }
        data.append(chunk)
        guard final, !data.isEmpty else { return nil }
        let complete = data
        data.removeAll(keepingCapacity: true)
        return complete
    }
}

import Foundation
import CoreFoundation

/// CoreMotion raw acceleration is expressed in g and includes gravity.
/// Two distinct above-threshold excursions, separated by 200...600ms.
/// Hysteresis prevents a wide pulse from being counted as several impacts.
public struct DoubleImpactDetector {
    public struct Configuration {
        public var thresholdG: Double = 2.5
        public var rearmG: Double = 1.8
        public var minimumInterval: Double = 0.2
        public var maximumInterval: Double = 0.6
        public var cooldown: Double = 1.2
        public init() {}
    }
    private let config: Configuration
    private var firstImpact: Double?
    private var lastTimestamp: Double?
    private var armed = true
    private var suppressUntil = -Double.infinity

    public init(configuration: Configuration = Configuration()) {
        precondition(configuration.thresholdG > configuration.rearmG)
        precondition(configuration.minimumInterval > 0)
        precondition(configuration.maximumInterval >= configuration.minimumInterval)
        precondition(configuration.cooldown >= 0)
        config = configuration
    }

    public mutating func reset() {
        firstImpact = nil
        lastTimestamp = nil
        armed = true
        suppressUntil = -Double.infinity
    }

    public mutating func consume(timestamp t: Double, x: Double, y: Double, z: Double) -> Bool {
        guard t.isFinite, x.isFinite, y.isFinite, z.isFinite else { return false }
        if let last = lastTimestamp, t <= last {
            // Duplicate/reordered samples cannot join two unrelated clock epochs.
            firstImpact = nil
            armed = false
            return false
        }
        lastTimestamp = t
        let g = sqrt(x*x + y*y + z*z)
        guard g.isFinite else { return false }
        if let first = firstImpact, t - first > config.maximumInterval + 1e-9 {
            firstImpact = nil
        }
        if g < config.rearmG { armed = true }
        guard armed, g > config.thresholdG else { return false }
        armed = false
        guard t >= suppressUntil else { return false }
        if let first = firstImpact {
            let interval = t - first
            // A very early rebound is ignored, not promoted to a new first clap.
            if interval + 1e-9 < config.minimumInterval { return false }
            if interval <= config.maximumInterval + 1e-9 {
                firstImpact = nil
                suppressUntil = t + config.cooldown
                return true
            }
        }
        firstImpact = t
        return false
    }
}

/// A deliberately labelled heuristic: gravity rotates toward the watch face,
/// followed by 150ms stable face-up. This is NOT Apple's private wrist-raise event.
public struct WristRaiseDetector {
    private var wasDown = false
    private var stableSince: Double?
    private var lastRaise = -Double.infinity
    public init() {}
    public mutating func reset() { self = Self() }
    public mutating func consume(timestamp: Double, gravityZ: Double, rotation: Double) -> Bool {
        guard timestamp.isFinite, gravityZ.isFinite, rotation.isFinite else { return false }
        if gravityZ > -0.3 { wasDown = true; stableSince = nil }
        guard wasDown, gravityZ < -0.75, rotation < 1.5 else {
            stableSince = nil
            return false
        }
        if stableSince == nil { stableSince = timestamp }
        guard timestamp - stableSince! >= 0.15, timestamp - lastRaise >= 3 else { return false }
        wasDown = false
        stableSince = nil
        lastRaise = timestamp
        return true
    }
}

public enum WatchWire {
    public static let maxAudioBytes = 600_000
    public static let maxMessageBytes = 2 * 1024 * 1024
    public static let maxReplyBytes = 24 * 1024 * 1024
    public static let pcmFrameBytes = 640 // 20ms, 320 mono int16 samples

    public static func validatedEndpoint(_ value: String) -> URL? {
        guard let parts = URLComponents(string: value), let scheme = parts.scheme,
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.fragment == nil,
              parts.query == nil else { return nil }
        guard scheme == "wss" || (scheme == "ws" && isPrivateHost(host)) else { return nil }
        return parts.url
    }

    /// Plain ws:// is allowed only for loopback, home LAN (192.168/16, 10/8,
    /// 172.16/12) and Tailscale CGNAT (100.64/10). Public hosts need wss://.
    static func isPrivateHost(_ host: String) -> Bool {
        if ["localhost", "127.0.0.1", "::1"].contains(host) { return true }
        let n = host.split(separator: ".").compactMap { Int($0) }
        guard n.count == 4, n.allSatisfy({ (0...255).contains($0) }) else { return false }
        return n[0] == 10 || (n[0] == 192 && n[1] == 168) || (n[0] == 172 && (16...31).contains(n[1]))
            || (n[0] == 100 && (64...127).contains(n[1]))
    }

    public static func message(_ object: [String: Any]) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
    }

    public static func hello(deviceID: String, state: [String: Any]) throws -> String {
        try message(["type": "hello", "device_id": deviceID, "device_type": "watch",
                     "sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le", "state": state])
    }

    public static func deviceState(deviceID: String, state: [String: Any]) throws -> String {
        try message(["type": "device_state", "device_id": deviceID, "device_type": "watch", "state": state])
    }

    /// Extract the recorder's PCM without assuming a 44-byte WAV header. AVAudioRecorder
    /// can include JUNK/LIST chunks. Reject a compressed, stereo or wrong-rate container.
    public static func pcm(fromWAV wav: Data) throws -> Data {
        let bytes = [UInt8](wav)
        guard bytes.count > 44, bytes.count <= maxAudioBytes + 65_536,
              String(bytes: bytes[0..<4], encoding: .ascii) == "RIFF",
              String(bytes: bytes[8..<12], encoding: .ascii) == "WAVE" else { throw WireError.invalidAudio }
        func u16(_ i: Int) -> Int { Int(bytes[i]) | Int(bytes[i+1]) << 8 }
        func u32(_ i: Int) -> UInt64 { UInt64(u16(i)) | UInt64(u16(i+2)) << 16 }
        guard u32(4) + 8 == UInt64(bytes.count) else { throw WireError.invalidAudio }
        var offset = 12
        var validFormat = false
        var pcm: Data?
        while offset + 8 <= bytes.count {
            let name = String(bytes: bytes[offset..<offset+4], encoding: .ascii)
            let rawSize = u32(offset+4)
            let start = offset + 8
            guard rawSize <= UInt64(bytes.count - start) else { throw WireError.invalidAudio }
            let size = Int(rawSize) // Bound before narrowing on watchOS arm64_32.
            if name == "fmt " {
                guard size >= 16, u16(start+2) == 1, u32(start+4) == 16000,
                      u32(start+8) == 32000, u16(start+12) == 2, u16(start+14) == 16 else {
                    throw WireError.invalidAudio
                }
                let tag = u16(start)
                let pcmGUID: [UInt8] = [1,0,0,0,0,0,16,0,128,0,0,170,0,56,155,113]
                guard tag == 1 || (tag == 0xfffe && size >= 40 && u16(start+16) >= 22
                    && u16(start+18) == 16 && Array(bytes[start+24..<start+40]) == pcmGUID) else {
                    throw WireError.invalidAudio
                }
                validFormat = true
            } else if name == "data" {
                guard pcm == nil, size > 0, size % 2 == 0, size <= maxAudioBytes else {
                    throw WireError.invalidAudio
                }
                pcm = Data(bytes[start..<start+size])
            }
            offset = start + size + size % 2
        }
        guard validFormat, let pcm, offset == bytes.count else { throw WireError.invalidAudio }
        return pcm
    }

    public static func audio(_ pcm: Data) throws -> String {
        guard !pcm.isEmpty, pcm.count % 2 == 0, pcm.count <= pcmFrameBytes else { throw WireError.invalidAudio }
        return try message(["type": "audio", "format": "pcm_s16le", "sample_rate": 16000,
                            "data": pcm.base64EncodedString()])
    }

    public enum Event {
        case text(String, role: String, generation: Int?)
        case audio(Data, generation: Int, final: Bool)
        case interrupt(Int?), suppressed, selected, error(String), ignored
    }

    public static func event(_ text: String) throws -> Event {
        guard text.utf8.count <= maxMessageBytes,
              let object = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let type = object["type"] as? String else { throw WireError.invalidMessage }
        func generation(required: Bool = false) throws -> Int? {
            guard let value = object["generation"] else {
                if required { throw WireError.invalidMessage }; return nil
            }
            guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue >= 0, number.doubleValue < Double(Int.max),
                  number.doubleValue.rounded() == number.doubleValue else { throw WireError.invalidMessage }
            return number.intValue
        }
        switch type {
        case "audio":
            guard object["format"] as? String == "mp3", let encoded = object["data"] as? String,
                  let data = Data(base64Encoded: encoded),
                  let finalNumber = object["final"] as? NSNumber,
                  CFGetTypeID(finalNumber) == CFBooleanGetTypeID(),
                  !data.isEmpty || finalNumber.boolValue else { throw WireError.invalidMessage }
            return .audio(data, generation: try generation(required: true)!, final: finalNumber.boolValue)
        case "text":
            guard let text = object["text"] as? String, let role = object["role"] as? String,
                  ["user", "assistant"].contains(role) else { throw WireError.invalidMessage }
            return .text(text, role: role, generation: try generation())
        case "interrupt": return .interrupt(try generation())
        case "suppressed": return .suppressed
        case "selected", "resumed": return .selected
        case "error": return .error(object["code"] as? String ?? "unknown")
        default: return .ignored // hello/device_state acknowledgements and future controls.
        }
    }

    public enum WireError: Error { case invalidAudio, invalidMessage }
}

/// Gateway generation replaces the old custom request UUID. Buffer only one bounded
/// MP3 response; never play a partial chunk or accept interrupted/stale generations.
public struct WatchReplyBuffer {
    public private(set) var generation = -1
    public private(set) var suppressed = false
    private var minimumGeneration = 0
    private var completedGeneration = -1
    private var bytes = Data()
    public init() {}
    public mutating func reset() { self = Self() }
    public mutating func interrupt(generation next: Int? = nil) {
        minimumGeneration = max(minimumGeneration, next ?? (generation + 1))
        bytes.removeAll()
    }
    public mutating func setSuppressed(_ value: Bool) {
        if value { interrupt() }
        suppressed = value
    }
    public mutating func observe(_ value: Int?) {
        if let value, value >= minimumGeneration, value > generation {
            generation = value; bytes.removeAll()
        }
    }
    public mutating func append(_ chunk: Data, generation value: Int, final: Bool) throws -> Data? {
        guard !suppressed, value >= minimumGeneration, value >= generation,
              value > completedGeneration else { return nil }
        observe(value)
        guard chunk.count <= WatchWire.maxReplyBytes - bytes.count else {
            bytes.removeAll(); throw WatchWire.WireError.invalidAudio
        }
        bytes.append(chunk)
        guard final else { return nil }
        completedGeneration = value
        let result = bytes
        bytes.removeAll()
        return result.isEmpty ? nil : result
    }
}

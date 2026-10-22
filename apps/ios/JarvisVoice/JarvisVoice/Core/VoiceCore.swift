import Foundation

import CoreFoundation

enum VoiceServerEvent {
    case audio(Data, Int, Bool)
    case text(String, String)
    case interrupt
    case suppressed
    case selected
    case error(String)
    case ignored
    case invalid
}

/// Shared iPhone wire vocabulary. No model/provider credentials live on a device.
enum VoiceWire {
    static func decode(_ object: [String: Any]) -> VoiceServerEvent {
        switch object["type"] as? String {
        case "audio":
            guard object["format"] as? String == "mp3",
                  let encoded = object["data"] as? String,
                  let data = Data(base64Encoded: encoded),
                  let generation = object["generation"] as? NSNumber,
                  CFGetTypeID(generation) != CFBooleanGetTypeID(),
                  generation.doubleValue.rounded() == generation.doubleValue,
                  generation.doubleValue >= 0,
                  generation.doubleValue < Double(Int.max),
                  let final = object["final"] as? Bool else { return .invalid }
            return .audio(data, generation.intValue, final)
        case "text": return .text(object["text"] as? String ?? "", object["role"] as? String ?? "assistant")
        case "interrupt": return .interrupt
        case "suppressed": return .suppressed
        case "selected", "resumed": return .selected
        case "error": return .error(object["code"] as? String ?? "unknown")
        default: return .ignored
        }
    }
    static func audio(_ data: Data) -> [String: Any] {
        ["type": "audio", "format": "pcm_s16le", "sample_rate": 16000,
         "data": data.base64EncodedString()]
    }

    static func hello(deviceID: String, state: [String: Any]) -> [String: Any] {
        ["type": "hello", "device_id": deviceID, "device_type": "iphone",
         "sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le",
         "state": state]
    }
}

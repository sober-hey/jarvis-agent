import Foundation

/// Honest public-API observations, kept independent of UIKit for executable tests.
struct DeviceSnapshot {
    let batteryLevel: Double?
    let charging: Bool
    let headphonesConnected: Bool
    let foreground: Bool
    let faceDown: Bool?
    let audioOutputs: [String]
    let protectedDataAvailable: Bool
    let observedAt: Double

    var wire: [String: Any] {
        ["battery_level": batteryLevel.map { $0 as Any } ?? NSNull(),
         "charging": charging,
         "headphones": headphonesConnected,
         "headphones_connected": headphonesConnected,
         "headphones_worn": NSNull(),
         "audio_outputs": audioOutputs,
         "screen_on": foreground ? true : NSNull(),
         "screen_state_source": foreground ? "app_foreground" : "unavailable_public_api",
         "app_foreground": foreground,
         "face_down": faceDown.map { $0 as Any } ?? NSNull(),
         "orientation_source": "UIDevice.orientation",
         "protected_data_available": protectedDataAvailable,
         "observed_at": observedAt]
    }
}

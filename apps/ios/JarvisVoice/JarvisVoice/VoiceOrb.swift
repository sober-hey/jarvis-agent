import SwiftUI

/// The whole home screen: one abstract, breathing form that reacts to the voice.
///
/// Deliberately not an icon and not a waveform: a soft iridescent body that
/// gathers while listening, tightens while thinking and opens up while speaking,
/// in the spirit of the ChatGPT voice surface. Everything is derived from the
/// live input level and the session state, so it never animates for decoration
/// alone.
enum VoiceOrbState {
    case idle, listening, thinking, speaking

    var tint: (Color, Color, Color) {
        switch self {
        case .idle:
            return (Color(red: 0.30, green: 0.36, blue: 0.48),
                    Color(red: 0.18, green: 0.22, blue: 0.33),
                    Color(red: 0.42, green: 0.50, blue: 0.66))
        case .listening:
            return (Color(red: 0.16, green: 0.72, blue: 0.92),
                    Color(red: 0.20, green: 0.36, blue: 0.86),
                    Color(red: 0.55, green: 0.92, blue: 0.98))
        case .thinking:
            return (Color(red: 0.52, green: 0.36, blue: 0.94),
                    Color(red: 0.28, green: 0.22, blue: 0.72),
                    Color(red: 0.72, green: 0.58, blue: 1.00))
        case .speaking:
            return (Color(red: 0.22, green: 0.84, blue: 0.76),
                    Color(red: 0.16, green: 0.52, blue: 0.74),
                    Color(red: 0.66, green: 0.98, blue: 0.92))
        }
    }

    /// How much the body reacts to the microphone level.
    var reactivity: Double {
        switch self {
        case .idle: return 0.35
        case .listening: return 1.0
        case .thinking: return 0.25
        case .speaking: return 0.6
        }
    }

    /// Base breathing rate in cycles per second.
    var breath: Double {
        switch self {
        case .idle: return 0.18
        case .listening: return 0.34
        case .thinking: return 0.9
        case .speaking: return 0.55
        }
    }
}

struct VoiceOrb: View {
    var state: VoiceOrbState
    var level: Float
    var diameter: CGFloat = 260
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 1.0 / 8 : 1.0 / 30,
                                paused: false)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let audio = Double(min(max(level, 0), 1)).squareRoot()
            let breathe = (sin(t * state.breath * 2 * .pi) + 1) / 2
            let energy = breathe * 0.55 + audio * state.reactivity
            let scale = 0.92 + 0.10 * energy
            let (first, second, third) = state.tint

            ZStack {
                // Outer bloom: the part that reads as "presence" in a dark room.
                Circle()
                    .fill(RadialGradient(colors: [third.opacity(0.55), .clear],
                                         center: .center,
                                         startRadius: diameter * 0.10,
                                         endRadius: diameter * (0.62 + 0.10 * energy)))
                    .blur(radius: diameter * 0.06)
                    .opacity(0.55 + 0.35 * energy)

                // The body itself: a slow, uneven mesh so the surface never looks
                // like a flat disc.
                MeshGradient(width: 3, height: 3, points: meshPoints(t: t, energy: energy), colors: [
                    first, first, second,
                    second, third, second,
                    second, first, first,
                ])
                .frame(width: diameter, height: diameter)
                .mask(Circle())
                .blur(radius: diameter * 0.035)
                .scaleEffect(scale)

                // Specular sheen: a highlight that drifts, so the surface feels wet
                // rather than matte.
                Circle()
                    .fill(RadialGradient(colors: [.white.opacity(0.55), .white.opacity(0.02)],
                                         center: UnitPoint(x: 0.34 + 0.05 * sin(t * 0.7),
                                                           y: 0.28 + 0.05 * cos(t * 0.9)),
                                         startRadius: 0,
                                         endRadius: diameter * 0.45))
                    .blendMode(.plusLighter)
                    .mask(Circle())
                    .scaleEffect(scale * 0.98)
                    .opacity(0.5 + 0.4 * energy)

                // Rim: keeps the silhouette crisp against the background.
                Circle()
                    .strokeBorder(
                        LinearGradient(colors: [.white.opacity(0.35), .clear, .white.opacity(0.18)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing),
                        lineWidth: 1)
                    .scaleEffect(scale)
            }
            .frame(width: diameter * 1.45, height: diameter * 1.45)
        }
        .accessibilityElement()
        .accessibilityLabel("JARVIS 语音")
        .accessibilityValue(label)
    }

    private var label: String {
        switch state {
        case .idle: return "待命"
        case .listening: return "正在聆听"
        case .thinking: return "正在思考"
        case .speaking: return "正在回答"
        }
    }

    private func meshPoints(t: TimeInterval, energy: Double) -> [SIMD2<Float>] {
        // Corner points stay put; the mid-edge and centre points wander, which is
        // what makes the surface look liquid instead of merely scaled.
        let wobble = 0.05 + 0.05 * energy
        func f(_ phase: Double, _ speed: Double) -> Float {
            Float(sin(t * speed + phase) * wobble)
        }
        return [
            SIMD2(0, 0), SIMD2(0.5 + f(0.0, 0.31), 0), SIMD2(1, 0),
            SIMD2(0, 0.5 + f(1.3, 0.27)), SIMD2(0.5 + f(0.7, 0.23), 0.5 + f(2.1, 0.29)), SIMD2(1, 0.5 + f(2.7, 0.33)),
            SIMD2(0, 1), SIMD2(0.5 + f(3.4, 0.25), 1), SIMD2(1, 1),
        ]
    }
}

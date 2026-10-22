import SwiftUI

/// Watch-sized version of the phone orb: same four states, same colour language,
/// but a lighter body so it stays smooth on the Series 8 GPU and does not eat
/// battery while the wrist is raised.
enum WatchOrbState {
    case idle, listening, thinking, speaking

    var tint: (Color, Color, Color) {
        switch self {
        case .idle:
            return (Color(red: 0.32, green: 0.38, blue: 0.50),
                    Color(red: 0.16, green: 0.20, blue: 0.30),
                    Color(red: 0.44, green: 0.52, blue: 0.68))
        case .listening:
            return (Color(red: 0.16, green: 0.72, blue: 0.92),
                    Color(red: 0.18, green: 0.34, blue: 0.84),
                    Color(red: 0.56, green: 0.92, blue: 0.98))
        case .thinking:
            return (Color(red: 0.52, green: 0.36, blue: 0.94),
                    Color(red: 0.26, green: 0.20, blue: 0.70),
                    Color(red: 0.72, green: 0.58, blue: 1.00))
        case .speaking:
            return (Color(red: 0.22, green: 0.84, blue: 0.76),
                    Color(red: 0.14, green: 0.50, blue: 0.72),
                    Color(red: 0.66, green: 0.98, blue: 0.92))
        }
    }

    var reactivity: Double {
        switch self {
        case .idle: return 0.30
        case .listening: return 1.0
        case .thinking: return 0.20
        case .speaking: return 0.55
        }
    }

    var breath: Double {
        switch self {
        case .idle: return 0.20
        case .listening: return 0.36
        case .thinking: return 0.85
        case .speaking: return 0.55
        }
    }
}

struct WatchOrb: View {
    var state: WatchOrbState
    var level: Float
    var diameter: CGFloat = 120
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // 20 fps is enough for a soft body and keeps the wrist display cheap.
        TimelineView(.animation(minimumInterval: reduceMotion ? 1.0 / 6 : 1.0 / 20,
                                paused: false)) { timeline in
            let t = timeline.date.timeIntervalSinceReferenceDate
            let audio = Double(min(max(level, 0), 1)).squareRoot()
            let breathe = (sin(t * state.breath * 2 * .pi) + 1) / 2
            let energy = breathe * 0.55 + audio * state.reactivity
            let scale = 0.90 + 0.12 * energy
            let (first, second, third) = state.tint

            ZStack {
                Circle()
                    .fill(RadialGradient(colors: [third.opacity(0.60), .clear],
                                         center: .center,
                                         startRadius: diameter * 0.08,
                                         endRadius: diameter * (0.60 + 0.12 * energy)))
                    .blur(radius: diameter * 0.05)
                    .opacity(0.55 + 0.35 * energy)

                MeshGradient(width: 3, height: 3,
                             points: meshPoints(t: t, energy: energy),
                             colors: [
                                 first, first, second,
                                 second, third, second,
                                 second, first, first,
                             ])
                .frame(width: diameter, height: diameter)
                .mask(Circle())
                .blur(radius: diameter * 0.03)
                .scaleEffect(scale)

                Circle()
                    .fill(RadialGradient(colors: [.white.opacity(0.50), .white.opacity(0.02)],
                                         center: UnitPoint(x: 0.36 + 0.05 * sin(t * 0.7),
                                                           y: 0.30 + 0.05 * cos(t * 0.9)),
                                         startRadius: 0,
                                         endRadius: diameter * 0.42))
                    .blendMode(.plusLighter)
                    .mask(Circle())
                    .scaleEffect(scale * 0.98)
                    .opacity(0.45 + 0.4 * energy)

                Circle()
                    .strokeBorder(
                        LinearGradient(colors: [.white.opacity(0.32), .clear, .white.opacity(0.16)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing),
                        lineWidth: 0.8)
                    .scaleEffect(scale)
            }
            .frame(width: diameter * 1.4, height: diameter * 1.4)
        }
        .accessibilityElement()
        .accessibilityLabel("JARVIS")
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

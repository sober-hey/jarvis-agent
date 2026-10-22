import SwiftUI

@main
struct JarvisVoiceApp: App {
    @StateObject private var voice = VoiceSession.shared
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(voice)
                .task {
                    guard NSClassFromString("XCTestCase") == nil else { return }
                    GatewayDiagnostics.record(.launch)
                    voice.bootstrap()
                }
        }
        .onChange(of: phase) { _, next in
            voice.foregroundChanged(next == .active)
        }
    }
}

/// One thing on screen: the orb. Everything configurable lives behind the gear,
/// so the home surface stays quiet enough to talk to.
private struct ContentView: View {
    @EnvironmentObject var voice: VoiceSession
    @State private var settings = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var orbState: VoiceOrbState {
        if voice.speaking { return .speaking }
        if voice.thinking { return .thinking }
        if voice.active { return .listening }
        return .idle
    }

    var body: some View {
        ZStack {
            // A near-black field so the orb is the only light in the room.
            LinearGradient(colors: [Color(white: 0.055), Color(white: 0.02)],
                           startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                HStack {
                    Spacer()
                    Button { settings = true } label: {
                        Image(systemName: "gearshape")
                            .font(.system(size: 17, weight: .regular))
                            .foregroundStyle(.white.opacity(0.35))
                            .frame(width: 44, height: 44)
                    }
                    .accessibilityLabel("设置")
                }
                .padding(.horizontal, 8)

                Spacer(minLength: 0)

                Button {
                    if voice.active { voice.stop() } else { Task { await voice.start() } }
                } label: {
                    VoiceOrb(state: orbState, level: voice.level)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(voice.active ? "结束语音" : "开始语音")

                Spacer(minLength: 0)

                Text(caption)
                    .font(.system(size: 15, weight: .light)).tracking(0.5)
                    .foregroundStyle(.white.opacity(0.72))
                    .frame(minHeight: 44)
                    .padding(.bottom, 28)
                    .animation(reduceMotion ? nil : .easeInOut(duration: 0.35), value: caption)
            }
        }
        .preferredColorScheme(.dark)
        .sheet(isPresented: $settings) { SettingsView() }
    }

    private var caption: String {
        if let error = voice.error, !voice.active { return error }
        if voice.speaking { return "正在说" }
        if voice.thinking { return "正在想" }
        if voice.active { return voice.connected ? "说吧，我在听" : "正在连接" }
        return "轻点开始"
    }
}

private struct SettingsView: View {
    @EnvironmentObject var voice: VoiceSession
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var address = GatewayEndpoints.custom ?? ""
    @State private var token = ""
    @State private var gatewayMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("连接") {
                    LabeledContent("状态", value: voice.connected ? "已连接" : "未连接")
                    LabeledContent("当前地址") { Text(voice.endpoint).font(.caption.monospaced()) }
                    TextField("ws://100.64.0.1:8765", text: $address)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .font(.callout.monospaced())
                    SecureField(voice.credentialReady ? "令牌已保存（留空不改）" : "网关令牌", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("保存并重连") {
                        gatewayMessage = voice.updateGateway(address: address, token: token) ?? "已保存，正在重连"
                        token = ""
                    }
                    if let gatewayMessage { Text(gatewayMessage).font(.caption) }
                    Text("地址留空则依次尝试局域网 IP、Tailscale IP。")
                        .font(.caption)
                }
                Section("人声检测") {
                    Slider(value: $voice.threshold, in: 0.005...0.08, step: 0.001)
                    Text("阈值越低越灵敏，环境噪声也更容易打断。改完重新开始语音生效。")
                        .font(.caption)
                }
                Section("说明") {
                    Text("点球体开始或结束对话。离开 App 会暂停录音；iOS 不允许后台常听。")
                        .font(.caption)
                }
                Section("打字") {
                    HStack {
                        TextField("输入一句话", text: $text)
                            .onSubmit(send)
                        Button("发送", action: send)
                            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
                Section("设备") {
                    Text(voice.deviceID).font(.caption.monospaced()).textSelection(.enabled)
                    Text("耳机状态表示音频路由已连接，不代表耳机确实戴在耳朵上。")
                        .font(.caption)
                }
                if !voice.assistantText.isEmpty {
                    Section("最近一次回答") {
                        Text(voice.assistantText).font(.callout).textSelection(.enabled)
                    }
                }
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") {
                        voice.saveSettings()
                        if voice.active { voice.stop() }
                        dismiss()
                    }
                }
            }
        }
    }

    private func send() {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        text = ""
        dismiss()
        Task { await voice.start(text: value) }
    }
}

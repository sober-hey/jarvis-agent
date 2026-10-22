import AppIntents

/// Foreground entry is deliberate: public iOS APIs require first-use microphone
/// consent and don't let an arbitrary background intent bypass device unlock.
struct StartJarvisIntent: AppIntent {
    static var title: LocalizedStringResource = "开始 JARVIS 语音"
    static var description = IntentDescription("进入 JARVIS 语音模式；可同时把文字交给 JARVIS。")
    static var openAppWhenRun: Bool { true }
    static var authenticationPolicy: IntentAuthenticationPolicy { .requiresLocalDeviceAuthentication }

    @Parameter(title: "文字", description: "留空直接进入语音模式。")
    var text: String?

    static var parameterSummary: some ParameterSummary {
        Summary("开始 JARVIS 语音，附带 \(\.$text)")
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        await VoiceSession.shared.start(text: text)
        return .result()
    }
}

struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartJarvisIntent(), phrases: [
            "Start \(.applicationName)",
            "Talk to \(.applicationName)",
            "开始 \(.applicationName) 语音",
            "和 \(.applicationName) 说话"
        ], shortTitle: "开始 JARVIS 语音", systemImageName: "waveform")
    }
}

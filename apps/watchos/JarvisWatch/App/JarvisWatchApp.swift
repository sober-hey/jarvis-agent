import Combine
import SwiftUI
import WatchKit

@MainActor
final class WatchModel: ObservableObject {
    let motion = MotionMonitor()
    let audio = WatchAudio()
    let connection = WatchConnection()
    let runtime = WorkoutRuntime()
    @Published var armed = false
    @Published var autoRaise = false { didSet { motion.detectRaises = autoRaise } }
    @Published var message = "请先设置网关，再开启监听"
    @Published private(set) var lastRaise: Date?
    /// 已上传、还没拿到回答：球体在这个窗口里收紧成「思考」态。
    @Published private(set) var awaitingReply = false
    private var raiseSource: String?
    private var phase: ScenePhase = .inactive
    private var seenActive = false
    private var permissionGranted = false
    private var triggering = false
    private var lastTrigger = Date.distantPast
    private var lastClap: Date?
    private var playbackGeneration: Int?
    private var timer: Timer?
    private var subscriptions = Set<AnyCancellable>()

    init() {
        _ = WatchTokenImport.importIfPresent()
        WKInterfaceDevice.current().isBatteryMonitoringEnabled = true
        motion.onImpact = { [weak self] in self?.trigger("double_impact") }
        motion.onRaise = { [weak self] in
            self?.lastRaise = Date(); self?.raiseSource = "motion_heuristic"
            self?.trigger("wrist_raise_motion_heuristic")
        }
        let error: (String) -> Void = { [weak self] text in self?.message = text }
        motion.onError = error; runtime.onError = error; connection.onError = error
        audio.onInterrupted = { [weak self] in
            self?.triggering = false
            self?.message = "录音被系统打断；没有发送"
            self?.syncMotion()
        }
        audio.onSilence = { [weak self] in
            self?.triggering = false
            self?.message = "未检测到声音，录音已取消"
            self?.syncMotion()
        }
        runtime.onStateChange = { [weak self] in self?.syncMotion(); self?.reportStatus() }
        connection.onReady = { [weak self] in self?.reportStatus() }
        connection.deviceState = { [weak self] in self?.deviceSnapshot() ?? [:] }
        connection.onStopPlayback = { [weak self] in
            self?.playbackGeneration = nil
            self?.audio.stopPlayback()
        }
        connection.onSuppressed = { [weak self] in
            self?.audio.cancel()
            self?.triggering = false
            self?.message = "网关已选择其他设备，手表暂停转发"
        }
        connection.onText = { [weak self] text, role in
            if role == "assistant" { self?.message = text }
        }
        audio.onPlaybackFinished = { [weak self] in
            guard let self, let generation = self.playbackGeneration else { return }
            self.playbackGeneration = nil
            Task { await self.connection.playbackDone(generation: generation) }
        }
        connection.onReply = { [weak self] data, generation in
            guard let self else { return }
            // Do not unexpectedly speak after the user has disarmed the watch.
            guard self.armed else { return }
            self.awaitingReply = false
            do { try self.audio.play(data); self.playbackGeneration = generation; self.message = "播放回复" }
            catch { self.message = "手表音频播放失败，请检查静音和输出路由" }
        }
        audio.$recording.dropFirst().sink { [weak self] _ in
            Task { @MainActor in self?.syncMotion() }
        }.store(in: &subscriptions)
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let clap = self.lastClap, Date().timeIntervalSince(clap) >= 5 else { return }
                self.lastClap = nil
                self.reportStatus()
            }
        }
    }

    func setArmed(_ enabled: Bool) async {
        guard enabled else {
            armed = false
            triggering = false
            motion.stop(); audio.cancel(); audio.stopPlayback()
            runtime.stop(); connection.disconnect()
            message = "监听已停止，麦克风已关闭"
            return
        }
        permissionGranted = await audio.requestPermission()
        guard permissionGranted else { message = "请在设置中允许麦克风权限"; return }
        armed = true
        message = connection.ready ? "等待双击掌或抬腕" : "已监听；发送语音前请连接网关"
        syncMotion()
    }

    func sceneChanged(_ newPhase: ScenePhase) {
        let previous = phase
        phase = newPhase
        if newPhase == .active {
            // WKInterfaceDevice has no public physical wrist-raise notification.
            // Returning active is a proxy and can also be caused by a tap or Crown.
            if seenActive, previous != .active, armed, autoRaise {
                lastRaise = Date(); raiseSource = "scene_active_proxy"
                trigger("wrist_raise_scene_proxy")
            }
            seenActive = true
            reportStatus()
        }
        if newPhase != .active, !runtime.running, !audio.recording {
            connection.disconnect()
        }
        syncMotion()
    }

    func syncMotion() {
        if armed && (phase == .active || runtime.running || audio.recording) { motion.start() }
        else { motion.stop() }
    }

    func trigger(_ reason: String) {
        guard armed, !triggering, permissionGranted, !audio.recording,
              Date().timeIntervalSince(lastTrigger) > 2 else { return }
        if reason == "double_impact" {
            lastClap = Date()
            reportStatus() // Even a suppressed Watch must advertise a new physical trigger.
        }
        guard connection.ready, !connection.awaitingReply else {
            message = connection.awaitingReply ? "等待上一轮回复；不重发" : "请先连接网关"
            return
        }
        guard let capture = connection.beginUtterance() else {
            message = "当前设备被抑制或网关未就绪"
            return
        }
        triggering = true
        lastTrigger = Date()
        reportStatus()
        WKInterfaceDevice.current().play(.start)
        do {
            try audio.record { [weak self] result in
                guard let self, self.armed else { return }
                self.triggering = false
                switch result {
                case .failure: self.message = "录音失败；没有发送"
                case .success(let data):
                    self.message = "发送中"
                    Task { @MainActor in
                        do {
                            try await self.connection.sendClip(data, capture: capture)
                            self.message = "等待回复"
                        } catch { self.message = "发送失败或结果未知；未自动重发" }
                        self.syncMotion()
                    }
                }
            }
            message = "正在录音，停顿后发送（最长 12 秒）"
        } catch { triggering = false; message = "无法启动麦克风" }
    }

    func stopRecording() {
        audio.stopRecording()
        triggering = false
        awaitingReply = true
    }
    func cancelRecording() {
        audio.cancel(); triggering = false; awaitingReply = false
        message = "录音已取消，没有发送"; syncMotion()
    }
    func deviceSnapshot() -> [String: Any] {
        let device = WKInterfaceDevice.current()
        let now = Date()
        let freshHR = runtime.lastHeartRateAt.map { now.timeIntervalSince($0) < 30 } ?? false
        return [
            "device_type": "watch", "observed_at": now.timeIntervalSince1970,
            "clap_triggered": lastClap.map { now.timeIntervalSince($0) < 5 } ?? false,
            "last_clap_at": lastClap?.timeIntervalSince1970 as Any? ?? NSNull(),
            "wearing": NSNull(),
            "app_foreground": phase == .active,
            "screen_on": NSNull(), "screen_state_source": "unavailable_public_api",
            "os_version": device.systemVersion,
            "battery_level": device.batteryLevel >= 0 ? device.batteryLevel as Any : NSNull(),
            "charging": device.batteryState == .unknown ? NSNull() :
                (device.batteryState == .charging || device.batteryState == .full) as Any,
            "wrist_detection_enabled": NSNull(), // No such public WKInterfaceDevice API.
            "on_wrist": NSNull(), // Not a real-time public measurement.
            "wear_evidence": freshHR ? "recent_heart_rate_suggests_worn" : runtime.wearEvidence,
            "wear_verified_at": runtime.authenticatedAt?.ISO8601Format() as Any? ?? NSNull(),
            "heart_rate_sample_at": runtime.lastHeartRateAt?.ISO8601Format() as Any? ?? NSNull(),
            "last_wrist_raise_at": lastRaise?.ISO8601Format() as Any? ?? NSNull(),
            "wrist_raise_source": raiseSource as Any? ?? NSNull(),
            "armed": armed, "motion_active": motion.running, "workout_active": runtime.running,
            "recording": audio.recording, "playing": audio.playing
        ]
    }
    func reportStatus() {
        Task { await connection.sendStatus() }
    }
}

@main
struct JarvisWatchApp: App {
    @StateObject private var model = WatchModel()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            WatchHome(model: model, audio: model.audio, motion: model.motion,
                      connection: model.connection, runtime: model.runtime)
                .onChange(of: phase, initial: true) { _, value in model.sceneChanged(value) }
        }
    }
}

struct WatchHome: View {
    @ObservedObject var model: WatchModel
    @ObservedObject var audio: WatchAudio
    @ObservedObject var motion: MotionMonitor
    @ObservedObject var connection: WatchConnection
    @ObservedObject var runtime: WorkoutRuntime
    @State private var showSettings = false

    private var orbState: WatchOrbState {
        if audio.playing { return .speaking }
        if model.awaitingReply || connection.awaitingReply { return .thinking }
        if audio.recording { return .listening }
        return .idle
    }

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(white: 0.06), .black],
                           startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()

            VStack(spacing: 2) {
                HStack {
                    Spacer()
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape")
                            .font(.system(size: 13))
                            .foregroundStyle(.white.opacity(0.35))
                            .frame(width: 34, height: 34)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("设置")
                }

                Spacer(minLength: 0)

                Button {
                    if audio.recording { model.stopRecording() }
                    else { model.trigger("button") }
                } label: {
                    WatchOrb(state: orbState, level: audio.level)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(audio.recording ? "结束并发送" : "点按说话")

                Spacer(minLength: 0)

                Text(caption)
                    .font(.system(size: 12, weight: .light))
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                    .padding(.bottom, 4)
            }
        }
        .sheet(isPresented: $showSettings) {
            WatchSettings(model: model, audio: audio, motion: motion,
                          connection: connection, runtime: runtime)
        }
    }

    private var caption: String {
        if audio.playing { return "正在说" }
        if model.awaitingReply || connection.awaitingReply { return "正在想" }
        if audio.recording { return "说吧，我在听" }
        if !connection.ready { return "未连接网关" }
        return model.armed ? "轻点说话" : "先在设置里开启监听"
    }
}

/// Everything that is not the orb lives here: connection, credentials, triggers,
/// sensors and diagnostics. The home surface stays one object and one line.
private struct WatchSettings: View {
    @ObservedObject var model: WatchModel
    @ObservedObject var audio: WatchAudio
    @ObservedObject var motion: MotionMonitor
    @ObservedObject var connection: WatchConnection
    @ObservedObject var runtime: WorkoutRuntime
    @AppStorage("gateway_endpoint") private var endpoint = "ws://100.67.229.1:8765"
    @State private var token = ""
    @State private var workoutPrompt = false
    @State private var operationPending = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("网关") {
                    Text(connection.state).font(.caption2)
                    NavigationLink("连接与令牌") { credentials }
                }
                Section("监听") {
                    Button(model.armed ? "停止监听" : "开始监听") {
                        operationPending = true
                        Task {
                            await model.setArmed(!model.armed)
                            operationPending = false
                        }
                    }.disabled(operationPending)
                    if audio.recording {
                        Button("结束并发送") { model.stopRecording() }
                        Button("取消录音") { model.cancelRecording() }
                    } else {
                        Button("点按说话") { model.trigger("button") }
                            .disabled(!model.armed || !connection.ready)
                    }
                    if audio.playing { Button("停止播放") { audio.stopPlayback() } }
                    Toggle("抬腕后录音（近似）", isOn: $model.autoRaise).font(.caption2)
                    Text("抬腕仅为运动/亮屏近似，可能误触；录音有震动与系统麦克风指示。")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Section("传感器") {
                    Text("\(motion.running ? "运行" : "暂停") · 峰值 \(motion.peakG, specifier: "%.2f")g")
                        .font(.caption2)
                    Text("输出 \(audio.route)").font(.caption2)
                    Button("验证此刻佩戴") { Task { await runtime.verifyWear(); model.reportStatus() } }
                    Text("佩戴：\(runtime.wearEvidence)").font(.caption2)
                    Button("录制 20 秒运动轨迹") {
                        do {
                            try motion.captureDiagnostic()
                            model.message = "轨迹仅保存在手表本机，20 秒后自动停止"
                        } catch {
                            model.message = "无法保存运动轨迹"
                        }
                    }.disabled(!motion.running)
                    if let url = motion.diagnosticURL { Text(url.lastPathComponent).font(.caption2) }
                }
                Section("体能训练") {
                    Button(runtime.running ? "结束体能训练监听" : "体能训练期间后台监听") {
                        if runtime.running { runtime.stop() } else { workoutPrompt = true }
                    }.disabled(!model.armed || operationPending)
                    Text("仅在真实体能训练时使用，会增加耗电，不保证全天后台可用。")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Section("状态") {
                    Text(model.message).font(.caption2)
                }
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .confirmationDialog("仅在真实体能训练时使用。此模式会显示系统训练指示并增加耗电，不保证全天后台可用。",
                            isPresented: $workoutPrompt) {
            Button("开始体能训练监听") {
                operationPending = true
                Task {
                    do { try await runtime.start(); model.syncMotion() }
                    catch { model.message = "无法开始体能训练，请检查健康权限" }
                    operationPending = false
                }
            }
        }
    }

    private var credentials: some View {
        Form {
            TextField("ws://100.67.229.1:8765", text: $endpoint)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            SecureField("网关令牌（仅本机保存）", text: $token)
            Button("保存令牌") {
                do { try WatchCredential.save(token); token = ""; model.message = "令牌已保存到钥匙串" }
                catch { model.message = "令牌保存失败" }
            }
            Text("保存空值会删除令牌。URL 不得含密钥。").font(.caption2)
            Button("连接") { connection.connect(endpoint: endpoint) }
            Button("断开连接") { connection.disconnect() }
            Text(connection.state).font(.caption2)
        }.navigationTitle("网关")
    }
}

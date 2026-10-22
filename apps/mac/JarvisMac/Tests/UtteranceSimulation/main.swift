import Foundation

// 用合成帧模拟各种说话方式，检查 UtteranceGate 的切分是否符合预期。
// 帧长 20ms，与 Mac 端 16kHz/320 采样一致。

let dt = 0.02
var failures = 0
var checks = 0

func check(_ ok: Bool, _ name: String) {
    checks += 1
    if !ok { failures += 1; print("FAIL: \(name)") }
}

struct Scenario {
    let name: String
    /// [(秒数, rms)] 顺序播放
    let frames: [(Double, Float)]
    let expected: [String]
}

func speech(_ seconds: Double, _ rms: Float) -> (Double, Float) { (seconds, rms) }
func silence(_ seconds: Double, _ floor: Float = 0.004) -> (Double, Float) { (seconds, floor) }

func run(_ scenario: Scenario) -> [String] {
    var gate = UtteranceGate()
    var events: [String] = []
    var cursor = 0.0
    for (seconds, rms) in scenario.frames {
        var t = 0.0
        while t < seconds - 1e-9 {
            switch gate.process(.init(rms: rms, seconds: dt)) {
            case .started: events.append("start")
            case .ended: events.append("end")
            case .discarded: events.append("discard")
            case .idle, .recording: break
            }
            t += dt; cursor += dt
        }
    }
    // 收尾：给 3 秒静音让最后的句子收口
    var tail = 0.0
    while tail < 3.0 {
        switch gate.process(.init(rms: 0.004, seconds: dt)) {
        case .started: events.append("start")
        case .ended: events.append("end")
        case .discarded: events.append("discard")
        case .idle, .recording: break
        }
        tail += dt
    }
    return events
}

let scenarios: [Scenario] = [
    // 正常一句话
    Scenario(name: "正常 2 秒一句话", frames: [silence(1), speech(2, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 短回答「嗯」
    Scenario(name: "短回答 0.25 秒", frames: [silence(1), speech(0.25, 0.05), silence(3)],
             expected: ["start", "end"]),
    // 思考停顿 1.2 秒（不应切断）
    Scenario(name: "句中思考停顿 1.2 秒", frames: [silence(1), speech(1.5, 0.05), silence(1.2), speech(1.5, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 长停顿 2 秒（应当切成两句）
    Scenario(name: "长停顿 2.0 秒", frames: [silence(1), speech(1.5, 0.05), silence(2.0), speech(1.5, 0.05), silence(2)],
             expected: ["start", "end", "start", "end"]),
    // 小声说话
    Scenario(name: "小声说话", frames: [silence(1), speech(2, 0.010), silence(2)],
             expected: ["start", "end"]),
    // 嘈杂房间
    Scenario(name: "嘈杂房间底噪 0.02", frames: [silence(1, 0.02), speech(2, 0.09), silence(2, 0.02)],
             expected: ["start", "end"]),
    // 只有噪声
    Scenario(name: "只有底噪", frames: [silence(4, 0.02)],
             expected: []),
    // 拖音「嗯——」不足最小语音
    Scenario(name: "语气词拖音", frames: [silence(1), speech(0.12, 0.05), silence(3)],
             expected: ["start", "discard"]),
    // 长独白 30 秒
    Scenario(name: "长独白 30 秒", frames: [silence(1), speech(30, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 说完立刻补充一句
    Scenario(name: "说完立刻补充", frames: [silence(1), speech(1.2, 0.05), silence(0.5), speech(1.2, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 键盘敲击（极短高频脉冲）
    Scenario(name: "键盘敲击噪声", frames: [silence(1), speech(0.04, 0.06), silence(0.3), speech(0.04, 0.06), silence(3)],
             expected: []),
    // 豆包 end_window_size 默认 800ms：0.8 秒内停顿不该切断
    Scenario(name: "停顿 0.8 秒（豆包默认阈值内）", frames: [silence(1), speech(2, 0.05), silence(0.8), speech(1.5, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 超过 1.6 秒才算真的说完（正常句 0.9+0.5 观察窗）
    Scenario(name: "停顿 1.6 秒（应切两句）", frames: [silence(1), speech(2, 0.05), silence(1.6), speech(1.5, 0.05), silence(2)],
             expected: ["start", "end", "start", "end"]),
    // 长独白中的思考停顿
    Scenario(name: "长独白中途停顿 1 秒", frames: [silence(1), speech(6, 0.05), silence(1.0), speech(4, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 极短回答后立刻补充
    Scenario(name: "短回答后立刻补充", frames: [silence(1), speech(0.3, 0.05), silence(0.4), speech(1.2, 0.05), silence(2)],
             expected: ["start", "end"]),
    // 环境噪声突然变大（说话时有人开抽油烟机）
    Scenario(name: "说话中途噪声抬升", frames: [silence(1), speech(2, 0.05), silence(1.5, 0.03), speech(1.5, 0.05), silence(2)],
             expected: ["start", "end"]),
]

for scenario in scenarios {
    let events = run(scenario)
    let ok = events == scenario.expected
    check(ok, "\(scenario.name)：期望 \(scenario.expected)，实际 \(events)")
}

print("\n\(checks - failures)/\(checks) 通过")
exit(failures == 0 ? 0 : 1)

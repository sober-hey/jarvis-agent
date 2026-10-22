# AccessibilityHelper

Native Swift `@MainActor`, no dependencies beyond system frameworks. Reads window titles and role/subrole metadata only; no AXValue, recursive tree scraping, clipboard, credentials, network, automatic prompts or TCC edits.

```swift
let accessibility = AccessibilityHelper()
// Explicit user operation while the intended app is frontmost:
let snapshot = try accessibility.captureFrontmostWindow()
// Display applicationName/title for confirmation without changing focus:
try accessibility.typeText("hello", into: snapshot.target)
// Caller supplies an explicitly selected AX element:
// try accessibility.press(button, in: snapshot.target)
```

Permission buttons may explicitly call `requestPermissionFromUserAction()` and `openPermissionSettingsFromUserAction()`. A prompt's return is current trust, not proof of subsequent approval. Recheck after returning to the app.

## Safety/compatibility

- Focused/main window from frontmost process; process PID + launch identity, captured window and focused element are rechecked for each chunk.
- Unicode CGEvent down/up pairs use `postToPid`, never global `post`. Max 4096 UTF-16 units, max 20 per chunk, surrogate pairs intact. No implicit activation/focus stealing. Return means **queued**, not verified insertion; failure may leave a prefix.
- Secure Event Input and AXSecureTextField are refused. Ordinary AXTextField/AXComboBox accept standard/search/empty subrole; AXTextArea also accepts absent optional subrole (TextEdit-style). Unknown metadata otherwise fails closed. Custom controls can misreport security metadata: no AX/CG API makes target/focus/delivery atomic. Never use for secrets.
- AXPress checks PID and captured AXWindow; unsupported action gives native AX error. Caller obtains explicit element, helper does not discover all controls.
- `AXUIElementPostKeyboardEvent` is deprecated since macOS 10.9. Unicode CGEvent is the modern replacement, not an invented `postKeyboardAction` API.

## 中文排障

- 未授权：系统设置 → 隐私与安全性 → 辅助功能，手动启用真正的宿主 App（或运行探针的终端）。更换签名/路径可能要重新授权并重启。本模块不代批权限。
- 焦点变化：旧 target 已失效；重新选中窗口与文本框，再捕获并确认。不要盲目重试。
- 密码框/安全输入：拒绝是预期行为，不绕过。若其它应用遗留安全输入模式，正常退出该应用后再试。
- 没标题/窗口：系统保护窗口、菜单栏及部分自绘 App 没有 AX 元数据，nil 标题不代表读屏正文失败。
- 事件投递拒绝：检查辅助功能权限及宿主签名后重启。系统设置深链不可用时手动导航。

## 验证

运行模块根目录的 `python3 run_checks.py`（必要时用 README 中绝对 Python 路径）。优先完整 Xcode，固定 ASCII TMPDIR；驱动仍不可用时才 fallback frontend+clang。独立 `AudioAccessibility.xcodeproj` 已通过 xcodebuild test；其中真实调用了显式系统权限申请 API，返回当前未授权状态，不能据此声称弹窗显示或用户已批准。Swift 6 + warnings-as-errors。

测试是注入 AX 状态、记录输入事件的离线分支测试，实际 trust 探针为只读。当前 `LIVE_AX_TRUSTED=false`。真实 Terminal/Safari 标题、TextEdit hello、成功 AXPress 均为 **待 Sir 手动授权后的真机验证**，不能用离线成功冒充。

授权后手动验收：打开专用空白 TextEdit 文档，保持其普通编辑区在前台，捕获 target，调用 typeText("hello")，肉眼确认；Terminal/Safari 只查标题，不向 Terminal 发送键盘事件。AXPress 仅对测试文档/测试窗口内明确选择的非破坏性控件执行。

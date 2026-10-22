# JARVIS 抽象智能视觉形象与轻量动态交互系统

独立开发实施方案 v1.0 · 2026-10-18 · 方案作者：Jarvis · 项目所有者：sober

## 1. 执行摘要与证据边界

开发一个本地优先、可独立演示、可嵌入其他前端的 JARVIS 抽象视觉组件。以三条集中、克制、有固定轮廓特征的矢量光带形成身份，通过节奏、相位、开合与局部亮度表达交互状态。目标环境为 Apple Silicon Mac M4、16GB 统一内存。

首版采用 **TypeScript + SVG + 原生 CSS + Vite**，核心运行时不依赖 React、三维引擎或动画库。Vitest 验证逻辑，Playwright 验证浏览器行为。未来系统只需通过通用事件接口传入状态和归一化强度，不需要改动核心渲染器。

这是一份待实施的工程规范。已核对技术资料与原对话；尚未构建项目、在 M4 上跑分或获得 sober 对最终视觉稿的认可。本文中的性能数值是验收预算，不是实测结果；参考路径是可执行起点，不代表正式品牌标识已经定稿。

完成首版的定义：另一位 AI 在新目录中按本文建立项目，断开外部业务服务后仍能演示全部状态、打断、任务进度、错误恢复、降级及销毁重建；提供代码、测试、浏览器证据和复现命令。只有方案、静态图片或测试数量均不能替代这一结果。

## 2. 项目范围

### 必须实现

- 独立视觉核心、静态标识、状态机、统一动画时钟、事件校验器、内存事件适配器。
- 全部状态的确定性模拟；滑杆、按钮、键盘操作、事件回放、异常注入。
- 64、128、256、512 CSS px 方形容器，以及 1440×900 与 390×844 演示布局。
- 减少动态、暂停、隐藏页面停绘、资源清理、性能分级和静态回退。
- 通用 TypeScript API、协议说明、测试、可复现构建、离线可运行的本地部署说明。

### 明确排除

不开发真人、脸、嘴、身体、口型、3D 模型、粒子宇宙、语音识别、语音合成、模型推理、全双工语音、长期记忆、真实任务执行、Hermes 插件或 JARVIS 底座集成。不得读取或修改现有相关工程。倾听和回答仅是视觉状态名称，演示数据完全模拟。

首版不做 Electron、Tauri、桌面置顶、透明穿透窗口、网络服务、WebSocket、遥测、CDN 字体或自动更新。后续接入属于新任务。

## 3. 技术资料核对与选型

核对日期：2026-10-18。以下官方项目文档、标准及 Web 平台参考支持路线选择；它们不提供本项目的实际资源占用证明。

| 资料 | 已核对内容 | 项目决定 |
|---|---|---|
| [W3C SVG 2 Paths](https://www.w3.org/TR/SVG2/paths.html) | 路径、贝塞尔曲线与描边的定义 | 用少量固定路径组成可缩放标识 |
| [WHATWG Animation frames](https://html.spec.whatwg.org/multipage/imagebitmap-and-animations.html#animation-frames) | 浏览器动画帧调度接口 | 单一 requestAnimationFrame 调度器 |
| [Vite 入门](https://vite.dev/guide/) | vanilla-ts 模板、构建、Node 兼容要求 | 生成纯 TypeScript 项目，锁定依赖 |
| [Node.js Releases](https://nodejs.org/en/about/previous-releases) | Node 版本生命周期 | 开工选 Node 24 LTS 可用补丁版本，记录精确版本 |
| [Vitest 入门](https://vitest.dev/guide/) | 测试环境与 Vite 配合 | 纯逻辑在 Node 环境测试 |
| [Playwright Browsers](https://playwright.dev/docs/browsers) | Chromium、Firefox、WebKit 测试支持 | 首版自动测 Chromium/WebKit，另测本机 Safari |
| [MDN requestAnimationFrame](https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame) | 帧时间戳与后台行为 | 时间驱动，避免按帧累加速度 |
| [MDN Page Visibility](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API) | 页面可见性事件 | 隐藏主动取消动画调度 |
| [MDN reduced motion](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion) | 系统减少动态偏好 | 静态状态姿态，取消循环运动 |

Vite 当前入门页列出 Node 20.19+、22.12+ 的兼容门槛；不据此推荐已结束支持的旧版本。执行者安装前复核 Vite、Vitest 的 engines 与选定 Node 是否兼容。若环境不兼容，记录问题并选受支持组合，不静默改系统 Node。

### 为什么这样选

三条光带不需要大量精灵或空间光照。SVG 路径便于检查几何、导出静态资产和保持小尺寸辨识。固定几何配合 transform、opacity、stroke-dashoffset 能覆盖首版状态，减少持续路径重算。SVG 仍可能产生绘制开销，必须实测，不能声称全部动画都由 GPU 免费执行。

Canvas 适合大量动态图元，但当前没有这项需求；WebGL、Three.js、PixiJS 和完整三维管线增加依赖与调试范围。React 仅可作为未来包装层，不进入首版核心。当前也不引入 XState、Zustand、GSAP、Motion、Tailwind：纯函数 reducer、单一调度器与少量 CSS 足够表达本任务。

## 4. 视觉身份规范

设计判断：这是 sober 日常使用的抽象智能标识与测试台，采用深色、矢量、低密度、克制动态的视觉语言。设计参数为 DESIGN_VARIANCE=5、MOTION_INTENSITY=3、VISUAL_DENSITY=2。它不是营销页，不套用卡片阵列、英雄标题、滚动叙事或照片模板。

### 4.1 固定身份：三带折返

三条开放光带构成横向、略向右上抬起的形态。最长主带在右端折回；上带短、下带稍长，三者错位而非镜像。左侧入口与右侧折返处形成两处可记忆的缺口。轮廓中心保留负空间，没有发光核心。不能画成字母 J、文字徽标、两只眼睛、嘴或面具。

固定特征：三条带、长度次序、右侧折返、开放缺口、非对称轮廓、中心留空。所有状态保留这些特征。动态不能把它变成另一种形象；最强状态也不能散成满屏光线。

建议起点：viewBox="0 0 512 512"，三条路径如下。实现者先渲染静态联系图检查辨识，不得因为它们可运行便宣称视觉通过。

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"
     fill="none" stroke="#79B9B4" stroke-linecap="round"
     stroke-linejoin="round" aria-hidden="true">
  <path stroke-width="5" opacity=".72"
        d="M144 211 C193 173 273 168 333 190 C353 197 365 210 358 224 C352 235 333 239 312 237"/>
  <path stroke-width="7"
        d="M112 254 C172 214 277 207 351 235 C384 248 395 266 380 283 C366 300 329 303 294 295"/>
  <path stroke-width="5" opacity=".64"
        d="M145 298 C193 273 261 267 310 280 C336 287 348 300 336 314 C325 328 287 336 252 331"/>
</svg>
```

禁止额外插入 circle、径向渐变、球形网格或圆形发光底盘。曲线可以弯曲，但整体不能封闭成球、圆环、无限符号或常见语音波形。若联系图出现脸部联想，调整带间距和端点，不添加解释文字掩盖问题。

### 4.2 颜色、尺寸与光感

| Token | 初值 | 使用规则 |
|---|---|---|
| surface | #0B1014 | 全页深色背景，不分段反色 |
| surface-control | #151D23 | 控件背景 |
| text-primary | #E4ECEE | 控件和状态文字，主形象内无文字 |
| text-secondary | #A8B7BD | 辅助说明，验收时检查对比度 |
| accent | #79B9B4 | 唯一主色，相近亮度变化，不用彩虹渐变 |
| border | #34464F | 控件边界 |
| control-radius | 8px | 控件统一圆角 |

默认无模糊滤镜。可选高质量层最多复制三条路径一次，描边宽度为原带的 1.5 倍、透明度不超过 0.08，不用 Gaussian blur。所有层合计最多 9 个可见 path：3 条基带、3 条高光、最多 3 条低透明描边。低档只保留基带。

默认 256px 组件：主体宽约 142px，周围留空；最小 64px 时通过 token 将细描边提高到至少 1 CSS px，不添加外圈补面积。最大外扩不超过基础包围盒的 8%。透明背景导出允许，但演示舞台仍使用深色。

### 4.3 动态与辨识

- idle 循环幅度 ±1%，周期 6 秒；不做持续大幅漂浮。
- 单带位移上限为 viewBox 的 4 单位，旋转不超过 ±2°，整体缩放 0.98～1.04。
- 正常状态切换 180～320ms，默认 240ms；取消和停止在下个可用帧更新目标。
- 无闪烁、无全屏亮度脉冲、无随机闪电，不用颜色作为唯一状态提示。
- demo 在舞台外显示简短中文状态及“模拟数据”；纯形象模式隐藏控件，可保留屏幕阅读器说明。
- 采用系统无衬线字体；按钮原生语义、可聚焦、焦点环清晰，文字对比度至少 4.5:1，控件必要边界及焦点至少 3:1。

### 4.4 视觉验收关口

提交静态单色、64/128/256/512px 联系图和全部状态短片。先内部检查所有禁项，再请 sober 判断是否像 JARVIS、是否克制、是否有脸部联想。未收到判断前可继续独立工程与测试，但正式身份状态写“待用户视觉验收”，不能自行写“已定稿”。这不是现在要求用户选方向。

小规模辅助辨识测试可邀请 3 人，在不显示名称时将静态稿与 5 个不侵权的抽象干扰图混排，观察 5 秒后再认，记录正确数和误认原因。该结果只用于改稿，不代表品牌独创性或商标安全认证。

## 5. 架构与模块职责

```text
Demo 按钮 / 时间线 / JSON 回放
              ↓
MemoryAdapter → validate → ingress 限流/顺序控制
              ↓
       reduce(model,event,now)
              ↓
         selectVisual(model)
              ↓
       MotionEngine + Clock
              ↓
          SVG Renderer
              ↓
       独立容器 / 静态回退

未来外部系统 → 新 Adapter → 同一 ingress（首版不实现网络）
```

| 模块 | 职责 | 禁止依赖 |
|---|---|---|
| protocol | 类型、JSON Schema、运行时校验、结果码 | DOM、业务底座 |
| core | 纯 reducer、状态投影、过期判定、任务与交互生命周期 | DOM、网络、真实时间读取 |
| motion | 插值、强度平滑、质量等级、可注入时钟 | 外部业务协议、框架状态 |
| renderer | 创建/更新 SVG、尺寸变化、静态回退、销毁 | 模型服务、语音 |
| adapters | 传输到规范事件的薄转换 | 直接修改 SVG 或跳过校验 |
| demo | 控件、模拟源、事件回放、诊断面板 | 核心内部可变对象 |

依赖单向。core 输入时间由调用方注入，返回新模型及有限副作用描述，不直接设置定时器。selectVisual 是纯函数，将业务状态转为亮度、位移、缩放、高光相位等参数。

组件 API：

```ts
interface JarvisVisual {
  dispatch(event: unknown): DispatchResult;
  subscribe(fn: (snapshot: Readonly<Snapshot>) => void): () => void;
  getSnapshot(): Readonly<Snapshot>;
  pause(): void;
  resume(): void;
  reset(): void;
  destroy(): void;
}
// createJarvisVisual(container: HTMLElement, options?: Options): JarvisVisual
// Options: reducedMotion: 'system'|'on'; quality: 'auto'|'low'|'standard';
// clock?: Clock; testMode?: { seed: number }; initialTheme?: 内置 token 子集
```

dispatch 永不执行输入中的命令。非法输入返回结构化失败，不使组件崩溃。destroy 幂等；之后 dispatch 返回 DESTROYED，subscribe 不再注册。Snapshot 至少包括只读 model、visibleState、quality、sessionId、lastSeenSeq、rejectedCount、drawCount、destroyed，不含 DOM 或可修改句柄。getSnapshot 返回副本或冻结对象。reset 清空当前演示 session，回到默认 idle，同时递增内部 generation，旧计时回调无效。

## 6. 数据模型、状态机与抢占

业务模型拆成三部分，避免“回答时任务还在运行”被错误覆盖：

```ts
type Interaction = 'idle' | 'attending' | 'thinking' | 'responding';
type TaskStatus = 'running' | 'paused';
interface Model {
  interaction: Interaction;
  interactionId: string | null;
  task: null | { id: string; status: TaskStatus; progress: number | null; lastActivityAt: number };
  fault: null | { code: string; recoverable: boolean };
  lastControlAt: number;
  lastActivityAt: number;
  transient: null | { kind: 'success' | 'cancelled'; until: number };
}
```

visibleState 为：error > 当前非 idle 的 interaction > 执行中 task > paused task > 未过期 transient > idle。保留 task 不意味着执行真实任务；它只是外部状态投影。success 只在无更高优先级显示时出现，800ms 后失效，不排队补播。

| 显示状态 | 进入/退出 | 动态表达 |
|---|---|---|
| idle | 初始、交互结束且无任务 | 基带缓慢亮度起伏 |
| attending | interaction.begin(kind=attending) | 上带轻抬；强度只放大局部开合 |
| thinking | begin(kind=thinking) | 三带亮度依次错相，周期 2.4 秒 |
| responding | begin(kind=responding) | 中带细微展开，强度驱动幅度 |
| executing | task.start/resume，交互 idle | 短高光沿主带前行；有进度时表达进度 |
| paused | task.pause，交互 idle | 三带静止，降低亮度但可识别 |
| success | task.complete | 轻微收拢一次，800ms 后 idle |
| cancelled | task.cancel 或交互 cancel | 160ms 回到基准姿态，短暂提示取消 |
| error | fault.raise | 固定不对称错位姿态，不红色闪烁 |

### 精确转移规则

1. interaction.begin 可从任意非 fault 状态进入指定交互态；必须提供新 interactionId。新 begin 使旧交互失效，任务保持。它是打断的唯一入口，不自动触发业务取消。
2. interaction.end/cancel 只接受当前 interactionId；end 变 idle，cancel 变 idle 并创建 300ms cancelled transient。旧 ID 返回 STALE_CONTEXT。强度数据不能创建交互。
3. task.start 仅在 task=null 且无 fault 时接受。已有任务则 BUSY，不静默覆盖。task.progress/pause/resume/complete/cancel 必须匹配当前 taskId。complete/cancel 清除 task；重复终结返回 STALE_CONTEXT。
4. fault.raise 可从任意状态进入，清除交互、任务、强度和 transient，保留短错误码。error 不自动重试业务。recoverable=true 时 fault.clear 返回 idle；false 时需用户显式 reset。error 时只接受 fault.raise、允许的 fault.clear、session.reset；其余返回 INVALID_STATE。
5. 所有成功交互控制事件更新全局 lastControlAt、lastActivityAt；任务事件仅更新 task.lastActivityAt。activity.ping 更新全局及现存任务的 lastActivityAt。强度、进度不续租交互，避免噪声让界面无限忙碌。
6. 非 idle 交互 30 秒无成功控制事件或 activity.ping，转入 recoverable TIMEOUT；running 任务 60 秒无 activity/progress/control 转 TIMEOUT；paused 任务不计超时，resume 时重新起算。task.lastActivityAt 在匹配任务的成功操作或 activity.ping 时更新；interaction 使用全局 lastControlAt/lastActivityAt，task.progress 不改全局租约。任务进度可续租任务；时钟回拨不影响租约，使用单调接收时间。长任务每 15 秒发送 ping。idle 永不超时。
7. UI pause() 只停绘，模型继续接受事件与租约判定；task.pause 是领域状态，两者不能混淆。隐藏页面不假装任务暂停。
8. session.reset 建立新 session；除此之外全部事件必须匹配当前 sessionId。重新连接时先 reset 再恢复显式快照，不推断任务成功。

## 7. 通用事件协议 v1

### 7.1 信封和有效载荷

```ts
interface Envelope<T extends string, P> {
  version: 1;
  sessionId: string;
  source: string;
  seq: number;
  sentAt: number;       // Unix ms，仅诊断，不用于动画/租约
  type: T;
  payload: P;
}
```

首版每实例只绑定一个权威 source，默认为 demo；options 绑定 source 后输入不得任意切换。多源并发仲裁留给未来适配层，不能靠不同机器的 sentAt 排序。

| type | payload | 规则 |
|---|---|---|
| session.reset | {} | 换新 sessionId，清序列与模型；仅可信本地调用可发送 |
| interaction.begin | {interactionId, kind} | kind=attending/thinking/responding |
| interaction.end / interaction.cancel | {interactionId} | 匹配当前交互 |
| signal.level | {interactionId, value} | value 有限数且 0～1；匹配当前交互ID且只在 attending/responding 接受 |
| task.start | {taskId} | 初始 progress=null |
| task.progress | {taskId, value} | 有限数 0～1，运行中且不后退 |
| task.pause / task.resume / task.complete / task.cancel | {taskId} | pause 仅 running；resume 仅 paused；终结允许两者 |
| activity.ping | {} | 当前 session 租约更新 |
| fault.raise | {code, recoverable} | code 为允许字符短字符串，不是堆栈或 HTML |
| fault.clear | {} | 仅允许可恢复 fault |

示例，演示回答开始：

```json
{"version":1,"sessionId":"demo-001","source":"demo","seq":2,"sentAt":1792281600000,"type":"interaction.begin","payload":{"interactionId":"turn-001","kind":"responding"}}
```

示例假设已发送 seq=1 的 session.reset。随后 seq=3 的 signal.level 携带同一 interactionId 和 value=0.65；seq=4 的 interaction.cancel 结束它；seq=5 可用新 interactionId 开始 attending。所有事件文件使用完整信封，不允许省字段的“魔法简写”。

### 7.2 校验、顺序和背压

- version 必须等于 1；type 为白名单；payload 每种独立校验，拒绝未知字段、原型危险键、NaN、Infinity。seq 为正安全整数，sentAt 为非负安全整数。
- sessionId/source/interactionId/taskId 长度 1～64，字符限 ASCII 字母、数字、下划线、短横线；code 长度 1～48，同样字符集。输入不接受任意 SVG、CSS、URL、函数、命令或 HTML。
- JSON 导入最多 1 MiB、5000 条；单事件序列化上限 2048 UTF-8 字节。先检查文件大小再解析；失败给出行号或数组下标。
- 首次 reset 前，实例使用初始 session；demo 应主动 reset。新 reset 只从绑定本地适配器接受，sessionId 必须不同，seq=1；成功后 lastSeen=1。其后的首个事件从 seq=2 开始。旧 session 不可通过更大 seq 复活。
- 对当前 session/source，schema 有效且 seq 大于 lastSeen 才参与语义处理；记录 lastSeen 后再执行 reducer。seq 不大于 lastSeen 返回 OUT_OF_ORDER。语义拒绝也消耗该序号，避免重复重放。schema 无效不推进 lastSeen。缺序允许跳过，不能等待缺失号卡住界面。
- 控制事件同步依输入顺序处理，不进入动画队列。signal.level 也先校验顺序和上下文，再仅保留当前交互最新值，最多每绘制帧应用一次；控制取消会立即清空待用 level。
- 每秒总输入上限 240；signal.level 上限 120，超额丢弃并计数。另保留 20 次/秒本地 reset/cancel/fault 控制额度，避免强度洪泛吞掉停止动作；导入回放也遵守节流，不瞬发整个数组。
- 日志环形缓冲最多 200 条，只保存类型、接收时间、结果码、数值摘要，禁止完整外部对话内容。订阅状态变化最多 10Hz；控制状态变化可立即通知。销毁后计时和订阅均停止。

DispatchResult 至少为 {ok:boolean, code:string, acceptedSeq?:number}。结果码包含 OK、INVALID_EVENT、UNSUPPORTED_VERSION、WRONG_SESSION、WRONG_SOURCE、OUT_OF_ORDER、STALE_CONTEXT、INVALID_STATE、BUSY、RATE_LIMITED、DESTROYED。

## 8. 动画实现与生命周期

渲染器初始化时创建固定 DOM，逐帧只更新已有属性。不每帧 innerHTML、不创建新 path、不让 UI 框架逐帧重渲染。不在核心保存真实媒体或波形。

注入时钟提供 now/request/cancel。生产 now 使用 performance.now；测试用可推进时钟。全实例只保留一个 rAF 句柄。通过时间戳与目标间隔节流：标准态 60Hz 上限、idle 15Hz、低档 30Hz；实际屏幕刷新率可能更低。禁止 setInterval 与 CSS 循环动画同时驱动同一属性。

强度使用指数平滑：a = 1 - exp(-dt / tau)，current += (target-current)*a；上升 tau=60ms，下降 tau=180ms。500ms 没有有效 level，target 自动归零。dt 上限 50ms，页面恢复重置上一帧时间；恢复时不补画隐藏期间所有帧。

状态插值从“当前渲染参数”开始，使用 smoothstep(u)=u²(3-2u)，而非从上一状态初值重播。中断时取消旧 transition token。相位按活跃可见时间积累；租约按接收单调时间独立判断，不因停绘冻结。reduced-motion 下直接绘制静态姿态、无循环，状态到来只重绘一次。

visibilitychange 隐藏时 cancelAnimationFrame；恢复时先核对租约、模型与质量等级，再画最新状态。页面隐藏时浏览器可节流计时器，因此 TIMEOUT 最迟在恢复或下一次输入时判定，不能承诺后台精确触发。ResizeObserver 只更新容器测量缓存，不循环修改触发自身；零尺寸停止绘制，恢复尺寸后重绘。

destroy 必须清理 rAF、DOM 事件、matchMedia 监听、ResizeObserver、回放定时器、诊断定时器、订阅集合。任何延迟回调先核对 generation 和 destroyed。render 抛错后尝试一次本地静态 SVG 回退并显示安全的文字错误，停止动画；禁止无界自动重建循环。

## 9. 独立演示与场景

页面默认打开即可显示 idle，不请求权限、不播放音频。桌面左侧为占主要面积的舞台，右侧为可折叠控制区；窄屏舞台在上、控件在下。纯形象模式只留舞台与可访问说明，退出按钮可由键盘触达。

控件包括状态按钮、强度滑杆、任务进度、开始/取消、暂停绘制、减少动态、质量等级、错误注入、重置、JSON 导入导出。诊断区显示当前状态、模拟源、最近结果码、丢弃数、帧间隔统计及所处质量档。统计不得覆盖主形象。文本状态以 aria-live=polite 在状态变化时播报，不播报每帧强度。

必须提供以下 fixture，使用固定 seed=42 的模拟强度函数，不使用 Math.random 或真实麦克风：

| 场景文件 | 操作与预期 |
|---|---|
| basic.json | idle → attending → thinking → responding → idle |
| interrupt.json | responding → cancel → 新 attending；旧交互 level 被拒绝 |
| task.json | start → progress=.25/.6 → pause → resume → complete → idle |
| overlap.json | task 运行中进入 responding；交互结束恢复 executing，进度保留 |
| error.json | recoverable fault → clear；fatal fault → clear 被拒 → reset |
| malformed.json | 非法类型、越界值、旧序号、旧 session，不改变有效状态 |
| timeout.json | 注入时钟推进交互/任务租约，恢复到 error；hidden 后恢复也检查 |
| lifecycle.json | 100 次挂载/销毁，不增加监听器与存活句柄 |

回放文件结构为 {format:1, seed:42, steps:[{atMs:0,event:{完整信封}}]}；atMs 为非负整数且单调不减。导入后先验证所有步骤，再用户点击回放；回放开始生成新本地 session 并重写导入事件的 session/source/seq，保留原值仅供诊断。不得把文件里的 session.reset 当成任意权限入口。停止回放取消待执行步骤，递增 generation，防止旧步骤污染手动操作。

## 10. 项目目录与公共构建

```text
jarvis-visual/
  README.md
  AGENTS.md
  package.json
  package-lock.json
  .nvmrc
  .gitignore
  index.html
  tsconfig.json
  vite.config.ts
  vite.lib.config.ts
  vitest.config.ts
  playwright.config.ts
  src/
    index.ts
    protocol/{types.ts,validate.ts,event.schema.json}
    core/{model.ts,reducer.ts,selectors.ts,timeouts.ts}
    motion/{clock.ts,engine.ts,presets.ts,quality.ts}
    renderer/{svg.ts,geometry.ts,theme.ts,lifecycle.ts}
    adapters/memory.ts
    demo/{main.ts,controls.ts,replay.ts,mock.ts,styles.css}
  public/assets/jarvis-static.svg
  fixtures/                 # 上述 JSON 场景
  tests/unit/              # 协议、纯状态机、插值与假时钟
  tests/e2e/               # 浏览器、截图、生命周期、异常
  scripts/{measure.mjs,check-bundle.mjs}
  docs/{architecture.md,protocol.md,visual-spec.md,acceptance.md,sources.md}
  evidence/{environment.json,acceptance-results.md,perf/,screenshots/}
  dist-demo/               # 构建生成，不手改
  dist-lib/                # ESM + 类型声明 + 可选 CSS
```

核心发布接口仅从 src/index.ts 导出，内部模块不成为外部契约。库构建包含类型声明，需单独 tsc emitDeclarationOnly；Vite 不负责生成类型声明。demo 与库分别构建，避免演示代码进入嵌入包。

## 11. 建立工程与分阶段任务

所有命令在新的独立项目目录执行。先检查目标目录是否存在；存在时读当前文件与 Git 状态，不覆盖。初次脚手架需要联网下载；安装完成后，演示运行不依赖外网。

```bash
node --version
npm --version
npm create vite@latest jarvis-visual -- --template vanilla-ts
cd jarvis-visual
npm install
npm install -D vitest @playwright/test @types/node
npx playwright install chromium webkit
```

执行者须在安装时检查来源、engines 与兼容性；将选定直接依赖固定为精确版本，保留 package-lock.json、Node 精确版本和 npm 版本。首次使用 latest 只用于发现可用组合，此后复现用 npm ci，不能每轮升级依赖或复写截图基线。

新增 npm scripts，以下是执行契约，必须创建对应配置与脚本后才能声称可运行：

| 命令 | 实现与作用 |
|---|---|
| npm run dev | vite --host 127.0.0.1，端口 5173，strictPort=true |
| npm run typecheck | tsc --noEmit |
| npm test | vitest run |
| npm run test:e2e | playwright test；webServer 启动本地演示 |
| npm run build:demo | vite build --outDir dist-demo |
| npm run build:lib | vite build --config vite.lib.config.ts 后生成 .d.ts |
| npm run build | 顺序执行两种构建 |
| npm run check:bundle | node scripts/check-bundle.mjs，统计 gzip 后核心 JS/CSS |
| npm run perf | node scripts/measure.mjs，测生产预览、输出原始采样 |
| npm run preview | vite preview --host 127.0.0.1 --port 4173 --strictPort |

### 工作包与通过条件

| 工作包 | 具体工作 | 交付与出口 |
|---|---|---|
| P0 环境与边界 | 新目录、Node/依赖核验、文档、scripts、source 记录 | 可启动空壳，未改现有工程，有环境记录 |
| P1 静态身份 | 三条路径、token、尺寸联系图、静态资产 | 禁项自查通过；用户视觉判断可待定 |
| P2 逻辑核心 | schema、validate、序号、ID、reducer、优先级、租约 | 单测覆盖有效/无效/边界，不依赖 DOM |
| P3 动画与生命周期 | rAF、插值、强度平滑、降级、隐藏/恢复、销毁 | 假时钟测试和浏览器挂载销毁测试通过 |
| P4 独立演示 | 控件、fixtures、回放、诊断、无障碍 | 全场景离线运行；无真实语音或业务请求 |
| P5 验收与打包 | 生产包、自动测试、Safari 手测、性能、证据 | 问题逐项列清，构建可复现，库可独立嵌入 |

每包完成更新 acceptance-results.md，记录 PASS/FAIL/BLOCKED/NOT_RUN，附证据路径。完成依赖顺序为 P0→P1/P2→P3→P4→P5。P1 的轮廓修订与 P2 可独立推进，但不得借此自行授权其他模型。工期随视觉迭代与设备测试变化，不承诺固定天数。

## 12. 质量与性能验收

### 12.1 测量条件

在目标 Mac M4/16GB 上使用生产构建、本地 HTTP、浏览器前台、100% 页面缩放、60Hz 显示环境（若不同则记录）、256px 组件及 1440×900 页面；记录 macOS、浏览器完整版本、DPR、电源模式、温度/后台重任务情况。主测 Safari，自动测 WebKit/Chromium。Playwright WebKit 不能替代真实 Safari 验收。

预热 30 秒，每状态测 60 秒，至少三轮，报告各轮与最差轮；隐藏 60 秒后恢复；另做 30 分钟连续运行。性能与截图分开运行，截图/录屏/DevTools 打开会影响采样。记录静态 SVG 对照页，以区分浏览器基础开销和动画增量。

### 12.2 预算

| 指标 | 目标/阻断条件 | 方法与限制 |
|---|---|---|
| 核心库体积 | 核心 JS+CSS gzip ≤50 KiB；demo ≤150 KiB | node:zlib 统计构建产物，类型和测试不计入 |
| 正常动态节奏 | 60Hz 条件下 p95 实际绘制间隔 ≤25ms，>33.4ms 比例 <5% | 记录实际执行绘制时间点；不能只测 rAF 回调 |
| idle 节奏 | 15Hz 上限，p95 ≤85ms | 单独统计，不能拿 idle 判 60fps |
| 输入到模型 | p95 <10ms | 本地入口到 reducer 完成；不含外部传输 |
| 输入到绘制提交 | 前台 p95 <100ms | performance.now 同域测量；不等同屏幕实际光子延迟 |
| 组件绘制 JS 耗时 | 标准态 p95 ≤4ms | 不包含完整 GPU/浏览器绘制成本 |
| CPU 增量 | idle ≤3、active ≤10 个百分点（单核100%口径） | 对照静态页，记录同口径 WebContent/renderer 进程；无法隔离则标记不可归因 |
| 页面关联进程常驻内存 | 相比静态页增量 ≤100 MiB，页面归因合计目标 ≤250 MiB | 系统进程工具多点采样，不把 JS heap 当总内存 |
| 长时内存 | 预热后 30min 无持续单调增长，末5min中位数比首5min ≤20 MiB | GC 抖动需解释；不能只比较单点 |
| 隐藏/减少动态 | 隐藏后无持续绘制；减少动态时无循环帧 | 句柄、绘制计数和浏览器观测交叉检查 |
| 生命周期 | 销毁后 rAF/observer/subscriber/回放句柄归零 | 测试注册计数与浏览器检查；100 次循环 |

这些是首版工程预算，测不出时必须 NOT_RUN 或 BLOCKED，不能填估计值。CPU/RSS 跨进程统计易受浏览器共享进程干扰；测试使用独立浏览器配置且只开一个目标页，保存采样口径和原始输出。

### 12.3 自动降级

quality=auto 初始 standard。仅在前台 active、目标60Hz、无 reduced-motion 时，以连续 5 秒窗口观察实际绘制间隔；连续两个窗口 p95>25ms 时降 low（30Hz、无高光、无附加描边）。low 连续两个窗口 p95>50ms 时进入 static。idle、隐藏、截图测试及用户主动暂停不参加判级。自动降档本 session 不自动升档，用户可手动重试 standard；降级不重置任务或交互状态。

low 的验收目标 p95≤50ms；static 仅状态变化时重绘。若目标 M4 只能静态通过，功能降级可用但标准态性能验收仍为 FAIL，不得用自动降级掩盖预算失败。

## 13. 自动测试矩阵

优先验证可观察行为，不为凑覆盖率写镜像实现的测试。

| 层级 | 必须断言 |
|---|---|
| validator | 每种事件合法/非法；缺字段、未知字段、过长ID、NaN、越界、版本与原型危险键 |
| ingress | seq 重放、乱序、缺序、语义拒绝消耗序号、旧session、source切换、限流后停止额度 |
| reducer | 全部合法转移、非法操作返回码、打断旧ID、任务并行、错误优先级、重复终结 |
| time | 30s/60s边界、ping续租、level不续交互、隐藏恢复、回拨不影响单调租约 |
| motion | 相同seed/时间同结果；dt上限；强度归零；中断连续；静态模式无循环 |
| lifecycle | destroy两次、销毁后dispatch、unsubscribe、reset后的旧回调、100次挂载销毁 |
| browser | 控件键盘操作、窄屏无横向溢出、错误回退、纯形象退出、尺寸0→恢复 |
| screenshots | 每状态、四尺寸、reduced-motion、low/static，固定时钟/seed/浏览器版本 |
| security | 外部请求计数为零、无权限调用、恶意JSON不插入DOM、超限文件拒绝 |

截图测试由测试入口注入时钟，固定 t=1000ms、seed=42。禁止仅依赖禁用 CSS 动画的截图选项，因为自定义 rAF 仍可能运行。初版基线必须人工检查；差异失败不得自动更新为通过。不同浏览器单独保留基线，允许经审查的小范围抗锯齿差异。

E2E 监听页面请求，只允许本地 origin；监听 console.error/pageerror；测试场景结束后不得有未解释错误。后台隐藏不能只靠合成 visibilitychange 假装真实停绘：逻辑测试可注入 visibility provider，另在本机实际切换标签记录证据。

推荐验收顺序：npm ci → typecheck → unit → build → bundle → e2e → production perf → Safari 手测。失败后只重跑受影响与必要回归；报告每个命令退出码、环境及未覆盖项。

## 14. 安全、异常恢复与未来边界

- 默认仅 127.0.0.1，无跨域开放、远程监听、凭证、麦克风、摄像头或文件系统权限。依赖安装需要网络，运行态无远程请求。
- 所有错误文字用 textContent；输入不能决定 SVG 路径、style、事件处理器或 import 地址。主题仅允许预定义 token 和严格颜色格式。
- 提供生产预览用 CSP 示例：default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'。frame-ancestors 必须由 HTTP header 设置，不能只写 meta。开发 HMR 使用独立策略，不能把开发放宽策略带入交付。
- renderer 用 SVG presentation attributes 更新属性，CSS 使用本地样式表；不依赖 inline script/eval。CSP 是否满足须在生产 HTTP 服务实测。
- malformed 输入拒绝且保留最近有效画面；renderer 异常才触发静态回退。恢复只重建视觉，不重发或重做外部任务。
- 导出仅含模拟事件与诊断摘要，无任何真实业务内容。禁止运行态自动上传、后台访问模型服务或执行工具。
- 后续若增加 postMessage，必须验证 origin、event.source、协议和消息大小；若增加 WebSocket，另立认证、重连、快照与限流设计。当前不预实现占位网络连接。
- 本组件只显示外部状态，不能作为真实任务完成或取消成功的证据。未来外部业务方负责授权、任务账本和结果验证。

## 15. 交付清单与复现/回退

执行者最终交付：

- [ ] 完整源代码、精确依赖、lockfile、Node/npm记录与许可证清单。
- [ ] 可运行独立 demo，全部模拟场景，无外部业务依赖。
- [ ] ESM库、类型声明、静态SVG及四尺寸联系图。
- [ ] architecture/protocol/visual-spec/acceptance/sources 文档。
- [ ] 协议JSON Schema与运行时校验一致性测试。
- [ ] 单测/E2E结果、截图、Safari手测、性能原始数据与方法。
- [ ] acceptance-results 每项标明状态，列已知缺陷、待用户视觉验收和未执行项。
- [ ] 一个最小嵌入示例：新建容器→create→reset→dispatch→destroy，不引用 demo 内部模块。
- [ ] 打包清单与 SHA-256，说明如何 npm ci、启动、构建和离线运行。

README 明确：离线运行是依赖安装/构建后通过本地 HTTP 打开，不承诺双击 file:// 下的 ESM 可用。不默认安装 service worker。

在独立 Git 仓库保存可复现提交；每个通过工作包建立标记并记录 commit。新版本部署为新的静态目录，先验证再切换本地服务目录；回退切换到上一目录/提交的构建，不用 reset --hard 清理用户文件。提供回退演练：新包→故障注入→旧包→重新打开 demo，确认视觉和场景恢复。本文未授权上传仓库、发布公共网站或修改生产配置。

## 16. 可直接发给 Codex / Hermes 的启动指令

下面两份指令指向同一个开发目标，选择实际执行平台发送。交接时同时附上本文完整文件；不能只发送对话链接或摘要。

### 16.1 Codex 启动指令

```text
你是本项目实施者。请完整阅读附件《JARVIS 抽象智能视觉形象与轻量动态交互系统》v1.0，再在当前工作区新建独立 jarvis-visual 项目，按 P0～P5 实施直到交付可运行成果。

目标环境是 Mac M4/16GB。技术基线为 TypeScript、SVG、原生 CSS、Vite，测试用 Vitest 与 Playwright。实现三条集中且克制的开放折返光带，不出现真人、脸、文字主视觉、Siri式彩色光球、中心光球、满屏粒子或三维角色。本文参考路径是起点，保持三带、右折返、开放缺口、中心负空间等固定身份特征。

本项目完全独立，不读取或修改现有 JARVIS、3D 或全双工语音项目，不接入麦克风、模型、真实任务或外部账号。不创建网络服务；只保留通用事件 API 与内存适配器。所有输入默认模拟，并在演示台说明。

先检查目录、Git状态、Node与依赖兼容性，再落地代码和测试。执行本方案的模块分层、协议、序号/上下文校验、状态优先级、动画时钟、隐藏停绘、减少动态、销毁、异常回退、降级与性能预算。为所有 npm scripts 创建真实可运行的配置和脚本。

每个工作包记录文件、命令、退出码与证据。不得把文档、测试数量或估计值写成实际能力证明。没有目标设备测量就标 NOT_RUN/BLOCKED，不编造 M4 数据；用户未确认视觉就标待用户视觉验收。可在等待视觉判断时继续无关工程。

如需委派模型，仅允许 deepseek_helper.delegate，通过用户的 https://xcode.best/v1 中继固定请求 deepseek-v4.1-flash。先核对helper实际配置；不能验证路由/模型就不委派。不得发送凭证，不使用原生子代理、其他模型或新聊天替代。主模型不变；helper不可访问本地文件，文件修改和验证由你执行。不强制委派。

不要停在计划或静态示意图。交付源代码、demo、库构建、静态资产、自动测试、Safari记录、性能原始数据、已知问题、复现与回退说明。禁止自行发布、上传、改生产或扩大付费资源使用。仅在缺少不可推断的必要条件时提出具体问题，同时推进不受阻的工作。
```

### 16.2 Hermes 启动指令

```text
请作为主实施者读取所附 JARVIS 独立视觉系统 v1.0 完整方案，按其中 P0～P5 在新 jarvis-visual 目录实施，交付真实可运行项目。以方案中的范围、三带折返视觉、SVG/TypeScript/Vite架构、事件协议、状态机、测试矩阵和性能预算为执行契约。不要只生成报告。

先确认当前会话有文件读写和终端执行能力；如果没有，明确报告缺失能力并交付可转交的任务拆分，不声称已创建文件或通过测试。可执行时，按工作包写代码、跑检查并保存证据，不需要接入现有 JARVIS、语音、消息渠道或外部业务。

默认全部数据本地模拟；不请求麦克风、不调用模型、不发送消息、不启动后台Cron，不修改当前Hermes运行配置。未来只保留通用API，不实现真实集成。不得将 visual success 当作业务成功。

委派限制：只能使用 deepseek_helper.delegate，确认经 https://xcode.best/v1 固定调用 deepseek-v4.1-flash；无法确认或不可用就自己继续并报告，不换模型/供应商、不用其他子代理/新聊天，不发送凭证。此限制不要求委派，主模型保持不变。

按本文交付清单报告 PASS/FAIL/BLOCKED/NOT_RUN；视觉用户确认和 M4 性能实测分开列，不造数据。最终提供源代码、锁定依赖、可复现命令、独立演示、自动测试、实际浏览器证据、性能原始记录和可验证回退。未经另行授权不发布、不上传、不改生产、不增加付费资源。
```

## 17. 当前交接记录

- 用户要求与引用对话已核对；轻量抽象形象方向已明确，不沿用对话早期的三维数字人和语音优先级建议。
- taste 技能已查阅，仅采用与本项目相关的克制动态、身份一致性、性能、无障碍与状态完整性原则。当前深色、手写 SVG 和无 React 架构依据用户目标与任务性质，不强套营销页面默认规则。
- 用户指定的 Humanizer 和 Humanizer-zh 路径不存在，常见技能目录检索未找到；未声称已加载。本文按自然中文、明确证据边界和可执行说明撰写。
- 本次交付为实施方案文件；没有委派模型、创建开发工程、跑应用测试、测量目标硬件、连接业务系统或发布服务。

文档自检记录：已检查要求覆盖、Markdown 代码围栏闭合、相对证据路径约定、协议序号示例与 reset 规则、任务/交互分离租约。仅验证文档一致性，未验证待开发应用。

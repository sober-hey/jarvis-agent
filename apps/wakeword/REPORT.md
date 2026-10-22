# Jarvis 离线唤醒词：实测交付报告

## 结论

已交付一个**真实从音频识别英语单词 “Jarvis” 的本地 Core ML 检测器**，不是 VAD、不是云 ASR、不是关键词文本匹配。Mac 上原生 Swift 重放、禁止网络的沙箱重放、现有 `WakeWordDetector` 协议连接和分类器消融均已实际执行。

但这是**个人离线原型，不是已经过真人/全天候验收的产品模型**：小规模同音色 TTS 留出集命中率高，真实音乐中仍有 **2.803 次/小时**误唤醒。不能宣称已达到“长期常开、低误报”标准，也不能把合成测试的 100% 当作 Sir 真人命中率。

所有改动/产物限于 `apps/wakeword/`；未改 Mac/iPhone/Watch 应用、未接管麦克风、未修改后台进程。主应用接入仍需父任务完成。

## 路线与许可证

- 调研来源和原文：[`research/RESEARCH.md`](research/RESEARCH.md)、[`research/SOURCES.md`](research/SOURCES.md)。
- **openWakeWord 代码 Apache-2.0；官方 `hey_jarvis` 权重 CC BY-NC-SA 4.0。** 允许个人非商业本地使用，但必须保留署名/许可证；衍生权重分发涉及 ShareAlike。不能把“代码 Apache”误写成“全部模型可商用”。
- 共享 mel/embedding 权重发布物的独立许可在上游 issue #348 仍有疑问；出处可追到 Torch mel 和 Google speech_embedding，但出处不等于完整商用许可。**本交付只限定个人非商业实验；商业和公开模型再分发未获清关。** 自训练分类器不自动清除前端权重许可问题。
- Porcupine 是离线引擎，但需要有效 AccessKey/账户授权，模型依平台生成，官方列表未明确 watchOS。未引入它，避免 Key 生命周期和 watchOS 支持不确定性。
- microWakeWord / sherpa-onnx 已调研，未为赶交付再造一套前端。采用 openWakeWord 既有 16kHz mel→96维嵌入→轻量分类器结构；没有从零训练语音编码器。
- 自训练流程是**沿用 openWakeWord 的冻结嵌入 + 合成语音训练分类头的方法**，并非声称跑完上游官方大规模 Colab 训练。当前小型 MLP 的数据量和负例覆盖显著低于上游推荐，详见局限。

## 数据与严格划分

实际生成并保存 111 条百炼 TTS：

| 类别 | 数量 | 说明 |
|---|---:|---|
| 独立 “Jarvis.” | 15 | 5 音色 × 0.85/1.0/1.15 本地 tempo；每个都是独立 TTS 请求，不是同一 WAV 三次复制 |
| “Jarvis, …” 开头命令 | 24 | 不含 hey，不能冒充“只喊 Jarvis”样本 |
| “Hey Jarvis” 控制 | 8 | 对照上游模型 |
| 日常中文负例 | 32 | 无 Jarvis/贾维斯 |
| 英文普通负例 | 16 | 无唤醒词 |
| 英文近音负例 | 16 | 如 Travis、harvest、service；无唤醒词 |

音色为 qwen3-tts-flash 的 Cherry、Serena、Ethan、Chelsie，以及提供的 cosyvoice-v3.5-flash 完整复刻音色 ID。**不是 Sir 真实麦克风录音。** 语速变化用 ffmpeg `atempo`，未声称是不受支持的服务端语速参数。全部标准化为 16kHz、mono、signed PCM16 WAV。

- `data/manifest.json` 保存 96 条命令/控制/负例；`data/standalone.json` 保存 15 条单词。
- 训练只用 calibration：20 条命令 + 10 条独立单词 + 55 条语音负例；7 条 hey 控制不训练。共 3558 窗口，其中 330 正窗口。
- 纯留出：4 条命令、5 条独立单词、9 条语音负例、1 条 hey 控制。阈值预设 0.8，未看留出结果调阈值或按音乐负例回训。
- **这是 clip 留出，不是说话人留出。** 5 种 TTS 声线在训练和留出中都出现；同字同音色的不同生成可能高度相关，不能据此推断未知人声泛化。tempo 派生和后续扰动样本不作为独立人数/独立语料。
- TTS 碰到 429/一次超时，串行重试后 111 条均有真实音频和 SHA-256；原始生成记录含错误历史，未填充假音频。Key 只从 `~/.jarvis/dashscope.key` 读取，不存入产物。signed URL 不写进证据。

## 命中率：实际 Swift/Core ML 重放

每个 clip 前后各补 1s 静音、每 80ms 推理一次，置信度 >=0.8 触发，重复事件冷却 1.5s。命中定义为目标 clip 至少一次触发，不是每帧 accuracy。

| 测试 | 官方 hey_jarvis + 官方 Python runtime | 新 Jarvis 分类头 + Swift/Core ML |
|---|---:|---:|
| 独立 Jarvis，全部 | 1/15，6.67% | 15/15，100%（含训练数据，非泛化成绩） |
| 独立 Jarvis，纯留出 | 0/5 | 5/5（小样本、同音色） |
| Jarvis 开头命令，全部 | 7/24，29.17% | 24/24（含训练数据） |
| Jarvis 开头命令，纯留出 | 3/4 | 4/4 |
| Hey Jarvis 控制 | 8/8 | 7/8（该短语不是新模型训练目标） |
| 中文/英文/近音负例 | 0/64 触发 | 0/64 触发；其中纯留出仅 9 条 |

这证明 `hey_jarvis` 不能直接当作可靠的单词 “Jarvis” 模型。新分类头确实改变了单词检测行为，而非靠 VAD。

另外，5 条留出单词各添加 0.2 倍音量、10dB 白噪声、人工短反射混响，三组分别 **5/5**。这是同一 5 条音频的派生测试，不把它包装成 15 条独立真人样本。

## 每小时误唤醒：真实负例持续重放

| 负例内容 | 实际音频时长（不含前后测试 padding） | 误唤醒事件 | 实测次数/小时 |
|---|---:|---:|---:|
| 20 首 MUSAN/FMA 真实音乐 + 1 条 MUSAN 环境声 | 5136.4715 秒，1.4268 小时 | **4** | **2.80348/h** |
| 64 条 TTS 中文/普通英文/近音句 | 142.0603125 秒 | 0 | 0/h（只有约 2.37 分钟，不能外推长期零误报） |
| 静音/白噪/粉噪/程序生成器乐各 10 分钟 | 2400 秒，40 分钟 | 0 | 0/h（这是人工信号压力测，不是真实音乐指标） |

真实音乐误触发发生在 `music-fma-0017` 的 15.48/22.36s 与 `music-fma-0018` 的 50.52/130.52s。取每处 5s 切片另调百炼 ASR 做审计，返回歌词均没有 Jarvis/贾维斯；ASR 不是人工标注保证，但支持按误报计数。见 `evidence/music-false-positive-asr-audit.json`。推理主链没有用 ASR。

音乐是所下载 parquet 中前 20 条，按文件顺序事前固定，没有挑低误报歌曲。**这些音乐未用于训练/调阈值。** 目前误报水平不够做安静无打扰的常开助手，后续应另设训练负例、扩大独立人声测试、在新的未见音乐集复验，而非拿本测试集反复调参再报留出成绩。

MUSAN 源、SHA、逐文件持续时间保存在 `data/musan.json`。MUSAN 总体 CC BY 4.0，音乐仍须看各原始来源条款；下载的音频只是本地评测数据，不随产品打包。HF 按要求用 `HF_ENDPOINT=https://hf-mirror.com`、去代理；首次错误 URL 404 后使用真正存在的 parquet。GitHub release 模型用 ghproxy.net 前缀和 `curl -C -`。

## 延迟、体积、数值一致性

实测宿主 **Apple M4 / macOS 27.0.1 / arm64**，CPU-only，release Swift。不能移植这些数字当 Watch/iPhone 的耗时或电量。

- 真实音乐重放，单个 80ms hop 的完整 Swift mel+embedding+classifier 调用：p50 **0.398ms**、p95 **0.462ms**、p99 **0.674ms**、max **23.228ms**。另一次人工信号压力测 max 49.032ms；不是实时无丢帧证明。
- 单词音频以 |PCM|>0.01 定义起音：起音到第一次触发 median **0.598s**，范围 **0.213–0.696s**。这是文件级声学起音，不是人工标注的完整词尾；没有声称“词尾后 X ms”。部分触发在单词结束之前，故词尾差值为负没有意义。
- 每 hop 1280 新样本（80ms），mel 带 480 样本历史，76×32 mel 上下文，16×96 embedding 环。全程本地，无云识别调用。
- `Melspectrogram.mlpackage` **1,092,615 bytes**；`SpeechEmbedding.mlpackage` **1,355,181 bytes**；`JarvisClassifier.mlpackage` **860,735 bytes**。部署三包合计 **3,308,531 bytes / 3.155 MiB**（容器文件逻辑字节，不是峰值 RAM）。
- 原始 ONNX→等价 Torch→Core ML 使用 float32。前端与 ONNX 最大绝对误差：mel **9.54e-7**，embedding **2.38e-5**。同一 111 clips 下 Swift/Core ML 与 Torch 分类器的逐 clip max score 最大差 **4.81e-7**。先验证权重/层行为再交付，不是只转换成功就算准确。
- coremltools 提醒当前 Torch 2.14.1 超出其已测试 2.7.0 范围；本交付以真实数值 gate 和原生重放通过为凭证，未隐去兼容警告。环境固定于 `requirements.lock`。

## 工程/平台验收

| 项目 | 结果 | 证据/边界 |
|---|---|---|
| Mac release Swift 构建 | PASS | `evidence/swift-build-v2.log` |
| 111 clips Swift 原生推理 | PASS | `swift-main.json`、`swift-standalone.json` |
| 原有 WakeWordDetector 协议接入 | PASS | bare clip 产生 1 个 `.wakeWord` 事件；不是单测 fake scorer |
| 输入拒绝 | PASS | 非 1280 长度、NaN、超范围 PCM 均拒绝 |
| 本机禁止网络仍识别 | PASS | `sandbox-exec ... (deny network*)`；15/15 单词，`swift-offline-sandbox.json` |
| 消融负控 | PASS | 相同前端/相同 15 条音频，分类器所有权重置零→0/15，原始头15/15；`swift-ablation.json` |
| iOS SDK arm64 构建 | PASS | `evidence/ios-build-v2.log`；未装真机、无 iPhone 延迟/耗电结论 |
| watchOS SDK arm64_32/arm64 库构建 | PASS（适配后） | `evidence/watch-build-v2.log`；不是 Watch 真机运行 |
| 三包编译为 watchOS `.mlmodelc` | PASS | `models/compiled/`；需要随设备 target 编译打包，不是运行时编译 |
| 真人喊词/实时麦克风/远场 | **NOT RUN** | 未录 Sir 真人、未声称麦克风 E2E 成功 |
| Watch 真机时延/准确率/电量 | **NOT RUN** | 无目标设备验收 |
| 锁屏/抬腕外常开 | **NOT ESTABLISHED** | Apple watchOS 后台执行受限；不能靠假 workout/静音保活绕过 |

初始 watch 构建真实失败：现有 Activation 通用加载器调用 `MLModel.compileModel(at:)`，此 API 在 watchOS 不可用。仅在本目录适配：Mac/iOS 直接实现原有 `WakeWordScoring`；Watch 条件编译不依赖这个 Mac 模块，导出同形状 PCM 协议，只接受预编译 `.mlmodelc`。没有偷偷修改上游 `apps/mac` 修报错。Watch 协议是独立同形状接口，不冒称与 Mac 模块有跨模块类型一致性。

因此回答“Watch 能不能跑”：**SDK 构建和模型编译已经证明该实现可被构建；实际硬件推理、24小时后台监听、可接受续航均未证明。** 可先把 Watch 用作主动触发/限定会话客户端，让 Mac/iPhone 承担常开监听。

## 文件与接入

- 正式模型：`models/{Melspectrogram,SpeechEmbedding,JarvisClassifier}.mlpackage`。只打包这三个；`AblatedClassifier` 是测试负控，不是部署模型。
- Swift scorer：`Sources/JarvisWakeWord/JarvisWakeWordScorer.swift`；library product `JarvisWakeWord`，本地依赖现有 Activation 模块。
- Swift 真实文件重放 CLI：`Sources/WakeWordReplay/main.swift`。
- 下载/生成/训练/转换/评估/聚合脚本：`scripts/`。初版脚本的证据文件名固定，复跑前复制到独立 run 目录，避免覆盖本次历史证据。
- 逐项证据：`evidence/SUMMARY.json`、`evidence/model-sha256.json`、`data/*.json`。大评测音频、venv/build 目录本地存在，由 `.gitignore` 排除，避免把几百 MB corpus 混进模型提交。

用法详见 [`README.md`](README.md)。关键点：**输入是连续不重叠的80ms新音频，不是重复2秒滑窗**；scorer 有上下文，和 detector 一起在采集重启/时间断点时 reset。Mac/iPhone app 必须显式接入此 scorer 和三份资源，现有“有人说话”逻辑不会自动变成唤醒词识别。

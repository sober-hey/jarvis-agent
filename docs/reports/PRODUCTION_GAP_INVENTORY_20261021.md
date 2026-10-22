# JARVIS v1.0 正式上线缺口盘点

## 结论与证据边界

目前不能认定具备“独立服务器常驻＋生产日用”条件。不是没有产品：单一时间线、真实模型对话、感官接入、情绪、主动联系、3 小时真实常驻及语音网关已有局部证据；欠的是生产安全闭环、目标服务器适配、稳定供给、真实设备日用和最终长测。

本轮只读静态盘点，未运行项目代码/测试、未联网、未启动或停止服务、未读取真实密钥。只创建本报告；“实测”仅指本轮读取/静态计数，或明确标明的历史实测证据读回，不表示重演成功。路径除特别注明外均相对 `仓库根/`。

基线：2026-10-21 15:55 CST，Git HEAD `051ed3d4`；首次读取为 `143ea58e`，盘点期间外部新增了 iOS/Watch 修复提交，因此不是冻结快照。两次 `git --no-optional-locks status --short` 均为空；`git remote -v` 为空。历史 HANDOFF 的“全未提交”“PID 在运行”“唯一卡点”不是本轮现场结论。

核查覆盖：HANDOFF 全文（包括第一节、〇节、第三节、末尾 200 行与全部指定关键词）；AGENTS、PROJECT_STATE、decisions/adr、OPEN 报告；注册表静态声明、96 个非 test_ 的 packaging 直接 Python 文件头、src 草案；Git 最近 40 条。指定宿主 `/Volumes/扩展硬盘/hermes/state/JARVIS_v1.0.md` 不存在；找到的真实指针是 `/Volumes/扩展硬盘/hermes/.hermes/state/JARVIS_v1.0.md:7-9`，内容仍停在 10-21 01:10。

## A 阻断上线

- A1. 还没有“真实模型连续 24 小时正常思考”的通过记录，不能承诺全天在岗。
  证据：`.runs_01/resident-real-24h-20261018/run/RESULTS.json` 为 `ok=false`、`duration_reached=false`；HANDOFF:388-394、430-437 记录 aicoming 402 后停摆。`.runs_01/resident-real-3h-v3-20261017/run/RESULTS.json` 为 real、10800 秒、ok=true，只支持历史 3 小时；最新 fake 长测目录只有 RUNNING、checks 为空，不是完成报告。
  等级：实测（历史机器结果读回）＋文档称。余额是否已恢复、本机现在是否常驻，本轮未联网/查进程，不能断言仍在欠费或停机。

- A2. 一家模型站拒绝服务后仍不能自动切到已验合格的备用大脑，日用会再次失去认知供给。
  证据：`packaging/product_config.py:26-29` 仍为单路六字段；`docs/adr/ADR-0007-provider-failover-and-degraded-operation.md:3,43-94` 是未批准草案；`packaging/integration_registry.py:1511-1523` 明确 switch_model 只是人工迁移 CLI。OPEN 报告:10-17 记载 deepseek-v4.1-flash 真实拒绝 32/40、qwen-plus 4/4；不是所有 DeepSeek/Qwen 型号均不可用的结论。
  等级：实测（静态源码）＋文档称。HANDOFF:131 已证拒绝分类和人工迁移补齐，不能再说“402 一概被吞成 unknown”，但这不是自动故障切换。

- A3. 还没有可验收的独立生产服务器部署，Mac 上能跑不能当作上云成功。
  证据：`packaging/linux_preflight.py:167-195` 明列 ECS 镜像、磁盘、SQLite、隔离、备份、systemd、24h、PG-L 为 NOT_RUN，production_ready=false；`AGENTS.md:7,27-28` 指定生产端阿里云 ECS。`docs/decisions/CR-01_20261014_HERMES_PRODUCT_ROUTE.md:33`、HANDOFF:453 禁止碰腾讯云外卖服务器，需另建隔离机器。
  等级：实测（静态源码）＋文档称。本轮未 SSH，未核查是否已有用户新建但未登记的机器。

- A4. 把大脑搬到 Linux 后，技能执行、iMessage、Mac 生活数据不能照搬原路径继续用，需要服务端与设备桥接方案。
  证据：`packaging/skill_loader.py:185-192,320-322` 非 macOS 无 Seatbelt 即拒绝执行；`packaging/daemon_wiring.py:824-831,874` 硬编码外置盘仓库及宿主 health.db；HANDOFF:74 指向本机 lifelog/health/tracks。Linux/ECS 实测仍 NOT_RUN。
  等级：实测（静态源码）＋推断。需配置化路径、远端数据回传及 macOS 通道代理，不能靠复制目录宣称独立常驻。

- A5. “取消后不再产生新副作用、崩溃后不重复办事”的生产安全证明尚未闭合，不能直接开放通用执行权限。
  证据：`docs/adr/ADR-0006-cancel-publish-linearization.md:3,15-25,268-293` 为 NOT_APPROVED/NOT_VERIFIED，保留 U-1～U-8 与平台/实现审计；`docs/adr/ADR-0004-atomic-publish.md:26-30` 保留 G1～G6、Linux/ECS NOT_RUN。`packaging/task_api.py:1-4,41-49` 也明确不是冻结生产内核或 OS 沙箱。
  等级：文档称＋实测（静态边界）。macOS 原子发布原语已有局部通过，不是“完全没测”，但局部通过不能关闭取消、耐久和恢复门禁。

- A6. 内心思考更会调用记忆的改造仍未通过原验收，不能把召回收益写成可安全上线。
  证据：`.runs_01/quota-fix-r4b-20261021-003605/acceptance.json` 为 NOT_PASSED：B 召回达标，但 validate 28 对 A 29、非法出处 B=1 对 A=0；side_checks 还包括 single_wire_call_per_row=false、r1_control_byte_exact_30_of_30=false。HANDOFF:145-151 明确不许事后自行改门槛；提交 `0bec107d` 标记“未通过验收门槛”。
  等级：实测（历史机器结果读回）。本轮未证明当前 daemon 加载哪版；不能沿用旧 PID 文档断言“补丁尚未生效”，也不能在当前源码已有改动时假定仍是旧基线。

- A7. 以语音为主的产品还缺手机、手表真实连续对话的闭环验收，不能把安装与球体可见等同日用。
  证据：HANDOFF:50-67、199、519-522 仍区分装机、语音与连接待测；最新 `051ed3d4` 修设置页、连接路由并删除 iOS 唤醒词，属于修复代码，不是装机后成功记录。HANDOFF:120-123 的局域网/本机探针成功只证明当时网关链路。
  等级：文档称＋实测（提交读回）。已有网关端到端成功，故不能说“语音从未跑通”；但没有证据证明本轮 iPhone/Watch 修复已真机解除失败。

## B 功能未做或未接线

- B1. 能处理资料任务、也有工具适配器，但日常对话还不能据证据认定会自主调用工具把通用任务办完。
  证据：`packaging/integration_registry.py:1550-1559,1574-1594` 明确 TaskScheduler 无 daemon/inbox 驱动、ToolRouter 构造不等于模型工具循环；`packaging/daemon_wiring.py:2212-2277` 的 tasks 主要处理 socket 与情绪待办，非该 TaskScheduler。反证：`packaging/task_api.py:532-592` 已有真实 router.route 执行与逐步回执，注册表:1535 的“fixture-only”已落后，不能概括为完全不能执行。
  等级：实测（静态调用链）＋文档称；缺的是自然输入→计划→执行→独立验证→回报的常驻闭环。

- B2. 思考控制器与红队评审不是每轮都在把关，不能承诺回答/任务已经过统一路由和反对意见检查。
  证据：`packaging/daemon_wiring.py:752-760` 构造 ThinkController；`packaging/integration_registry.py:1562-1571` 明确 route/execute/feedback 未进入常驻循环。`packaging/task_scheduler.py:670-687` 确实会调 CriticAgent，但该调度器缺常驻驱动（注册表:1559）。
  等级：实测（静态源码）＋文档称。“完全没有调用方”过度，正确说法是消费者存在、生产触发链不闭合。

- B3. 不能自动研究模型、更新可用模型档案、评估新库并投入运行，模型档案和库评分没有形成优化闭环。
  证据：`packaging/daemon_wiring.py:746` 已建 ModelRegistry；注册表:1260-1269 明确 model_search 不更新它，:1205-1214 明确 LibraryEvaluator 无产品实例/注入/调用；`packaging/skill_loader.py:157-177` 仅接受可选 evaluator，实际 Wiring:769 未注入。
  等级：实测（静态源码）＋文档称。不是“模型档案模块完全未接线”，而是更新与评估使用链缺失。

- B4. 技能系统已经存在，但还不能认定会按任务挑技能、执行技能、稳定管理版本；插件/MCP 扩展也未完成生产接入。
  证据：`packaging/skill_loader.py:1-5,36,144-177,387,434` 有运行 loader/执行/匹配，Wiring:764-770 已构造并供提示使用；注册表:1439-1448 明确 task match/execute、API 适配未观察到，:1229-1238、1912-1921 明确 MCP 无已验常驻调用。直接非测试模块扫描未见 skill_loader.match/execute 调用。
  等级：实测（静态源码）＋文档称。已知背景“产品层不存在技能/插件”不准确；正确是基础件和提示接线已做，可执行扩展闭环未交付。

- B5. 做完事后可以产出技能/改进提案，但不能自动把合格提案安全变成已激活的新能力。
  证据：`packaging/daemon_wiring.py:1545-1579` 有完成后后台蒸馏；注册表:1427-1436 明确生成技能仍为 PENDING/inactive，审批、实现、激活未闭合；self_evolution/evolution_scheduler 的审计说明同样只承诺提案，未证明投递和生效。
  等级：实测（静态源码）＋文档称。不是全无自进化，也不是已经能自行上线改造。

- B6. 目标、断点记录已有，但还不能保证重启后自动续做未完成工作并给出真实进度。
  证据：`packaging/integration_registry.py:963-974,1391-1402` 的 goal_tracker、session_resume 审计说明：构造与只读查询存在，不代表任务自动更新、自动 checkpoint/resume 或连续执行；`packaging/daemon_wiring.py:777-852` 可核构造边界。
  等级：文档称＋实测（静态构造）。需要实际跨重启任务演练，不是再增加一个查询页面。

- B7. 记忆、人格、假设、情绪和复盘不是一套已验收的统一认知账，可能存得下却想不起或无法追溯完整来源。
  证据：`packaging/integration_registry.py:540-550,1031-1042,1241-1257,1338-1349,1415-1425` 的审计说明分别使用多库，假设证据/确认/过期生命周期未观察到、部分蒸馏无 KG 投影；knowledge_graph（:1202）与 kg_projector（:1960 起）说明已有构造和后台投影，但常驻效果、完整历史类别覆盖仍未验证。
  等级：文档称。多数据库本身不是缺陷；这里缺的是可验证的信息流、来源和跨库恢复一致性。

- B8. 健康分析与提醒已接后台构造，仍不能承诺真实健康异常会被准确识别并送到 Sir。
  证据：`packaging/integration_registry.py:975-1042` 的 health_analyzer/health_scheduler/health_alert 说明已有后台 tick 和 mind 写入，但真实测量接入、跨库重复恢复、实际通知回执未验；senses.health 条目仍是元数据，不等于测量分析；`packaging/daemon_wiring.py:874` 还依赖本机健康库。
  等级：文档称＋实测（静态路径）。这是安全相关功能，不能用入队、文件变化或假阈值测试代替送达。

- B9. 大脑仍等整段回答再送 TTS，不是真正“边想边说”；原生全双工模型自行回答模式仍缺接口。
  证据：`.runs_01/voice-stream-wire-20261021-0900/REPORT.md:14-18,67-72` 明确非 token 流、客户端需声明 stream_pcm；`packaging/voice_port.py:20-23` 的 NATIVE 缺只记录时间线端点；Wiring:795-797 未传 stream_handler/config，`packaging/jarvis_socket.py:629-630` 返回 stream_chat_unavailable。
  等级：实测（静态源码）＋文档称。网关接入 TTS 流已完成，“还没接网关”的旧 HANDOFF/注册表说法已过时。

- B10. 视觉能单次拍摄/描述，但还不能认定具备服务器常驻“看见现场”的自动感知能力。
  证据：`packaging/vision.py:1-7` 明确 one-shot、无自动拍摄循环；HANDOFF:264 已修摄像头帧率问题，但真拍画面近黑、明亮场景/人数准确性未验；注册表 vision_input 仍缺常驻工具循环与 KG 接入。
  等级：实测（静态源码）＋文档称。旧“摄像头失败原因未定”已被修复记录取代。

- B11. 小爱音箱和通用设备控制还不能确认会在正确房间真正出声或执行动作。
  证据：HANDOFF:83、205 明确未装机、缺房间判断并 fail closed；注册表 device_controller、device_routing、voice_xiaoai 说明未验生产设备注册、状态采集、房间策略、实际播放，云提交不等于听见；`packaging/device_routing.py:105` 为房间/在家守卫入口。
  等级：文档称＋实测（静态守卫）。不得靠关闭守卫来“接通”。

- B12. 3D 形象、iPhone 侧边按钮/快捷指令、Hermes 正式档案移交尚未全部交付，但可从最小生产版本中明确延期。
  证据：HANDOFF:79-86、203-205；`PROJECT_STATE.md:3-22` 与 HANDOFF:404-406 明确形象外观/核心连接未验；HANDOFF:348-369 已有档案吸收和 SSE 局部验收，不能说这两项从零未做。正式个人资料导入需 Sir 触发。
  等级：文档称。可延期项应在发布范围中写清，不应挡住基础服务器闭环，也不能被宣传为已具备。

## C 已知缺陷与未解问题

- C1. qwen 隔离探针全过却在真实上下文全部被拒，根因仍未关闭，不能用探针合规率批准备用路线。
  证据：`docs/reports/OPEN_20261018_QWEN_PASSES_PROBE_FAILS_LIVE.md:4,30-61` 仍 OPEN，原库已被清理。当前 `packaging/inner_runtime.py:1486-1496` 已记 field/error_type/reason；`scripts/export_mind_stats.py:111-119` 已有 validate 回放，尚不能替代 validate_matrix 等完整阶段。
  等级：文档称＋实测（静态源码）。旧“只记 call_id、还没补错误日志”已过时；诊断增强不代表根因修好。

- C2. 内心思考时已经能收 iMessage 并发收到回执，但正式处理仍需等当前模型调用到检查点，不能保证急话立即打断。
  证据：`packaging/jarvis_daemon.py:103-126,161-210` 有并行 ingress，但 think/tasks/dispatch/reflection 主路径串行；:186-195 明示不是 provider response.cancel；`packaging/daemon_wiring.py:1283-1328` 有异步忙碌回执。语音还与 daemon 争同一 launcher 锁，HANDOFF:121、43 只把等锁限为 20 秒。
  等级：实测（静态源码）＋推断。已知背景“严格串行堵住 inbox”过度；改正为“收信已并行，处理/锁竞争仍可拖延回复”。

- C3. 有一条面向 Sir 的忙碌回执写死在代码中，与“所有话由模型现场生成”的既定规格冲突。
  证据：`packaging/daemon_wiring.py:1305-1313` 固定发送“已收到，正在处理上一件事，稍后回复”；对照 HANDOFF:18、20、309 的不写话术规则。
  等级：实测（静态源码＋规格对照）。这是当前实际代码冲突，不把它夸大成所有人格回答都写死。

- C4. 任务“发布批准”不等于“执行批准”，用户可能误以为没批准发布就不会产生文件或命令副作用。
  证据：`packaging/task_api.py:41-49` 明确 submit tool plan 先执行、--publish 仅批准报告发布；:555-558 先入账 attempt 再 router.route。该模块也说明取消不能撤销已在途效果。
  等级：实测（静态源码）。需把执行授权与结果发布分开、在界面/语音说明，不可用发布按钮冒充统一审批门。

- C5. 已有技能清单校验，但它不验证实际 skill.py 字节或作者；多版本选择和宿主 API 适配也有缺口。
  证据：`packaging/skill_loader.py:48-53` 明列同名版本被覆盖、checksum 仅元数据、默认读取面宽；:284-287 要求 context.mind_store，:315-317 要求可调用 API；Wiring:769 直接传 self，注册表:1448 记录 API 不匹配。
  等级：实测（静态源码）。尚未动态复现每个技能；这是明确接口/校验边界，不是已证明发生恶意执行。

- C6. 注册表不是可靠的上线完成表：全部登记项都标 pending，旧说明滞后，新火山 TTS 还未纳入覆盖。
  证据：AST 静态读出 105 个唯一声明；`packaging/integration_registry.py:2072-2098` 无条件添加 runtime_wiring、pending=True；:477-479 两个孤岛为有意的一次性/测证工具，不是缺失常驻功能；voice_stream_tts 条目仍说无网关调用，和新报告冲突；voice_volcengine 无声明但网关:1158-1184 已导入。
  等级：实测（静态计数/源码）。self_evaluate=False 共 8 项（affect_engine、affect_rules、daemon_wiring、dialogue_affect、inner_runtime、inner_voice、jarvis_daemon、think_controller），被 core 规则豁免；不能把 105 个 pending 当 105 个完全没做，也不能把 core 豁免当已做自评。

- C7. 交接台账互相矛盾，会让接手者重做已修功能、误用未验代码或照旧端口部署。
  证据：HANDOFF:45 称 iPhone 唤醒词接入、:53 却称未打包，最新 `051ed3d4` 已删除；:132、205 说流式没接网关但新报告已接；:153-164 说信封溢出未修，`4694b262` 和 `packaging/context_limits.py:50` 已改 UTF-8；PROJECT_STATE:155-161 仍停旧 K0 指针，而 K4 RC 已落盘。宿主状态也只指向 01:10。
  等级：实测（文档、源码、提交对照）。不能把所有历史“还没开始”照抄为当前缺口，特别是单一时间线、人格缩减、daemon 接线已实现。

## D 验收欠账

- D1. K0～K4 草案与大量单测不等于生产内核已启用，仍缺逐层生产接线、真实模型与跨进程恢复验收。
  证据：`docs/reports/K4-010_RC_20261018.md:4,41-62` 明示 DRAFT/macOS-only、未接生产、Linux/真实模型/跨进程重启未验。AST 本轮扫描 src/jarvis：52 个直接 .py、除 __init__ 为 51 模块；45 个文件存在显式 runtime_enabled/RUNTIME_ENABLED=False 赋值，42 个文件头文档声明 runtime_enabled=False（口径不同）。例如 worker.py:25、recovery.py:33、skill_registry.py:21。
  等级：实测（仅 AST 解析，未导入项目）＋文档称。不把包装产品历史实模通过移植成 src 草案实模通过，也不把草案完成提交标题当上线许可。

- D2. 需要在最终冻结版本跑全量回归与独立评审，旧局部 PASS 不能覆盖后续语音、热引擎和设备修复。
  证据：HANDOFF:81、101 明确全项目评审未做；:90 留旧边界失败，但 `4694b262` 已有修复。热引擎报告有定向测试通过，未给当前 HEAD 全仓回归；最新 `143ea58e`、`051ed3d4` 又增加/修改生产链。
  等级：文档称＋实测（提交对照）。本轮没跑测试，所以不将旧 test_inner_worst_escaped_boundary 列为当前必然仍红，列为需重验项。

- D3. 热引擎的 8ms 是假模型的本地热轮次，不是用户真实对话响应延迟，仍欠真实连续调用验收。
  证据：`.runs_01/warm-engine-20261021-0900/REPORT.md:5,23-31,92-96` 明确 fake provider、真实模型三轮 NOT_RUN、120 秒 idle 退出未专门等待；HANDOFF:47 的“本地开销近零”不能外推端到端。
  等级：文档称（报告含历史日志）。需验证真实上下文/凭据轮换、死亡恢复、会话隔离及同 identity 不重发，而不只测速度。

- D4. 流式 TTS 的探针首包通过，不等于三端边收边播、实际打断与云故障回退都通过。
  证据：流式网关报告:30-46、48-52、67-72：探针声明 stream_pcm=true、最近首包 1133ms，真实成功轮未触发取消/回退；客户端未改。iOS `Core/VoiceCore.swift:44-47`、Watch `Core/WatchCore.swift:123-125` 的 hello 均未声明 stream_pcm；iOS 测试 main.swift 的 PlaybackAccumulator 仍等 MP3 final。
  等级：文档称＋实测（静态协议）。旧客户端兼容可用，但不会因此获得 PCM 低延迟；也未稳定证明严格小于 1 秒。

- D5. 火山 TTS 已写进网关且成为默认选择，但还缺默认线上链路与设备听感的端到端验收，旧百炼结果不能代替。
  证据：`143ea58e`；`packaging/voice_gateway.py:1158-1184,1481`，火山适配器刻意无 create_stream，走 MP3 分块；`packaging/voice_volcengine.py:1-11` 自述有单句延迟实测。HANDOFF:529 仍写 401 待修，已落后于代码；未见本轮独立网关→iPhone/Watch 播放报告。
  等级：实测（静态源码）＋文档称。新的默认音色是爽快思思，不是此前 JARVIS 百炼克隆音色；是否符合 Sir 听感尚不能由代码判断。

- D6. 唤醒、通话让路、运动触发、手机锁屏和多设备仲裁还欠真人场景验收。
  证据：HANDOFF:54、66-67、84、199-200 区分安装与待验；iOS 最新提交已去唤醒词，不能沿用“iPhone 离线唤醒已交付”。网关日志 hello/转写/MP3 或 playback_done 只能证明协议观察，不证明人听见、权限稳定或后台一直收音。
  等级：文档称＋实测（提交对照）。天水话识别质量仍无明确最终验收（历史 HANDOFF:220）；不要求继续已明确暂停的 voice_live 调优。

- D7. 长期记忆规模、内存增长、自然关怀与真实冲突纠正仍缺最终生产版本的长窗证明。
  证据：HANDOFF:298-300 说明有界历史修复后 RSS 改善、剩余积压是假阳性边界，不能再宣称已确认持续泄漏；:244 保留真实问题复核未触发、更高层关怀未观察；:352-356 的 3 小时仅支持当时版本。正式发布门禁 `docs/PRODUCTION_READINESS.md:9-12` 仍要求 24h 资源趋势。
  等级：文档称。不能以未观察到行为证明永远不会出现，但也不能把工程循环能跑宣传成已达到人格/自主性目标。

## E 安全与运维

- E1. 模型子进程仍拿到真实 API key，正式凭据代理隔离未落地；拿到工具执行能力后必须重新审查这一边界。
  证据：`packaging/chat_launcher.py:699-745` 组装含 api_key 的匿名管道载荷并交 child；`PROJECT_STATE.md:31-35` 的独立传输实验只证明本机假 secret，不证明 OS 隔离。`packaging/product_config.py:307-328` 的 owner/0600/普通文件检查已做，但不是 broker。
  等级：实测（静态源码）＋文档称。HANDOFF:453 已扩大开发授权，不能再把“还没准许碰真实凭据”当唯一阻塞；缺的是实现与目标 OS 证据，密钥仍不得进聊天/日志/argv/提交。

- E2. Web 没有生产身份认证，语音有共享 Bearer 但网关直连无 TLS，不能直接放到公网。
  证据：`packaging/web_server.py:1-5,442-450,1081-1082` 只允许 loopback、Host/Origin 校验不等于认证；`packaging/voice_gateway.py:984-993,1014-1024` 有 Bearer、serve 未配置 SSL；`docs/voice-docs/voice_gateway.md:20-26` 要各设备共用 token。
  等级：实测（静态源码）＋推断。需私网/Tailscale或经过验证的 TLS/认证入口、设备撤权与令牌轮换方案；尾网可加密不代表 LAN 明文路径也安全。

- E3. systemd 模板和现有端口约定未整合，照旧模板部署可能冲突，且语音启动脚本仍依赖 Mac 宿主。
  证据：`packaging/deploy/jarvis.service:1,17` 明示模板且 Web 指向 8765；`docs/voice-docs/voice_gateway.md:14-15,28-35` 语音使用宿主脚本与 8765、Web 改 8767。README-supervisor 旧“假 Worker”说明与 `packaging/supervisor_components.py:153-176` 的真实 daemon 接线已不一致。
  等级：实测（静态配置）＋推断。需要可重建安装清单、依赖/路径/端口/开机顺序，不是复制 launchd plist 到 Linux。

- E4. 监督器自己被强杀后的孤儿接管还没实现，子组件能被拉起不等于整机/服务故障恢复完成。
  证据：`packaging/deploy/README-supervisor.md:40-47` 明确跨 supervisor crash orphan adoption 未实现；systemd 模板:22 有 KillMode=control-group，但未在目标 Linux 验证。`packaging/linux_preflight.py:172-176` 的断电耐久、产品故障、生命周期都 NOT_RUN。
  等级：文档称＋实测（静态模板）。需实际演练 supervisor SIGKILL、重启/断电、unknown 对账与不重复副作用。

- E5. 备份目前是禁用的本地草案，不是可用的异地灾备；丢 Mac/外置盘后能否恢复尚无生产证据。
  证据：`src/jarvis/backup.py:1-7,35-37` 仅 draft_fixture，B2 是本地 tar.gz；:56-75 分别备份数据库和产物，尚无应用跨资源一致性证明；:122-133 恢复未先走 verify_b2。`DECISIONS_REQUIRED.md:50-52` 仍列频率、保留、加密、位置与恢复审批。
  等级：实测（静态源码）＋文档称。应备份生产多库、产物、配置及可轮换凭据边界，并在另一环境恢复核对；“B2”不等于已接 Backblaze 或任何云存储。

- E6. 心跳与部分日志已有，但还没有完整证据证明断供、认知停止、盘满、长日志增长会被独立监控并真正告警到 Sir。
  证据：`packaging/jarvis_daemon.py:128-159` 有认知心跳，HANDOFF:388-394 有宿主 cron 历史通知；`src/jarvis/health.py:1-4,44-47` 仍是禁用草案。`packaging/supervisor.py:153-169,204-209` 追加日志，所核路径无轮转；语音单独有轮转（voice_gateway.py:1495 附近）。
  等级：实测（静态源码）＋文档称＋推断。不是“没有监控”；需与产品一起迁往服务器的外部存活/认知进度、额度、磁盘、队列、日志保留与送达回执，不能依赖开发者 Mac 在线。

- E7. 长测会清理原始数据，旧诊断已因此不可复现；生产必须区分证据封存、私密保留和安全清理。
  证据：OPEN 报告:43-55 记 route B/C 原库丢失；`packaging/soak/run_soak.py:1072-1104` 清临时数据根；HANDOFF:148 记旧 outreach 只有哈希无原文。Git 本轮无 remote，只有本地提交不能证明异地源码备份。
  等级：文档称＋实测（静态清理/Git）。建议错误样本与上下文脱敏封存、访问控制/保留期先定；不能为排错无限保存全部生活录音、转写和凭据。

- E8. Watch 调试签名会到期，运维交接与发布批准也没收口，不能把一次装机当长期可用发布。
  证据：HANDOFF:54、91、134 记录免费签名 2026-10-27 到期、需重签；`AGENTS.md:24-28`、`PROJECT_STATE.md:150-162` 保留 production NO/NOT_FROZEN/无 release；ADR-0001/2/3/5 各:3 为 PROPOSED，ADR-0004:3 为 PENDING_VALIDATION，ADR-0006/7:3 为未批准草案（实际在 docs/adr，不是 docs/decisions）。
  等级：文档称。需长期签名/分发、回滚、轮换、恢复手册和最终具名发布结论；广泛研发授权不自动变成生产门禁通过。

## 上线前最短路径（建议顺序，不是本轮已执行）

1. 冻结最小发布范围与候选版本：先交付服务器常驻、可信对话/记忆、手机/手表可用语音；3D、小爱、NATIVE、自动进化可明示延期。把 HANDOFF/注册表/状态指针与当前代码对齐，明确配额候选是否撤回或重新验收；理由：先知道究竟在验哪件产品，避免边长测边改代码。
2. 确立稳定主供给与真实上下文合格备用：补余额/配额监控；保留明确拒绝和 unknown 的不同处理，落实经批准的切换/降级方案；理由：断供会使后续所有实模验收失效，探针 200/合规不能充当准入。
3. 关闭会产生真实副作用的安全门：取消/发布、对账、预算授权、凭据代理与 OS 隔离；通用任务执行未闭合前明确限权。若正式范围需要“替 Sir 办事”，同步接 TaskScheduler→执行→红队/独立核验→回执；理由：不能先开放高权限再补防重复和撤权。
4. 建独立 ECS 候选环境并适配平台边界：禁止用腾讯云外卖机；配置化 Mac 路径、做设备/数据/iMessage 回传桥、实现 Linux 沙箱或明确禁用技能执行、整合 Web/语音端口与私网/TLS；理由：生产真实环境会暴露本机验收看不到的问题。
5. 先补可恢复运维底座：systemd、整服务强杀/重启恢复、异地备份恢复、日志轮转、远端告警和凭据轮换；理由：必须先做到出故障可看见、可恢复，才适合长跑与导入真实生活数据。
6. 在该候选版做真实设备端到端：iPhone/Watch 连接/令牌、收音→转写→大脑→当前默认火山声音→实际播放，锁屏/通话/多设备/打断；再决定是否升级 PCM。补热引擎真实三轮与语音听感；理由：语音占主入口，网关探针不能代替真人日用。
7. 全量回归与独立审查后，冻结字节跑目标服务器真实 24h：验证认知每小时在推进、感知无丢失、来源正确、队列有界、RSS/磁盘趋势、重启/断供/恢复与通知回执；失败原始证据先封存再清理。理由：验收对象必须包含已整合的最终链路，不能拿旧 3h 或 fake 24h 代替。
8. 做长期试用与正式发布裁决：Sir 验自然主动性、关怀、记忆与日常响应，补长期设备签名、操作手册和版本回滚包，关闭适用 ADR/发布门禁后才上正式生产；最后按 Sir 指令吸收 Hermes 正式档案。理由：工程安全、稳定通过之后，才能判断是否真是可日用的管家，而非模块集合。

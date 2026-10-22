# 代码地图

由 `scripts/gen_code_map.py` 从源码自动生成，不要手改。
每一行是「文件 / 职责（模块 docstring 首行）/ 行数」。

## 语音子系统（voice/，已隔离）

| 文件 | 职责 | 行数 |
|---|---|---|
| `voice/voice.py` | File-only speech: injectable ASR/TTS, local lifelog reuse, no recording/playback. | 718 |
| `voice/voice_dashscope.py` | Bailian REST speech adapter; no recording, playback or import-time I/O. | 441 |
| `voice/voice_gateway.py` | Authenticated private-address ears/mouth for the single JARVIS BRAIN timeline. | 1751 |
| `voice/voice_ledger.py` | Append-only voice observations on the authoritative MindStore.entries timeline. | 424 |
| `voice/voice_live.py` | Opt-in local live voice; no fixed speech, retained recordings, or cloud ASR. | 922 |
| `voice/voice_port.py` | Adapter contract for a future full-duplex voice model (JARVIS voice port). | 183 |
| `voice/voice_speaker.py` | 声纹验证：判断一段 16 kHz PCM 是不是 Sir 本人在说。 | 183 |
| `voice/voice_stream_tts.py` | Incremental CosyVoice TTS over the Bailian duplex WebSocket protocol. | 446 |
| `voice/voice_volcengine.py` | 火山 seed-tts-2.0 双向流式 TTS — JARVIS 网关适配层。 | 302 |
| `voice/voice_xiaoai.py` | Guarded, single-device XiaoAI cloud TTS. Importing performs no I/O. | 259 |

## 语音相关设备接入（仍在 packaging/）

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/avatar_signals.py` | Numeric-only, renderer-independent avatar cues on the audio playback timeline. | 570 |
| `packaging/avatar_stream.py` | Bounded, numeric avatar read projection; never initializes product stores. | 241 |
| `packaging/device_controller.py` | Unified, explicit-registration device control; importing never controls devices. | 468 |
| `packaging/device_routing.py` | Deterministic offline policies for wake ownership and audio output. | 226 |

## 守护进程与认知循环

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/curiosity.py` | Generic belief bookkeeping and deterministic value-of-information scheduling. | 1266 |
| `packaging/daemon_wiring.py` | Product adapters for the resident scheduler; no work starts at import. | 2531 |
| `packaging/inner_loop.py` | Bounded, append-only product mind. No background service is installed. | 449 |
| `packaging/inner_loop_fts.py` | Local, disposable lexical recall projection; never edits source evidence. | 688 |
| `packaging/inner_model.py` | Single-call mind adapter using the existing launcher's verified FD bootstrap. | 140 |
| `packaging/inner_runtime.py` | Thinking, epistemic routing, cadence and a bounded command-line runner. | 1707 |
| `packaging/inner_voice.py` | Inner-only system channel; ordinary chat persona is deliberately separate. | 35 |
| `packaging/reflection.py` | Read-only daily evidence collection and first-person JARVIS reflection. | 726 |

## 模型调用与路由

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/chat_entry.py` | JARVIS one-shot text chat. Product Python -I -S -B required. | 1265 |
| `packaging/chat_launcher.py` | JARVIS dedicated one-shot chat launcher; no host Hermes config/resolver. | 1012 |
| `packaging/config_launcher.py` | JARVIS-owned read-only configuration check and future launcher handoff. | 266 |
| `packaging/engine_entry.py` | Offline JARVIS engine import check, using only the product interpreter. | 817 |
| `packaging/model_registry.py` | Private, durable model research metadata; never stores API key contents. | 242 |

## 记忆与知识

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/citations.py` | Persistent citation ledger for JARVIS. | 152 |
| `packaging/kg_projector.py` | Bounded, deterministic MindStore -> KnowledgeGraph projection. | 661 |
| `packaging/knowledge_graph.py` | Provenance-preserving SQLite fusion graph above profile and hypotheses. | 985 |
| `packaging/memory_distiller.py` | Bounded, incremental background distillation of Sir observations. | 616 |
| `packaging/sir_profile.py` | Sir's persistent, provenance-bearing profile (not yet wired into think). | 453 |

## 健康、生活与外部数据

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/device_controller.py` | Unified, explicit-registration device control; importing never controls devices. | 468 |
| `packaging/device_routing.py` | Deterministic offline policies for wake ownership and audio output. | 226 |
| `packaging/health_alert.py` | Health anomaly persistence, graph projection, and concise Sir-facing advice. | 204 |
| `packaging/health_analyzer.py` | Local read-only health analytics, not medical diagnosis or emergency monitoring. | 762 |
| `packaging/health_rules.py` | Non-diagnostic engineering alerts for analyzer dictionaries. | 325 |
| `packaging/health_scheduler.py` | Non-diagnostic health scheduling; tick never reads a database or runs analytics. | 752 |

## 运维、安全与工具

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/evidence_support.py` | Disposable packaging evidence; persistent output requires an explicit opt-in. | 60 |
| `packaging/product_home.py` | Single-user product data directory validation, not an OS sandbox. | 182 |
| `packaging/search.py` | Bounded, failure-tolerant internet search for JARVIS. | 70 |
| `packaging/supervisor.py` | Single-owner JARVIS process supervisor (POSIX, macOS/Linux). | 530 |
| `packaging/supervisor_components.py` | Component definitions and bounded health adapters for web and resident daemon. | 244 |
| `packaging/tool_router.py` | Lazy registry for local tools; route() is the validated/audited entry point. | 926 |
| `packaging/web_server.py` | Loopback-only development HTTP adapter; no production authentication claim. | 1198 |

## 其它运行时模块

| 文件 | 职责 | 行数 |
|---|---|---|
| `packaging/__init__.py` | Local application modules, sharing dependency submodules with PyPA packaging. | 34 |
| `packaging/absorb.py` | Independent, provenance-aware archive absorption. No live personality changes. | 1002 |
| `packaging/absorb_formats.py` | Immutable, content-addressed archive snapshots and provenance-aware readers. | 271 |
| `packaging/absorb_memory.py` | Explicit publication of archive evidence into product-owned long-term memory. | 159 |
| `packaging/affect_engine.py` | Independent affect state, analytic time decay and durable decisions. | 982 |
| `packaging/affect_rules.py` | Bounded Chinese fallback appraisal and deterministic retort guard. | 248 |
| `packaging/agent_protocol.py` | JSON task/result envelopes shared by independent agent execution units. | 226 |
| `packaging/amap.py` | Amap (高德地图) client: reverse geocoding and nearby POI search. | 461 |
| `packaging/bootstrap.py` | 工程本机 bootstrap；仅转交 JARVIS 的标准库检查入口，不安装依赖。 | 36 |
| `packaging/build_replay_set.py` | Deterministic structured SQLite replay builder; see replay_harness API docs. | 365 |
| `packaging/capabilities.py` | Trusted senses, web search, citation ledger and synthetic fixture capabilities. | 679 |
| `packaging/code_search.py` | Code search backed by ripgrep (no shell or Python regex translation). | 189 |
| `packaging/conftest.py` | pytest 引导：让 packaging/ 的测试能找到隔离出去的语音子系统。 | 15 |
| `packaging/context_limits.py` | One source for UTF-8 prompt budgets (not tokens or Unicode code points). | 83 |
| `packaging/context_metrics.py` | Offline, content-free context measurements; never called by the daemon. | 480 |
| `packaging/critic_agent.py` | Independent red-team review using the product's existing model transport. | 132 |
| `packaging/dialogue_affect.py` | Product dialogue bridge: affect/memory data in, live first-person speech out. | 361 |
| `packaging/evolution_scheduler.py` | Non-blocking, proposal-only weekly research scheduling. | 459 |
| `packaging/file_ops.py` | UTF-8 file operations with component-aware optional root restrictions. | 641 |
| `packaging/file_task_chain.py` | Private local/semantic file chains; not the approved K0/P2 control kernel. | 668 |
| `packaging/git_ops.py` | Small subprocess-backed Git API, without shell interpolation or GitPython. | 282 |
| `packaging/goal_tracker.py` | Persistent caller-reported goals with a WAL-compatible durable audit outbox. | 329 |
| `packaging/hypothesis_engine.py` | Persistent hypothesis lifecycle; callers adjudicate evidence relevance. | 378 |
| `packaging/imessage_input.py` | Owned, offline evaluation surface for the logical iMessage ingress resource. | 33 |
| `packaging/integration_registry.py` | Explicit product wiring ledger; importing never starts product components. | 2190 |
| `packaging/jarvis.py` | JARVIS product CLI: distribution checks, explicit configuration and one-shot chat. | 812 |
| `packaging/jarvis_daemon.py` | Bounded resident scheduler. No service is installed by this entrypoint. | 280 |
| `packaging/jarvis_socket.py` | Private Unix-socket ingress and read-only mind queries for the resident daemon. | 840 |
| `packaging/jarvis_ui.py` | JARVIS mini: a native, always-on-top macOS capsule and three recent turns. | 827 |
| `packaging/library_evaluator.py` | Two-layer library evaluation; imports and construction never use the network. | 480 |
| `packaging/linux_preflight.py` | Read-only environment preflight; standard library only, Python >= 3.12. | 292 |
| `packaging/mcp_bridge.py` | Synchronous MCP stdio client for explicitly configured local servers. | 462 |
| `packaging/migrate_beliefs.py` | One-shot, idempotent Sir-belief migration; never modifies mind.db. | 50 |
| `packaging/outreach.py` | Durable, local-only outreach queue with replayable SSE and draft email. | 1258 |
| `packaging/product_config.py` | JARVIS-owned non-secret route configuration and credential boundary. | 423 |
| `packaging/product_memory.py` | Product-owned SQLite facts, provenance, tombstones and Chinese trigram search. | 301 |
| `packaging/replay_harness.py` | Deterministic, offline frozen replay harness for copied JARVIS SQLite data. | 478 |
| `packaging/replay_quota.py` | Fixed-quota inner request allocation. No I/O, model calls or persistent state. | 260 |
| `packaging/self_evolution.py` | Proposal-only external research; never patches JARVIS or executes candidates. | 882 |
| `packaging/session_resume.py` | Private internal task checkpoints; no new user-visible conversation timeline. | 240 |
| `packaging/shell_executor.py` | Synchronous argv-based command execution (not a PTY/unified_exec session). | 366 |
| `packaging/skill_distiller.py` | Distil complex task traces into inert, pending skill proposals. | 315 |
| `packaging/skill_loader.py` | Runtime skill loading, capability-gated APIs and OS-enforced worker sandbox. | 565 |
| `packaging/skill_manifest.py` | Runtime skill metadata. Declarations do not grant execution permissions. | 309 |
| `packaging/skill_registry.py` | Persistent runtime skill metadata registry with atomic dependency checks. | 344 |
| `packaging/stream_response.py` | Text streaming protocol: ordered deltas and exactly one terminal chunk. | 122 |
| `packaging/switch_model.py` | Fail-closed, offline-safe model-route migration for JARVIS data roots. | 401 |
| `packaging/task_api.py` | Durable task adapter with explicit fixture mode and bounded real tool plans. | 888 |
| `packaging/task_executor.py` | Keyword task routing and text execution through the product chat boundary. | 187 |
| `packaging/task_scheduler.py` | Single-process threaded agent scheduler; not wired to daemon/inbox. | 760 |
| `packaging/think_controller.py` | Local thinking policy. This standalone module is not wired into the daemon. | 1072 |
| `packaging/verify_avatar_web.py` | Offline browser acceptance fixture. Private stdin controls only, no HTTP writes. | 85 |
| `packaging/vision.py` | One-shot JARVIS vision: capture, describe, and emit a camera/vision event. | 620 |
| `packaging/vision_input.py` | Image attachments for JARVIS; no screenshot or network activity on import. | 346 |
| `packaging/web_tasks.py` | Private loopback document tasks, durable receipts and bounded workers. | 172 |

## 内核层（K0–K4 草案，未全部接线）

| 文件 | 职责 | 行数 |
|---|---|---|
| `src/jarvis/__init__.py` | JARVIS kernel: bootstrap only, no task execution enabled. | 3 |
| `src/jarvis/approval.py` | K1-007 DRAFT; NOT_APPROVED / NOT_FROZEN, runtime_enabled=False. | 446 |
| `src/jarvis/autonomy_budget.py` | K3-006 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 344 |
| `src/jarvis/autonomy_controls.py` | K3-007 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 359 |
| `src/jarvis/backup.py` | K0-017 DRAFT: local B1/B2 backup and restore. | 161 |
| `src/jarvis/budget.py` | K1-008 DRAFT: in-memory model budget reservations and accounting. | 208 |
| `src/jarvis/cancellation.py` | K0-014 cancellation DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 256 |
| `src/jarvis/cli.py` | K0-008 CLI DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 301 |
| `src/jarvis/context_builder.py` | K1-009 context-builder DRAFT; NOT_APPROVED / NOT_FROZEN. | 183 |
| `src/jarvis/controlled_release.py` | K4-009 Controlled Release DRAFT; NOT_FROZEN / NOT_APPROVED. | 327 |
| `src/jarvis/domain/__init__.py` | JARVIS domain layer: contract projections only, no persistence, no execution. | 25 |
| `src/jarvis/domain/states.py` | Pure state projections of the K0 machine-readable contracts. | 92 |
| `src/jarvis/environment.py` | Read-only platform inspection and isolated SQLite baseline verification. | 69 |
| `src/jarvis/episodic_memory.py` | K2-003 Episodic Memory DRAFT; NOT_FROZEN / NOT_APPROVED. | 223 |
| `src/jarvis/event_dedup.py` | K3-004 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 281 |
| `src/jarvis/event_schema.py` | K3-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False. | 389 |
| `src/jarvis/event_store.py` | K3-002 Durable Event Store DRAFT; NOT_FROZEN / NOT_APPROVED. | 340 |
| `src/jarvis/fault_injection.py` | K0-015 DRAFT: controlled fault injection for offline fixtures only. | 55 |
| `src/jarvis/health.py` | K0-016 DRAFT: health monitor, structured logging, resource limits. | 173 |
| `src/jarvis/improvement_proposal.py` | K4-005 Improvement Proposal DRAFT; NOT_FROZEN / NOT_APPROVED. | 388 |
| `src/jarvis/intent.py` | K0-011 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 708 |
| `src/jarvis/isolated_runner.py` | K4-004 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 298 |
| `src/jarvis/memory_schema.py` | K2-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False. | 364 |
| `src/jarvis/mock_provider.py` | K1-002 DRAFT mock provider; NOT_FROZEN; runtime_enabled=False. | 159 |
| `src/jarvis/model_provider.py` | K1-001 DRAFT: provider-independent call, response and error contracts. | 126 |
| `src/jarvis/openai_adapter.py` | K1-003 DRAFT: OpenAI-compatible Chat Completions transport. | 313 |
| `src/jarvis/outbox.py` | K3-003 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 325 |
| `src/jarvis/path_control.py` | K0-009 path/permission DRAFT; NOT_APPROVED / NOT_FROZEN, runtime disabled. | 255 |
| `src/jarvis/persona.py` | K2-009 Persona Manager DRAFT; NOT_FROZEN / NOT_APPROVED. | 240 |
| `src/jarvis/plan_history.py` | K1-006 plan history DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 154 |
| `src/jarvis/planner.py` | K0-006 deterministic Planner DRAFT; NOT_FROZEN / NOT_APPROVED. | 280 |
| `src/jarvis/preference_store.py` | K2-005 Preference Store DRAFT; NOT_FROZEN / NOT_APPROVED. | 349 |
| `src/jarvis/prompt_guard.py` | K1-010 prompt-injection heuristic DRAFT; NOT_APPROVED / NOT_FROZEN. | 298 |
| `src/jarvis/provenance.py` | K2-007 Provenance & Conflict DRAFT; NOT_FROZEN / NOT_APPROVED. | 265 |
| `src/jarvis/provider_registry.py` | K1-004 DRAFT provider capability registry. | 200 |
| `src/jarvis/publisher.py` | K0-012 DRAFT; NOT_APPROVED / NOT_FROZEN, runtime_enabled=False. | 332 |
| `src/jarvis/reconciler.py` | K0-013 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 256 |
| `src/jarvis/recovery.py` | K3-008 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 408 |
| `src/jarvis/regression_evaluator.py` | K4-007 Regression Evaluator DRAFT; NOT_FROZEN / NOT_APPROVED. | 239 |
| `src/jarvis/release_approval.py` | K4-008 Release Approval DRAFT; NOT_FROZEN / NOT_APPROVED. | 159 |
| `src/jarvis/repository.py` | K0-005 DRAFT repository; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False. | 516 |
| `src/jarvis/retention.py` | K2-008 Deletion & Retention DRAFT; NOT_FROZEN / NOT_APPROVED. | 252 |
| `src/jarvis/retrieval.py` | K2-006 retrieval DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 299 |
| `src/jarvis/rollback.py` | K4-010 Rollback DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 302 |
| `src/jarvis/scheduler.py` | K3-005 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False. | 389 |
| `src/jarvis/semantic_memory.py` | K2-004 Semantic Memory DRAFT; NOT_FROZEN / NOT_APPROVED. | 208 |
| `src/jarvis/skill_manifest.py` | K4-001 DRAFT; NOT_FROZEN, NOT_APPROVED, runtime_enabled=False. | 215 |
| `src/jarvis/skill_registry.py` | K4-002 Skill Registry DRAFT; NOT_FROZEN / NOT_APPROVED. | 309 |
| `src/jarvis/snapshot.py` | K0-010 input snapshot DRAFT; NOT_FROZEN / NOT_APPROVED, runtime disabled. | 358 |
| `src/jarvis/structured_planner.py` | K1-005 structured planner DRAFT; NOT_FROZEN / NOT_APPROVED. | 249 |
| `src/jarvis/test_runner.py` | K4-006 DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 376 |
| `src/jarvis/version_manager.py` | K4-003 Version Manager DRAFT; NOT_FROZEN / NOT_APPROVED. | 463 |
| `src/jarvis/worker.py` | K0-007 Worker DRAFT; NOT_FROZEN / NOT_APPROVED, runtime_enabled=False. | 165 |
| `src/jarvis/working_memory.py` | K2-002 working-memory DRAFT; NOT_APPROVED / NOT_FROZEN. | 146 |

## 客户端

| 位置 | 说明 |
|---|---|
| `apps/mac/` | macOS 菜单栏 App（SwiftUI + AVAudioEngine；UtteranceGate 负责一句话切分） |
| `apps/ios/` | iPhone App（球体界面；SpeechGate + 引擎内播放） |
| `apps/watchos/` | Apple Watch App（球体、令牌导入） |
| `apps/wakeword/` | 离线唤醒词模型训练与推理 |
| `apps/web/` | 本机 Web 面板（仅 loopback） |

语音子系统：`voice/` 下 10 个模块、12 个测试文件（独立运行：`PYTHONPATH=voice:packaging pytest voice/tests -q`）。
运行时模块合计：`packaging/` 下 193 个 .py（含测试）。

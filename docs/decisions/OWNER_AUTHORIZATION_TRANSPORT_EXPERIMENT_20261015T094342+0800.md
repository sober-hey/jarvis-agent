# Sir 批准独立传输进程隔离实验

登记时间：2026-10-15T09:43:42+08:00（本机真实时间）。
source=当前桌面clarify本人确认。
授权人：Sir。状态：`OWNER_APPROVED_ISOLATED_EXPERIMENT_ONLY / EXPERIMENT_DISPATCHED_NOT_VERIFIED`。

## 原问答

问：「是否批准按已落盘提案调整实验架构，将推理传输放进独立进程，仅用假密钥验证，不改正式产品或服务器？」

Sir 明确选择：「批准，仅开展隔离实验」。

本登记依据当前桌面 clarify 本人确认及主脑转交的原问答，仅机械记录明确批准，不推导额外授权。

## 提案引用与批准范围

原提案：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-architecture-decision-20261015/PROPOSAL.md`。
原提案 SHA-256：`000a4577478b87e05f919d390e6a6fc13544e66984e8c04bbadf69ecedb31f71`，登记前已重新计算一致。原件状态文字 `QUEUED_PROPOSAL_NOT_IMPLEMENTED` 与历史内容原样保留；本批准记录作为后继授权引用该哈希，提案现可作为此次隔离实验的实现输入。

仅批准在隔离实验目录内将推理传输放进独立受信任进程，使用合成假密钥、私有 socketpair、fresh exec 开展本地实验及测试；不得引入真实 provider 或网络。保留提案中的撤销、在途 `unknown/pending`、原 operation identity、无自动重试/无新 ID 补发、进程回收等候选验收要求，均须实际取证后判断。

## 未批准范围与保留门禁

未批准生产凭据读取或使用、外网服务、服务器安装或部署、正式产品凭据链改造、正式核心协议、全局权限/模型配置变更、发布，以及旧 cron/邮件/PTC/看门狗等自动化恢复。真实密钥、OS 隔离、持久化与 epoch 授权仍有各自门禁。

此次是经 Sir 明确确认的架构隔离实验。旧三轮风格修复上限保留，不追加旧样机第四轮修复，不应用旧 PEP 695 建议 diff，不改旧样机或既有源码，不借新版本回改旧样机；原失败及质量记录继续保留，不宣称旧样机全质量通过。

## 登记时状态与下一步

据主脑派工上下文，当前实验已派工，状态 `NOT_VERIFIED`；本登记未执行实验，未预填测试或安全通过。后续须核验独立实验产物、测试及独立复验，并另行登记局部结论。

现有产品 key FD 链未修改；本轮未读取真实 secret。`PREPARATION_IN_PROGRESS / production NO`、Contract Pack `NOT_FROZEN`、DP/ADR/正式核心协议 `NOT_APPROVED`、历史 K 状态及原用户验收确认位、Linux/ECS 实机 `NOT_RUN` 保持。此次实验批准不提升原阶段验收，也不构成生产或正式凭据安全验收。

本次文件变更仅为本独立授权记录，以及在 `PROJECT_STATE.md` 和 `/Volumes/扩展硬盘/hermes/.hermes/state/JARVIS_v1.0.md` 顶部追加指针；既有源码、历史提案与历史段落保持原样。

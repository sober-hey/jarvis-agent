# JARVIS Decisions Required

- Plan: v1.1.2（旧 ADR 身份及未批准状态保留）
- Status: ACTIVE

## 决策状态

PROPOSED / PENDING_VALIDATION /
ACCEPTED / REJECTED / SUPERSEDED

## 当前决策

| ID | 内容 | 状态 | 阻塞任务 |
|---|---|---|---|
| ADR-0001 | flock + Worker Epoch | PROPOSED | K0-007 |
| ADR-0002 | Prepared → Intent → Publish | PROPOSED | K0-011 |
| ADR-0003 | 文件级快照一致性 | PROPOSED | K0-010 |
| ADR-0004 | macOS 原子不覆盖发布 | PENDING_VALIDATION | K0-012 |
| ADR-0005 | B2 冻结备份 | PROPOSED | K0-017 |

## 待确认事项

### DEC-001：项目实际状态

ACCEPTED（事实确认，不是架构审批）：原目录非 Git 仓库，无源码或测试。现已初始化 main，Python 3.12 与 uv 工程及真实质量门禁可用。证据见 docs/reports/K0-001_DEVELOPMENT_REPORT.md 和原始日志。

### DEC-002：完整 Master Plan

确认以下文件存在且包含完整 v1.1.1 正文：

`JARVIS 独立智能体系统工程开发总规划-JARVIS_Engineering_Master_Plan_v1.1.1.md`

ACCEPTED（文档完整性事实确认）：已完整读取正文至最后开发顺序，版本为 v1.1.1；原文件保留并与初始化哈希存档一致。解除文档完整性前置限制，不等于 Contract Pack 获批。

### DEC-003：文件系统能力

确认外接硬盘文件系统类型及其对以下能力的支持：

- 原子不覆盖创建。
- 硬链接。
- O_NOFOLLOW。
- 目录文件描述符操作。
- fsync 及目录同步。

### DEC-004：取消与发布的并发边界

必须定义取消请求提交与文件发布之间的
线性化规则，避免“取消已提交但新副作用仍启动”。

### DEC-005：B2 备份策略

确认频率、保留周期、加密、存储位置和恢复审批。

### DEC-006：K1 可信审批

审批身份不得由模型输出或普通 CLI 参数冒充。

### DEC-007：K4 安全隔离

Git Worktree 不是安全沙盒。
必须验证独立执行隔离方案。

## K0-002 决定准备（2026-10-13）

具体备选见 docs/reports/K0-002_CORE_DECISION_PACKAGE.md（首版adc32091…，独立设计评审中，非可审批定稿）：DP-1 Guard/11条Step来源；DP-2 Plan状态；DP-3 Attempt状态；DP-4恢复证据/事件幂等；DP-5身份/Epoch/CAS；DP-6兼容；DP-7具名批准与冻结边界。全部NOT_APPROVED；D1/D2、各DOMAIN/RECOVERY未决项与ADR状态不变。已发现epoch全局比较、同键异载荷等疑点，先完成评审与返修再请求先生裁定，不接受AI自批。

### 当前复评状态（覆盖上方首版历史状态）

v2（3177cfaf… / 978行）历史复评REWORK_REQUIRED保留，精确对象已归档。当前v3为 `dc148e8f3aaa1ebd80a505ab7424e312b48accd5160e3ccb886d63f321359a9e` / 989行，提交7e095f3；`docs/reviews/K0-002-core-decision-package-review-v3.md`为REVIEW_PASS（限设计输入）。FC逐68边、S19取消来源、R-6条件化候选文本已复评；不等于产品INV009证明。真实AUTHOR_V3承接本轮差异，不补造缺失AUTHOR_V2。全部 DP 仍 NOT_APPROVED；整体包工程验收、其余具名决策与适用冻结边界尚需收口，未批准不得进入 K0-003。

### ADR 实施状态（2026-10-21 补记，与上句区分）

- ADR-0001..0005、0006、0007：状态不变（0006/0007 仍 DRAFT / NOT_APPROVED）。
- ADR-0008 语音合成改火山 seed-tts-2.0：**已实施**（代码在跑，参数与实测见该文件）。
- ADR-0009 一句话切分放客户端（UtteranceGate）：**已实施**，16 种说话方式仿真通过。
- ADR-0010 声纹识别与让路（本地 CAM++）：**已实施**，参考音为 Sir 真实录音。
- ADR-0011 结果未知默认拒绝新发送：**已实施**，产品链路显式放行并留痕。
- ADR-0012 上下文裁剪与三处测试语义变化：**改动已申报**，但 A6 验收未重跑，不得视为已验收。

即"ADR 仍全部 NOT_APPROVED"已不成立：0008–0012 是已落地的工程决策记录，不是待批提案。

## 决策规则

重大决策需要记录：

- 背景。
- 备选方案。
- 风险。
- 影响范围。
- 批准人。
- 验证证据。

PROPOSED 不代表已获人工批准。

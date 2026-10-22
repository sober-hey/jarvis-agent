# CR-01：Hermes 产品路线与开发顺序裁决

- 记录时间：2026-10-14（CST）
- 裁决来源：当前会话主脑转录的 Sir 授权与路线裁决；执行代理仅机械登记，不是 JARVIS 自批。
- 授权依据：`/Volumes/扩展硬盘/jarvis/docs/decisions/OWNER_AUTHORIZATION_20261014_HERMES.md`
- 状态：`OWNER_DELEGATED_SCHEDULING_DECISION_RECORDED / IMPLEMENTATION_NOT_VERIFIED`

## 1. 范围与保留边界

Sir 当前授权 Hermes 负责完整 JARVIS v1.0 的阶段安排与执行代理编排，不以 P2 样机止步。旧 Goal、Master Plan、原 63 项任务、旧证据、旧状态、安全目标、合同和 ADR 均保留；本裁决不改写其原文，不把 P 阶段进展自动计为旧 K 阶段验收，也不把路线登记写成产品完成、契约批准或生产许可。

- 新 P0-P6 是并行工程里程碑候选，替代旧的“K0 完成后才开始任何 K1 准备”的调度顺序。原 K 任务验收仍逐项遵守适用契约和发布门禁；P 阶段允许跨原阶段准备与有界实现，不自动计 K 验收，涉及核心执行协议仍须相关契约批准。P 里程碑与 K 任务分开计数、分开验收，任何旧 K 状态不得因本文件自动升格。

## 2. 产品路线候选

- 交付目录采用独立方案：从干净 Hermes 源码派生，自有入口、配置和数据升级；不依赖当前 Hermes 安装或复制 dirty 活跃安装、整个 home。
- 产品形态候选为中文 Web 入口，同时保留旧 CLI 入口。
- 固定源码候选：clean commit `d97007c32d6340f7ba79ac1dfb5773cc00846448`。先核对资源、许可和依赖；兼容性与安全评审完成前称“候选”，不称实现。
- 两进程候选：控制服务保留 Python 3.12；派生引擎独立 Python 3.14 候选；通过窄 RPC 连接。两端分别锁定和验证依赖，不替换系统 Python，不共享写库或 secret。该结构仍待兼容性与安全评审。

## 3. 不变的安全与治理门禁

安全局部契约及适用冻结、Intent 先于副作用、未知先核对、取消接纳点、唯一执行权、独立审查与独立验收、人工生产发布等要求继续有效。当前 Contract Pack 仍 `NOT_FROZEN`；不批准全部 DP/ADR，不授权未知核心执行协议。准备工作可以先行，但不得越过适用门禁。

## 4. 服务器与环境边界

本轮远端数值取自真实本轮 delegation transcript `/Volumes/扩展硬盘/hermes/.hermes/cache/delegation/live/deleg_39fe2529/task-1.log` 的只读 SSH 执行记录；主脑裁决负责路线决定。以下三个旧报告仅作工程背景，不是本轮 SSH 取证依据，不用来佐证今天的远端数字：

- `docs/reports/K0-002_DELEGATION_RUNTIME_AUDIT.md`
- `docs/reports/JARVIS_HANDOVER_20261013.md`
- `docs/reports/JARVIS_INDEPENDENT_TECHNICAL_REVIEW_2026-10-13.md`

本轮 transcript 报告口径：腾讯云 SSH 只读调查得到根盘约 89%、余约 5.5 GiB，2 vCPU、约 1.9 GiB 内存、swap 已用约 1.2 GiB；无 Docker/Podman，非 root namespace 失败，外卖服务活跃。当前不在腾讯云创建用户或目录、不安装运行时、不部署、不清理外卖资源、不改服务。开发先在本机项目隔离；独立 Linux 环境路径待选；阿里云原生产验证未被替代。

## 5. 模型、费用与材料边界

首次产品模型真调用尚未执行，基线测试待跑。当前接口已获使用授权，研发测试无金额上限；限制无效重试，记录 usage；不改模型站，不恢复旧监控 cron，不自动充值或扩大生产权限。当前 Sir 材料需求为无；内部可操作样机通过内部验收后再请求真实资料与体验反馈。连接器和异地备份另批。

## 6. 当前登记与下一步

本文件只登记路线和调度裁决。下一步为基线校验与持久 executor 核验；未启动事项保持 `NOT_VERIFIED`。任何实现、兼容性结论、安全结论、验收结论和生产发布均须另有真实证据及对应批准。

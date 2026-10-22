# Owner Decision: Credential Broker Specification Rework V2

- 日期：2026-10-15
- 决策人：Sir
- 对象：`CRED-BROKER-SPEC-REWORK-V2-20261015`
- 状态：`DESIGN_BASELINE_APPROVED_NOT_IMPLEMENTED`
- 新输出目录：`/Volumes/扩展硬盘/jarvis/.runs_01/credential-broker-spec-rework-v2-20261015/`

## 批准范围

只批准第二轮规格返工和该新目录内的机器自校验、条款映射、负控夹具、源文件读回哈希与报告生成。批准的是设计基线修订，不是产品实现、正式合同冻结或运行时安全验收。

本轮必须机器化闭合上一轮独立复审指出的九个阻塞缺口：完整协议根 schema；approval 与 `formal_acceptance`/scope 状态互斥；固定十三项预算及单位、计数点、缺项、重复项和零负规则；真实入口重复键与非法 UTF-8 负控登记；QUERY/DISPATCH 分离及 response 组合互斥；issuer/peer 绑定未来认证通道字段；control.db、故障结果、迟到结果与 reconciliation owner 机器化登记；正式拓扑单图与 credential 可见者；send scope/route/network/OS 继续保持阻塞状态。

## 明确不批准

- 不实现或接线任何产品、broker、transport、worker、provider 或 control.db。
- 不读取、生成、注入、传递或测试真实 credential/key。
- 不调用模型，不访问 provider，不联网，不执行真实网络、服务器、Linux/OS 隔离或跨重启验收。
- 不修改旧规格、旧复审、旧 contracts、state、product、config、vendor、runtime 或服务器。
- `formal_acceptance` 必须为 `null`；`implementation_status` 必须为 `NOT_IMPLEMENTED`。
- send scope、route、API、redirect、proxy、host/DNS/certificate、provider/version/rotation/revocation、OS 隔离的值继续为 `null`、`NOT_APPROVED` 或 `NOT_RUN`，不得填真实数值、网络值或批准人。

## 验收口径

v2 校验通过只证明新规格、schema、矩阵和负控自洽；不证明实现、凭据保密、认证通道已存在、control.db 已部署、正式拓扑已运行或生产可用。报告必须逐条保留原 CBR-001 至 CBR-022，并把九个阻塞缺口分别绑定到机器断言和未实现边界。

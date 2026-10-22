# 逐契约兼容性与变更策略候选登记

状态：DRAFT / NOT_APPROVED / NOT_IMPLEMENTED。基线HEAD 22746ae。本文件不是正式兼容策略，不修改契约、代码、ADR或审批状态，不关闭R16；仅提供待评审的登记表与方向性候选判据。

## 1. 依据与版本边界

- MP §30.1 L3043–3054：10项机器契约＋2份协议文档。仓库contracts目录实际12个机器文件（6 JSON＋6 YAML）；database_constraints.yaml、tool_manifest.schema.json不在该逐项清单，但contracts/README.md范围含二者。Pack范围与§30.1清单不得混为一谈，也不因未列名而删除或降级额外文件。
- MP §30.2 L3058–3066：规则1版本、2/3正反例、4自动验证、5明确兼容策略、6实现、7可追溯变更记录。MP L496另要求数据库和契约明确版本兼容策略；不据此虚构具体窗口。
- 12个机器文件顶层schema_version均为1；6个JSON在properties.schema_version中还有const:1。Pack版本1.0-draft与实例版本是不同层，不代表兼容承诺、审批或冻结。
- 协议文档没有自身schema_version声明：规则1需按文档类型确定版本化方式，不能从缺该字段推导规则5/7不适用。协议行为变更仍需评估兼容影响与可追溯记录。
- 当前尚未建立逐契约明确兼容策略；Git已有文件历史与局部报告，不能说变更记录全无。仍缺逐契约关联变更原因、兼容方向、受影响读写者、批准范围及复验任务的登记。此判定依据R16与实际文件内容，不以单个关键词零命中证明不存在。

## 2. 文件逐项现状（静态核对，非本轮测试执行）

表内“测试”只登记已有验证对象，不表示覆盖全部执行语义；“实现”排除测试解析器与候选SQL夹具的产品能力。各机器文件策略均待明确，变更记录均有Git历史、逐份治理登记待补。

| 文件（contracts/，协议另列docs/） | 来源类别 | 自身版本 | 正反例/验证对象 | 当前实现与缺口 |
|---|---|---|---|---|
| task_spec.schema.json | §30.1机器 | 1，JSON L3/const L7 | examples/valid_task.json、invalid_task.json；test_contract_baseline.py | 无产品请求验证执行链；策略待明确 |
| task_states.yaml | §30.1机器 | 1，L1 | baseline、test_domain_states.py；无独立实例正反例包 | src/jarvis/domain/states.py枚举投影；状态机未实现 |
| step_states.yaml | §30.1机器 | 1，L1 | baseline、domain_states；无独立实例正反例包 | 同上，Step枚举投影 |
| task_transitions.yaml | §30.1机器 | 1，L1 | baseline结构/来源核对；无逐边Guard正反例 | Guard仍占位，转换实现缺 |
| step_transitions.yaml | §30.1机器 | 1，L1 | baseline结构核对；无逐边Guard正反例 | Guard占位、11条来源未决 |
| execution_invariants.yaml | §30.1机器 | 1，L1 | baseline声明/meta验证 | 18条INV为NOT_IMPLEMENTED，非可执行判定器 |
| operation_intent.schema.json | §30.1机器 | 1，JSON L3/const L7 | baseline、test_schema_examples.py、domain_states | Intent状态枚举投影；其余字段/执行语义缺 |
| file_write_intent.schema.json | §30.1机器 | 1，JSON L3/const L7 | baseline、schema_examples正反例 | 发布执行链未实现 |
| reconciliation.schema.json | §30.1机器 | 1，JSON L3/const L7 | baseline、schema_examples正反例 | 恢复判定器未实现；配对/重试仍未决 |
| snapshot_manifest.schema.json | §30.1机器 | 1，JSON L3/const L7 | baseline、schema_examples正反例 | 快照执行链未实现；映射未决 |
| database_constraints.yaml | 在盘额外/README范围 | 1，L1 | baseline；test_database_contract_fixture.py候选SQL、busy/WAL探针 | 非产品数据库/迁移，不关闭R04/R05 |
| tool_manifest.schema.json | 在盘额外/README范围 | 1，JSON L3/const L7 | baseline、schema_examples正反例 | 工具调用/策略引擎未实现 |
| docs/design/FILESYSTEM_PROTOCOL.md | §30.1协议 | DRAFT，无自身schema_version | 独立设计报告；平台探针仅局部 | 行为与版本策略待定，不因无Schema版本免除变更追踪 |
| docs/design/RECOVERY_PROTOCOL.md | §30.1协议 | DRAFT，无自身schema_version | 独立设计报告，非恢复运行测试 | 恢复/重试语义未实现；同上 |

12个机器文件＝10个§30.1文件＋2额外；§30.1的12行＝10机器＋2协议，两个“12”不是同一集合。是否扩充正式清单、协议版本化形式及冻结判定仍待审定。

## 3. 方向性候选判据与反例

定义候选方向：B＝所有旧合法载荷是否被新读者接受；F＝所有新合法载荷是否被旧读者接受。实例通过只证明该实例，不证明集合包含；一个拒绝反例足以否定对应包含关系。读者Schema接受不等于业务、授权或状态机行为兼容。

以下均是假定其余约束不变、版本标签不变的结构变更分析，不是真实契约变更或已运行的升级。示例A/B为合成枚举值，note为合成字段，不新增正式字段/状态。

| 候选变更 | B方向/反例 | F方向/反例 | 机制与限制 |
|---|---|---|---|
| 新增可选note，旧读者additionalProperties:false | 旧{state:A}仍接受 | 新{state:A,note:x}被旧拒，F失败 | 仅未知字段导致拒绝；新写者未写note的实例仍可通过 |
| 扩展enum A→A/B | 旧A仍接受 | 新B被旧enum拒，F失败 | 原因是enum，不是additionalProperties |
| 删除enum B或收紧上限/pattern | 被删除值/越新上限的旧合法例拒，B失败 | 若仅收紧则新合法集合可为旧集合子集 | 边界有真实旧合法例才成立；语义影响另审 |
| 新增必填note，旧读者禁止未知字段 | 旧{state:A}缺note，新读者拒 | 新载荷含note，被旧未知字段拒 | B由required；F由旧additionalProperties:false；若字段早已可选则不能套用F结论 |
| 减少必填或扩大上限 | 若仅放宽则旧合法例仍接受 | 缺旧必填/超旧上限的新例被旧拒，F失败 | 不是未知字段的共同机制；需实际边界反例 |
| Schema不变但行为含义改变 | 结构可能接受，行为兼容未知 | 同左 | 需状态/Guard/权限与恢复证据，不能以Schema绿判语义兼容 |
| JSON实例schema_version const1→const2 | 旧版本1被新const拒 | 新版本2被旧const拒 | 与以上结构变更独立；版本分派/转换器尚不存在，不承诺自动迁移 |

依据：主代理合成JSON Schema探针演示新增可选/扩大枚举的方向差异与新增必填反例；独立核查指出新增必填的F失败由未知字段约束引起。其余行仅文本候选分析，未实跑、非通用兼容算法。JSON与YAML的机制不得直接互套；状态/转换/INV/数据库连接参数变更需独立语义分析，特别Guard未定前不能证明兼容。

## 4. 候选变更登记模板与未决事项

候选记录应包含：具名文件及旧/新哈希、Schema/Pack版本、变更原因、读写方向与反例、受影响任务/协议/不变量、数据转换需求、测试和独立报告、批准/未批准状态、回滚限制。Git提交只证明字节历史，不自动证明上述语义已审定。

未决：策略须写入每份契约还是外部索引；支持窗口与读写者组合；旧数据升级/版本拒绝行为；额外两文件的正式清单归属；协议文档版本化；正式策略审批与R16收口。数据库迁移/备份恢复/服务升级由对应任务复验，应用回滚不推出新数据库可被旧代码读取（MP L4228）。

本文件仅为K0-002设计提案，规则5/7仍未逐契约闭合，不改R16 PARTIAL，不批准D1/D2或ADR，不实现迁移、不修改不变量、不启用后续模块。作者与主代理完成收口后停写，独立报告必须绑定最终文件哈希。

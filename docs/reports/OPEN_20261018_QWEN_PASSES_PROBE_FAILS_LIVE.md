# 测量记录：qwen-plus 通过隔离探针却在真实常驻运行中被全数拒绝

- 记录时间：2026-10-18 07:15 CST
- 状态：`OPEN` / 原因未定
- 影响：备用路由不可用；主路由欠费期间 JARVIS 无常驻认知
- 相关：`docs/adr/ADR-0007-provider-failover-and-degraded-operation.md`、`scripts/route_compliance_probe.py`、`scripts/export_mind_stats.py`

## 已确证的事实

| 路线 | 隔离探针（`route_compliance_probe.py`，用产品校验器 `inner_runtime.validate`） | 真实常驻运行 |
|---|---:|---:|
| gpt-6.1-sol-official（主） | 未测（402 欠费） | 82 响应 / 1 被拒 = **1.2%** |
| qwen-plus | 26 次调用 **26 通过 = 100%** | 4 响应 / 4 被拒 = **100%**，零念头 |
| deepseek-v4.1-flash | 18 次调用 13 通过 = 72% | 40 响应 / 32 被拒 = **80%** |

- qwen-plus 的真实运行：`.runs_01/resident-continuity-routec-20261018/`，199 秒，0 条被接受念头。
- deepseek 的真实运行：`.runs_01/resident-continuity-routeb-20261018/`，1441 秒，29 条被接受念头。
- 主路由的 `mind-stats-before-stop.json` 显示：82 响应里 44 条已接受念头带 `["t1"]` 这类本地引用，唯一那次被拒的响应不带本地引用。**说明"引用自己较早念头"本身合法。**

## 已排除的解释

**"同一响应内引用较早念头被拒"——不成立。** 直接验证：

- 代码路径 `packaging/inner_runtime.py:274-275`：每验证完一条念头，就把它的 id 加入 `known`，所以后面的念头引用前面的是允许的。
- 实跑构造：`t2` 引用 `t1` → 通过；`t1` 引用 `t3`（序号更大、尚未出现）→ 被拒 `Invalid thought or provenance`。方向性符合设计。
- 真实数据也支持：主路由 44 条带 `t1` 引用的念头全部通过。

**"冷启动导致自我锁死"——同样不成立**，理由同上：合法性不依赖历史状态。

## 仍未解释的差异

同一套校验器，隔离探针下 qwen 26/26 通过，真实运行 4/4 被拒。差异只能在**输入**里，而不在校验器：

真实上下文比探针富得多，至少包括：

- 完整的 `INSTRUCTIONS + CURIOSITY_INSTRUCTIONS`（探针只用了简化版）；
- 真实信念（`perception:…` 这类 id）、`relevance_candidates`、`cached_relevance`；
- `events`（含真实时间戳）、`memories`、`recalled`、`unresolved_questions`、`unbound_questions`；
- 随后还有一步 `curiosity.validate_matrix`（校验 relevance 是否覆盖全部合格信念×能力），它在 `validate` 之后单独抛错。

被拒的 4 条响应在 `keys` 层面都是完整的七个键，所以失败发生在字段级或矩阵级，不是信封级。

## 为什么现在无法定论

`packaging/soak/run_soak.py` 停机时会删除整个临时数据根，`mind.db` 一并消失。route B / route C 停机后已无原始响应可供回放，`validation_error` 条目本身也只记 `call_id`，不记异常信息。**这是本轮的方法论失误**：应该先导出再停。

已补工具：`scripts/export_mind_stats.py`（停机前导出，含被拒样本的本地引用分布）。但它目前不记录异常消息，下一步要补——那才是定论所需的一条信息。

## 下一步（按成本从低到高）

1. 给 `validation_error` 的写入点补上异常类型与消息（改 `packaging/inner_runtime.py:915-921`，需等冻结解除）。
2. 或者：不改产品，在 `export_mind_stats.py` 里用同一条响应 + 当轮 context 回放 `validate` 与 `validate_matrix`，把异常消息抓出来。`model_call` 条目里存了 context，可行。
3. 再跑一次 qwen 短常驻（10 分钟），停机前导出，用第 2 条的办法定位。

在拿到异常消息之前，任何"根因"说法都是猜测。本文件只登记现象与已排除项，不写结论。

## 对路由准入的影响

隔离探针**必要但不充分**，已用两条路线证实：deepseek 探针 72% 而真实 20% 通过；qwen 探针 100% 而真实 0% 通过。

真实准入必须跑带完整上下文的短常驻，并停机前导出统计。探针只能用来筛掉明显不合格的候选，不能用来放行。

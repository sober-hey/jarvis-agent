# Disposable JARVIS soak harness

`run_soak.py` 默认运行 **24 小时墙钟时间**，不是默认执行真实模型。必须给一个尚不存在的证据目录；不会覆盖旧目录。只在明确 `--mode real` 时使用真实路由。本轮验收只用假模型。

## 命令

在仓库根目录用项目 Python 3.12 执行：

```sh
# 3 分钟墙钟，调度时钟加速 10 倍；约等于 30 分钟逻辑调度，不等于 30 分钟真实稳定性。
.venv/bin/python -B packaging/soak/run_soak.py \
  --mode fake --duration-seconds 180 --speed 10 --inject-faults \
  --evidence-dir /absolute/fresh/soak-evidence

# 未来经授权才执行：默认 24 小时、真实时钟；总调用保护默认读取产品日预算。
# 当前来源是 daemon_wiring.MODEL_CALLS_PER_DAY；省略上限参数，避免复制数字。
# 路由配置原样传给现有产品 launcher，不解析宿主配置，不切供应商、不读取聊天密钥。
.venv/bin/python -B packaging/soak/run_soak.py \
  --mode real --config /absolute/private/product-config.json \
  --key-file /absolute/private/product-key \
  --evidence-dir /absolute/fresh/real-soak-evidence

# 独立重分析：输出目录也必须尚不存在。
.venv/bin/python -B packaging/soak/analyze_soak.py \
  /absolute/evidence/samples.jsonl --output-dir /absolute/fresh/reanalysis

# 快速离线分析器测试，以及可选的完整 3 分钟进程自检。
.venv/bin/python -B -m unittest discover -s packaging/soak -p 'test*.py' -v
JARVIS_SOAK_INTEGRATION=1 .venv/bin/python -B -m unittest \
  discover -s packaging/soak -p 'test*.py' -v
.venv/bin/ruff check --no-cache packaging/soak
.venv/bin/pyright --pythonpath .venv/bin/python packaging/soak
```

`--duration-seconds` 始终为墙钟秒。`--speed` 只加速假模型模式的 daemon 调度时钟，真实模式要求 1。故障开关 `--inject-faults` 在默认时长的 1/3、2/3 处分别杀 daemon、web；可以用 `--kill-daemon-at`、`--kill-web-at` 指定墙钟秒。假模型还在起始 15 秒连续失败，可用 `--fail-model-at`、`--fail-model-seconds` 调整。短于恢复时间的测试会如实失败，不自动缩短故障。

## 隔离与执行边界

1. 只复制 `packaging/`、`apps/`、固定 vendor `components/agent_engine/upstream/` 到新的 `/tmp/jarvis-soak-snapshot-*`，含产品独立 venv；不复制 Git、历史证据、宿主配置或真实用户数据库。哈希覆盖副本所有文件。复制不是并行源码编辑的原子事务；以本轮冻结哈希为准。
2. 启动器复制后将运行器重新启动到副本中，后续协调、supervisor、web、daemon、分析都来自副本。开发解释器仍用启动时选定的 Python；真实模型子进程用副本中的产品 Python 3.14，保留原 launcher 的隔离参数及凭据 FD。
3. 数据根为新建 0700 `/tmp/jarvis-acceptance-soak-*/data`。macOS 上规范路径显示 `/private/tmp`。运行器从不读取真实 key 内容；真实模式只把既有绝对 key-file 引用交产品 launcher。假模式拒绝真实 config/key 参数，只生成 `FAKE-` 非秘密文件供 web 初始化。
4. 通过真实 `default_components()` 和公开 supervisor CLI，不传自制组件清单；保留组件名称、健康、重启和停止策略。daemon 入口增加假叶子/真实总预算包装。web 仍使用真实 HTTP/static 服务，但拒绝全部 POST 并禁用它的模型写入，防止浏览器访问绕过总预算；因此不是完整网页聊天验收。
5. 假模式使用真实调度、SQLite、消费与重试链；假感官每个逻辑 15 秒产生一条合成事件并保留产品重放样本。daemon 审计钩子拒绝网络 connect；真实模型/读 key 的入口有失败哨兵。无真实感官输入，无外部消息发送。
6. 真实总上限以 `daemon.db` 的持久发送预约实现，跨重启、跨日有效，耗尽后拒绝新调用。`--max-model-calls` 默认在解析参数时只读导入 `daemon_wiring.MODEL_CALLS_PER_DAY`，和常驻进程日预算同源；可显式覆盖，代码没有另存数字副本。产品预算按自然日重算，soak 总上限不换日重置，两道闸的周期不同；超过 24 小时的运行仍受一次总上限约束。供应商结果不确定、异常和 SIGKILL 中断保留 `unknown`，不伪记 failed；产品自己的日限额/unknown 门禁仍可能先阻止。此上限是 launcher 调用/发送预约，不是 token 或供应商账单上限；引擎内部请求数量没有在本轮实测。

## 固定采样

每 **5 个墙钟秒**采一行，并在结束前补最后一行。此间隔能覆盖短重启/降级窗口，同时将 24 小时常规采样约束在 17,281 行；不随虚拟时钟加速。`ps`/`lsof`/只读数据库查询有超时；失败值为 `null`，不当作零。

- web/daemon PID、重启计数、组件 state、认知 heartbeat healthy/degraded；daemon 心跳校验 PID、新鲜度及 supervisor 活态。
- 每组件主 PID 的 RSS、CPU 百分比、数字打开文件描述符计数。不是所有模型后代的资源总和；`ps` 的 CPU 是操作系统口径，不是本采样周期差值。
- 每个 SQLite DB/WAL/SHM 的文件字节数；嵌套文件名仅保存哈希标签。
- inbox 入队、交付、上游待交付；mind 感官入队、唯一消费、搁置、待处理；已交付 inbox ID 缺少 mind 对应项的数量；重复投递/消费数。
- 念头、调度 action 意图、未解决 ask_sir/pending_sir_permission 队列、主动 messages 行数。action 是执行尝试，不宣称执行成功；messages 不是实际 Sir 收到数量。
- 模型 completed/failed/dispatched_unknown，失败率分母为 success+failed，unknown 单列。

采样不含感知 payload、坐标、健康测量值、转写、网址、念头或消息正文；SQL 只读出关联 ID/状态以在内存对账，证据只写数字。原始产品日志、模型配置及临时数据库均留在待删除数据父目录，不复制到证据。

## 分析规则

- 内存：保留连续 PID+重启代分段全窗 OLS（十进制 MB/墙钟小时）和旧三均值诊断，但短窗正斜率不再决定 ISSUE。每段以首个样本为起点，按 300 墙钟秒窗口报告 RSS 均值、最大值、样本数和覆盖缺口；分类斜率去掉该段前 300 秒预热。至少 1800 墙钟秒、六个完整窗口（五个在预热后）、每完整窗至少 30 点、最大缺口不超过 15 秒才有资格定性。持续线性增长需显著拟合增量、R²≥0.95、低残差、均值/最大值窗增量与末 900 秒斜率一致，标记 `sustained_linear_growth` / ISSUE，仍不是泄漏诊断。末 900 秒低漂移、低波动且没有明显回落或平台后再增长，才标记 `rise_then_plateau` / OBSERVED_OK（从头平稳用 `rise_observed=false` 明示）；其余为 `uncertain` / UNKNOWN。具体阈值与失败条件写入 SUMMARY，不把预热、慢增长或未来稳定性当作已证明。
- 重启：3 次及以上为反复重启；1 次预设 SIGKILL 不叫反复。记录 `circuit_open`/fused 状态。并不自动把异常归因于注入。
- 心跳：用左侧样本状态覆盖两个墙钟采样时刻间隔，不在末尾外推；因此降级时长精度受 5 秒采样影响。缺失为 UNKNOWN。
- 对账：分别检查 `enqueued=delivered+upstream_pending`、`mind_enqueued=consumed+suspended+pending`；并检查已交付 inbox ID 是否在 mind 关联集合。两数据库非原子联合快照可能短暂不一致，残差是待调查证据，不直接认定丢失。跨阶段数量不可粗暴相等（历史摘要可能聚合）。
- 积压：至少 3 点且待处理数 OLS 正斜率、终值大于初值；有待处理不等于持续积压。重复用稳定 ID 对账。
- 结论 `ISSUE` / `UNKNOWN` / `OBSERVED_OK` 仅表示数据窗口；故障注入造成的失败、降级也原样输出 ISSUE，不为了自检绿灯抹掉。

## 停止与已知边界

到时或收到 SIGTERM/SIGINT 转给 supervisor；验证退出码、全部记录组件 PID、带快照/数据路径的残留进程、监听端口、最终 stopped，前后副本哈希一致后删除快照和数据根。若仍有活进程则保留根目录并在 RESULTS 中报失败，避免删掉进程仍使用的数据；由人按记录处理。宿主或协调器 SIGKILL、断电不保证清理。进程表看不到从未被观察且已逃逸重归属的后代，不等于 cgroups。

假模型会受产品每日念头/调用预算约束；即使设 24 小时也不代表持续 24 小时都有认知调用。真实模式代码路径本轮未发起模型请求；没有真实 24 小时、真实在途模型 SIGKILL、Linux 或生产环境验收，没有安装 launchd/systemd。

# 长驻 Agent 可靠性：工业界做法 + JARVIS 修复方案

> 目标：把今天暴露的三个真实故障（SQLite 慢查询吃满 CPU、flock 锁竞争导致语音等 20s 失败、语音/后台无优先级）落到可执行的改动上。
> 约定：文中所有**数值**只有两类 —— 「官方默认值」（标 A 级证据）和「我们建议取的值」（明确标为建议，非行业标准）。不编造基准数字；需要数字的地方一律给「怎么量」。

---

## 0. TL;DR（先看这三条）

1. **慢查询不是 SQLite 的锅，是查询计划 + 缺索引 + 没有执行预算**。231,780 行对 SQLite 是小数据（SQLite 官方定位是 TB 级单库），跑几分钟说明在反复全表扫描（相关子查询按行重算）。三件事必须同时做：改写成 JOIN/窗口函数、建对索引、给连接装 `set_progress_handler` 时间预算。
2. **flock 不该跨模型调用持有**。文件锁的语义是「保护临界区」，不是「保护一次长 I/O」。守护进程和语音共用一个数据根 + 一个 flock，等于把「后台跑 90 秒模型」和「用户说一句话」串成一条队列。正确做法是锁分层 + 租约（lease）替代长持有。
3. **优先级要靠调度器实现，不能靠「先来先服务 + 运气」**。语音通道必须有独立的锁命名空间、独立的并发额度，并且后台任务在 await 点可被抢占（checkpoint 后让出）。

---

## 1. SQLite：数十万行、单机嵌入式的生产实践

### 1.1 连接级配置（每次开连接都要设，PRAGMA 大多是连接级的）

| PRAGMA | 官方默认 | 我们建议 | 说明 |
|---|---|---|---|
| `journal_mode=WAL` | `delete` | `WAL` | 持久化设置，设一次即长期生效；读写不互斥（读不阻塞写、写不阻塞读），但仍**只允许一个写者**。[A: sqlite.org/wal.html] |
| `synchronous` | `FULL`(2) | WAL 下 `NORMAL` | WAL + NORMAL：应用崩溃不丢已提交事务；**断电/硬重启可能丢最近若干事务**（不会损坏库）。可接受的前提是「记忆条目可重放」。[A: sqlite.org/pragma.html#synchronous] |
| `busy_timeout` | 无 busy handler，遇锁立即 `SQLITE_BUSY` | `5000`（交互连接）/ `30000`（批处理） | 只解决「瞬时锁」，不解决「长事务占着写锁」。[A: sqlite.org/c3ref/busy_timeout.html] |
| `wal_autocheckpoint` | 1000 页 | 保持 1000，或降到 500 换取更平滑的 checkpoint | 自动 checkpoint 都是 PASSIVE；**长读事务会让 checkpoint 停在原地、WAL 无限增长**（Litestream 正是靠长读事务实现 shadow WAL）。[A: pragma.html#wal_autocheckpoint；B: litestream.io/how-it-works] |
| `foreign_keys` | OFF | ON | 与性能无关，防脏数据。 |
| `cache_size` | -2000（约 2MB） | `-65536`（64MB） | 负值单位是 KiB。嵌入式单机给几十 MB 页缓存是便宜的。 |
| `temp_store` | 0 | `MEMORY` | 避免大排序落盘；代价是峰值 RSS。 |
| `mmap_size` | 0 | 视情况，`268435456`(256MB) | 只读多的时候收益明显；写多时收益有限。 |

**必须知道的两个 WAL 硬限制**（会直接咬人）：
- WAL 依赖 `-shm` 共享内存，**不能放在网络文件系统上**（NFS/SMB）。[A: wal.html]
- 一旦进入 WAL，**page_size 不能再改**（除非回到 rollback 模式）。[A: wal.html]

> 实测前提（本机，A 级）：`mind.db` 所在卷 `/Volumes/扩展硬盘` 是 **APFS / local / PCI-Express NVMe SSD**（`diskutil info` 实测）。所以 WAL 可用，不存在网络盘问题。但要注意外置盘休眠/掉线会导致 `SQLITE_IOERR`，需要重连重试。

### 1.2 索引设计：表达式索引与 JSON 列

- **表达式索引必须与查询里的表达式逐字一致**，否则优化器不认。`json_extract(meta,'$.kind')` 建了索引，查询写 `json_extract(meta, '$.kind ')`（多空格）或用 `meta->>'$.kind'` 都可能匹配不上。[B: dba.stackexchange.com/questions/172617]
- 更稳的写法是**生成列 + 普通索引**：列名稳定、可读、可在 `EXPLAIN QUERY PLAN` 里直接看到，也不用担心表达式文本漂移。SQLite 3.31+ 支持 generated column，且**可以在 VIRTUAL 生成列上建索引**。

```sql
ALTER TABLE entries ADD COLUMN kind TEXT
  GENERATED ALWAYS AS (json_extract(meta,'$.kind')) VIRTUAL;
CREATE INDEX idx_entries_kind_ts ON entries(kind, created_at DESC);
```

- **相关子查询是本次事故的头号嫌疑**。SQLite 的相关子查询如果无法被 flatten（含聚合、LIMIT、DISTINCT 等），会对**外层每一行**执行一次内层查询；外层 23 万行 × 内层一次索引查找，就是几分钟的量级。判据看 `EXPLAIN QUERY PLAN` 输出里的 `CORRELATED SCALAR SUBQUERY`。[A: sqlite.org/optoverview.html]
- 改写三件套：
  1. 相关标量子查询（取「每个会话最后一条」）→ **窗口函数** `ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY created_at DESC)`；
  2. 相关聚合（`(SELECT count(*) ... WHERE e2.conv=e1.conv)`）→ **LEFT JOIN 聚合子查询**；
  3. 外层有等值过滤的，确认过滤列在索引**最左**（`WHERE conv=? ORDER BY ts DESC` → 索引 `(conv, ts DESC)`，否则会走 `USE TEMP B-TREE FOR ORDER BY`）。
- 覆盖索引（covering index）能省掉回表：把 SELECT 里需要的列放进索引尾部，`EXPLAIN QUERY PLAN` 会显示 `USING COVERING INDEX`。
- 定期 `ANALYZE`（或 `PRAGMA optimize`，SQLite 官方推荐在连接关闭前跑，做增量统计）。[A: pragma.html#optimize]

### 1.3 归档 / 分区 / VACUUM

- **单表 23 万行不需要分区**。真正需要的是「热表 + 冷归档」：把超过 N 天的 entries 搬到 `entries_archive`（同库不同表，或独立 `mind_archive.db` 用 `ATTACH`）。
- 删除必须**分批**，每批一个短事务（例如 5000 行 + `LIMIT`），否则一个 10 万行的 DELETE 会长时间持有写锁并撑爆 WAL。[A: wal.html「大事务」章节；B: sqlitetutorial.net/sqlite-vacuum]
- `VACUUM` 会重写整库、**需要约等于库大小的临时空间**、期间独占。策略：低峰期跑、跑前 `PRAGMA wal_checkpoint(TRUNCATE)`。
- 更优雅：`auto_vacuum=INCREMENTAL` + 定期 `PRAGMA incremental_vacuum(N)`。注意**从 none 切到 incremental 必须重建库（VACUUM）**，所以要早决定。[A: pragma.html#auto_vacuum]
- 观测：`PRAGMA freelist_count`（空闲页）、`page_count*page_size`（文件大小）、WAL 文件大小。WAL 持续增长 = 有长读事务或 checkpoint 被阻塞。

### 1.4 知名项目的可抄经验

- **Datasette（Simon Willison）**：把「查询必须有时限」做成产品级默认。实现方式是 `conn.set_progress_handler(handler, n)`，每 **1000 个 VM 指令**回调一次，超时返回非 0 → 查询被中断（`sqlite3.OperationalError: interrupted`）；时限 <50ms 时把 n 降到 1 提高精度。对外暴露为 `sql_time_limit_ms` 设置项（文档示例 3500ms）。[A/B: sqlite.org/forum/forumpost/f37ae374cc + docs.datasette.io/en/stable/settings.html]
  → **这是本次故障最直接的解药**：即使查询写错，也不会把 CPU 烧几分钟，而是 1 秒内报错并留下日志。
- **Litestream**：用「长读事务」把 WAL 钉住不被 checkpoint，从而稳定复制。反向教训：**任何长事务（读或写）都会让 checkpoint 停滞、WAL 膨胀**，所以我们的查询必须短。[B: litestream.io/how-it-works；litestream.io/guides/wal-truncate-threshold]
- **Signal Desktop**：把 schema 全量导出成 `DATABASE_SCHEMA.md` 纳入版本管理，用触发器维护冗余统计列（如备份字节数），迁移按版本号递增。可抄的是「schema 文档化 + 触发器维护冗余计数」，减少运行时的聚合查询。[B: github.com/signalapp/Signal-Desktop/blob/main/DATABASE_SCHEMA.md]

---

## 2. 单机 Python 守护进程：锁与优先级

### 2.1 flock 的真实语义（很多人踩的坑）

- `fcntl.flock` 是**建议锁（advisory）**，按 **open file description** 归属：同一进程 `dup` 出来的 fd 共享同一把锁；**不同 fd 各自 `open` 同一路径，在同一进程内也会互相阻塞**。
- `fork` 后子进程继承锁；`exec` 后仍持有（除非 `FD_CLOEXEC`）。
- **没有优先级、没有公平性、没有超时** —— 只有「阻塞」或 `LOCK_NB` 立即失败。所以「语音优先」不可能靠 flock 实现。
- 锁的粒度是 inode：换路径 = 换锁；同一路径的软链/不同挂载点可能绕开。

### 2.2 锁分层（核心修法）

把「一把大锁保护整个数据根」拆成三层：

| 层 | 保护对象 | 持有时间 | 实现 |
|---|---|---|---|
| L1 状态锁 | 任务状态、队列、租约表 | 毫秒级 | 短事务 + `busy_timeout`；或 `flock(LOCK_EX)` 只包住「读-改-写」 |
| L2 写者串行 | SQLite 写 | 单事务 | 靠 SQLite 自身（WAL 单写者），**不再叠一把文件锁** |
| L3 长调用 | 模型 API 调用 | 秒~分钟 | **不持锁**。改用「租约 + 心跳」：写一行 `lease(owner, expires_at)`，定期续期，别人可抢占过期租约 |

关键判据：**任何跨越网络 I/O 的代码路径里都不允许出现 `with flock(...)`**。

### 2.3 语音/交互优先于后台批处理

工业界通用做法，按落地成本从低到高：

1. **优先级队列**：`asyncio.PriorityQueue`，元素 `(priority, seq, payload)`，`seq` 单调递增保证同优先级 FIFO（避免比较 payload 报错）。语音 = 0，交互 = 10，后台 = 100。[A: docs.python.org/3/library/asyncio-queue.html]
2. **后台并发额度 = 1（或 0）**：后台批处理永远只跑一个，且只在「无语音待处理」时取下一个任务。
3. **抢占式让出（cooperative preemption）**：后台任务在每次 `await` 前后检查 `voice_pending.is_set()`，若为真 → 把进度写进 checkpoint → `return` 让出。asyncio 的取消（`Task.cancel()`）**只在 await 点生效**，同步阻塞调用（比如一次 `conn.execute` 慢查询）**根本收不到取消** —— 这正是要配 `set_progress_handler`/`conn.interrupt()` 的原因。
4. **Token bucket 限流**：后台每秒/每分钟最多 N 次模型调用；桶满才允许发起。语音不走这个桶。
5. **Deadline/SLO**：语音请求带 `deadline=2s`，调度器按 EDF（最早截止优先）排序；超时立即降级（回「我在，稍等」或本地快回复），不排队。
6. **老化（aging）**：后台任务等待超过 M 分钟提升一级优先级，防饿死。

### 2.4 读写分离

- 读连接池 + 写连接单例。WAL 下读连接可以并发，且不会阻塞写。
- 读连接设 `PRAGMA query_only=1`，防止后台任务误写。
- 长报表类查询走**独立只读连接 + 更短的 progress handler 预算**，避免污染交互路径的页缓存。

---

## 3. 长驻 Agent 的崩溃恢复与幂等

### 3.1 checkpoint / resume

最小可用模型（三张表就够）：

```sql
CREATE TABLE tasks(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  priority INTEGER NOT NULL,
  state TEXT NOT NULL,            -- queued|running|done|failed|dead
  idempotency_key TEXT UNIQUE,
  attempts INTEGER DEFAULT 0,
  lease_owner TEXT, lease_until REAL,
  checkpoint TEXT,                -- JSON：已完成到第几步
  created_at REAL, updated_at REAL
);
CREATE TABLE steps(task_id, step_no, status, output_digest, PRIMARY KEY(task_id, step_no));
CREATE TABLE outbox(id INTEGER PRIMARY KEY, topic, payload, sent_at);
```

- 启动时扫 `state='running' AND lease_until < now` → 视为崩溃遗留，重新入队（`attempts+1`）。
- 每完成一个 step 就提交一次（短事务），把「重放成本」限制在一个 step 内。
- `attempts > MAX` → `dead` + 告警，不无限重试。

### 3.2 exactly-once 副作用：outbox + 幂等键

- 严格 exactly-once 在分布式里不存在，工业界统一做法是 **at-least-once + 去重**：把「要发的外部副作用」和业务状态写在**同一个本地事务**里（outbox 表），由单独的 dispatcher 读取并投递，成功后标记 `sent_at`。[A/B: microservices.io/patterns/data/transactional-outbox.html]
- 外部调用带幂等键（Stripe 的做法：客户端生成 key，服务端按 key 缓存首次结果并复用）。[A: docs.stripe.com/api/idempotent_requests]
- 我们的场景：模型调用本身不幂等（会花钱、会写记忆），所以幂等键 = `hash(task_id, step_no, prompt_digest)`，落库前先查重。

### 3.3 分段重试

- 指数退避 + 抖动：`sleep = min(cap, base * 2**attempt) * random(0.5, 1.5)`。
- 只重试**可重试错误**（超时、429、连接重置）；4xx 参数错误直接 `failed`。
- 每次重试都从 `checkpoint` 恢复，不从头再跑。

---

## 4. 服务监督：systemd 与 launchd

### 4.1 systemd（Linux）

```ini
[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30
Restart=on-failure
RestartSec=2
StartLimitIntervalSec=60
StartLimitBurst=5
KillMode=control-group
TimeoutStopSec=20
MemoryMax=2G
TasksMax=512
```

- `WatchdogSec` + `sd_notify(WATCHDOG=1)` 是**唯一能发现「进程活着但事件循环卡死」的机制**；`Restart=on-failure` 只在进程退出时生效，抓不到死锁。[A: freedesktop.org/.../systemd.service.html；sd_notify(3)]
- **致命坑**：心跳必须由**事件循环本身**发（`loop.call_later` 定时器里发），不能在独立线程里发 —— 否则主循环卡死、心跳线程照发，看门狗形同虚设。
- `KillMode=control-group`（默认）在停止时杀整个 cgroup，能收掉子进程；`KillMode=process` 只杀主进程，**必然留孤儿**。孤儿治理的根本是 cgroup + `KillMode=control-group`，Linux 上再叠 `PR_SET_PDEATHSIG`（子进程侧）。
- `TimeoutStopSec` 内没退干净会被 SIGKILL：所以 SIGTERM handler 必须做「停止接新任务 → 等当前 step 落盘 → 退出」，且耗时要小于 `TimeoutStopSec`。

### 4.2 launchd（macOS，我们实际用的）

- `KeepAlive`（bool 或 dict）控制重启；`ThrottleInterval` 是**最小重启间隔**（防止崩溃循环打爆 CPU）；`ExitTimeOut` 是停止宽限；`ProcessType=Interactive` 影响调度优先级。[B: real-world-systems.com/docs/launchdPlist.1.html；A: man launchd.plist]
- **launchd 没有 `WatchdogSec`**。替代方案：
  1. 守护进程每 N 秒更新心跳文件（`~/.../heartbeat`，写 mtime）；
  2. 外部 cron/launchd 定时任务检查 `now - mtime > 3N` → `launchctl kickstart -k gui/$UID/<label>`；
  3. 心跳同样必须由事件循环驱动。
- `KeepAlive` 设为无条件 true 会在「正常退出」时也拉起，配合 `ThrottleInterval` 使用；若希望「只在异常退出时重启」，用 `KeepAlive={SuccessfulExit:false}`。
- 注意退出码语义：`launchd` 把某些退出码（如 78 = EX_CONFIG）视为配置错误并停止重启，这会造成「一次配置写错 → 服务永久躺平」。

---

## 5. 长跑内存/句柄泄漏：检测与上限

### 5.1 检测（先量，再改）

- **RSS 趋势**：每小时采样 `psutil.Process().memory_info().rss`，落一张 `metrics` 表；用最近 24h 的线性回归斜率判断「真泄漏」（单调上升）还是「高水位不降」（碎片/缓存）。`ru_maxrss` 只能看峰值，不能看趋势。
- **对象级定位**：`tracemalloc.start()` + 定时 `take_snapshot()`，比较两次快照的 `compare_to(...,'lineno')` 前 10 名。生产环境建议用采样模式（`tracemalloc.start(25)` 只存 25 帧）控制开销。
- **句柄**：macOS 用 `psutil.Process().num_fds()`（内部走 `proc_pidinfo`），Linux 直接数 `/proc/self/fd`。SQLite 连接、模型 HTTP 连接池、子进程管道是最常见的三类泄漏源。
- **SQLite 专项**：`PRAGMA page_count`、`freelist_count`、WAL 文件大小；`sqlite3` 连接未 close 会同时泄漏 fd 和内存。

### 5.2 上限（超限自我了断，比 OOM 好）

- **Linux**：cgroup v2 `memory.max`（硬限，超了 OOM-kill 整个 cgroup）+ `memory.high`（软限，超了先节流回收）。systemd 里就是 `MemoryMax=` / `MemoryHigh=`。
- **macOS**：**没有 cgroup**，且 `RLIMIT_AS` 在 Darwin 上对 malloc 的约束并不可靠（不能当硬限用）。现实做法是**应用内软上限 + 优雅重启**：RSS 超过阈值（例如基线的 1.5×）→ 落盘 checkpoint → `os.execv` 自重启或 `exit(75)` 交给 launchd 拉起。语音网关与守护进程分离部署时，重启守护进程不影响语音。
- **jemalloc / malloc_trim 的真实效果**：
  - `malloc_trim(0)` 是 **glibc 专有**，macOS 上不存在，别指望。
  - jemalloc 在 macOS 可通过 `DYLD_INSERT_LIBRARIES` 注入，`MALLOC_CONF` 调 `dirty_decay_ms` / `background_thread:true` 能把「释放后不还 OS」的脏页降下来。
  - 但它**只治碎片，不治泄漏**：真有对象泄漏，jemalloc 只会让曲线更平滑。所以顺序必须是「先 tracemalloc 证明是泄漏还是碎片」，再决定用不用。[B: man7.org/linux/man-pages/man3/malloc_trim.3.html；discuss.python.org 相关讨论]

---

## 6. 针对我们三个故障的具体修复方案

### 故障 A：mind.db 慢查询（相关子查询）烧 CPU 数分钟

| 项 | 内容 |
|---|---|
| **改哪里** | ① `mind.db` 的 schema 初始化处（建索引/生成列）；② 那条带相关子查询的 SQL 所在函数；③ 所有 SQLite 连接的创建函数（统一 PRAGMA + progress handler） |
| **改成什么** | 1) 统一连接初始化：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`cache_size=-65536`、`temp_store=MEMORY`、`foreign_keys=ON`；<br>2) 装查询预算：`conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)`，交互 1000ms、后台 5000ms；<br>3) 索引：`CREATE INDEX idx_entries_conv_ts ON entries(conversation_id, created_at DESC)`；JSON 过滤改成生成列 + 索引；<br>4) SQL 重写：相关标量子查询 → `ROW_NUMBER() OVER (PARTITION BY ...)`；相关聚合 → `LEFT JOIN (SELECT ... GROUP BY ...)`；<br>5) 加 `PRAGMA optimize` 在连接关闭前。 |
| **怎么验证** | ① `EXPLAIN QUERY PLAN` 前后对比：改前应能看到 `SCAN entries` / `CORRELATED SCALAR SUBQUERY`，改后必须是 `SEARCH entries USING INDEX idx_...`，且无 `USE TEMP B-TREE FOR ORDER BY`；<br>② 在 `mind.db` 的**副本**上跑 5 次取中位数（`sqlite3 .timer on`），记录改前/改后；<br>③ 注入测试：故意跑一条 `SELECT count(*) FROM entries a, entries b` 笛卡尔积，断言 progress handler 在预算 +100ms 内抛 `interrupted`；<br>④ 运行期观测：`ps -o %cpu` 采样，慢查询期间单核占用应从「接近 100% 持续数分钟」变为「毫秒级脉冲」；<br>⑤ 加一条断言：CI 里对核心查询跑 EQP，出现 `SCAN` 即失败。 |

### 故障 B：flock 锁竞争 —— 语音等 20s 拿不到锁

| 项 | 内容 |
|---|---|
| **改哪里** | `chat_launcher` 的数据根锁封装（那层 `with flock(...)`）；守护进程长模型调用的调用点；语音网关的取锁路径 |
| **改成什么** | 1) **删掉跨模型调用的 flock**：把 `with flock(): model_call()` 改成 `acquire_lease(owner, ttl=60s)` → `model_call()` → `release_lease()`；租约写进 `tasks.lease_owner/lease_until`，崩溃后靠 `lease_until` 过期自动回收；<br>2) **锁分层**：语音与后台用**不同锁文件**（`voice.lock` / `daemon.lock`），互不阻塞；只有真正共享的状态（同一 SQLite）交给 WAL + `busy_timeout` 串行化；<br>3) 若确实需要互斥，`flock(LOCK_EX|LOCK_NB)` + 短重试（10ms 起，指数退避，上限 2s），**超时立即降级**而不是死等 20s；<br>4) 所有取锁路径打印「等待时长」指标。 |
| **怎么验证** | ① 复现脚本：后台起一个 sleep 90s 的假模型调用占住原锁，同时发 20 次语音请求，改前应能复现「约 20s 后失败」，改后要求 **0 失败**；<br>② 打点统计 `lock_wait_ms` 的 p50/p95/max，目标 p95 < 200ms（我们的 SLO，非行业标准）；<br>③ 崩溃测试：`kill -9` 持锁进程，断言新进程在 `ttl` 内接管，且不会出现双写者（用 `lease_owner` 唯一约束 + 断言）；<br>④ 静态检查：CI 里 grep「flock 作用域内出现 `await`/`requests`/模型客户端调用」即失败。 |

### 故障 C：语音/后台没有优先级

| 项 | 内容 |
|---|---|
| **改哪里** | 守护进程的任务调度器（内心循环的取任务处）、模型调用的并发控制、语音网关的入队处 |
| **改成什么** | 1) 统一 `asyncio.PriorityQueue`，元组 `(priority, seq, task)`：语音 0、交互 10、后台 100；<br>2) 后台并发额度 = 1，且取任务前检查 `voice_pending`，为真则 `await asyncio.sleep(0)` 让出并重排；<br>3) 后台任务在每个 step 边界写 checkpoint，然后检查 `preempt_requested` → 保存并 `return`；<br>4) token bucket 限制后台模型调用速率（建议：每分钟 4 次，可配）；<br>5) 语音请求带 deadline（建议 2s），超时走降级回复，不排队；<br>6) aging：后台等待 > 10 分钟提升一级，防饿死。 |
| **怎么验证** | ① 并发压测：后台连续跑 50 个长任务的同时，每 2s 发一次语音，统计语音 p95 延迟与超时率；<br>② 抢占正确性：在后台任务执行中插入语音，断言后台在 ≤1 个 step 内让出，且 checkpoint 可完整恢复（重放后 step 不重复执行，用 `steps` 表计数断言）；<br>③ 饿死测试：持续语音压力 30 分钟，断言后台任务最终仍完成（aging 生效）；<br>④ 指标：语音队列等待时间直方图、后台任务 step 吞吐。 |

---

## 7. 落地顺序（按风险/收益）

1. **今天就能上**：连接统一 PRAGMA + `set_progress_handler` 时间预算（止住 CPU 空转，改动最小、可独立回滚）。
2. **今天到明天**：`EXPLAIN QUERY PLAN` 定位并重写那条 SQL + 补索引（真正解决根因）。
3. **本周**：拆锁（租约替代长持有 + 语音/后台独立锁命名空间）。
4. **本周**：优先级队列 + 后台并发额度 + 抢占。
5. **下周**：outbox/幂等键 + 崩溃恢复扫描；systemd/launchd 心跳与自愈；RSS 趋势监控 + 软上限自重启。

每步都保留「改前/改后」的 EQP 与压测数据，避免「感觉快了」。

---

## 8. 证据等级

| 级 | 含义 | 本文对应内容 |
|---|---|---|
| **A** | 官方文档/源码，直接读过 | SQLite WAL/PRAGMA/optoverview、systemd.service/sd_notify、Python asyncio/sqlite3、Stripe 幂等、launchd.plist man page、本机 `diskutil` 实测 |
| **B** | 一手项目文档或作者本人说明 | Datasette 的 progress handler 实现与 `sql_time_limit_ms`、Litestream 的 shadow WAL 说明、microservices.io outbox、Signal Desktop schema 文档、launchd 关键词整理 |
| **C** | 二手博客/社区问答/我们的推断 | 具体建议值（busy_timeout=5000、cache_size=64MB、token bucket=4/min、SLO p95<200ms 等）**均为我们的工程选择，不是行业标准**；「231,780 行 × 相关子查询 = 几分钟」是基于执行计划的推断，需用实测替换 |

> 未验证、因此**没有写进结论**的：任何具体性能提升倍数、任何具体项目的 pragma 取值（未读到其源码配置）、jemalloc 在 Python 长跑中的量化收益。

---

## 9. 来源

- SQLite WAL：https://www.sqlite.org/wal.html
- SQLite PRAGMA（synchronous / wal_autocheckpoint / auto_vacuum / optimize）：https://www.sqlite.org/pragma.html
- SQLite 查询优化器概述（子查询 flatten、索引选择）：https://www.sqlite.org/optoverview.html
- SQLite busy handler：https://www.sqlite.org/c3ref/busy_timeout.html
- SQLite VACUUM：https://www.sqlite.org/lang_vacuum.html
- SQLite 论坛「如何给查询加时间限制」（Simon Willison 回复，含 Datasette 实现）：https://sqlite.org/forum/forumpost/f37ae374cc
- Datasette 设置（`sql_time_limit_ms`）：https://docs.datasette.io/en/stable/settings.html
- Litestream 工作原理（shadow WAL / 长读事务）：https://litestream.io/how-it-works/
- Litestream WAL truncate 阈值：https://litestream.io/guides/wal-truncate-threshold/
- Signal Desktop schema 文档：https://github.com/signalapp/Signal-Desktop/blob/main/DATABASE_SCHEMA.md
- 表达式索引与 json_extract 匹配问题：https://dba.stackexchange.com/questions/172617
- Python asyncio 队列（PriorityQueue）：https://docs.python.org/3/library/asyncio-queue.html
- Python asyncio 任务取消语义：https://docs.python.org/3/library/asyncio-task.html
- Python sqlite3 `set_progress_handler` / `interrupt`：https://docs.python.org/3/library/sqlite3.html
- systemd.service（WatchdogSec / Restart / KillMode / TimeoutStopSec）：https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
- sd_notify(3)（WATCHDOG=1）：https://www.freedesktop.org/software/systemd/man/sd_notify.html
- launchd.plist 关键词（KeepAlive / ThrottleInterval / ExitTimeOut）：https://real-world-systems.com/docs/launchdPlist.1.html
- Transactional outbox：https://microservices.io/patterns/data/transactional-outbox.html
- Stripe 幂等请求：https://docs.stripe.com/api/idempotent_requests
- malloc_trim(3)（glibc 专有）：https://man7.org/linux/man-pages/man3/malloc_trim.3.html
- cgroup v2 内存控制：https://docs.kernel.org/admin-guide/cgroup-v2.html

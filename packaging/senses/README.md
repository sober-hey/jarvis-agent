# 感官适配层 v1

```python
from pathlib import Path
from packaging.senses import poll_all

events = poll_all(data_root=Path.home() / ".jarvis", dry_run=True)
```

```sh
.venv/bin/python -B -m packaging.senses --since 2026-10-01T00:00:00+08:00 --dry-run
.venv/bin/python -B -m unittest packaging.senses.test_senses -v
```

`poll_all()` 返回 `SenseEvent` 列表。统一字段为 source、kind、observed_at、ingested_at、summary、payload、sensitivity、provenance。时间使用带时区的 UTC datetime；所有 naive 源时间按 Asia/Shanghai 解释。`observed_at` 取传感器时间：Overland timestamp、HAE ts/睡眠结束、AW timestamp、JPR 日期与文件名起点加分段偏移。mtime 仅用于判定文件快照是否稳定。

可传入 `statuses=[]` 获取每个适配器的 available/unavailable、扫描量、坏记录数和输出量。缺失/无权限/坏结构不会让其它数据源停止。普通 poll 写入 `<data_root>/senses_cursor.db`，缺省根为 `$JARVIS_HOME` 或 `~/.jarvis`。dry-run 把已有游标复制到内存，不创建持久目录。CLI 永远只输出 count/kinds，参数错误只输出固定错误代号。

- 轨迹：追加游标按文件 inode/字节偏移/行号/前缀摘要恢复，半行延后；批次中逐点隔离错误，跨批次按事件指纹去重。精确坐标仅在函数局部校验、去重与可选本地解析时使用，不进入事件 payload/summary。行政区可由源 city/district 字段或 `LocationAdapter(path, resolver=...)` 提供；默认真实 Overland 源无行政名称，尚未安装离线行政边界库，region 为 None、region_status 为 unknown。没有网络逆地理查询，也不会冒造行政区。
- 健康：仅读 points/sleep，不重复摄入 daily 聚合；保留结构化测量值与睡眠起止，summary 无数值。provenance 提供源路径、表与 rowid。未覆盖 notifications/stateOfMind 等原始 extra。
- ActivityWatch：仅选 currentwindow/app-only bucket，输出应用标识、时长和结束时间；不读出窗口标题、网址、AFK 或 browser bucket。时长表示前台暴露，不表示注意力。相同 event ID 的 heartbeat 时长变化会产生修订事件，消费者应按 provenance ID 合并。
- 转写：只读 merged JSONL，全文只在局部用于固定粗主题和字符启发式语种检测；输出分段时间、时长、语种、粗主题，不携带全文或说话者。主题是分类标签，不是精细语义摘要；语种启发式不能可靠区分相近语种。

SQLite 源文件及 WAL 只按字节复制到私有临时目录，前后检查 inode/size/mtime 一致，然后在副本上 mode=ro/query_only 查询；拒绝在不稳定复制窗口读取。没有打开源库的可写 SQLite 连接，不创建源 SHM、不执行 checkpoint。副本关闭后删除。此为本机文件边界，不是 OS 沙箱证明。

JSONL 实现增量读取；SQLite 当前为完整稳定快照扫描 + 每条指纹判变（支持回填与 INSERT OR REPLACE），并非 O(新增行) 的扫描成本。游标只存 offset/哈希/无内容状态；既有游标没有自动重置或迁移。`since` 是发生时间的下限，正常 poll 会消费低于下限的记录。poll_all 对 unavailable 的适配器回滚该源游标。请通过 poll_all 调用以获得此事务边界。

游标在 poll 返回时提交，返回事件尚未与下游持久化组成事务；若调用方在收到返回值后崩溃，可能失去本轮事件。下一轮接内心循环时需要 outbox/ack 或事务接线。并发多消费者语义未实现；本版按单消费者使用。没有修改 mind.db、没有启动采集或定时任务。

新增 `packaging/__init__.py` 是运行模块入口所需：本地 namespace 原先被已安装 PyPA packaging 掩盖。使用 extend_path 保留其 version/requirements 等依赖子模块，测试已覆盖两者兼容；发行打包策略仍需主工程决定。

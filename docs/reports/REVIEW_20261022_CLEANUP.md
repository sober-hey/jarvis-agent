# 复盘评审：目录整理与索引（2026-10-22 晚）

对象：把外部 jarvis 目录收进仓库、`docs/voice-docs`→`docs/voice` 重命名、新增 INDEX/INTRO 这一轮改动。

## 一、最严重的一条：我自己把模型调用搞挂了

在仓库根跑了一次 `pytest . --collect-only` 做"死链/收集"检查。它走进了
`components/agent_engine/upstream/`（第三方引擎固定副本），Python 在里面写下了
**156 个 `__pycache__` 目录、6982 个 `.pyc`**。

引擎的完整性校验是哈希整棵 vendor 树，文件集一变就拒绝启动：

```
{"ok": false, "error": {"detail": "Fixed vendor manifest mismatch; refusing import"}}
```

后果：**从那次收集到清理之间，所有模型调用失败**（守护进程照常写传感器事件，
所以表面上看还在动，但不再产生念头）。窗口约 20 分钟。

发现方式：`voice/tests` 从"213 passed"变成"1 failed"，
`test_actual_web_service_retains_voice_in_single_timeline` 报
`timeline outcome uncertain`；顺着 `warm_engine_turn_failed` 才查到引擎自检失败。

清理与恢复：

```sh
find components/agent_engine/upstream -name __pycache__ -type d -prune -exec rm -rf {} +
packaging/runtime/engine-venv/bin/python3 -I -S -B packaging/engine_entry.py --self-test
# → ok: true
```

恢复后：`voice/tests` 213 passed，守护进程重新产出 `thought`/`model_response`。

防护（已落地）：`pyproject.toml` 的 `norecursedirs` 排除 `components`，并在
`CONTRIBUTING.md` 写明这个坑与恢复命令；`docs/guides/RUN.md` 排错表加入对应条目。

## 二、其余问题（均已修）

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| 1 | `packaging/runtime/engine-venv` 没在 `.gitignore` 显式忽略 | 只靠 venv 内部的 `.gitignore` 兜底，那个文件一丢，396 MB 会进 git | 补规则 |
| 2 | 从仓库根跑 pytest 会收集 `apps/**/Validation/**/test_core.py` 与 `apps/watchos/Scripts/test_core.py` | 收集即报错，跑不起来 | `norecursedirs` 加 `apps`/`Evidence`/`Validation`；从根跑由 40 s + 报错变成 0.18 s、6 条干净收集 |
| 3 | 从仓库根跑 ruff 会扫进 `public-repo/`、`_backups/`、`subprojects/` | 扫镜像与备份，结果没意义 | `extend-exclude` 排除 |
| 4 | README 写"三个入口"，表里五行 | 读者数不对 | 改成"从这里进门" |
| 5 | README 结构块里 `docs/` 下的 `voice/` 与顶层代码 `voice/` 同名 | 容易误读 | 标注"语音文档" |
| 6 | INDEX 里 `.runs_01/`、`public-repo/`、`_backups/`、两个 demo 目录没标"仅本机" | 公开库读者找不到这些目录 | 逐条标注 |
| 7 | `_backups` 里的 tarball 没有校验和 | 以后无法验证完整性 | 生成 `SHA256SUMS`，校验 OK |
| 8 | `.hermes/plans/` 是**被 git 跟踪**的内部计划文档，INDEX 没提 | 目录地图不完整（该文件未进公开库） | INDEX 补一行并标注仅本机 |

## 三、发现但没动（附理由）

1. **又有外部 AI 往仓库根写文件**：`PARTICLE_ANALYSIS.md`、`README_PARTICLE.md`、
   `particle-demo.html`，外加一个 `.pytest-cache/`。已归到 `subprojects/particle-demo/`
   并加 gitignore；`.pytest-cache` 删除。
   这已是第二次（上一轮是「3D 交互演示」直接覆盖了 `README.md` 和 `ARCHITECTURE.md`）。
   只要还有别的会话在这个目录上工作，这类冲突会再发生。
2. `ruff check .` 仍有 1114 条错误，主要是 `packaging/` 的存量债务。CI 的范围是
   `src tests scripts voice`，全绿。不属于本轮引入，未扩大改动面。
3. 内部仓库有 232 条死链，集中在 10-13 的历史交接与 backlog，指向旧目录布局；
   公开库不含这些文件，公开库死链 1 条（代码块里的 grep 示例被误判）。
4. `.runs_01` 里 4 个证据文件在我做全库替换时被 sed 改过路径字符串，已逐字改回。

## 四、核对通过项

| 检查 | 结果 |
|---|---|
| `tests` | 2381 passed |
| `voice/tests` | 213 passed |
| 引擎自检 | `ok: true`，vendor 树 `__pycache__` = 0 |
| 公开库 CI | python + macos-clients 双绿（commit `d0327a5`） |
| 公开库死链 | 1（误报） |
| 本机服务 | 守护进程 + 网关都在跑，账本持续写入 |
| 工作区 | `git status` 干净 |

## 五、结论

整理本身的问题都修完了，但这一轮最大的教训不是"哪个文件放错了"，而是：
**在仓库根跑全量工具（pytest/ruff）会踩到第三方 vendor 树，代价是模型调用全部失效。**
工具范围必须显式声明，不能用"全仓库"这种默认值。

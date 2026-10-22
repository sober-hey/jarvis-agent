# JARVIS 仓库重组迁移映射表（只读盘点 + 风险 + 分阶段命令）

盘点：2026-10-21 17:52–18:05（CST）　仓库：`仓库根/`，`main`，13 GB

---

## 0. 先读这条：盘点期间另一个进程把第一轮迁移做完了（并已提交）

17:52 首次 `ls -la`：顶层 39 项、`git ls-files` = **103,709**。
17:58 复查：顶层 19 项、`git ls-files` = **39,450**，`git status` 出现 224 个 rename + 64,259 个 staged deletion。
18:05 复查：`git status` 清空，落成提交 **`f65df3dc chore(repo): 按 GitHub 结构整理仓库…`**。

已落地的动作（git 证据，非推测）：

| 原始位置 | 现在 | 证据 |
|---|---|---|
| `项目代码/`（2.4 MB / 169）+ 空目录 `交接文档-26.10.12-由hermes交接/` | `archive/legacy-snapshot-20261012/`；空目录已消失 | 147 条 rename |
| `jarvis形象/`（21 MB / 15） | `assets/avatar/` | 15 条 rename |
| `contracts/`（27）+ `planning/`（6） | `docs/contracts/`(15) `docs/planning/`(4) + `archive/`(14) | 33 条 rename |
| 根 `JARVIS_GOAL.md`、`JARVIS_PERSONALITY_*.md`、`…_R2_20261011.zip`（14.6 MB） | `docs/planning/`、`docs/archive/bootstrap-…/`、`archive/legacy-artifacts/` | rename |
| `.runs_01/`（4.0 GB / 64,259 文件） | 磁盘未动，**已出 index** | 64,259 条 `D` + `.gitignore` 加 `.runs_01/` |

**影响**：本表按"原始 → 目标"写，第四列标当前状态。`f65df3dc` 里的 `contracts/ → docs/contracts/` **把 9 处测试路径打断了**（见 B-1），HEAD 现在是红测试。

---

## A. 顶层迁移映射表

| # | 原始项 | 判定 | 体量 / 跟踪 | 建议去向 | 现状 |
|---|---|---|---|---|---|
| 1 | `apps/` | 源码（多端客户端） | 3.5 GB / 1,804 | 保留；`ios` `mac` `watchos` `web` `wakeword` 已是标准形态 | 未动，合格 |
| 2 | `packaging/` | **源码（产品运行时，不是打包目录）** | 412 MB / 255 | 改名 `apps/engine/`（高风险，B-3） | 未动。名字严重误导：`absorb.py` `voice_gateway.py` `web_server.py` `supervisor.py` 都在这 |
| 3 | `packaging/runtime/engine-venv/` | 构建产物 | 396 MB / **1** | 删除或入 `evidence/` | 已被同级 `.gitignore` 挡住 |
| 4 | `packaging/deploy/` | 部署物 | 88 KB | `deploy/`（plist / systemd / supervisor 验收） | 未动 |
| 5 | `src/jarvis/` `tests/` `scripts/` | 源码 + 测试 + 脚本 | 6.9 MB / 132 | 全部保留（`tests/` 已按 unit/contract/e2e/security/recovery 分层） | 合格 |
| 8 | `contracts/` | 契约（DRAFT/NOT_FROZEN，领域单一真源） | 472 KB / 27 | **回滚到顶层**（见 B-1） | 已迁 `docs/contracts/`，破 9 处测试 |
| 9 | `planning/` | 文档 | 40 KB / 6 | `docs/planning/` | 已迁 |
| 10 | `docs/` | 文档（混入长跑产物） | 67 MB / 692 | 保留，`experiments/` 62 MB 是证据不是文档 | 部分已迁 |
| 11 | `.runs_01/` | **长跑证据（218 个 run）** | 4.0 GB / 64,259 | `evidence/runs/` 只留磁盘，不入 git | index 已清，磁盘未动 |
| 12 | `项目代码/` + `交接文档-26.10.12-由hermes交接/` | 遗留旧快照 + **空目录（0 文件 0 字节）** | 2.4 MB / 169 + 0 | `archive/`；空目录直接删 | 均已消失 |
| 13 | `components/agent_engine/upstream/` | **第三方（vendored Hermes Agent 全量，无 .git）** | 246 MB / **18,543** | `vendor/hermes-agent/` 或 submodule；底线只留 `VENDOR_MANIFEST.json` | 未动，第二大体积源 |
| 14 | `subprojects/jarvis-avatar/` | 子项目（无独立 .git） | 3.2 GB / 17,831 | 保留，重资产外移（A-15） | 未动，占全仓 91% 字节 |
| 15 | `jarvis形象/` | 素材 | 21 MB / 15 | `assets/avatar/` | 已迁 |
| 16 | `logs/` + `.hermes/plans/` + `.pytest_cache/` `.ruff_cache/` `.DS_Store` | 运行日志 + agent 草稿 + 缓存 | 712 KB / 0 | 全删（日志与缓存已 ignore） | 未动，无害 |
| 17 | 根 10 md + 1 zip + `voice_config.json` | 文档/归档/配置 | A-18 | 分散归位 | 已迁，`voice_config.json` 留根 |
| 18 | `archive/`（新） | 归档 | 16 MB | 保留，确认不进发布产物 | 新建 |

### A-15 `subprojects/jarvis-avatar/` 拆解（全仓 91% 字节）

| 子目录 | 磁盘 | 跟踪 | 判定 | 建议 |
|---|---|---|---|---|
| `evidence/` | 1.3 GB | 1,326 MB / 2,459 | 长跑证据 | 出 git → `evidence/avatar/` |
| `assets/` | 568 MB | 557 MB / 2,956 | 模型/贴图二进制 | 出 git，LFS 或发布产物 |
| `spikes/` | 405 MB | 372 MB / 9,015 | 试验重建（mpfb-*） | 出 git → `archive/avatar-spikes/` |
| `vendor/` | 791 MB | 357 MB / 341 | 第三方（keentools 467 MB、system-assets.zip 268 MB） | 出 git，留 licenses + 下载脚本 |
| `candidates/` `runtime/` | 151 MB | 144 MB / 2,991 | 候选渲染 + 运行时 | 只留最终版，其余出 git |
| `blender/ scripts/ tests/ docs/ licenses/` | 0.7 MB | 0.6 MB / 63 | 源码 | **留在 git** |

### A-18 根目录散落文件（**实际 10 个 md，不是 17 个**；另 1 zip + 1 json）

| 文件 | 主题 | 去向 | 现状 |
|---|---|---|---|
| `README.md` | 项目入口、检查命令、恢复顺序 | 根 | 在根 |
| `AGENTS.md` | 接手者必读：恢复顺序、K0→K4 门禁、禁止事项 | 根（agent 约定） | 在根 |
| `PROJECT_STATE.md` | 工程状态快照 48 KB | 根 或 `docs/status/` | 在根 |
| `JARVIS_Engineering_Master_Plan_v1.1.2.md` 158 KB | **现行有效总规划** | `docs/planning/` | 已迁 |
| `JARVIS 独立…_Master_Plan_v1.1.1.md` 107 KB | 旧版总规划，仅作历史授权 | `docs/archive/` | 已迁 |
| `JARVIS_GOAL.md` | 长期目标（授权原文，禁改） | `docs/planning/` | 已迁 |
| `JARVIS_PERSONALITY_BEHAVIOR_CHARACTER_ARCHITECTURE.md` 88 KB | 人格/行为架构 | `docs/`（仍在被引用，不该进 archive） | 已迁 `docs/planning/`，建议移到 `docs/` |
| `DECISIONS_REQUIRED.md` / `DUPQ_HOTFIX_APPROVED.md` | 待所有者决策清单 / 一次性审批记录 | `docs/planning/` / `docs/decisions/` | 已迁 `docs/planning/`，后者建议移 `docs/decisions/` |
| `JARVIS_Astra_技术讨论交接_2026-10-14.md` | 技术讨论纪要 | `docs/reports/` | 已迁 `docs/planning/` |
| `…_R2_20261011.zip` 14.6 MB / `voice_config.json` | 对话设计全量归档 / TTS 音色参数（非密钥） | `archive/` / 根 | zip 已迁 `archive/legacy-artifacts/`；json 留根 |

### A-10 `docs/` 内部（长跑产物与私密内容核查）

| 子项 | 体量 | 判定 | 建议 |
|---|---|---|---|
| `docs/experiments/20261013-hermes-k0/` + 同名 `.zip` | 33 MB + 30 MB | **长跑产物混进文档，且目录与压缩包双份** | 移 `evidence/`，删 zip |
| `docs/reports/`(47) `docs/reviews/`(63) | 2.5 MB | 报告 + 评审记录，合格 | 保留 |
| `docs/audits/initial_takeover/` | 532 KB | 首次接管基线（README 引用其 `STATUS.json`） | 保留 |
| `docs/archive/`(3 快照) `docs/draft/`(43) `docs/decisions/`(8 份 `OWNER_AUTHORIZATION_*`) | 792 KB | 归档 + 草稿 + **所有者授权书（敏感）** | 保留，确认无凭据 |
| 根级 26 个 `*_PROPOSAL/_DESIGN/_PROTOCOL.md` | ~300 KB | 设计文档散在 docs 根 | 归 `docs/design/` |
| `docs/guides/` `docs/releases/` `docs/retrospectives/` | **全空** | 空占位 | 删或补 |
| 私密内容扫描 | — | `sk-`/`api_key=`/`Bearer` **0 命中**；`voice_config.json` 仅音色 id | 无需脱敏 |

---

## B. 风险清单（file:line）

### B-1 已破：`contracts/` 迁走后 9 处测试硬编码（**HEAD 现在跑 pytest 就红**）

| 文件:行 | 内容 | 改法 |
|---|---|---|
| `tests/unit/test_repository.py:73` | `ROOT / "contracts/task_transitions.yaml"` | 加 `docs/` 前缀 |
| `tests/unit/test_repository.py:262` | `ROOT / f"contracts/{kind}_transitions.yaml"` | 同上 |
| `tests/unit/test_repository.py:469` | `ROOT / "contracts/task_transitions.yaml"` | 同上 |
| `tests/unit/test_planner.py:142` | `ROOT / f"contracts/draft/{name}.schema.json"` | 同上 |
| `tests/unit/test_planner.py:153` | `ROOT / "contracts/examples/valid_task.json"` | 同上 |
| `tests/unit/test_planner.py:155` | `ROOT / "contracts/task_spec.schema.json"` | 同上 |
| `tests/unit/test_intent.py:327` | `ROOT / "contracts/step_transitions.yaml"` | 同上 |
| `tests/unit/test_intent.py:495` | `ROOT / "contracts/reconciliation.schema.json"` | 同上 |
| `tests/unit/test_snapshot.py:38` | `Path(__file__).parents[2] / "contracts/snapshot_manifest.schema.json"` | 同上 |

`ROOT` 定义在 `tests/unit/test_repository.py:18`、`test_planner.py:22`、`test_intent.py:18`（均 `parents[2]` = 仓库根，**根不能动**）。
**推荐不是改 9 行，而是回滚这条 rename**：`git mv docs/contracts contracts`。理由：`src/jarvis/domain/__init__.py:3-4` 与 `src/jarvis/domain/states.py:5-7` 的 docstring 都以 `contracts/` 为权威路径，契约是代码真源，文档才该进 `docs/`。

### B-2 已破：`tests/unit/test_quality_scope.py:57` 与 `:80` 引用 `项目代码/src`
该目录已移 `archive/legacy-snapshot-20261012/src`。测试用 `tmp_path` 造同构假树，只需两处字符串一致 → 改为 `"archive/legacy-snapshot-20261012/src"`。留着旧名会误导下一个人。

### B-3 给 `packaging/` 正名时，以下常量是相对路径硬编码（改目录名即断）

| 文件:行 | 内容 |
|---|---|
| `packaging/engine_entry.py:100` | `VENV = PROJECT / "packaging/runtime/engine-venv"` |
| `packaging/engine_entry.py:211` | `PROJECT / "packaging/VENDOR_MANIFEST.json"` |
| `packaging/engine_entry.py:599` | `allowed = [VENDOR, VENV, …, PROJECT / "packaging"]` |
| `packaging/jarvis.py:22` | `VENV_REL = Path("packaging/runtime/engine-venv")` |
| `packaging/jarvis.py:204` | `(project / "packaging/VENDOR_MANIFEST.json")` |
| `packaging/linux_preflight.py:141-143` | 三条 `"packaging/runtime/engine-venv/…"`、`"packaging/VENDOR_MANIFEST.json"` |
| `packaging/chat_entry.py:886` | `engine.PROJECT / "packaging"` |
| `packaging/soak/run_soak.py:30` | `COPY_TREES = ("packaging", "apps", "components/agent_engine/upstream")` |
| `packaging/soak/run_soak.py:902` | `snapshot / "packaging/soak/run_soak.py"` |
| `packaging/soak/run_soak.py:960` | `sys.path.insert(0, str(snapshot / "packaging"))` |
| `packaging/soak/run_soak.py:1020` | `sys.path.insert(0, str(snapshot / "packaging/soak"))` |
| `packaging/test_launcher.py:68,131,158,187` | `self.root / "packaging"`、`"packaging/VENDOR_MANIFEST.json"`、`shutil.rmtree(self.root / "packaging")` |
| `packaging/test_evidence_isolation.py:73` | `(ROOT / "packaging", ROOT / "tests")` |
| `packaging/test_product_home.py:99,186` | `home.PROJECT / "packaging/data"` |

**结论**：`packaging/` 改名 = 8 个源文件 + 4 个测试 + 2 个部署模板 + soak 快照契约联动，不是 `git mv`。验收标准是 `grep -rn '"packaging' | wc -l` 归零。

### B-4 部署模板绝对路径（移动前先改这几行）

| 文件:行 | 内容 | 说明 |
|---|---|---|
| `packaging/deploy/com.jarvis.supervisor.plist:10-11` | `/ABSOLUTE/jarvis/.venv/bin/python` + `/ABSOLUTE/jarvis/packaging/supervisor.py` | 文件头自注 "Template only"，装机时替换；`packaging/` 改名须同步 |
| `packaging/deploy/com.jarvis.supervisor.plist:15` | `<string>/ABSOLUTE/jarvis</string>`（WorkingDirectory） | 同上 |
| `packaging/deploy/jarvis.service:11` | `WorkingDirectory=/opt/jarvis` | 生产固定，勿动 |
| `packaging/deploy/jarvis.service:14` | `ExecStart=/opt/jarvis/.venv/bin/python -B /opt/jarvis/packaging/supervisor.py …` | 改名须改 `/opt/jarvis/packaging/` 段 |

### B-5 SwiftPM / Xcode

| 文件:行 | 内容 | 影响 |
|---|---|---|
| `apps/wakeword/Package.swift:7` | `.package(path: "../mac/Modules/Activation")` | 相对 `apps/wakeword`；wakeword 移出 `apps/` 即断 |
| `apps/mac/JarvisMac/Package.swift:20-22` | 仅远程 `Starscream 4.0.8` + 同包 target | 与磁盘布局无关，安全 |
| `apps/mac/JarvisMac/Jarvis.xcodeproj/project.pbxproj`（14 处 `../`） | 全是 `@executable_path/../Frameworks`、`@loader_path/../../..` 等**运行时 rpath** | 移目录不断；但 `apps/mac/Modules/*/.build/` 与 `JarvisMac/Output/`（均已 ignore）需 `swift package clean` 重建 |

### B-6 Python `sys.path` 假定（依赖 `packaging/` 是包根 + 平铺 import）

`packaging/web_server.py:27,160`、`chat_launcher.py:38`、`reflection.py:502` 用 `Path(__file__).parent`；`packaging/deploy/daemon_timeline.py:14`、`daemon_manifest.py:11`、`accept_daemon.py:20`、`packaging/soak/run_soak.py:320,1164,1175` 用 `parents[1]`。整体移动目录**不会断**；把 `packaging/*.py` 拆进 `src/` 子包会全断（`import absorb` 平铺导入）。

### B-7 子项目硬编码绝对路径（移 avatar 必断）

`subprojects/jarvis-avatar/scripts/live_preview.py:25` 与 `live_preview_bootstrap.py:10` 均写死 `Path("仓库根/subprojects/jarvis-avatar")`，移前改 `Path(__file__).resolve().parents[1]`。

### B-8 历史证据里的绝对路径别当配置改

`packaging/runtime/BUILD_MANIFEST.json:23-263` 有数十条 `仓库根/.runs_01/b01-build-20261014/…`。这是**构建当时的证据**，改写即伪造；整文件移 `evidence/` 即可，接受它记录的是旧路径。

### B-9 打包与门禁元数据锁定根结构

`pyproject.toml:15` `packages=["src/jarvis"]`、`:19` `testpaths=["tests"]`、`:29` pyright include（`scripts/check_environment.py`、`scripts/evidence_status.py` 两个文件名不可改）——`src/` `tests/` 与两个脚本名都不能动。另 `scripts/run_quality_checks.sh:6` 只 lint `src tests scripts`，新代码目录不在三者内就**不被检查**（`packaging/` 的 255 文件当前就在盲区）。

### B-10 `.gitignore` 与已跟踪文件冲突

`.gitignore:15` 有 `*.zip`，但 `archive/legacy-artifacts/…_R2_20261011.zip` 是**已跟踪文件**，ignore 对已跟踪无效，zip 仍在历史里；要真排除需 `git rm --cached`。另 `docs/README.md:28` 写 "`contracts/` 下是数据与领域契约"，与现状 `docs/contracts/` 不一致（B-1 回滚则无需改）。

---

## C. 分阶段命令

> 前置：确认没有第二个进程在动这个仓（`git status` 连续 30 秒不变）。Phase 0 已被 `f65df3dc` 完成，可直接从 Phase 1 起。

### Phase 1 — 修 contracts（二选一，推荐 1a）
```sh
cd "仓库根/"
git mv docs/contracts contracts            # 1a 回滚到顶层
git commit -m "revert: contracts 回到顶层，文档才进 docs/"
# 1b 若坚持 docs/contracts：改 B-1 表 9 行
uv run --frozen pytest tests/unit tests/contract -q
```
验证：pytest 全绿；`git grep -n 'ROOT / "contracts/' tests/` 为空（1b 时为 `ROOT / "docs/contracts/`）。

### Phase 2 — 修 `test_quality_scope.py` 的 `项目代码` 字样
```sh
# 改 tests/unit/test_quality_scope.py:57 与 :80
uv run --frozen pytest tests/unit/test_quality_scope.py -q
```

### Phase 3 — avatar 重资产出 git（-2.75 GB / -17,762 文件）
```sh
mkdir -p evidence/avatar archive/avatar-spikes
mv subprojects/jarvis-avatar/evidence/* evidence/avatar/
mv subprojects/jarvis-avatar/spikes/*   archive/avatar-spikes/
git rm -r --cached subprojects/jarvis-avatar/{evidence,assets,spikes,vendor,candidates,runtime}
cat >> .gitignore <<'EOF'
subprojects/jarvis-avatar/{evidence,assets,spikes,vendor,candidates,runtime}/
EOF
git add .gitignore && git commit -m "chore(avatar): 重资产出 git，保留源码/脚本/测试/licenses"
```
验证：`git ls-files subprojects/jarvis-avatar | wc -l` ≈ 60；`du -sh subprojects/jarvis-avatar` 仍 3.2 GB（磁盘未丢）；`python3 -m pytest subprojects/jarvis-avatar/tests -q`（若测试依赖被移资产，需把资产根参数化）。

### Phase 4 — `.runs_01` 落位（index 已清，补磁盘）
```sh
mkdir -p evidence/runs && mv .runs_01/* evidence/runs/ && rmdir .runs_01
```
验证：`ls evidence/runs | wc -l` = 218；`uv run --frozen python scripts/evidence_status.py`（若该脚本扫 `.runs_01`，先改其根常量）；`git status --short` 为空。

### Phase 5 — vendored Hermes 出 git（-204 MB / -18,543 文件，文件数收益最大）
```sh
git rm -r --cached components/agent_engine/upstream
printf '\n# 第三方 vendored：按 VENDOR_MANIFEST 版本重新拉取\ncomponents/agent_engine/upstream/\n' >> .gitignore
git add packaging/VENDOR_MANIFEST.json .gitignore
git commit -m "chore(vendor): Hermes upstream 出 git，版本由 VENDOR_MANIFEST 固定"
```
验证：`uv run --frozen python -c "import packaging.engine_entry"`；`uv run --frozen pytest packaging/test_launcher.py -q`（它校验 VENDOR_MANIFEST）。

### Phase 6 — `packaging/` 正名（可选，最后做，风险最高）
```sh
# 先改 B-3 的 14 处 + B-4 的 2 个模板，再动目录
git mv packaging apps/engine
grep -rn '"packaging\|packaging/' --include="*.py" --include="*.json" --include="*.sh" apps/engine scripts tests | grep -v engine-venv
```
验证顺序：① `uv run --frozen python -m apps.engine.jarvis --help` ② `uv run --frozen pytest -q` ③ `sh scripts/run_quality_checks.sh` ④ `python -B apps/engine/supervisor.py run --help`（对齐 plist/service 的 ExecStart）。

### Phase 7 — 文档收尾
```sh
rmdir docs/guides docs/releases docs/retrospectives 2>/dev/null
mv docs/experiments evidence/k0-hermes-experiment && rm -f docs/experiments/*.zip
mkdir -p docs/design && git mv docs/*_PROPOSAL.md docs/*_DESIGN.md docs/*_PROTOCOL.md docs/design/
```
验证：`docs/README.md` 内相对链接逐个 `test -e`（`grep -o '](\.\?[^)]*)' docs/README.md`）。

---

## D. `.gitignore` 建议 + 体量估算

```gitignore
# 环境与缓存
.venv/
packaging/runtime/engine-venv/     # 396 MB，只该留 BUILD_MANIFEST.json
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.pyright/
.coverage
htmlcov/

# 构建产物
.build/
DerivedData/
*.xcuserstate
xcuserdata/
apps/mac/JarvisMac/Output/
apps/mac/Modules/*/.build/

# 长跑证据（磁盘保留，不进 git）
.runs_01/
evidence/runs/
evidence/avatar/
logs/

# 模型权重与音频语料
apps/wakeword/data/{musan,stress,original,wav,perturbed}/
apps/wakeword/data/*.parquet
subprojects/jarvis-avatar/{evidence,assets,spikes,vendor,candidates,runtime}/

# 第三方 vendored 与归档大件
components/agent_engine/upstream/
archive/legacy-artifacts/*.zip

# 凭据与本地数据
.env
.env.*
*.db
*.db-wal
*.db-shm
*.key
/ABSOLUTE/

# macOS
**/.DS_Store
```

**剔除后体量（实测）**

| 阶段 | 跟踪文件 | 跟踪内容 |
|---|---|---|
| 盘点起始（含 `.runs_01`） | 103,709 | ≈ 13 GB 工作树 |
| 当前 HEAD（`.runs_01` 已出 index） | 39,454 | **3.02 GB** |
| + Phase 3 avatar 重资产出库 | ≈ 21,692 | **334.6 MB** |
| + Phase 5 vendored upstream 出库 | **≈ 3,149** | **≈ 131 MB** |

目标形态：**约 3,100 文件 / 130 MB**，符合 GitHub 常规仓库预期（单文件 <100 MB、总量 <1 GB）。

`.git` 现 **1.22 GB**（35,967 对象 / 2 packs）。剔除目录**不会**自动缩小历史：可选 `git gc --aggressive --prune=now`（收益有限），或 `git filter-repo --path .runs_01 --invert-paths` 重写历史（**改写全部 commit hash，`wt/*` 11 个 worktree 分支全部失效**——除非明确要公开仓库，不建议）。

## 附：一句话结论
目录形状已接近 GitHub 标准，真正的问题是三处错位：`packaging/` 是产品源码却叫打包目录、`components/agent_engine/upstream` 把 18,543 个第三方文件混进源码树、`subprojects/jarvis-avatar` 把 2.75 GB 二进制塞进 git。顺序：Phase 1（修 contracts，测试立刻回绿）→ Phase 3/5（砍到 130 MB）→ `packaging/` 正名放最后或干脆不改名。

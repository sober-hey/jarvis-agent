# Linux 只读预检与独立测试机验收

本脚本仅收集运行它的本机环境，无安装、远端登录、数据库写入或产品启动。当前没有独立 Linux 主机，真实 Linux/ECS 验收为 `NOT_RUN`。腾讯云现有主机不在本轮使用范围。

## 运行入口

从拷贝后的项目根目录，使用独立安装且已具备的 Python 3.12 或更高版本执行：

```sh
python3.12 -I -B packaging/linux_preflight.py --json
python3.12 -I -B packaging/linux_preflight.py --json --require-target
python3.12 -B packaging/test_linux_preflight.py -v
```

普通采集成功退出 0；`--require-target` 的窄环境门槛未通过退出 2。运行前需要满足 Python >=3.12，旧系统 Python 不适用。脚本不会安装或替换解释器。`-I -B` 避免借用环境导入路径及生成字节码；采集输出只写标准输出，证据文件由操作者显式重定向。

现有自有入口可分别核对版本与发行资源：

```sh
python3.12 -I -B packaging/jarvis.py version --json
python3.12 -I -B packaging/jarvis.py check --json --home /absolute/path/to/isolated-jarvis-home
python3.12 -I -B packaging/bootstrap.py --check --json --home /absolute/path/to/isolated-jarvis-home
```

`/absolute/path/to/isolated-jarvis-home` 是待选的绝对隔离路径，不是已创建目录。现有入口由其实现决定检查结果；新预检不会调用入口或启动引擎。旧 `src/jarvis/environment.py` 和 `scripts/check_environment.py` 保留 Darwin/Python 3.12 开发门禁，在 Linux 上不得改断言换取通过。

## 输出契约

- `schema_version=1`，`read_only=true`，真实采集固定 `synthetic=false`、`evidence_kind=local_observation`、`production_ready=false`。
- `environment.category` 区分 `development_macos`、`linux_generic`、`target_alibaba_linux4_x86_64`、`unsupported`。`target_match` 只核对 Linux、`ID=alinux`、`VERSION_ID` 主版本 4、`machine=x86_64`。Ubuntu 不静默替代目标；缺发行身份字段保持 `NOT_VERIFIED`；macOS 为 `NOT_APPLICABLE`。
- `environment_gate` 的 PASS 仅表示 OS 身份匹配、Python 最低版本满足以及磁盘空间可读取，没有指定容量阈值。它不验证标准 RPM 变体、ECS 镜像来源、文件系统类型、依赖构建或产品运行。
- `runtime` 记录实际 system/platform/machine/kernel/python/sqlite/有效 UID。SQLite 只读取标准库版本，不打开数据库。
- `os_release` 仅在 Linux 读取 `/etc/os-release`，只输出身份字段；解析器不会执行变量、Shell 或命令。读取失败暴露错误类别并保持未知状态。
- `filesystem` 记录当前工作目录所在文件系统的容量与有效用户可用量；不推断 ext4、ESSD、fsync 或耐久性。
- `cgroup` 仅在 Linux 读固定系统路径判断 v1/v2 及成员条目存在，不输出可能带租户信息的路径。限制额度仍 `NOT_RUN`。
- `dependencies.artifacts` 检查项目固定路径的文件存在性。每项 PASS 仅表示文件存在；总状态固定 `NOT_VERIFIED`、`build_verified=false`。不启动解释器、不导入派生引擎、不读取锁内容、不安装。Mac venv 不能拷贝到 Linux 作为构建完成证据。
- `target_linux_validation`、`preproduction_linux`、`production_evidence` 和产品验收项保留 `NOT_RUN`。环境匹配不能提升这些状态。

状态为 `PASS/FAIL/NOT_RUN/NOT_VERIFIED/NOT_APPLICABLE`。没有 Linux 实机时，单元测试的 `synthetic=true`、`fixture_branch_only` 结果只证明判定分支。名称中带 `fixture` 的 collector mock 测试也属于夹具，不能保存成实际 Linux 环境证据。

## 独立 Linux 测试机前置

先由所有者提供或批准独立 Linux 隔离测试机。保留一套 macOS 开发验证和一套真实 Linux 预生产验证，两者证据分开。不要接入生产凭据或个人资料。拷贝固定版本源码、许可证、资源清单和固定锁，记录源码版本及文件哈希；不要复制宿主 Hermes home、现成 macOS venv、宿主 tools 或活跃 dirty 安装。

控制端需要独立 Python 3.12 环境，派生引擎候选需要独立 Python 3.14 环境。依赖构建路线见本仓库 `BUILD_PLAN.json` 和自有入口实际检查；预检不生成安装/部署命令，也不假设 Linux 构建已经完成。两端锁定依赖、原生 wheel、引擎真实构造和生命周期须分别验证，不共享写库或 secret。无批准的 RPC/worker/db 契约时，不实现协议或绕过旧门禁。

实际 ECS 镜像 ID、名称、构建号、标准 RPM/dnf 变体、架构、内核、根盘类型、ESSD/ext4 挂载与 systemd 均需从目标实机取证；`os-release` 无法独自证明云镜像来源。仍需真实产品故障恢复、SQLite WAL/FULL/外键、目录同步及文件发布、断电耐久、权限隔离、B1/B2 备份恢复、24 小时稳定性、长期所有者试用、PG-L 和正式发布批准。SIGKILL 不等于断电。

## 本轮证据

`.runs_01/linux-preflight-20261014/` 保存 RED 失败证据、strict Pyright 专用配置、最终 Ruff/格式/Pyright/pytest 命令和退出码、本机采集 JSON，以及旧 src/tests/lock/环境入口哈希对照。任何 Linux 夹具结果都不计真实 Linux 通过。最终状态以 `verification.json` 的实际命令结果为准。

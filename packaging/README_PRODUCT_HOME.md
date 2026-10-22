# JARVIS Product Home Configuration

This document describes the product-owned configuration boundary and explicit one-shot
CLI chat wiring. It does not read or copy the host Hermes configuration, home,
profiles, keys, or resolver.

## Operational CLI

Use the control interpreter (Python 3.12). Every configuration command requires explicit
absolute paths. The data root must already exist, be owned by your user with mode `0700`,
and be outside the source tree and host Hermes. For example, create a dedicated empty
root with `mkdir -m 700 /absolute/private-parent/jarvis-data` (choose your own path).
No command below chooses or creates the default `~/.jarvis` implicitly.

```sh
.venv/bin/python -B packaging/jarvis.py config init \
  --config /absolute/private-parent/jarvis-config/config.json \
  --data-dir /absolute/private-parent/jarvis-data \
  --model YOUR_MODEL --provider custom --base-url https://YOUR_PROVIDER/v1 \
  --api-mode chat_completions --context-length 256000 --reasoning-effort high

.venv/bin/python -B packaging/jarvis.py config check \
  --config /absolute/private-parent/jarvis-config/config.json \
  --data-dir /absolute/private-parent/jarvis-data

.venv/bin/python -B packaging/jarvis.py config key-check \
  --key-file /absolute/private-parent/provider.key
```

`init` writes only the six fields, using `product_config.write_config`'s atomic replace
and exclusive lock. Missing configuration parent directories are created `0700`; an
existing immediate parent must already be `0700`, and existing files must be `0600`.
To change an existing valid configuration, repeat `init` with
`--expected-sha256 SHA_FROM_CHECK`; omitted or stale expectations reject the write.
The hash is the canonical configuration digest, not a digest of JSON whitespace.

`check` reads and validates configuration and product data root and returns
`config_sha256`, `data_root_sha256`, `binding_sha256`, paths and `ok`.
Changing configuration or data root changes the binding. It creates no files,
starts no chat/process, and makes no network or model request. `key-check` validates
an already-created dedicated file's current-user ownership, exact `0600` mode,
regular-file/no-link policy and bounded single-line UTF-8 content. It prints only
its path and `ok`, never key content or its digest. Create/manage credentials using
your own secure local workflow; there is no key-writing command and no argv/env key
input. Unknown arguments and secret flags are rejected without echoing their values.

Success exits `0` and emits JSON. Failure exits `2` and emits the fixed JSON
`{"ok": false}` on stderr without exception details. `--help` prints argparse help.
Normal endpoint policy is HTTPS port 443; literal IPv4/IPv6 loopback and localhost
are rejected even under HTTPS. Fake tests must explicitly supply `--test-port N`
with `http://127.0.0.1:N`; init never enables this mode by default. DNS resolution is
not performed by these offline checks, so they cannot inspect DNS targets.

The configuration commands validate local files only; they do not prove credentials
or a real model connection. Existing `version`, `check`, `--version`, `--check` and
`--home` behavior remains available for the separate legacy distribution checker.
Its `launch_ready: false` field still describes distribution/production readiness,
not whether the explicit developer chat command exists.

## One-shot chat from the product CLI

After creating your dedicated configuration and credential file, Sir can send directly:

```sh
.venv/bin/python -B packaging/jarvis.py chat \
  --config /absolute/private-parent/jarvis-config/config.json \
  --data-dir /absolute/private-parent/jarvis-data \
  --key-file /absolute/private-parent/provider.key \
  --prompt 'JARVIS，请介绍你当前可以做的事。' \
  --attempt-id sir-first-message

.venv/bin/python -B packaging/jarvis.py query-attempt \
  --data-dir /absolute/private-parent/jarvis-data \
  --attempt-id sir-first-message
```

`chat` requires explicit `--config`, `--data-dir`, `--prompt` and exactly one credential
source: `--key-file PATH` or `--key-fd FD`. It never selects or creates `~/.jarvis`.
For stdin from an anonymous pipe or private file, use `--key-fd 0`; for an already-open
private descriptor, use its FD number and ensure your caller passes it to this CLI.
Do not put key text in a shell command or environment variable. The CLI forwards the
FD without reading it; only the current launcher reads and validates credentials.
Configuration and data-root validation also remain in that launcher.

Each invocation sends one prompt and returns the launcher's successful JSON unchanged,
including `text`, `session_id`, `attempt_id` and binding digests. Without `--session`,
it reuses the saved session in this data root. `--session SESSION_ID` selects an explicit
session. Without `--attempt-id`, the launcher creates a fresh ID for a deliberate new
send. Reusing an existing attempt is refused, including `started` and `unknown` records:
query it first and never automatically retry an uncertain send. The query command starts
only the launcher reader, not an engine or model request.

Optional `--expected-sha256 SHA_FROM_CONFIG_CHECK` and
`--expected-binding-sha256 SHA_FROM_CONFIG_CHECK` reject stale bindings before engine
startup. Explicit fake HTTP tests use `--test-port N`, forwarded to the launcher's
`--test-local-port`; they require `http://127.0.0.1:N` and a dedicated `FAKE-` credential.
Never use this test flag for real credentials or a real provider.

CLI syntax/spawn failures emit only `{"ok": false}` on stderr and exit `2`. Launcher
failures emit the same fixed JSON and preserve its exit code (currently `1`); arbitrary
launcher diagnostics are suppressed. Success exits `0`. The launcher retains its
65-second engine timeout, attempt ledger and process-group cleanup; the CLI adds no
retry or competing timeout. The launcher starts in a separate process group, and CLI
SIGINT/SIGTERM is forwarded as SIGINT so it can clean its separate engine group.

Verified scope: local fake HTTP through CLI → existing launcher → isolated Python 3.14
engine → persistent SessionDB, including key file, stdin FD, another inherited FD,
session continuation, attempt replay refusal and expected-config conflicts. This round
made no real provider call and proves neither credential validity nor GUI, interactive
REPL, tools, Linux/ECS deployment or production readiness.

## 从统一 CLI 启动中文网页

配置和凭据仍使用上面的专用路径；启动命令不会创建凭据，也不会读取宿主
Hermes 的配置。用 Python 3.12 控制解释器在前台启动：

```sh
.venv/bin/python -B packaging/jarvis.py web \
  --config /absolute/private-parent/jarvis-config/config.json \
  --data-dir /absolute/private-parent/jarvis-data \
  --key-file /absolute/private-parent/provider.key
```

启动成功立即输出一行 JSON，例如 `{"url": "http://127.0.0.1:PORT", "scope":
"loopback development only"}`；实际 `PORT` 由系统分配。手动打开该地址即可使用现有
中文网页。命令持续在前台运行，不打开浏览器、不自动重启。`--port N` 可指定
`0–65535`；默认 `0` 自动分配，启动记录始终包含实际端口。网页只绑定
`127.0.0.1`，不接受 `--host` 或外部绑定。

`--config`、`--data-dir` 和 `--key-file` 必须是显式绝对路径；配置是自有 `0600`
文件，数据根为自有 `0700` 专用目录，具体所有权、链接、端点和数据边界检查由
现有 Web/launcher/共享校验器执行。省略数据路径会直接失败，不回退到默认 home。
文件凭据可换成 `--key-fd FD`，包括匿名管道 stdin `--key-fd 0`；调用者负责把已打开的
FD 传入。统一入口仅传递 FD，不读取密钥内容，不接收 argv/env 密钥，也不写日志。

按 Ctrl-C 或发送 SIGTERM，入口向自己创建的 Web 进程组转发 SIGINT。Web 会停止接纳新请求，
等待已接纳请求收尾；现有引擎仍使用 65 秒超时，Web 排空预算为 70 秒，入口在 75 秒后
最终回收自己创建的 Web 进程组。它不扫描或终止其它进程。完成的 attempt 可按原 ID
回读；started/unknown 不会自动重发。出现 `{"state":"unknown","retry_allowed":false}`
时须人工核对原 attempt，取消启动命令不表示 provider 操作已撤销。

显式本机 fake-provider 联调用 `--test-port N`，仅转发为现有 Web 的
`--test-local-port N`（`1–65535`）；须对应 `http://127.0.0.1:N` 配置和专用 `FAKE-`
凭据。该参数不是网页监听端口，不能用于真实凭据或真实 provider。

参数、启动或异常 stdout 失败只在 stderr 输出 `{"ok": false}`；异常诊断不透传。
仅符合固定结构的本机启动地址和 fixed unknown 记录会实时显示，避免为了抑制异常输出
而丢失正常 URL。语法/转发失败退出 `2`；正常结束退出 `0`；子进程的非零退出码保留。

本轮验证范围是显式 fake HTTP：统一 CLI → 真实 Web → 现有 launcher → 独立 Python 3.14
引擎 → SQLite，覆盖静态页与 health、真实 fake 结果、完成 attempt 回放不重发、FD
传递、SIGINT/SIGTERM 收尾和 owned 进程组超时回收。没有真实 key/模型调用；不代表公开
服务认证、真实 provider 凭据有效、Linux/ECS 或生产验收。

## Files and fields

The default non-secret configuration path is `~/.jarvis/config.json`. Callers may pass
an explicit absolute `--config` path. The JSON object must contain exactly these fields:

- `model`
- `provider`
- `base_url`
- `api_mode`
- `context_length`
- `reasoning_effort`

The file is a current-user-owned regular `0600` file, and its parent directories are
current-user-owned `0700` directories. Writes are atomic and use an exclusive lock.
An existing file must be updated with its matching `expected_sha256`; stale or omitted
expectations are rejected. Unknown fields, secret fields, symlinks, hard links, foreign
owners, and shared permissions are rejected.

`product_config.read_config()` returns a read-only mapping and a canonical SHA-256.
`bind_route()` additionally binds the route to a validated `0700` data root and returns
config, data-root, and combined binding digests. The binding is a handoff summary, not
an OS sandbox or a public-service authorization protocol.

## Credentials

Credentials never belong in `config.json`, argv, environment, logs, or the binding.
`read_key()` accepts exactly one of:

- `key_file`: a current-user-owned regular file with mode `0600` and no hard links or
  symlink components;
- `key_fd`: an anonymous pipe or the same kind of dedicated `0600` regular file.

The value is bounded single-line UTF-8 text. The caller owns lifecycle and must keep
it in memory only long enough to hand it to the credential pipe used by the existing
launcher.

## Endpoint policy and check

Normal configuration accepts only credential-free HTTPS port 443 endpoints. Loopback
HTTP is rejected unless the caller supplies an explicit `test_port`; that mode exists
only for fake HTTP tests.

`python packaging/product_config.py check --config PATH` reads and validates one file.
It does not create `~/.jarvis`, create parent directories, use the network, invoke a
model, or alter the file. The product CLI now delegates explicit chat and attempt
queries to `chat_launcher.py`; validators and credential handling are still shared
through the existing launcher rather than duplicated in the CLI.

The scope is one user on one local machine. These checks do not claim an OS sandbox,
multi-user isolation beyond owner/mode checks, public deployment, or production
credential management.

## Controlled material task (P2 candidate, fixture provider)

The new `task` entry accepts one private UTF-8 file (at most 1 MiB) under an
explicit data root. It produces a deterministic candidate Markdown: trimmed,
deduplicated, sorted source lines plus the instruction. Instructions are recorded
as text; they never select tools, shell, code, agents or output paths. Only dedicated
`FAKE-` credentials are accepted. The configured endpoint is never contacted.

```sh
.venv/bin/python -B packaging/jarvis.py task submit \
  --config /absolute/private-parent/jarvis-config/config.json \
  --data-dir /absolute/private-parent/jarvis-data \
  --key-file /absolute/private-parent/fake-task.key \
  --input /absolute/private-parent/jarvis-data/notes.txt \
  --instruction '整理资料，生成候选稿' --request-key notes-review-1

.venv/bin/python -B packaging/jarvis.py task query \
  --data-dir /absolute/private-parent/jarvis-data --task-id TASK_ID

.venv/bin/python -B packaging/jarvis.py task cancel \
  --data-dir /absolute/private-parent/jarvis-data --task-id TASK_ID
```

Inspect `tmp/TASK_ID/candidate.md` before repeating the identical `submit` with
`--publish`. That explicit flag approves the exact hashed candidate and publishes
only `published/TASK_ID.md`. The source and existing destination are never replaced.
`--key-fd FD` is available instead of key-file, including anonymous stdin pipes.

Stable request/task/operation identities and payload hashes are recorded in
`tasks/TASK_ID.json`. Reusing a request key with changed input bytes, instruction,
route or input path is rejected; completed and unknown tasks are not executed again.
Default state is `awaiting_approval`; transient `planned`, `completed`, `unknown`
and `failed` are explicit. Status and operation intent/attempt/receipt reside in
one atomically replaced JSON file. All adapter files are 0600, directories 0700.
Input snapshots and candidate/published artifacts have independently read-back
SHA-256 evidence. Directory-FD/no-follow traversal rejects links and root escapes.

Cancellation commits a separate atomic admission marker under an admission lock.
Already admitted operations may finish; the flag stops subsequent admissions and
preserves in-flight/unknown records. Cancellation does not delete or roll back files.
Unknown results require manual investigation; this entry offers no retry/reconcile
command. Root-scoped locking serializes execution for this local adapter.

This is a P2 candidate adaptation, with Contract Pack `NOT_FROZEN`. It does not
claim K0 protocol implementation, worker epochs, core approval/contract acceptance,
OS isolation, production accounts, real model planning, Linux/ECS, power-loss
recovery or production delivery. The existing chat launcher and legacy CLI remain
separate and unchanged by this task command.


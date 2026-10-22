# JARVIS supervisor

This POSIX supervisor owns web plus an explicit `inner-placeholder` fixture.
The fixture is not the real inner loop and calls no model. The real worker must
later supply a continuous command and progress heartbeat through the manifest.
This package does not claim a persistent task queue, reliable task handoff,
exactly-once effects, or completed 24-hour/Linux acceptance.

## Run and inspect

Run from the checkout with its Python environment:

```sh
.venv/bin/python -B packaging/supervisor.py run \
  --data-root /absolute/private/jarvis-data \
  --config /absolute/private/config.json \
  --key-file /absolute/private/provider.key --port 8765

.venv/bin/python -B packaging/supervisor.py status \
  --data-root /absolute/private/jarvis-data
```

Stop by sending SIGTERM (or SIGINT) to the supervisor PID reported by status.
Use `owner_alive` and the owner PID/start-time/command identity to check stale
snapshots before operational signalling. Status is read-only and never takes the
owner lock; `snapshot_age` shows how old the snapshot is. Component states include
starting, healthy, backoff, circuit_open, stopped. Circuit-open components stay
stopped until an operator stops and starts the supervisor; healthy siblings keep
running. `restart_count` is the count for this supervisor lifetime; historical
restarts remain in the append-only JSONL `supervisor.log`.

The default web command invokes the same `web_server.py` used by `jarvis web`
directly, avoiding an extra CLI-created process session. It probes `/api/health`
and verifies `{"status":"ok"}`. That endpoint establishes HTTP responsiveness,
not successful model inference or progress of an in-flight model call. The inner
fixture writes `inner-placeholder.heartbeat`. Old-generation/future-dated
heartbeats are rejected. Application workers must update a progress heartbeat
from their actual work path, not from an independent always-live ticker.

Each child starts a separate process group. Stop sends TERM, waits the component
stop timeout (default 75 seconds), then KILLs verified surviving groups. Owned
nested descendant sessions observed while the supervisor is alive are also
collected. A descendant that escapes into a separate session and reparents before
it can ever be observed is outside a portable process-table solution; Linux
systemd `KillMode=control-group` provides an additional deployment boundary.
Killing the supervisor itself with SIGKILL is not a clean-stop operation and
cross-supervisor-crash orphan adoption is not implemented in this version.

The lifetime flock is authoritative; the stored UID, PID, process-group,
full start-time and command hash are checked as a second guard. A stale identity
with a reused PID does not block a new start. A corrupt owner file fails closed.
Never delete or replace a held `supervisor.lock` inode. Run the data directory
as a dedicated user with mode 0700; manifests are trusted operator input and
commands are argv arrays, never shell strings. Logs and snapshots are local
operator diagnostics. Model credentials belong only in a dedicated key file.

## Component manifest

Use `--components /absolute/components.json` to replace the defaults. For example:

```json
{
  "components": [
    {
      "name": "inner-placeholder",
      "command": ["/absolute/python", "-B", "/absolute/jarvis/packaging/supervisor_components.py", "fake", "--heartbeat", "/absolute/private/jarvis-data/inner.heartbeat"],
      "health": {
        "kind": "heartbeat",
        "target": "/absolute/private/jarvis-data/inner.heartbeat",
        "timeout": 30,
        "interval": 2,
        "startup_grace": 30,
        "request_timeout": 0.5
      },
      "restart": {
        "base_delay": 1,
        "max_delay": 60,
        "crash_limit": 5,
        "crash_window": 120,
        "stable_after": 120
      },
      "stop_timeout": 75
    }
  ]
}
```

For HTTP health use `kind: http` and an explicit
`http://127.0.0.1:PORT/api/health` target. A request has a bounded socket timeout.
Actual timeout observation includes probe interval, scheduling and cleanup time.
Failures receive exponential delays 1, 2, 4, … up to 60 seconds. Five failures
within 120 seconds open the default circuit. A run lasting at least `stable_after`
resets the failure streak. Exit zero also restarts: components are long-lived.

## Validation

```sh
.venv/bin/python -B -m unittest discover -s packaging -p test_supervisor.py -v
.venv/bin/python -B packaging/deploy/acceptance_supervisor.py \
  --evidence /absolute/new/evidence-directory
```

The acceptance script starts the real web server with `--acceptance-mode`, a
private `/tmp/jarvis-acceptance-*` tree and a local FAKE provider. It SIGKILLs each
component, checks new healthy PIDs, stops the supervisor, checks process exit and
closed port, and deletes the temporary root. It makes no chat/model request.
Retained evidence must be a new directory, not an existing historical run.

`jarvis.service` and `com.jarvis.supervisor.plist` are templates only. Replace
paths and arrange dedicated config/key/data ownership before installing. This
change does not install or start either service. The default template still runs
the fixture inner worker. Linux/systemd and 24-hour soak tests remain to be run.

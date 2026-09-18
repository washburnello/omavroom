# omavroom MCP server

`omavroom-mcp` is a [FastMCP](https://gofastmcp.com) server that exposes the
seat manager to coding agents over MCP (stdio transport). It is a thin client
of the manager daemon's Unix socket; the daemon is the only process that talks
to libvirt/QEMU. See `PLAN.md` (Phase 5) and `PHASES.md` for the design.

## Launch

```bash
cd /home/washburnello/Work/omavroom
uv run omavroom-mcp            # serves MCP on stdio
```

Flags:

| Flag | Meaning |
| --- | --- |
| `--socket PATH` | daemon socket to connect to (default `$XDG_RUNTIME_DIR/omavroom/daemon.sock`) |
| `--provisioner NAME` | provisioner for an auto-started daemon (`libvirt`/`real`/`fake`) |
| `--no-autostart` | require an already-running daemon; never start one (wins over the env) |
| `--start-timeout SECONDS` | how long to wait for an auto-started daemon's socket |

Environment:

| Variable | Meaning |
| --- | --- |
| `OMAVROOM_PROVISIONER` | provisioner for an auto-started daemon (default `libvirt`) |
| `OMAVROOM_MCP_AUTOSTART` | `0`/`false` disables auto-start (same as `--no-autostart`) |
| `OMAVROOM_CONFIG` | config file for the daemon |
| `OMAVROOM_LOG_LEVEL` | daemon log level |

### Auto-start

If the socket is not live, the server starts `python -m omavroom.daemon`
detached (new session, stdio discarded) with the configured provisioner and
waits for the socket. If a daemon is already running it is reused and nothing
is started. If auto-start is disabled, or the daemon exits during startup, or
the socket never appears, the server exits non-zero with a message pointing at
the daemon log (`~/.local/state/omavroom/daemon.log`).

Precedence: `--no-autostart` always disables starting a daemon; otherwise
`OMAVROOM_MCP_AUTOSTART` decides (`0`/`false`/`no`/`off` disables it, anything
else or unset enables it).

## opencode config

The manager/user wires this into opencode; this repo does **not** edit the
real config. Add to `~/.config/opencode/opencode.json` (or `opencode.jsonc`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "omavroom": {
      "type": "local",
      "command": [
        "uv",
        "run",
        "--directory",
        "/home/washburnello/Work/omavroom",
        "omavroom-mcp"
      ],
      "enabled": true,
      "environment": {
        "OMAVROOM_PROVISIONER": "libvirt",
        "OMAVROOM_MCP_AUTOSTART": "1"
      }
    }
  }
}
```

Equivalent using the venv binary directly:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "omavroom": {
      "type": "local",
      "command": ["/home/washburnello/Work/omavroom/.venv/bin/omavroom-mcp"],
      "enabled": true
    }
  }
}
```

MCP tools are registered with the server name as a prefix, so this server's
tools appear as `omavroom_pool_status`, `omavroom_exec_run`, etc. Disable all
of them with `"tools": { "omavroom*": false }`.

## Toolset

| Tool | Signature | Returns |
| --- | --- | --- |
| `pool_status` | `pool_status()` | `dict` — seats, queue, per-type admission |
| `queue_view` | `queue_view(include_history=False)` | `list[dict]` |
| `seat_status` | `seat_status(request_id: int)` | `dict` (with nested `seat`/`lease`) |
| `list_seats` | `list_seats(include_history=False)` | `list[dict]` |
| `list_events` | `list_events(limit=200)` | `list[dict]` |
| `list_execs` | `list_execs(seat_id, include_output=True, max_total_output_bytes=8192)` | `list[dict]` (bounded; `None` = full rings) |
| `request_seat` | `request_seat(agent_label, seat_type, image=None, project=None)` | `dict` with `request_id` (queues if full) |
| `wait_for_seat` | `wait_for_seat(request_id, timeout_s=60)` | `dict` (bounded poll) |
| `heartbeat` | `heartbeat(seat_id=None, request_id=None, lease_id=None)` | `dict` (lease view) |
| `exec_start` | `exec_start(seat_id, command, label=None, timeout_s=None)` | `dict` with `exec_id` (async) |
| `exec_poll` | `exec_poll(seat_id, exec_id)` | `dict` — `state`, bounded `stdout`/`stderr`, `exit_code`, `truncated` |
| `exec_kill` | `exec_kill(seat_id, exec_id, signal=9)` | `dict` (terminal view) |
| `exec_run` | `exec_run(seat_id, command, timeout_s=30, label=None)` | `dict` — combined output + `exit_code` + `timed_out` (bounded sync) |
| `screenshot` | `screenshot(seat_id, max_width=None, max_bytes=None)` | MCP **image content** + **base64 text** (downscaled + byte-capped PNG) |
| `input` | `input(seat_id, events: list[dict])` | `dict` — `{"applied": n}`; events are `{"kind":"key"\|"text"\|"click","value":...}` |
| `peek_endpoint` | `peek_endpoint(seat_id)` | `dict` — `{"endpoint": "vnc://..."}` (never opens a window) |
| `peek_url` / `peek_attach` | aliases of `peek_endpoint` | `dict` |
| `prepare_repo` | `prepare_repo(seat_id, url, branch=None)` | `dict` — `job_id` (long op) |
| `export_seat` | `export_seat(seat_id, repo, branch=None, ref=None)` | `dict` — `job_id` (long op; keeps seat) |
| `release_seat` | `release_seat(seat_id, repo=None, export=True, branch=None, ref=None)` | `dict` — `job_id` (long op; destroys seat) |
| `reset_seat` | `reset_seat(seat_id)` | `dict` — `job_id` (long op) |
| `retry_release` | `retry_release(seat_id)` | `dict` — `job_id` (long op) |
| `force_discard` | `force_discard(seat_id, reason="force_discard")` | `dict` — `job_id` (long op) |
| `reconcile` | `reconcile()` | `dict` — `job_id` (long op) |
| `job_poll` | `job_poll(job_id)` | `dict` — `state` (`pending`/`done`/`error`) |
| `job_wait` | `job_wait(job_id, timeout_s=600)` | `dict` — last view (bounded) |

## Usage guidance

**Short vs long commands.** Use `exec_run` for short commands (a few seconds:
`ls`, `git status`, a quick build check). It runs synchronously with a bounded
default timeout (30 s) and returns combined stdout/stderr plus `exit_code`. If
the command is still running at the deadline it is killed and `timed_out` is
`True` with the partial output. For anything long — test suites, builds,
servers, interactive loops — use the async path:

```
exec_start(seat_id, command) -> {"exec_id": ...}
exec_poll(seat_id, exec_id)  -> {"state": "running"|"finished"|"killed",
                                 "stdout": ..., "stderr": ..., "exit_code": ...}
exec_kill(seat_id, exec_id)  -> terminal view
```

**Long lifecycle ops.** `release_seat`, `reset_seat`, `prepare_repo`,
`export_seat`, `retry_release`, `force_discard`, and `reconcile` return a
`job_id` immediately. Poll with `job_poll` or `job_wait` (bounded). Nothing
agent-facing blocks indefinitely.

**Heartbeats.** `heartbeat` is its own channel: a long-running exec never
makes the agent look dead, as long as the agent keeps heartbeating the lease.

**Screenshots.** `screenshot` returns the PNG as MCP image content (the agent
reads it) *and* as base64 text. The daemon downscales to `max_width` (default
1024, hard cap 2048) *and* caps the encoded size at `max_bytes` (default
2 000 000, hard cap 8 000 000), so a response is never multi-MB. If the image
needs resizing but the resizer (ImageMagick) is unavailable, the call fails
with a clear error instead of returning an oversized image.

**Concurrency.** The MCP server keeps one shared daemon connection for fast
calls and uses a dedicated short-lived connection for desktop ops
(`screenshot`/`input`/`peek_endpoint`). A desktop op waits for the seat's
exclusive lock at most `desktop_lock_timeout_s` (default 5 s) and then fails
with wire code `seat_busy` rather than blocking behind a long
export/reset/release; because it uses its own connection, a blocked desktop op
cannot starve `heartbeat` or any other tool.

**Typical flow.**

```text
request_seat(agent_label, "desktop", project="my-repo")
wait_for_seat(request_id)              # -> seat.id
prepare_repo(seat_id, url) + job_wait  # optional
exec_run(seat_id, "git status")        # short
exec_start(seat_id, "make test")       # long
exec_poll(seat_id, exec_id) ... heartbeat(seat_id=...) ...
screenshot(seat_id); input(seat_id, [...])
export_seat(seat_id, repo=...) + job_wait
release_seat(seat_id, repo=..., branch="task") + job_wait
```

## Exec engine semantics (what the daemon enforces)

- **One worker per command exec.** Commands run on a dedicated per-exec
  thread, never on the daemon's shared lifecycle job queue, so a long command
  cannot delay provisioning, release, reset, or admission.
- **Seat state.** An exec is accepted only on a `ready`/`busy` seat whose VM
  is running; otherwise the daemon returns error code `exec_not_allowed`.
- **Concurrency.** At most `exec.max_concurrent_per_seat` (default 4) running
  execs per seat and `exec.max_concurrent_total` (default 16) across all
  seats; a new one beyond either cap is rejected with `exec_limit`.
- **Output.** Per-stream ring buffer capped at `exec.max_output_bytes`; older
  output is dropped and `truncated` is set. `list_execs` additionally caps the
  aggregate stdout+stderr it returns (default 8 KiB across the list) so an
  overview call is never a multi-MiB response; use `exec_poll` for one exec's
  full ring output.
- **Timeout.** `timeout_s` is clamped to `exec.max_runtime_s` (default
  3600 s) and applied as a hard transport timeout (exit code `124`).
- **Kill.** `exec_kill` marks the exec `killed` immediately and signals the
  worker; with the libvirt provisioner the worker terminates the local `ssh`
  process, closing the channel (a command that daemonises itself in the guest
  may outlive the channel — documented limitation). The `signal` value only
  sets the recorded `exit_code` (`-signal`); it is *not* delivered to the
  guest process.
- **Release/reset.** Releasing or resetting a seat cancels its running execs
  without waiting for them, so teardown is never blocked by a long command.

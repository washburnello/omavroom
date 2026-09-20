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

Configure the MCP **globally** (`~/.config/opencode/opencode.json`), so every
opencode session — in every project — gets the omavroom tools. The command is
the client binary only; it is not tied to the agent's working directory.

Install the entry points where opencode can find them (a symlink from
`~/.local/bin` to the venv entry point is enough), then:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "omavroom": {
      "type": "local",
      "command": ["omavroom-mcp"],
      "enabled": true,
      "environment": {
        "OMAVROOM_PROVISIONER": "libvirt",
        "OMAVROOM_MCP_AUTOSTART": "1"
      }
    }
  }
}
```

If `omavroom-mcp` is not on the `PATH` of whatever launches opencode, point at
the entry point directly instead:

```jsonc
"command": ["/home/<you>/Work/omavroom/.venv/bin/omavroom-mcp"]
```

One shared daemon owns the pool — run it as a systemd **user** service
(`omavroom-daemon.service`) so it is always available; the MCP reuses a live
daemon and only starts one if none is running. Because there is a single
daemon, every project shares the same seats and the Command Center sees them
all.

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
| `heartbeat` | `heartbeat(seat_id=None, request_id=None, lease_id=None)` | `dict` (lease view; automatic — agents normally need not call it) |
| `exec_start` | `exec_start(seat_id, command, label=None, timeout_s=None)` | `dict` with `exec_id` (async) |
| `exec_poll` | `exec_poll(seat_id, exec_id)` | `dict` — `state`, bounded `stdout`/`stderr`, `exit_code`, `truncated` |
| `exec_kill` | `exec_kill(seat_id, exec_id, signal=9)` | `dict` (terminal view) |
| `exec_run` | `exec_run(seat_id, command, timeout_s=30, label=None)` | `dict` — combined output + `exit_code` + `timed_out` (bounded sync) |
| `screenshot` | `screenshot(seat_id, max_width=None, max_bytes=None, region=None)` | MCP **image content** + **base64 text**. `max_width=None`/`0` = **native full-resolution**; a positive width downscales. `region=[x,y,w,h]` crops the native frame first. Byte-capped PNG |
| `input` | `input(seat_id, events: list[dict])` | `dict` — `{"applied": n}`; events are `{"kind":"key"\|"text"\|"type"\|"click","value":...}` (`type` also takes `delay_ms`) |
| `copy_in` | `copy_in(seat_id, host_path, guest_path)` | `dict` — `{"bytes": n}`; host path must be under the daemon transfer root |
| `copy_out` | `copy_out(seat_id, guest_path, host_path)` | `dict` — `{"bytes": n}`; host path must be under the daemon transfer root |
| `launch_app` | `launch_app(seat_id, command, tui=False)` | `dict` — `tui=True` runs in a terminal via `omarchy-launch-tui` |
| `list_windows` | `list_windows(seat_id)` | `dict` — `{"windows": [...]}` (class, title, address, geometry) |
| `focus_window` | `focus_window(seat_id, match)` | `dict` — `{"window": ...}`; `match` is a class/title regex |
| `resize_window` | `resize_window(seat_id, match, width, height)` | `dict` |
| `move_window` | `move_window(seat_id, match, x, y)` | `dict` |
| `float_window` | `float_window(seat_id, match, on=True)` | `dict` |
| `set_theme` | `set_theme(seat_id, name)` | `dict` — `omarchy theme set <name>` |
| `clipboard_get` | `clipboard_get(seat_id)` | `dict` — `{"text": ...}` (`wl-paste`) |
| `clipboard_set` | `clipboard_set(seat_id, text)` | `dict` (`wl-copy`) |
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

**Heartbeats and seat lifetime.** Agents do **not** babysit heartbeats.
omavroom owns the seat lifetime: the MCP server auto-beats every tracked
seat's lease on a background thread (at `min(heartbeat_interval_s,
heartbeat_timeout_s/2)`) and refreshes opportunistically on each tool call.
A long-running exec therefore never makes the agent look dead. A heartbeat
timeout only detects a genuinely dead MCP server process.

When a lease does lapse, the daemon does not destroy the seat: it puts it
into **stasis** (`held`), preserving the VM and overlay. If a durable export
intent was recorded by an explicit `export_seat`, the normal gated
fetch → gate → push runs automatically; a seat with nothing to preserve (no
VM) is finalized to `off` instead. Stasis is visible in `omavroom status`
and the *needs-attention* panel. Recover with `retry_release` (re-run the
export, then destroy) or tear it down with `force_discard`; `leases.held_ttl_s`
(default `0` = keep indefinitely) can bound how long stasis holds a seat.

**Screenshots.** `screenshot` returns the PNG as MCP image content (the agent
reads it) *and* as base64 text. By default (`max_width=None` or `0`) it returns
the **native, full-resolution** frame; pass a positive `max_width` to downscale
(hard cap 2048) and `region=[x, y, w, h]` to crop the native frame before
encoding. The encoded size is still capped at `max_bytes` (default 2 000 000,
hard cap 8 000 000), so a response is never multi-MB. If the image needs
resizing/cropping but the resizer (ImageMagick) is unavailable, the call fails
with a clear error instead of returning an oversized image.

**Typing.** `input` events are `{"kind": "key"|"text"|"type"|"click",
"value": ...}`. `key` is one keysym (`"Return"`) or a `Mod+...+Key` combo
(`"Super+Return"`); `text` types in bulk; `type` types **per character** via
`wtype -d` and takes an optional `"delay_ms"` so incremental rendering can be
exercised; `click` is `"x,y[,button]"`.

**File transfer.** `copy_in`/`copy_out` move one file over the pinned SSH key.
The **host** side is constrained to the daemon's transfer root (under the
omavroom data dir, `~/.local/share/omavroom/transfer` by default); a path
outside it is rejected. Guest paths must be absolute. Transfers are `scp` argv
lists — never a host shell string.

**Desktop helpers.** On desktop seats, `launch_app` starts an app (TUI via
`omarchy-launch-tui`, otherwise a direct Hyprland dispatch), `list_windows`
returns the windows, and `focus_window`/`resize_window`/`move_window`/
`float_window` act on the window whose class/title matches a regex.
`set_theme` applies an Omarchy theme, and `clipboard_get`/`clipboard_set` use
`wl-paste`/`wl-copy`. These use this Omarchy/Hyprland version's **Lua**
dispatcher form, e.g. `hyprctl dispatch 'hl.dsp.focus({ window =
"address:0x..." })'` (the plain `hyprctl dispatch exec ...` form is rejected).

**Concurrency.** The MCP server keeps one shared daemon connection for fast
calls and uses a dedicated short-lived connection for desktop ops
(`screenshot`/`input`/`copy_*`/`launch_app`/`*_window`/`set_theme`/
`clipboard_*`/`peek_endpoint`). A desktop op waits for the seat's exclusive
lock at most `desktop_lock_timeout_s` (default 5 s) and then fails with wire
code `seat_busy` rather than blocking behind a long export/reset/release;
because it uses its own connection, a blocked desktop op cannot starve
`heartbeat` or any other tool.

**Typical flow.**

```text
request_seat(agent_label, "desktop", project="my-repo")
wait_for_seat(request_id)              # -> seat.id
prepare_repo(seat_id, url) + job_wait  # optional
exec_run(seat_id, "git status")        # short
exec_start(seat_id, "make test")       # long
exec_poll(seat_id, exec_id) ...          # heartbeat is automatic
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
- **Timeout.** `timeout_s` is applied as a hard transport timeout (exit code
  `124`). `exec.max_runtime_s` is an optional engine-wide ceiling: when it is
  positive a requested timeout is clamped to it, and `0` (the default) means
  no engine cap, so a long build/test is never killed by a fixed timer.
- **Kill.** `exec_kill` marks the exec `killed` immediately and signals the
  worker; with the libvirt provisioner the worker terminates the local `ssh`
  process, closing the channel (a command that daemonises itself in the guest
  may outlive the channel — documented limitation). The `signal` value only
  sets the recorded `exit_code` (`-signal`); it is *not* delivered to the
  guest process.
- **Release/reset.** Releasing or resetting a seat cancels its running execs
  without waiting for them, so teardown is never blocked by a long command.

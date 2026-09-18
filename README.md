# omavroom

A room full of disposable Omarchy VMs where coding agents can work without
ever touching the operator's desktop session. Agents run on the host; each
agent gets its own disposable VM seat — `desktop` (a real Omarchy/Hyprland
guest whose screen exists only as a framebuffer, never rendered on the
host) or headless `terminal` — does its work, pushes it to a repo, and
releases the seat, which destroys the VM. See `PLAN.md` for the full
design and `PHASES.md` for the build order.

> Phase 5: the real exec engine (per-exec worker threads, streaming ring
> buffers, kill/timeout) and the FastMCP server are in place. Phase 4 added
> the scheduler + state core (atomic claiming, fair queue, leases/heartbeats
> with auto-reclaim, dynamic admission, destroy-on-release, content-gated
> export, reattach/reconcile) and the Phase 4B libvirt provisioner. The CLI/TUI
> is still to come; tests exercise everything against an in-process
> `FakeProvisioner`.

## Quickstart

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/)
(installed user-locally to `~/.local/bin` via the official installer).

```bash
cd /home/washburnello/Work/omavroom
uv sync --group dev   # create .venv and install dev tools (pytest, ruff)
uv run pytest         # run the test suite (no VMs)
uv run omavroom --help
```

## MCP server

`omavroom-mcp` serves the seat manager to agents over MCP (stdio), auto-starting
the daemon when one is not already running. The opencode config snippet, the
full tool list, and usage guidance live in [`docs/mcp.md`](docs/mcp.md).

```bash
uv run omavroom-mcp --help
```

Configuration is optional: without a config file, sane defaults apply
(per-seat-type min/max, mandatory resource caps, dynamic admission with a
headroom floor, bounded prewarm, and export content-gate limits). To
override, point `$OMAVROOM_CONFIG` at a TOML file using the section layout
documented in `omavroom/config.py`.

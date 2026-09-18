# omavroom

A room full of disposable Omarchy VMs where coding agents can work without
ever touching the operator's desktop session. Agents run on the host; each
agent gets its own disposable VM seat — `desktop` (a real Omarchy/Hyprland
guest whose screen exists only as a framebuffer, never rendered on the
host) or headless `terminal` — does its work, pushes it to a repo, and
releases the seat, which destroys the VM. See `PLAN.md` for the full
design and `PHASES.md` for the build order.

> Phase 4A: scheduler + state core complete and VM-free (atomic claiming,
> fair queue, leases/heartbeats with auto-reclaim, dynamic admission,
> destroy-on-release, content-gated export, reattach/reconcile). The
> libvirt provisioner, MCP server, and CLI/TUI are still to come; the
> scheduler is exercised against an in-process `FakeProvisioner`.

## Quickstart

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/)
(installed user-locally to `~/.local/bin` via the official installer).

```bash
cd /home/washburnello/Work/omavroom
uv sync --group dev   # create .venv and install dev tools (pytest, ruff)
uv run pytest         # run the test suite
uv run omavroom --help
```

Configuration is optional: without a config file, sane defaults apply
(per-seat-type min/max, mandatory resource caps, dynamic admission with a
headroom floor, bounded prewarm, and export content-gate limits). To
override, point `$OMAVROOM_CONFIG` at a TOML file using the section layout
documented in `omavroom/config.py`.

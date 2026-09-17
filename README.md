# omavroom

A room full of disposable Omarchy VMs where coding agents can work without
ever touching the operator's desktop session. Agents run on the host; each
agent gets its own disposable VM seat — `desktop` (a real Omarchy/Hyprland
guest whose screen exists only as a framebuffer, never rendered on the
host) or headless `terminal` — does its work, pushes it to a repo, and
releases the seat, which destroys the VM. See `PLAN.md` for the full
design and `PHASES.md` for the build order.

> Phase 0 skeleton: package layout, config schema, sqlite stub, and CLI
> stubs only. No VMs yet.

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
(4-unit capacity budget, `desktop` = 4 units, `terminal` = 1 unit). To
override, point `$OMAVROOM_CONFIG` at a TOML file using the section
layout documented in `omavroom/config.py`.

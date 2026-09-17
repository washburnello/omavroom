"""MCP server: the agent-facing interface (stub for Phase 5).

Future home of the FastMCP server exposing the async-first toolset from
PLAN.md — `pool_status`, `request_seat`/`seat_status`, `exec_start`/
`exec_poll`/`exec_kill` with capped ring-buffer output, `heartbeat` on
its own channel, `screenshot` (downscaled framebuffer PNG), `input`,
`peek_url`/`peek_attach`, and `release_seat`. Nothing agent-facing
blocks; the server translates tool calls into manager-daemon operations.
"""

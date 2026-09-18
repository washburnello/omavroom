"""MCP server: the agent-facing interface (Phase 5).

:mod:`omavroom.mcp.server` is the FastMCP server exposing the async-first
toolset from PLAN.md — ``pool_status``, ``request_seat``/``seat_status``,
``exec_start``/``exec_poll``/``exec_kill`` with capped ring-buffer output,
``exec_run`` for short bounded commands, ``heartbeat`` on its own channel,
``screenshot`` (downscaled framebuffer PNG), ``input``,
``peek_url``/``peek_attach``, repo/export/release/reset/recovery jobs, and
``reconcile``. Nothing agent-facing blocks: long lifecycle operations return
a ``job_id`` to poll and long execs return an ``exec_id``.

The server is a thin client of the manager daemon over its Unix socket and
never touches libvirt itself. The ``omavroom-mcp`` console script serves it
over stdio for opencode (see ``docs/mcp.md``).
"""

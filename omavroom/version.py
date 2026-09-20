"""Shared version + capability constants for the MCP client/server handshake.

Deliberately dependency-free (no FastMCP, no daemon, no manager) so both the
daemon process and the MCP server can import it without cycles. The daemon
uses these to decide whether a client is stale; the MCP server reports them on
``hello`` at startup.
"""

from __future__ import annotations

from omavroom import __version__

#: Human-readable server name echoed by ``hello``.
SERVER_NAME = "omavroom"

#: Server package version echoed by ``hello``/``ping``.
SERVER_VERSION = __version__

#: MCP client identity/version reported on ``hello``. Bump the version (and/or
#: add capabilities) when the client's wire behaviour changes, so an
#: un-restarted opencode process is distinguishable from a current one.
MCP_CLIENT_NAME = "omavroom-mcp"
MCP_CLIENT_VERSION = "1"

#: Capability names. ``auto_heartbeat`` means the MCP server renews the leases
#: of the seats it holds on the agent's behalf; a client that lacks it predates
#: the auto-heartbeat feature, so the daemon warns and flags it as stale.
CAPABILITY_AUTO_HEARTBEAT = "auto_heartbeat"
MCP_CLIENT_CAPABILITIES: tuple[str, ...] = (CAPABILITY_AUTO_HEARTBEAT,)

__all__ = [
    "CAPABILITY_AUTO_HEARTBEAT",
    "MCP_CLIENT_CAPABILITIES",
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
]

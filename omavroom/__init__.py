"""omavroom: a room full of disposable Omarchy VMs where coding agents work.

Agents run on the host; each agent gets its own disposable VM seat
(`desktop` with a real Hyprland session, or headless `terminal`) that never
touches the operator's desktop session. See PLAN.md for the full design.
"""

__version__ = "0.0.1"

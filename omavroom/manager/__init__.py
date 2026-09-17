"""Seat manager: scheduler + libvirt/QEMU control plane (stub for Phase 4).

Future home of the capacity pool (per-type min/max from config, atomic
seat claiming, fair queue with positions), leases with the independent
heartbeat channel and auto-reclaim of dead leases, dynamic admission
(live free-RAM check minus the headroom floor), mandatory per-seat
CPU/RAM caps and overlay disk quotas, and destroy-on-release with
work-export verification. The manager is the only component that ever
talks to libvirt/QEMU; agents see only the MCP tools.
"""

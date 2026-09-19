"""Static configuration schema for omavroom.

Config loads from a TOML file with these section names, falling back to the
defaults for anything unset:

    [capacity]
    total_units = 4
    [seats.desktop]
    cost_units = 4
    min_seats = 0
    max_seats = 1
    image = "golden-omarchy"
    [seats.terminal]
    cost_units = 1
    min_seats = 0
    max_seats = 2
    image = "omavroom-base"
    [images.golden-omarchy]
    golden = "~/.local/share/omavroom/images/golden-omarchy.qcow2"
    seat_type = "desktop"
    [images.golden-desktop]
    golden = "~/.local/share/omavroom/images/golden-desktop.qcow2"
    seat_type = "desktop"
    [images.golden-term]
    golden = "~/.local/share/omavroom/images/golden-term.qcow2"
    seat_type = "terminal"
    [resources.desktop]
    cpu_vcpus = 2
    memory_mb = 4096
    overlay_max_gb = 20
    [resources.terminal]
    cpu_vcpus = 2
    memory_mb = 2048
    overlay_max_gb = 10
    [host]
    headroom_floor_mb = 2048
    [network]
    subnet_prefix = "192.168.122"
    gateway = "192.168.122.1"
    dns = ["192.168.122.1"]
    host_range_start = 200
    host_range_end = 250
    [admission]
    dynamic = true
    override = "auto"
    [leases]
    lease_timeout_s = 1800
    heartbeat_interval_s = 60
    heartbeat_timeout_s = 300
    held_ttl_s = 0
    [exec]
    max_output_bytes = 1048576
    max_runtime_s = 3600
    max_concurrent_per_seat = 4
    max_concurrent_total = 16
    [export]
    max_files_changed = 200
    max_insertions = 5000
    max_deletions = 5000
    protected_paths = [".git/", ".github/"]
    approval_required = false
    [prewarm]
    max_retries = 1
    backoff_s = 60
    [golden]
    profile = "stock"
    [gui]
    thumbnail_width = 480
    focused_width = 1024
    focused_interval_s = 0.5
    wall_interval_s = 2.0
    live_mode = "stills"

Config discovery precedence (first that exists wins): an explicit ``path``
argument, then ``$OMAVROOM_CONFIG``, then the per-user XDG config
``$XDG_CONFIG_HOME/omavroom/config.toml`` (``~/.config`` when unset), then the
built-in defaults. The ``[golden]`` section selects where golden images come
from: ``stock`` (the default) builds from the stock Omarchy image, ``mirror``
mirrors this machine's Omarchy. Only the setting is modelled here; the golden
build itself lives elsewhere.

``[gui]`` tunes the native Command Center's adaptive monitor capture. The wall
captures every desktop seat as a cheap ``thumbnail_width`` still on the slow
``wall_interval_s`` cadence, and only the focused monitor at ``focused_width``
on the fast ``focused_interval_s`` cadence, so enlarging a monitor gets crisp,
current frames without re-capturing the whole wall at high resolution.
``live_mode`` is ``stills`` (default) or ``vnc``; ``vnc`` streams the focused
desktop monitor over its passwordless VNC endpoint (falling back to stills with
a notice on any stream error). ``focused_width`` must be at least
``thumbnail_width`` and ``wall_interval_s`` at least ``focused_interval_s``.

``[network]`` pins the static address the provisioner gives each seat so two
concurrent seats can never collide on one DHCP lease (the goldens share a
machine-id/DUID, so dnsmasq would hand out a single lease). ``subnet_prefix``
is the first three octets of the libvirt NAT subnet, ``gateway``/``dns`` are
the resolvers, and ``host_range_start``/``host_range_end`` bound the pool of
guest addresses (the last octet). :meth:`NetworkConfig.allocate` hands out a
unique, per-seat-deterministic address from that range; the provisioner writes
it into the guest over the qemu-guest-agent channel and SSHes to it directly.

Phase 4 scheduler policy is deliberately *per seat type* (a locked PLAN.md
decision: the operator, not the scheduler, decides how capacity splits
between lanes):

- ``seats.<type>.max_seats`` is the hard cap on concurrently occupying
  seats of that type; requests beyond it queue.
- ``seats.<type>.min_seats`` is the prewarm floor: the manager keeps at
  least this many seats provisioned (each still bounded by admission).
- ``resources.<type>`` are the mandatory per-seat caps handed to libvirt
  (CPU/RAM) plus the overlay disk quota.
- ``seats.<type>.image`` names the image a seat is provisioned from, and
  ``[images.<name>]`` maps that name to a read-only golden qcow2
  (``golden``) and the seat type it may serve (``seat_type``). The
  provisioner resolves the name with :meth:`Config.golden_for`; an
  unregistered name (including the stock default ``omavroom-base``) falls
  back to the seat type's ``golden-<type>`` entry. Desktop seats default
  to ``golden-omarchy`` (the Omarchy 4.0.4 golden); ``golden-desktop``
  stays registered as the fallback, so switching back is the single
  ``seats.desktop.image`` line above.
- ``admission`` controls the dynamic live-RAM check: ``dynamic`` enables
  it, ``override`` is a manual escape hatch (``auto`` = normal,
  ``allow`` = skip the live-RAM gate but still honour static bounds,
  ``deny`` = refuse all new admissions and drain).

``capacity.total_units`` / ``cost_units`` are retained from the Phase 0
unit-budget model and validated, but the Phase 4 scheduler bounds by
per-type ``max_seats`` (the later decision supersedes the flat unit pool);
they stay for the Phase 10 capacity-tuning guide.

Static values bound; live measurement admits. Validation is strict: unknown
sections, unknown seat types, unknown keys inside any section, non-table
sections, and wrongly typed values are all rejected with ValueError
(integral floats are coerced to int).
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import threading
import tomllib
from dataclasses import dataclass, field, is_dataclass, replace
from pathlib import Path

CONFIG_ENV_VAR = "OMAVROOM_CONFIG"

SEAT_TYPES: tuple[str, ...] = ("desktop", "terminal")

DEFAULT_IMAGE = "omavroom-base"
DEFAULT_GOLDEN_BY_TYPE: dict[str, str] = {
    "desktop": "golden-desktop",
    "terminal": "golden-term",
}
ADMISSION_OVERRIDES: tuple[str, ...] = ("auto", "allow", "deny")
#: Golden-image source profiles: ``stock`` (default) or ``mirror``.
GOLDEN_PROFILES: tuple[str, ...] = ("stock", "mirror")
DEFAULT_GOLDEN_PROFILE = "stock"

#: GUI capture modes: ``stills`` (default) renders periodic PNG framebuffers;
#: ``vnc`` streams the focused desktop monitor in real time, falling back to
#: stills for that seat (with a notice) on any VNC error.
LIVE_MODES: tuple[str, ...] = ("stills", "vnc")
DEFAULT_LIVE_MODE = "stills"

#: Serializes writes to the per-user config file (the daemon is the only
#: writer, but a single daemon may serve concurrent clients).
_CONFIG_WRITE_LOCK = threading.Lock()

_INT = "int"
_BOOL = "bool"
_STR = "str"
_STR_LIST = "str_list"
_FLOAT = "float"

_SECTION_SCHEMA: dict[str, dict[str, str]] = {
    "capacity": {"total_units": _INT},
    "host": {"headroom_floor_mb": _INT},
    "leases": {
        "lease_timeout_s": _INT,
        "heartbeat_interval_s": _INT,
        "heartbeat_timeout_s": _INT,
        "held_ttl_s": _INT,
    },
    "exec": {
        "max_output_bytes": _INT,
        "max_runtime_s": _INT,
        "max_concurrent_per_seat": _INT,
        "max_concurrent_total": _INT,
    },
    "admission": {"dynamic": _BOOL, "override": _STR},
    "network": {
        "subnet_prefix": _STR,
        "gateway": _STR,
        "dns": _STR_LIST,
        "host_range_start": _INT,
        "host_range_end": _INT,
    },
    "export": {
        "max_files_changed": _INT,
        "max_insertions": _INT,
        "max_deletions": _INT,
        "protected_paths": _STR_LIST,
        "approval_required": _BOOL,
        "allowed_remotes": _STR_LIST,
    },
    "prewarm": {"max_retries": _INT, "backoff_s": _INT},
    "golden": {"profile": _STR},
    "gui": {
        "thumbnail_width": _INT,
        "focused_width": _INT,
        "focused_interval_s": _FLOAT,
        "wall_interval_s": _FLOAT,
        "live_mode": _STR,
    },
}

_SEAT_KEYS: dict[str, str] = {
    "cost_units": _INT,
    "min_seats": _INT,
    "max_seats": _INT,
    "image": _STR,
}

_RESOURCE_KEYS: dict[str, str] = {
    "cpu_vcpus": _INT,
    "memory_mb": _INT,
    "overlay_max_gb": _INT,
}


def _coerce_int(field: str, value: object, where: str) -> int:
    """Validate and coerce a config value to int.

    `field` is the dotted field name (e.g. ``capacity.total_units``) used
    in error messages; `where` is ``" in <path>"`` when the file is known.
    Bools are rejected (TOML `true` is not a seat count); integral floats
    are coerced; anything else raises ValueError.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, got {value!r}{where}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{field} must be an integer, got {value!r}{where}")


def _coerce_float(field: str, value: object, where: str) -> float:
    """Validate and coerce a config value to a finite float.

    Bools are rejected (``true`` is not a duration); ints and floats are
    accepted (an integral TOML int means the same thing as ``2.0``); NaN and
    infinity are rejected.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, got {value!r}{where}")
    if isinstance(value, (int, float)):
        coerced = float(value)
        if not math.isfinite(coerced):
            raise ValueError(f"{field} must be finite, got {value!r}{where}")
        return coerced
    raise ValueError(f"{field} must be a number, got {value!r}{where}")


def _coerce(kind: str, field: str, value: object, where: str) -> object:
    """Validate/coerce a single config value according to its declared type."""
    if kind == _INT:
        return _coerce_int(field, value, where)
    if kind == _FLOAT:
        return _coerce_float(field, value, where)
    if kind == _BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean, got {value!r}{where}")
        return value
    if kind == _STR:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string, got {value!r}{where}")
        return value
    if kind == _STR_LIST:
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"{field} must be a list of non-empty strings, got {value!r}{where}")
        return list(value)
    raise AssertionError(f"unknown config value kind: {kind}")


@dataclass
class SeatTypeConfig:
    """Static budget for one seat type."""

    cost_units: int = 1
    min_seats: int = 0
    max_seats: int = 1
    image: str = DEFAULT_IMAGE

    def __post_init__(self) -> None:
        if self.cost_units < 1:
            raise ValueError("cost_units must be >= 1")
        if self.min_seats < 0 or self.max_seats < 0:
            raise ValueError("min_seats/max_seats must be >= 0")
        if self.min_seats > self.max_seats:
            raise ValueError("min_seats must be <= max_seats")
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("image must be a non-empty string")


@dataclass
class ResourceConfig:
    """Mandatory per-seat resource caps (enforced on every VM in Phase 4)."""

    cpu_vcpus: int = 2
    memory_mb: int = 2048
    overlay_max_gb: int = 10

    def __post_init__(self) -> None:
        for name in ("cpu_vcpus", "memory_mb", "overlay_max_gb"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


def default_images_dir() -> Path:
    """Default directory holding read-only golden qcow2 images."""
    return Path.home() / ".local" / "share" / "omavroom" / "images"


@dataclass
class ImageConfig:
    """One named golden image: where it lives and which seat type it serves.

    ``seat_type`` is optional but, when set, a seat of another type may not
    be provisioned from it (the provisioner enforces the match).
    """

    golden: str = ""
    seat_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.golden, str) or not self.golden.strip():
            raise ValueError("golden must be a non-empty string")
        if self.seat_type is not None and self.seat_type not in SEAT_TYPES:
            raise ValueError(f"seat_type must be one of {SEAT_TYPES}, got {self.seat_type!r}")

    @property
    def path(self) -> Path:
        """The golden qcow2 path with ``~`` expanded."""
        return Path(self.golden).expanduser()


@dataclass
class CapacityConfig:
    """Total unit budget shared by all seat types (advisory in Phase 4)."""

    total_units: int = 4

    def __post_init__(self) -> None:
        if self.total_units < 1:
            raise ValueError("total_units must be >= 1")


@dataclass
class HostConfig:
    """Host-level guards for the admission check."""

    headroom_floor_mb: int = 2048

    def __post_init__(self) -> None:
        if self.headroom_floor_mb < 0:
            raise ValueError("headroom_floor_mb must be >= 0")


def _parse_ipv4(value: object, field: str) -> str:
    """Validate a dotted-quad IPv4 literal (leading zeros rejected)."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a dotted-quad IPv4 address, got {value!r}")
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise ValueError(f"{field} must be a dotted-quad IPv4 address, got {value!r}") from exc


def _parse_subnet_prefix(value: object, field: str) -> str:
    """Validate the first three octets of a /24 (e.g. ``192.168.122``)."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a /24 prefix like '192.168.122', got {value!r}")
    parts = value.strip().split(".")
    if len(parts) != 3 or not all(
        part.isdigit() and str(int(part)) == part and 0 <= int(part) <= 255 for part in parts
    ):
        raise ValueError(f"{field} must be a /24 prefix like '192.168.122', got {value!r}")
    return value.strip()


@dataclass
class NetworkConfig:
    """Static per-seat addressing for the libvirt NAT network.

    The goldens share a machine-id/DUID, so a DHCP-only boot lets two seats
    colocate on one lease. Each seat is instead handed a unique address from
    this host range and configured in-guest (systemd-networkd) before SSH.
    ``subnet_prefix`` is the /24 network (the libvirt ``default`` NAT subnet);
    ``host_range_start``/``host_range_end`` are the last-octet bounds.
    """

    subnet_prefix: str = "192.168.122"
    gateway: str | None = None
    dns: tuple[str, ...] | None = None
    host_range_start: int = 200
    host_range_end: int = 250

    def __post_init__(self) -> None:
        self.subnet_prefix = _parse_subnet_prefix(self.subnet_prefix, "network.subnet_prefix")
        # Gateway and DNS default to the subnet's ``.1`` when unset, so a
        # single-key change to ``subnet_prefix`` stays self-consistent.
        if self.gateway is None:
            self.gateway = f"{self.subnet_prefix}.1"
        self.gateway = _parse_ipv4(self.gateway, "network.gateway")
        if not self.gateway.startswith(self.subnet_prefix + "."):
            raise ValueError("network.gateway must be inside network.subnet_prefix")
        if self.dns is None:
            self.dns = (self.gateway,)
        if not isinstance(self.dns, (list, tuple)) or not self.dns:
            raise ValueError("network.dns must list at least one resolver")
        self.dns = tuple(_parse_ipv4(server, "network.dns") for server in self.dns)
        for name in ("host_range_start", "host_range_end"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 254:
                raise ValueError(f"network.{name} must be in 1..254, got {value!r}")
        if self.host_range_start > self.host_range_end:
            raise ValueError("network.host_range_start must be <= host_range_end")

    @property
    def prefix_len(self) -> int:
        """The CIDR prefix length implied by ``subnet_prefix`` (always /24)."""
        return 24

    def ip_for_host(self, host: int) -> str:
        """Render a ``subnet_prefix`` address for a last-octet ``host``."""
        if not self.host_range_start <= host <= self.host_range_end:
            raise ValueError(
                f"host {host} outside configured range "
                f"{self.host_range_start}..{self.host_range_end}"
            )
        return f"{self.subnet_prefix}.{host}"

    def host_ips(self) -> list[str]:
        """Every address in the configured pool, in ascending order."""
        return [self.ip_for_host(h) for h in range(self.host_range_start, self.host_range_end + 1)]

    def allocate(self, seat_name: str, used: set[str]) -> str:
        """Return a unique address from the pool for ``seat_name``.

        The starting offset is a stable hash of the seat name, so a given
        seat deterministically prefers the same address across resets and
        re-creation (``where possible``); linear probing then guarantees
        uniqueness against every address already handed out. Raises
        :class:`ValueError` when the pool is exhausted.
        """
        candidates = self.host_ips()
        digest = hashlib.sha256(seat_name.encode()).hexdigest()
        start = int(digest, 16) % len(candidates)
        for offset in range(len(candidates)):
            candidate = candidates[(start + offset) % len(candidates)]
            if candidate not in used:
                return candidate
        raise ValueError(
            f"no free address in network range {self.host_range_start}.."
            f"{self.host_range_end} for {seat_name!r}"
        )


@dataclass
class AdmissionConfig:
    """Dynamic-admission controls (live free RAM minus the headroom floor)."""

    dynamic: bool = True
    override: str = "auto"

    def __post_init__(self) -> None:
        if not isinstance(self.dynamic, bool):
            raise ValueError("dynamic must be a boolean")
        if self.override not in ADMISSION_OVERRIDES:
            raise ValueError(
                f"admission.override must be one of {ADMISSION_OVERRIDES}, got {self.override!r}"
            )


@dataclass
class LeaseConfig:
    """Lease + heartbeat timeouts (reclaim logic lives in the scheduler).

    ``held_ttl_s`` bounds how long a seat may sit in ``held`` (stasis) before
    the pump auto-discards it. ``0`` (the default) means "keep until an
    operator acts" — stasis never destroys on its own.
    """

    lease_timeout_s: int = 1800
    heartbeat_interval_s: int = 60
    heartbeat_timeout_s: int = 300
    held_ttl_s: int = 0

    def __post_init__(self) -> None:
        for name in (
            "lease_timeout_s",
            "heartbeat_interval_s",
            "heartbeat_timeout_s",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.heartbeat_timeout_s < self.heartbeat_interval_s:
            raise ValueError("heartbeat_timeout_s must be >= heartbeat_interval_s")
        if self.lease_timeout_s < self.heartbeat_timeout_s:
            raise ValueError("lease_timeout_s must be >= heartbeat_timeout_s")
        if self.held_ttl_s < 0:
            raise ValueError("held_ttl_s must be >= 0")


@dataclass
class ExecConfig:
    """Caps for agent execs (ring buffer + runtime limits, enforced later)."""

    max_output_bytes: int = 1_048_576
    max_runtime_s: int = 3600
    max_concurrent_per_seat: int = 4
    max_concurrent_total: int = 16

    def __post_init__(self) -> None:
        for name in (
            "max_output_bytes",
            "max_runtime_s",
            "max_concurrent_per_seat",
            "max_concurrent_total",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass
class ExportConfig:
    """Content-gate limits applied to a fetched export before any push.

    ``0`` means "deny anything that trips this cap"; use a large number for
    effectively unlimited. ``protected_paths`` are prefix matches against the
    changed-file list. ``approval_required`` forces a manual decision (the
    export is held, never pushed, until approved).
    """

    max_files_changed: int = 200
    max_insertions: int = 5000
    max_deletions: int = 5000
    protected_paths: tuple[str, ...] = (".git/", ".github/")
    approval_required: bool = False
    #: Optional allowlist of *additional* push remotes. When empty, ``origin``
    #: plus known remotes (from ``git remote``) plus explicit paths/URLs are
    #: accepted. When set, ``origin`` remains allowed (git's default push
    #: remote) and these names are added to what is accepted, so configuring
    #: an allowlist never breaks a default push.
    allowed_remotes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("max_files_changed", "max_insertions", "max_deletions"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if not isinstance(self.approval_required, bool):
            raise ValueError("approval_required must be a boolean")
        self.protected_paths = tuple(self.protected_paths)
        for path in self.protected_paths:
            if not isinstance(path, str) or not path:
                raise ValueError("protected_paths entries must be non-empty strings")
        self.allowed_remotes = tuple(self.allowed_remotes)
        for remote in self.allowed_remotes:
            if not isinstance(remote, str) or not remote.strip():
                raise ValueError("allowed_remotes entries must be non-empty strings")

    def matches_protected(self, path: str) -> str | None:
        """Return the matching protected prefix for ``path``, if any."""
        for prefix in self.protected_paths:
            if path == prefix or path.startswith(prefix):
                return prefix
        return None


@dataclass
class PrewarmConfig:
    """Bounded retry policy for the prewarm floor.

    A prewarm seat that fails provisioning is retried up to ``max_retries``
    additional times (each after ``backoff_s``); after that the type's
    prewarm is suspended until :meth:`Scheduler.clear_prewarm_backoff`.
    """

    max_retries: int = 1
    backoff_s: int = 60

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.backoff_s < 0:
            raise ValueError("backoff_s must be >= 0")


@dataclass
class GoldenConfig:
    """Golden-image source profile.

    ``stock`` builds (out of scope here) from the stock Omarchy image;
    ``mirror`` mirrors this machine's Omarchy. The profile is persisted but
    this package only models the setting and its plumbing.
    """

    profile: str = DEFAULT_GOLDEN_PROFILE

    def __post_init__(self) -> None:
        if self.profile not in GOLDEN_PROFILES:
            raise ValueError(
                f"golden.profile must be one of {GOLDEN_PROFILES}, got {self.profile!r}"
            )


@dataclass
class GuiConfig:
    """Adaptive monitor-capture tuning for the native wall (package A).

    The wall captures every desktop seat cheaply and often only the focused
    monitor at high resolution:

    - ``thumbnail_width``: wall-scale capture width in pixels (cheap).
    - ``focused_width``: capture width for the enlarged/focused monitor. Must
      be at least ``thumbnail_width``; defaults *above* it so focusing reveals
      real detail instead of upscaling a thumbnail.
    - ``focused_interval_s``: fast cadence for the focused monitor.
    - ``wall_interval_s``: slow cadence for the rest of the wall. Must be at
      least ``focused_interval_s`` (the wall is the slower pass).
    - ``live_mode``: ``stills`` (default) or ``vnc``. ``vnc`` streams the
      focused desktop monitor over its VNC endpoint; any stream error falls
      back to stills for that seat with a notice.
    """

    thumbnail_width: int = 480
    focused_width: int = 1024
    focused_interval_s: float = 0.5
    wall_interval_s: float = 2.0
    live_mode: str = DEFAULT_LIVE_MODE

    def __post_init__(self) -> None:
        for name in ("thumbnail_width", "focused_width"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 4096:
                raise ValueError(f"{name} must be an integer in 64..4096, got {value!r}")
        if self.focused_width < self.thumbnail_width:
            raise ValueError("focused_width must be >= thumbnail_width")
        for name in ("focused_interval_s", "wall_interval_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.05
            ):
                raise ValueError(f"{name} must be a number >= 0.05, got {value!r}")
        if self.wall_interval_s < self.focused_interval_s:
            raise ValueError("wall_interval_s must be >= focused_interval_s")
        if self.live_mode not in LIVE_MODES:
            raise ValueError(f"live_mode must be one of {LIVE_MODES}, got {self.live_mode!r}")


def default_config_path() -> Path:
    """Per-user config file: ``$XDG_CONFIG_HOME/omavroom/config.toml``.

    ``XDG_CONFIG_HOME`` falls back to ``~/.config`` per the XDG base-dir spec.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "omavroom" / "config.toml"


def _default_seats() -> dict[str, SeatTypeConfig]:
    # Desktop seats boot the Omarchy 4.0.4 golden. To revert, set
    # ``image="golden-desktop"`` here (or in ``[seats.desktop]`` TOML).
    return {
        "desktop": SeatTypeConfig(cost_units=4, min_seats=0, max_seats=1, image="golden-omarchy"),
        "terminal": SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2),
    }


def _default_resources() -> dict[str, ResourceConfig]:
    return {
        "desktop": ResourceConfig(cpu_vcpus=2, memory_mb=4096, overlay_max_gb=20),
        "terminal": ResourceConfig(cpu_vcpus=2, memory_mb=2048, overlay_max_gb=10),
    }


def _default_images() -> dict[str, ImageConfig]:
    directory = default_images_dir()
    # ``golden-omarchy`` is the default desktop image; the stock
    # ``golden-desktop``/``golden-term`` stay registered as fallbacks.
    by_name = {name: seat_type for seat_type, name in DEFAULT_GOLDEN_BY_TYPE.items()}
    by_name["golden-omarchy"] = "desktop"
    return {
        name: ImageConfig(golden=str(directory / f"{name}.qcow2"), seat_type=seat_type)
        for name, seat_type in by_name.items()
    }


@dataclass
class Config:
    """Root config object; see module docstring for the TOML layout."""

    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    seats: dict[str, SeatTypeConfig] = field(default_factory=_default_seats)
    resources: dict[str, ResourceConfig] = field(default_factory=_default_resources)
    images: dict[str, ImageConfig] = field(default_factory=_default_images)
    host: HostConfig = field(default_factory=HostConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    leases: LeaseConfig = field(default_factory=LeaseConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    prewarm: PrewarmConfig = field(default_factory=PrewarmConfig)
    golden: GoldenConfig = field(default_factory=GoldenConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

    @classmethod
    def default(cls) -> Config:
        """Return a Config with all sane defaults (no file needed)."""
        return cls()

    @classmethod
    def from_toml(cls, path: str | Path) -> Config:
        """Load config from a TOML file; unset keys keep their defaults."""
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config TOML must be a table at the top level")
        return cls._from_dict(data, source=str(path))

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load the effective config, in precedence order.

        First that exists wins: an explicit ``path``, ``$OMAVROOM_CONFIG``, the
        per-user :func:`default_config_path` (``~/.config/omavroom/config.toml``),
        then the built-in defaults.
        """
        if path is not None:
            return cls.from_toml(path)
        env_path = os.environ.get(CONFIG_ENV_VAR)
        if env_path:
            return cls.from_toml(env_path)
        user_path = default_config_path()
        if user_path.exists():
            return cls.from_toml(user_path)
        return cls.default()

    @classmethod
    def validate_value(cls, section: str, key: str, value: object) -> object:
        """Validate one scalar ``section.key`` against the schema; return it coerced.

        Uses the same strict rules as file loading (:func:`_coerce`) and the
        same error style. Unknown sections/keys and wrongly typed values all
        raise :class:`ValueError`.
        """
        schema = _SECTION_SCHEMA.get(section)
        if schema is None:
            raise ValueError(f"unknown config section: {section!r}")
        if key not in schema:
            raise ValueError(f"unknown key in [{section}]: {key!r}")
        return _coerce(schema[key], f"{section}.{key}", value, "")

    def set_value(self, section: str, key: str, value: object) -> object:
        """Validate ``section.key = value`` and apply it in place; return the value.

        Every other field of the section is preserved (the section dataclass is
        rebuilt with :func:`dataclasses.replace`).
        """
        return self.set_values(section, {key: value})[key]

    def set_values(self, section: str, values: dict[str, object]) -> dict[str, object]:
        """Validate and apply several ``section`` keys atomically.

        All keys are validated against the schema first, then the section
        dataclass is rebuilt **once**, so cross-field rules (e.g.
        ``focused_width >= thumbnail_width``) are checked against the combined
        new values rather than each write in turn. On any error the config is
        left untouched. Returns the coerced values.
        """
        if not isinstance(values, dict) or not values:
            raise ValueError("set_values requires a non-empty mapping")
        schema = _SECTION_SCHEMA.get(section)
        if schema is None:
            raise ValueError(f"unknown config section: {section!r}")
        unknown = set(values) - set(schema)
        if unknown:
            raise ValueError(f"unknown key in [{section}]: {sorted(unknown)[0]!r}")
        current = getattr(self, section, None)
        if current is None or not is_dataclass(current):
            raise ValueError(f"unknown config section: {section!r}")
        coerced = {
            key: _coerce(schema[key], f"{section}.{key}", value, "")
            for key, value in values.items()
        }
        # ``replace`` runs the section's ``__post_init__`` (cross-field checks)
        # before the attribute is reassigned, so a failure is atomic.
        setattr(self, section, replace(current, **coerced))
        return coerced

    @property
    def golden_profile(self) -> str:
        """The configured golden-image source profile (``stock``/``mirror``)."""
        return self.golden.profile

    def image_for(self, seat_type: str) -> str:
        """Default golden image for a seat type."""
        return self.seats[seat_type].image

    def resources_for(self, seat_type: str) -> ResourceConfig:
        """Mandatory resource caps for a seat type."""
        return self.resources[seat_type]

    def golden_for(self, seat_type: str, image: str | None = None) -> Path:
        """Resolve a seat's image name to the golden qcow2 path.

        A registered image (``[images.<name>]``) wins and, when it declares a
        ``seat_type``, must match the requested seat type. An unregistered
        image name (including the historical default ``omavroom-base``) falls
        back to the seat type's own ``golden-<seat_type>`` entry. Desktop
        seats default to the registered ``golden-omarchy``; ``golden-desktop``
        remains the per-type fallback, so the stock config still resolves to a
        golden for every seat type without any TOML.
        """
        if seat_type not in SEAT_TYPES:
            raise ValueError(f"unknown seat type: {seat_type!r}")
        name = image or self.image_for(seat_type)
        entry = self.images.get(name)
        if entry is None:
            fallback_name = DEFAULT_GOLDEN_BY_TYPE.get(seat_type)
            fallback = self.images.get(fallback_name) if fallback_name else None
            if fallback is None:
                raise KeyError(
                    f"no golden image registered for {name!r} or seat type {seat_type!r}"
                )
            return fallback.path
        if entry.seat_type is not None and entry.seat_type != seat_type:
            raise ValueError(
                f"image {name!r} is for seat type {entry.seat_type!r}, not {seat_type!r}"
            )
        return entry.path

    @classmethod
    def _from_dict(cls, data: dict, source: str | None = None) -> Config:
        where = f" in {source}" if source is not None else ""
        allowed_sections = set(_SECTION_SCHEMA) | {"seats", "resources", "images"}
        unknown_sections = set(data) - allowed_sections
        if unknown_sections:
            raise ValueError(f"unknown config sections: {sorted(unknown_sections)}")
        section_values: dict[str, dict[str, object]] = {}
        for name, schema in _SECTION_SCHEMA.items():
            raw = data.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"[{name}] must be a table, got {type(raw).__name__}{where}")
            unknown = set(raw) - set(schema)
            if unknown:
                raise ValueError(f"unknown keys in [{name}]: {sorted(unknown)}{where}")
            section_values[name] = {
                key: _coerce(schema[key], f"{name}.{key}", value, where)
                for key, value in raw.items()
            }
        seats_data = cls._parse_typed_table(
            data, "seats", _SEAT_KEYS, where, allow_unknown_keys=False
        )
        resources_data = cls._parse_typed_table(
            data, "resources", _RESOURCE_KEYS, where, allow_unknown_keys=False
        )
        images_data = data.get("images", {})
        if not isinstance(images_data, dict):
            raise ValueError(f"[images] must be a table, got {type(images_data).__name__}{where}")
        for name, overrides in images_data.items():
            dotted = f"images.{name}"
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"[{dotted}] must be a table, got {type(overrides).__name__}{where}"
                )
            unknown = set(overrides) - {"golden", "seat_type"}
            if unknown:
                raise ValueError(f"unknown keys in [{dotted}]: {sorted(unknown)}{where}")
            golden = overrides.get("golden")
            if golden is not None and (not isinstance(golden, str) or not golden.strip()):
                raise ValueError(f"{dotted}.golden must be a non-empty string{where}")
            seat_type = overrides.get("seat_type")
            if seat_type is not None and seat_type not in SEAT_TYPES:
                raise ValueError(f"{dotted}.seat_type must be one of {SEAT_TYPES}{where}")
        cfg = cls.default()
        if section_values["capacity"]:
            cfg.capacity = CapacityConfig(**section_values["capacity"])
        if section_values["host"]:
            cfg.host = HostConfig(**section_values["host"])
        if section_values["network"]:
            cfg.network = NetworkConfig(**section_values["network"])
        if section_values["leases"]:
            cfg.leases = LeaseConfig(**section_values["leases"])
        if section_values["exec"]:
            cfg.exec = ExecConfig(**section_values["exec"])
        if section_values["admission"]:
            cfg.admission = AdmissionConfig(**section_values["admission"])
        if section_values["export"]:
            cfg.export = ExportConfig(**section_values["export"])
        if section_values["prewarm"]:
            cfg.prewarm = PrewarmConfig(**section_values["prewarm"])
        if section_values["golden"]:
            cfg.golden = GoldenConfig(**section_values["golden"])
        if section_values["gui"]:
            cfg.gui = GuiConfig(**section_values["gui"])
        for seat_type, overrides in seats_data.items():
            base = cfg.seats[seat_type]
            cfg.seats[seat_type] = SeatTypeConfig(
                cost_units=overrides.get("cost_units", base.cost_units),
                min_seats=overrides.get("min_seats", base.min_seats),
                max_seats=overrides.get("max_seats", base.max_seats),
                image=overrides.get("image", base.image),
            )
        for seat_type, overrides in resources_data.items():
            base = cfg.resources[seat_type]
            cfg.resources[seat_type] = ResourceConfig(
                cpu_vcpus=overrides.get("cpu_vcpus", base.cpu_vcpus),
                memory_mb=overrides.get("memory_mb", base.memory_mb),
                overlay_max_gb=overrides.get("overlay_max_gb", base.overlay_max_gb),
            )
        for name, overrides in images_data.items():
            base = cfg.images.get(name)
            cfg.images[name] = ImageConfig(
                golden=overrides.get("golden", base.golden if base is not None else ""),
                seat_type=overrides.get("seat_type", base.seat_type if base is not None else None),
            )
        return cfg

    @classmethod
    def _parse_typed_table(
        cls,
        data: dict,
        section: str,
        key_schema: dict[str, str],
        where: str,
        *,
        allow_unknown_keys: bool,
    ) -> dict[str, dict[str, object]]:
        """Parse a ``[section.<seat_type>]`` table of typed per-type keys."""
        raw_section = data.get(section, {})
        if not isinstance(raw_section, dict):
            raise ValueError(
                f"[{section}] must be a table, got {type(raw_section).__name__}{where}"
            )
        unknown_types = set(raw_section) - set(SEAT_TYPES)
        if unknown_types:
            raise ValueError(f"unknown seat types: {sorted(unknown_types)}")
        parsed: dict[str, dict[str, object]] = {}
        for seat_type, overrides in raw_section.items():
            dotted = f"{section}.{seat_type}"
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"[{dotted}] must be a table, got {type(overrides).__name__}{where}"
                )
            unknown = set(overrides) - set(key_schema)
            if unknown and not allow_unknown_keys:
                raise ValueError(f"unknown keys in [{dotted}]: {sorted(unknown)}{where}")
            parsed[seat_type] = {
                key: _coerce(key_schema[key], f"{dotted}.{key}", value, where)
                for key, value in overrides.items()
            }
        return parsed


def _toml_value(value: object) -> str:
    """Serialize one TOML scalar/list value (config values are scalars/lists)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"cannot serialize {value!r} to TOML")


def _dump_toml(data: dict) -> str:
    """Serialize a config document back to TOML text.

    The schema is at most two levels deep (sections, plus ``seats.<type>`` /
    ``resources.<type>`` / ``images.<name>``). Key order and comments are not
    preserved; every key is.
    """
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for section, body in data.items():
        if not isinstance(body, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        for key, value in body.items():
            if not isinstance(value, dict):
                lines.append(f"{key} = {_toml_value(value)}")
        for name, overrides in body.items():
            if not isinstance(overrides, dict):
                continue
            lines.append("")
            lines.append(f"[{section}.{name}]")
            for key, value in overrides.items():
                lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def set_config_value(
    section: str,
    key: str,
    value: object,
    *,
    path: str | Path | None = None,
) -> tuple[object, Path]:
    """Validate and persist one ``section.key = value`` to the user config.

    Validation reuses :meth:`Config.validate_value`, and the merged document is
    re-parsed with :meth:`Config._from_dict` before writing, so a persisted file
    can never be one the loader would reject. Returns ``(coerced_value, path)``;
    raises :class:`ValueError` for an unknown section/key or a bad value.
    """
    coerced, target = set_config_values(section, {key: value}, path=path)
    return coerced[key], target


def set_config_values(
    section: str,
    values: dict[str, object],
    *,
    path: str | Path | None = None,
) -> tuple[dict[str, object], Path]:
    """Validate and persist several ``section`` keys in one atomic write.

    The GUI applies a whole settings change at once (e.g. raising
    ``thumbnail_width`` and ``focused_width`` together), so a valid combined
    change is never rejected mid-sequence. Each value is schema-validated and
    the merged document is re-parsed with :meth:`Config._from_dict` (which
    enforces cross-field rules) before a single write. Returns
    ``(coerced_values, path)``; raises :class:`ValueError` for an unknown
    section/key or a bad value, leaving the file untouched.
    """
    if not isinstance(values, dict) or not values:
        raise ValueError("set_config_values requires a non-empty mapping")
    coerced = {key: Config.validate_value(section, key, value) for key, value in values.items()}
    target = Path(path) if path is not None else default_config_path()
    with _CONFIG_WRITE_LOCK:
        data: dict = {}
        if target.exists():
            data = tomllib.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config TOML must be a table at the top level")
        body = data.get(section)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ValueError(f"[{section}] must be a table, got {type(body).__name__}")
        body.update(coerced)
        data[section] = body
        Config._from_dict(data, source=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dump_toml(data), encoding="utf-8")
    return coerced, target

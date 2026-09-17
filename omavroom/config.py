"""Static configuration schema for omavroom (Phase 0 skeleton).

Defaults match PLAN.md's 4-unit starting point: a `desktop` seat costs 4
units, a `terminal` seat costs 1, out of a 4-unit capacity budget
(~1 desktop seat on this 15 GB box today; tune after measuring real RAM
use — see the Phase 10 capacity-tuning guide, not yet written).

Config loads from a TOML file with these same section names, falling back
to the defaults for anything unset:

    [capacity]
    total_units = 4
    [seats.desktop]
    cost_units = 4
    min_seats = 0
    max_seats = 1
    [seats.terminal]
    cost_units = 1
    min_seats = 0
    max_seats = 2
    [host]
    headroom_floor_mb = 2048
    [leases]
    lease_timeout_s = 1800
    heartbeat_interval_s = 60
    heartbeat_timeout_s = 300
    [exec]
    max_output_bytes = 1048576
    max_runtime_s = 3600
    max_concurrent_per_seat = 4

Static values bound; live measurement admits (Phase 4 adds the dynamic
free-RAM admission check minus the headroom floor at claim time).

Validation is strict: unknown sections, unknown seat types, unknown keys
inside any section, non-table sections, and non-integer values are all
rejected with ValueError (integral floats are coerced to int).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV_VAR = "OMAVROOM_CONFIG"

SEAT_TYPES: tuple[str, ...] = ("desktop", "terminal")

_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "capacity": ("total_units",),
    "host": ("headroom_floor_mb",),
    "leases": ("lease_timeout_s", "heartbeat_interval_s", "heartbeat_timeout_s"),
    "exec": ("max_output_bytes", "max_runtime_s", "max_concurrent_per_seat"),
}

_SEAT_KEYS: tuple[str, ...] = ("cost_units", "min_seats", "max_seats")


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


@dataclass
class SeatTypeConfig:
    """Static budget for one seat type."""

    cost_units: int = 1
    min_seats: int = 0
    max_seats: int = 1

    def __post_init__(self) -> None:
        if self.cost_units < 1:
            raise ValueError("cost_units must be >= 1")
        if self.min_seats < 0 or self.max_seats < 0:
            raise ValueError("min_seats/max_seats must be >= 0")
        if self.min_seats > self.max_seats:
            raise ValueError("min_seats must be <= max_seats")


@dataclass
class CapacityConfig:
    """Total unit budget shared by all seat types."""

    total_units: int = 4

    def __post_init__(self) -> None:
        if self.total_units < 1:
            raise ValueError("total_units must be >= 1")


@dataclass
class HostConfig:
    """Host-level guards for the admission check (enforced in Phase 4)."""

    headroom_floor_mb: int = 2048

    def __post_init__(self) -> None:
        if self.headroom_floor_mb < 0:
            raise ValueError("headroom_floor_mb must be >= 0")


@dataclass
class LeaseConfig:
    """Lease + heartbeat timeouts (reclaim logic lands in Phase 4)."""

    lease_timeout_s: int = 1800
    heartbeat_interval_s: int = 60
    heartbeat_timeout_s: int = 300

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


@dataclass
class ExecConfig:
    """Caps for agent execs (ring buffer + runtime limits, enforced later)."""

    max_output_bytes: int = 1_048_576
    max_runtime_s: int = 3600
    max_concurrent_per_seat: int = 4

    def __post_init__(self) -> None:
        for name in (
            "max_output_bytes",
            "max_runtime_s",
            "max_concurrent_per_seat",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass
class Config:
    """Root config object; see module docstring for the TOML layout."""

    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    seats: dict[str, SeatTypeConfig] = field(
        default_factory=lambda: {
            "desktop": SeatTypeConfig(cost_units=4, min_seats=0, max_seats=1),
            "terminal": SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2),
        }
    )
    host: HostConfig = field(default_factory=HostConfig)
    leases: LeaseConfig = field(default_factory=LeaseConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)

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
        """Load config from `path`, `$OMAVROOM_CONFIG`, or defaults, in order."""
        if path is None:
            env_path = os.environ.get(CONFIG_ENV_VAR)
            path = env_path if env_path else None
        if path is None:
            return cls.default()
        return cls.from_toml(path)

    @classmethod
    def _from_dict(cls, data: dict, source: str | None = None) -> Config:
        where = f" in {source}" if source is not None else ""
        unknown_sections = set(data) - set(_SECTION_KEYS) - {"seats"}
        if unknown_sections:
            raise ValueError(f"unknown config sections: {sorted(unknown_sections)}")
        sections: dict[str, dict[str, int]] = {}
        for name, keys in _SECTION_KEYS.items():
            raw = data.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"[{name}] must be a table, got {type(raw).__name__}{where}")
            unknown = set(raw) - set(keys)
            if unknown:
                raise ValueError(f"unknown keys in [{name}]: {sorted(unknown)}{where}")
            sections[name] = {
                key: _coerce_int(f"{name}.{key}", value, where) for key, value in raw.items()
            }
        seats_data = data.get("seats", {})
        if not isinstance(seats_data, dict):
            raise ValueError(f"[seats] must be a table, got {type(seats_data).__name__}{where}")
        unknown_types = set(seats_data) - set(SEAT_TYPES)
        if unknown_types:
            raise ValueError(f"unknown seat types: {sorted(unknown_types)}")
        seats: dict[str, dict[str, int]] = {}
        for seat_type, overrides in seats_data.items():
            section = f"seats.{seat_type}"
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"[{section}] must be a table, got {type(overrides).__name__}{where}"
                )
            unknown = set(overrides) - set(_SEAT_KEYS)
            if unknown:
                raise ValueError(f"unknown keys in [{section}]: {sorted(unknown)}{where}")
            seats[seat_type] = {
                key: _coerce_int(f"{section}.{key}", value, where)
                for key, value in overrides.items()
            }
        cfg = cls.default()
        if sections["capacity"]:
            cfg.capacity = CapacityConfig(**sections["capacity"])
        if sections["host"]:
            cfg.host = HostConfig(**sections["host"])
        if sections["leases"]:
            cfg.leases = LeaseConfig(**sections["leases"])
        if sections["exec"]:
            cfg.exec = ExecConfig(**sections["exec"])
        for seat_type, overrides in seats.items():
            base = cfg.seats[seat_type]
            cfg.seats[seat_type] = SeatTypeConfig(
                cost_units=overrides.get("cost_units", base.cost_units),
                min_seats=overrides.get("min_seats", base.min_seats),
                max_seats=overrides.get("max_seats", base.max_seats),
            )
        return cfg

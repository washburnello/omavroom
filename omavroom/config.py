"""Static configuration schema for omavroom.

Config loads from a TOML file with these section names, falling back to the
defaults for anything unset:

    [capacity]
    total_units = 4
    [seats.desktop]
    cost_units = 4
    min_seats = 0
    max_seats = 1
    image = "omavroom-base"
    [seats.terminal]
    cost_units = 1
    min_seats = 0
    max_seats = 2
    image = "omavroom-base"
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
    [admission]
    dynamic = true
    override = "auto"
    [leases]
    lease_timeout_s = 1800
    heartbeat_interval_s = 60
    heartbeat_timeout_s = 300
    [exec]
    max_output_bytes = 1048576
    max_runtime_s = 3600
    max_concurrent_per_seat = 4
    [export]
    max_files_changed = 200
    max_insertions = 5000
    max_deletions = 5000
    protected_paths = [".git/", ".github/"]
    approval_required = false
    [prewarm]
    max_retries = 1
    backoff_s = 60

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
  back to the seat type's ``golden-<type>`` entry.
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

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV_VAR = "OMAVROOM_CONFIG"

SEAT_TYPES: tuple[str, ...] = ("desktop", "terminal")

DEFAULT_IMAGE = "omavroom-base"
DEFAULT_GOLDEN_BY_TYPE: dict[str, str] = {
    "desktop": "golden-desktop",
    "terminal": "golden-term",
}
ADMISSION_OVERRIDES: tuple[str, ...] = ("auto", "allow", "deny")

_INT = "int"
_BOOL = "bool"
_STR = "str"
_STR_LIST = "str_list"

_SECTION_SCHEMA: dict[str, dict[str, str]] = {
    "capacity": {"total_units": _INT},
    "host": {"headroom_floor_mb": _INT},
    "leases": {
        "lease_timeout_s": _INT,
        "heartbeat_interval_s": _INT,
        "heartbeat_timeout_s": _INT,
    },
    "exec": {
        "max_output_bytes": _INT,
        "max_runtime_s": _INT,
        "max_concurrent_per_seat": _INT,
    },
    "admission": {"dynamic": _BOOL, "override": _STR},
    "export": {
        "max_files_changed": _INT,
        "max_insertions": _INT,
        "max_deletions": _INT,
        "protected_paths": _STR_LIST,
        "approval_required": _BOOL,
    },
    "prewarm": {"max_retries": _INT, "backoff_s": _INT},
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


def _coerce(kind: str, field: str, value: object, where: str) -> object:
    """Validate/coerce a single config value according to its declared type."""
    if kind == _INT:
        return _coerce_int(field, value, where)
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
    """Lease + heartbeat timeouts (reclaim logic lives in the scheduler)."""

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


def _default_seats() -> dict[str, SeatTypeConfig]:
    return {
        "desktop": SeatTypeConfig(cost_units=4, min_seats=0, max_seats=1),
        "terminal": SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2),
    }


def _default_resources() -> dict[str, ResourceConfig]:
    return {
        "desktop": ResourceConfig(cpu_vcpus=2, memory_mb=4096, overlay_max_gb=20),
        "terminal": ResourceConfig(cpu_vcpus=2, memory_mb=2048, overlay_max_gb=10),
    }


def _default_images() -> dict[str, ImageConfig]:
    directory = default_images_dir()
    return {
        name: ImageConfig(golden=str(directory / f"{name}.qcow2"), seat_type=seat_type)
        for seat_type, name in DEFAULT_GOLDEN_BY_TYPE.items()
    }


@dataclass
class Config:
    """Root config object; see module docstring for the TOML layout."""

    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    seats: dict[str, SeatTypeConfig] = field(default_factory=_default_seats)
    resources: dict[str, ResourceConfig] = field(default_factory=_default_resources)
    images: dict[str, ImageConfig] = field(default_factory=_default_images)
    host: HostConfig = field(default_factory=HostConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    leases: LeaseConfig = field(default_factory=LeaseConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    prewarm: PrewarmConfig = field(default_factory=PrewarmConfig)

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
        back to the seat type's own ``golden-<seat_type>`` entry, so the
        stock config provisions ``golden-desktop`` for desktop seats and
        ``golden-term`` for terminal seats without any TOML.
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

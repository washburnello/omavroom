"""Shared fixtures for the Phase 4 scheduler/state tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omavroom.config import Config
from omavroom.manager.provisioner import FakeProvisioner


class FakeClock:
    """Deterministic, manually-advanced UTC clock."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class RamHolder:
    """Mutable fake free-RAM reading, so tests never touch the real host."""

    def __init__(self, value_mb: int = 10_000) -> None:
        self.value = value_mb

    def __call__(self) -> int:
        return self.value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def free_ram() -> RamHolder:
    return RamHolder()


@pytest.fixture
def config() -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    return cfg


@pytest.fixture
def fake() -> FakeProvisioner:
    return FakeProvisioner()


@pytest.fixture
def make_manager(tmp_path, clock, free_ram):
    """Build independent Managers (one sqlite file each) for a test."""
    from omavroom.manager import Manager

    created: list[Manager] = []

    def _make(config_override=None, *, provisioner=None, db_name=None, **kwargs):
        name = db_name or f"state-{len(created)}.db"
        manager = Manager(
            config_override or Config.default(),
            db_path=tmp_path / name,
            provisioner=provisioner if provisioner is not None else FakeProvisioner(),
            clock=kwargs.pop("clock", clock),
            free_ram_mb=kwargs.pop("free_ram_mb", free_ram),
            **kwargs,
        )
        created.append(manager)
        return manager

    return _make


@pytest.fixture
def manager(make_manager, config, fake):
    return make_manager(config, provisioner=fake)

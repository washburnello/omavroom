"""Shared fixtures for the Phase 4 scheduler/state tests."""

from __future__ import annotations

import os

# Hard guard: a test run must never open real windows on the user's session.
# This runs before any Qt import (test modules import PySide6 later), so the
# whole suite is offscreen even if a caller's environment asks for wayland.
# A developer who really wants a visible window sets OMAVROOM_GUI_TEST_REAL=1.
if os.environ.get("OMAVROOM_GUI_TEST_REAL") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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


@pytest.fixture
def fake_daemon(tmp_path):
    """Factory starting an in-process daemon (FakeProvisioner) for CLI/TUI tests.

    Usage::

        with fake_daemon() as pool:
            cli.main(["--socket", str(pool.socket_path), "status"])

    Each call gets a unique socket, database and manager, so a test can start
    more than one. Yielding a namespace keeps the manager (for state setup)
    and the socket path (for clients) in one place.
    """
    from omavroom.daemon import DaemonServer
    from omavroom.manager import Manager

    started: list[str] = []

    @contextmanager
    def _start(*, config=None, provisioner=None, free_ram_mb: int = 10**9):
        csv = config or Config.default()
        if config is None:
            csv.host.headroom_floor_mb = 512
        index = len(started)
        started.append(str(index))
        mgr = Manager(
            csv,
            db_path=tmp_path / f"state-{index}.db",
            provisioner=provisioner or FakeProvisioner(),
            free_ram_mb=lambda: free_ram_mb,
        )
        server = DaemonServer(mgr, socket_path=tmp_path / f"daemon-{index}.sock")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.socket_path.exists():
            if time.monotonic() >= deadline:  # pragma: no cover - startup guard
                raise RuntimeError("fake daemon did not start")
            time.sleep(0.01)
        try:
            yield SimpleNamespace(
                server=server,
                manager=mgr,
                config=csv,
                socket_path=server.socket_path,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    return _start

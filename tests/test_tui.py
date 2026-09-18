"""Phase 6 TUI tests: Textual ``run_test``/Pilot against a fake-provisioner daemon.

No VMs and no real terminal: ``run_test`` drives the app headlessly. Assertions
cover the fixed slot grid, the off/no-signal state, the queue sidebar, the
needs-attention panel, the daemon-down banner and the peek/screenshot actions.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import Static

from omavroom.client import DaemonClient
from omavroom.config import Config
from omavroom.poolview import NO_SIGNAL
from omavroom.tui import OmavroomTUI, SlotWidget, plan_from_config


def _ready(pool, agent, seat_type="terminal", **kwargs):
    client = DaemonClient(pool.socket_path)
    try:
        view = client.request_seat(agent, seat_type, **kwargs).wait_ready(timeout=5)
        return view["seat"]
    finally:
        client.close()


def _config(*, desktop: int = 1, terminal: int = 2) -> Config:
    cfg = Config.default()
    cfg.seats["desktop"].max_seats = desktop
    cfg.seats["terminal"].max_seats = terminal
    return cfg


async def _snapshot(app: OmavroomTUI, pilot, *, focus=None, press=None) -> dict:
    await pilot.pause()
    data = {
        "slots": {s.slot.key: s.slot_text for s in app.query(SlotWidget)},
        "queue": str(app.query_one("#queue-panel", Static).render()),
        "attention": str(app.query_one("#attention-panel", Static).render()),
        "pool_bar": str(app.query_one("#pool-bar", Static).render()),
        "down": app.query_one("#daemon-down", Static).display,
        "down_text": str(app.query_one("#daemon-down", Static).render()),
        "notice": str(app.query_one("#notice", Static).render()),
    }
    if focus:
        app.query_one(f"#slot-{focus}", SlotWidget).focus()
        await pilot.pause()
    if press:
        await pilot.press(press)
        await pilot.pause()
        data["notice"] = str(app.query_one("#notice", Static).render())
    return data


def test_plan_from_config_uses_max_seats():
    slots = plan_from_config(_config(desktop=1, terminal=2))
    assert [slot.key for slot in slots] == ["desktop-0", "terminal-0", "terminal-1"]


def test_slot_grid_reflects_seats_and_off_slots(fake_daemon):
    cfg = _config(desktop=1, terminal=2)
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "alice", "terminal", project="api")

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                return await _snapshot(app, pilot)

        data = asyncio.run(scenario())
        assert set(data["slots"]) == {"desktop-0", "terminal-0", "terminal-1"}
        assert "alice" in data["slots"]["terminal-0"]
        assert "project  api" in data["slots"]["terminal-0"]
        assert NO_SIGNAL in data["slots"]["desktop-0"]
        assert NO_SIGNAL in data["slots"]["terminal-1"]
        assert "terminal 1/2" in data["pool_bar"]


def test_slots_are_stable_across_teardown(fake_daemon):
    cfg = _config(desktop=1, terminal=2)
    with fake_daemon(config=cfg) as pool:
        seat = _ready(pool, "alice", "terminal")

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                before = await _snapshot(app, pilot)
                client = DaemonClient(pool.socket_path)
                try:
                    client.force_discard(seat["id"]).result(timeout=5)
                finally:
                    client.close()
                app.refresh_data()
                after = await _snapshot(app, pilot)
                return before, after

        before, after = asyncio.run(scenario())
        assert set(after["slots"]) == set(before["slots"])  # no slots disappeared
        assert "alice" in before["slots"]["terminal-0"]
        assert NO_SIGNAL in after["slots"]["terminal-0"]  # screen off in place


def test_queue_sidebar_shows_waiters(fake_daemon):
    cfg = _config(desktop=0, terminal=1)
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "first", "terminal")
        client = DaemonClient(pool.socket_path)
        try:
            client.request_seat("second", "terminal", project="later")
        finally:
            client.close()

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                return await _snapshot(app, pilot)

        data = asyncio.run(scenario())
        assert "second" in data["queue"]
        assert "NEXT" in data["queue"]
        assert "later" in data["queue"]


def test_needs_attention_panel_shows_held_seat(fake_daemon):
    cfg = _config(desktop=0, terminal=2)
    with fake_daemon(config=cfg) as pool:
        seat = _ready(pool, "dave", "terminal")
        pool.manager.provisioner.fail_exports = True
        handle = pool.manager.release_seat(seat["id"], repo="demo", branch="task")
        outcome = handle.result(timeout=5)
        assert outcome.held is True

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                return await _snapshot(app, pilot)

        data = asyncio.run(scenario())
        assert "held" in data["attention"]
        assert "dave" in data["attention"]


def test_daemon_down_renders_and_retries(tmp_path):
    cfg = _config()

    async def scenario():
        app = OmavroomTUI(socket_path=tmp_path / "missing.sock", config=cfg, refresh_interval=1000)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.down_message is not None
            banner = app.query_one("#daemon-down", Static)
            assert banner.display is True
            assert "DAEMON NOT RUNNING" in str(banner.render())
            assert "daemon not running" in str(app.query_one("#pool-bar", Static).render())
            await pilot.press("r")
            await pilot.pause()
            assert app.down_message is not None  # still down, still graceful
            return app.down_message

    message = asyncio.run(scenario())
    assert "not running" in message.lower()


def test_peek_action_prints_endpoint(fake_daemon):
    cfg = _config(desktop=1, terminal=0)
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "alice", "desktop")

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                return await _snapshot(app, pilot, focus="desktop-0", press="p")

        data = asyncio.run(scenario())
        assert "vnc://" in data["notice"]


def test_screenshot_action_prints_path_not_image(fake_daemon):
    cfg = _config(desktop=1, terminal=0)
    target = Path(tempfile.gettempdir()) / "omavroom-desktop-1.png"
    target.unlink(missing_ok=True)
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "alice", "desktop")

        async def scenario():
            app = OmavroomTUI(socket_path=pool.socket_path, config=cfg, refresh_interval=1000)
            async with app.run_test(size=(120, 40)) as pilot:
                return await _snapshot(app, pilot, focus="desktop-0", press="s")

        data = asyncio.run(scenario())
        assert ".png" in data["notice"]
        assert target.exists()

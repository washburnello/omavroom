"""Package A: pure adaptive-capture scheduling tests (no Qt, no daemon).

These pin the scheduling rule itself:

- the first tick is a full wall pass at ``thumbnail_width``;
- fast ticks with a focused live desktop capture *only* that seat at
  ``focused_width``;
- the wall pass excludes the focused seat (no double capture) and captures
  every other live desktop seat at ``thumbnail_width``;
- moving/clearing focus re-captures the seat that lost focus at thumbnail
  size on the next wall pass;
- a slow capture coalesces (time-based wall-due), and non-live seats are
  never captured.

The worker-level test injects a fake client and clock so the real
``PollWorker.poll_once`` path is exercised deterministically.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from omavroom.gui.backend import PollWorker  # noqa: E402
from omavroom.gui.capture import CapturePlanner  # noqa: E402


class FakeClock:
    """Manually-advanced monotonic clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeClient:
    """Records every screenshot (seat id, width); no socket."""

    def __init__(self, seats: list[dict]) -> None:
        self.seats = seats
        self.screenshots: list[tuple[int, int]] = []

    def pool_status(self) -> dict:
        return {"seats": self.seats, "per_type": {}}

    def list_execs(self, seat_id: int) -> list[dict]:
        return []

    def screenshot(self, seat_id: int, *, max_width: int | None = None):
        self.screenshots.append((int(seat_id), int(max_width or 0)))
        return f"PNG:{seat_id}:{max_width}".encode()

    def close(self) -> None:  # pragma: no cover - nothing to close
        pass

    def shutdown(self) -> None:  # pragma: no cover - nothing to close
        pass


def _desktop(seat_id: int, state: str = "ready", vm: bool = True) -> dict:
    return {
        "id": seat_id,
        "seat_type": "desktop",
        "state": state,
        "vm_name": f"vm-{seat_id}" if vm else None,
    }


def _planner(clock: FakeClock) -> CapturePlanner:
    return CapturePlanner(
        thumbnail_width=480,
        focused_width=1024,
        focused_interval_s=0.5,
        wall_interval_s=2.0,
        clock=clock,
    )


def _worker(client: FakeClient, planner: CapturePlanner) -> PollWorker:
    return PollWorker("unused", planner=planner, client=client)


# --------------------------------------------------------------------------
# planner decisions
# --------------------------------------------------------------------------
def test_first_tick_is_a_wall_pass_at_thumbnail_width():
    clock = FakeClock()
    plan = _planner(clock).plan(0.0, [_desktop(1), _desktop(2)])
    assert plan.focused is None
    assert [(c.seat_id, c.width, c.focused) for c in plan.wall] == [
        (1, 480, False),
        (2, 480, False),
    ]


def test_fast_tick_captures_only_the_focused_seat_at_focused_width():
    clock = FakeClock()
    planner = _planner(clock)
    planner.plan(0.0, [_desktop(1), _desktop(2)])  # consume the first wall pass
    planner.set_focus(1)
    clock.advance(0.5)
    plan = planner.plan(clock(), [_desktop(1), _desktop(2)])
    assert plan.wall == ()
    assert len(plan.captures) == 1
    assert (plan.focused.seat_id, plan.focused.width, plan.focused.focused) == (1, 1024, True)


def test_wall_pass_excludes_focused_seat_and_never_double_captures():
    clock = FakeClock()
    planner = _planner(clock)
    planner.set_focus(1)
    clock.advance(2.0)  # first tick is also a wall pass
    plan = planner.plan(clock(), [_desktop(1), _desktop(2)])
    by_seat = {capture.seat_id: capture.width for capture in plan.captures}
    assert by_seat == {1: 1024, 2: 480}
    # The focused seat appears exactly once, and only at high resolution.
    assert [c.seat_id for c in plan.captures].count(1) == 1
    assert all(c.width == 1024 for c in plan.captures if c.seat_id == 1)


def test_wall_due_is_time_based_and_coalesces():
    clock = FakeClock()
    planner = _planner(clock)
    planner.plan(0.0, [_desktop(1)])
    clock.advance(1.0)
    assert planner.plan(clock(), [_desktop(1)]).wall == ()  # not due yet
    clock.advance(5.0)  # a slow capture overran several fast ticks
    assert len(planner.plan(clock(), [_desktop(1)]).wall) == 1  # exactly one pass
    assert planner.plan(clock(), [_desktop(1)]).wall == ()  # and not again


def test_moving_focus_recaptures_the_seat_that_lost_focus():
    clock = FakeClock()
    planner = _planner(clock)
    planner.set_focus(1)
    planner.plan(0.0, [_desktop(1), _desktop(2)])  # wall pass: 1@1024, 2@480
    planner.set_focus(2)
    clock.advance(2.0)
    plan = planner.plan(clock(), [_desktop(1), _desktop(2)])
    by_seat = {capture.seat_id: capture.width for capture in plan.captures}
    assert by_seat == {1: 480, 2: 1024}


def test_clearing_focus_returns_everything_to_thumbnail():
    clock = FakeClock()
    planner = _planner(clock)
    planner.set_focus(1)
    planner.plan(0.0, [_desktop(1), _desktop(2)])
    planner.set_focus(None)
    clock.advance(2.0)
    plan = planner.plan(clock(), [_desktop(1), _desktop(2)])
    assert plan.focused is None
    assert {capture.seat_id: capture.width for capture in plan.wall} == {1: 480, 2: 480}


@pytest.mark.parametrize(
    "seat",
    [
        _desktop(1, state="provisioning"),
        _desktop(1, state="off"),
        _desktop(1, vm=False),
        {"id": 1, "seat_type": "desktop", "state": "ready"},  # no vm_name
    ],
)
def test_non_live_focused_seat_is_not_captured_high_res(seat):
    clock = FakeClock()
    planner = _planner(clock)
    planner.set_focus(1)
    plan = planner.plan(0.0, [seat])
    assert plan.focused is None


def test_terminal_seats_are_never_captured():
    clock = FakeClock()
    planner = _planner(clock)
    terminal = {"id": 7, "seat_type": "terminal", "state": "ready", "vm_name": "vm"}
    assert planner.plan(0.0, [terminal]).captures == ()


# --------------------------------------------------------------------------
# worker path with an injected client + clock
# --------------------------------------------------------------------------
def test_pollworker_focused_tick_then_wall_pass():
    clock = FakeClock()
    client = FakeClient([_desktop(1), _desktop(2)])
    worker = _worker(client, _planner(clock))

    worker.poll_once()  # first tick: full wall pass
    assert client.screenshots == [(1, 480), (2, 480)]

    client.screenshots.clear()
    clock.advance(0.5)
    worker.set_focus(1)  # prompt high-res capture for the new focus
    assert client.screenshots == [(1, 1024)]

    client.screenshots.clear()
    clock.advance(1.5)  # wall due (t == 2.0)
    worker.poll_once()
    assert sorted(client.screenshots) == [(1, 1024), (2, 480)]


def test_pollworker_clear_focus_returns_tile_to_thumbnail():
    clock = FakeClock()
    client = FakeClient([_desktop(1), _desktop(2)])
    worker = _worker(client, _planner(clock))
    worker.poll_once()
    clock.advance(0.5)
    worker.set_focus(1)
    worker.set_focus(-1)  # cleared; no immediate capture
    client.screenshots.clear()
    clock.advance(2.0)
    worker.poll_once()
    assert sorted(client.screenshots) == [(1, 480), (2, 480)]


def test_pollworker_emits_only_captured_frames_so_sticky_thumbnails_remain():
    clock = FakeClock()
    client = FakeClient([_desktop(1), _desktop(2)])
    worker = _worker(client, _planner(clock))
    snaps: list = []
    worker.snapshotReady.connect(lambda payload: snaps.append(payload))
    worker.poll_once()
    assert set(snaps[-1]["screenshots"]) == {1, 2}
    clock.advance(0.5)
    worker.set_focus(2)
    # The focused fast tick carries only the focused seat's frame; the viewer
    # keeps the other sticky thumbnail because the wall model carries it over.
    assert set(snaps[-1]["screenshots"]) == {2}


# --------------------------------------------------------------------------
# FIX 1: a capture error must never escape the timer and kill the GUI
# --------------------------------------------------------------------------
class _RaisingScreenshotClient(FakeClient):
    def screenshot(self, seat_id: int, *, max_width: int | None = None):
        raise RuntimeError("capture exploded")


class _BadBase64ScreenshotClient(FakeClient):
    def screenshot(self, seat_id: int, *, max_width: int | None = None):
        from omavroom.client import DaemonClient

        client = DaemonClient("/unused")
        client.call = lambda method, **params: {"png_base64": "!!!not-base64!!!"}
        return client.screenshot(seat_id, max_width=max_width)


@pytest.mark.parametrize("client_cls", [_RaisingScreenshotClient, _BadBase64ScreenshotClient])
def test_capture_errors_never_escape_the_tick(client_cls):
    clock = FakeClock()
    client = client_cls([_desktop(1)])
    worker = _worker(client, _planner(clock))
    snaps: list = []
    worker.snapshotReady.connect(lambda payload: snaps.append(payload))

    worker.poll_once()  # first tick is a wall pass whose capture fails
    assert snaps and snaps[-1]["ok"] is True
    assert snaps[-1]["screenshots"] == {}

    clock.advance(2.0)
    worker.poll_once()  # the worker keeps ticking; no exception escapes
    assert len(snaps) == 2


def test_daemon_client_screenshot_normalizes_bad_replies(monkeypatch):
    from omavroom.client import DaemonClient, DaemonRequestError

    client = DaemonClient("/unused")
    for reply in ({"png_base64": "!!!not-base64!!!"}, {}, {"png_base64": 123}):
        monkeypatch.setattr(client, "call", lambda method, **params: reply)
        with pytest.raises(DaemonRequestError):
            client.screenshot(1)


# --------------------------------------------------------------------------
# FIX 2: the worker shares the capture floor/ceiling and keeps focused >= wall
# --------------------------------------------------------------------------
def test_capture_config_honours_the_width_floor_of_64():
    clock = FakeClock()
    worker = _worker(FakeClient([_desktop(1)]), _planner(clock))
    worker.set_capture_config({"thumbnail_width": 64, "focused_width": 64})
    assert worker._planner.thumbnail_width == 64
    assert worker._planner.focused_width == 64


def test_capture_config_never_inverts_focused_below_thumbnail():
    clock = FakeClock()
    worker = _worker(FakeClient([_desktop(1)]), _planner(clock))
    worker.set_capture_config({"thumbnail_width": 1024, "focused_width": 480})
    assert worker._planner.focused_width >= worker._planner.thumbnail_width
    # Raising only the thumbnail also lifts the focused capture, never inverts.
    worker.set_capture_config({"thumbnail_width": 2048})
    assert worker._planner.focused_width >= worker._planner.thumbnail_width


# --------------------------------------------------------------------------
# FIX 3: status/exec polling runs on the wall cadence, not every fast tick
# --------------------------------------------------------------------------
class CountingClient(FakeClient):
    def __init__(self, seats: list[dict]) -> None:
        super().__init__(seats)
        self.status_calls = 0
        self.exec_calls = 0

    def pool_status(self) -> dict:
        self.status_calls += 1
        return super().pool_status()

    def list_execs(self, seat_id: int) -> list[dict]:
        self.exec_calls += 1
        return super().list_execs(seat_id)


def test_status_is_polled_on_the_wall_cadence_not_every_fast_tick():
    clock = FakeClock()
    client = CountingClient(
        [
            _desktop(1),
            _desktop(2),
            {"id": 9, "seat_type": "terminal", "state": "ready", "vm_name": "t"},
        ]
    )
    worker = PollWorker("unused", planner=_planner(clock), client=client)
    for _ in range(8):
        worker.poll_once()
        clock.advance(0.5)
    # Ticks at t=0.0 and t=2.0 are wall passes; the six in between are fast.
    assert client.status_calls == 2
    assert client.exec_calls == 2

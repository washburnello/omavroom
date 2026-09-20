"""Agent-facing seat helpers: native screenshots, typing, copy and desktop ops.

VM-free: the provisioner is driven through a recorded host runner so the
*exact* argv/command it would run is asserted (no fake-masking), and the
daemon/client/MCP layers are exercised in-process against a FakeProvisioner.
"""

from __future__ import annotations

import base64
import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omavroom.client import DaemonClient
from omavroom.config import Config
from omavroom.daemon import DaemonServer
from omavroom.manager import Manager
from omavroom.manager.libvirt_provisioner import (
    CommandResult,
    LibvirtProvisioner,
)
from omavroom.manager.provisioner import (
    FakeProvisioner,
    InputEvent,
    ProvisionerError,
)
from omavroom.mcp.server import MCP_TOOL_NAMES, OmavroomTools, build_server

PNG_8X8 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAAIGNIUk0AAHomAACAhAAA+gAAAIDo"
    b"AAB1MAAA6mAAADqYAAAXcJy6UTwAAAADUExURQAA/4p40lcAAAAHdElNRQfqCRQCOAMkcgnSAAAA"
    b"JXRFWHRkYXRlOmNyZWF0ZQAyMDI2LTA5LTIwVDAyOjU2OjAzKzAwOjAwWe9CuwAAACV0RVh0ZGF0"
    b"ZTptb2RpZnkAMjAyNi0wOS0yMFQwMjo1NjowMyswMDowMCiy+gcAAAAodEVYdGRhdGU6dGltZXN0"
    b"YW1wADIwMjYtMDktMjBUMDI6NTY6MDMrMDA6MDB/p9vYAAAAC0lEQVQI12NgQAUAABAAAaHFIcEA"
    b"AAAASUVORK5CYII="
)


def _is_magick(binary: str) -> bool:
    return binary in ("magick", "convert") or binary.endswith(("/magick", "/convert"))


def _prov(tmp_path: Path, runner) -> LibvirtProvisioner:
    return LibvirtProvisioner(Config.default(), base_dir=tmp_path, host_runner=runner)


def _meta(prov: LibvirtProvisioner, name: str, *, seat_type: str = "desktop") -> None:
    from omavroom.manager.libvirt_provisioner import _SeatMeta

    overlay = prov.seats_dir / name / "overlay.qcow2"
    meta = _SeatMeta(
        name=name,
        seat_type=seat_type,
        image="golden-desktop",
        golden=str(overlay),
        mac="52:54:00:00:00:01",
        uuid="00000000-0000-0000-0000-000000000000",
        domain=f"omavroom-seat-{name}",
        overlay=str(overlay),
        nvram=str(overlay.parent / "VARS.fd"),
        static_ip="10.0.0.5",
        identity_ready=True,
    )
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"x")
    prov._save_meta(meta)


class _Recorder:
    """Recorded host runner modelling virsh/magick/hyprctl/ssh responses."""

    def __init__(self, *, windows: list[dict] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.windows = windows if windows is not None else [WINDOW]

    def __call__(self, argv, timeout, input_text):  # noqa: ANN001
        self.calls.append(list(argv))
        if argv[0] == "virsh":
            if "screenshot" in argv:
                target = Path(argv[argv.index("--file") + 1])
                target.write_bytes(PNG_8X8)
            return CommandResult(0, "", "")
        if argv[0] in ("magick", "convert") or argv[0].endswith(("/magick", "/convert")):
            shutil.copyfile(argv[1], argv[-1])
            return CommandResult(0, "", "")
        if argv[0] == "scp":
            if "@" not in argv[-1]:
                Path(argv[-1]).write_bytes(b"x")
        return CommandResult(0, "", "")

    def commands(self) -> list[str]:
        """The remote command of every ssh call (last argv element)."""
        return [argv[-1] for argv in self.calls if argv and argv[0] == "ssh"]


WINDOW = {
    "address": "0x1234",
    "class": "foot",
    "title": "foot",
    "initialClass": "foot",
    "initialTitle": "foot",
    "workspace": {"id": 1, "name": "1"},
    "floating": False,
    "mapped": True,
    "pid": 42,
    "size": [800, 600],
    "at": [10, 20],
    "focusHistoryID": 0,
}


class _DesktopRunner(_Recorder):
    """Adds hyprctl JSON / dispatch responses and a run recorder."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.remote: list[str] = []

    def __call__(self, argv, timeout, input_text):  # noqa: ANN001
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            command = argv[-1]
            self.remote.append(command)
            if "hyprctl clients -j" in command:
                return CommandResult(0, json.dumps(self.windows), "")
            if "wl-paste" in command:
                return CommandResult(0, "pasted text", "")
            return CommandResult(0, "", "")
        if argv[0] == "virsh":
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


# --------------------------------------------------------------------------
# native / downscaled / cropped screenshots
# --------------------------------------------------------------------------
def _screenshot_prov(tmp_path: Path, **kwargs) -> tuple[LibvirtProvisioner, _Recorder]:
    recorder = _Recorder()
    prov = _prov(tmp_path, recorder)
    _meta(prov, "desktop-1")
    return prov, recorder


def test_screenshot_native_returns_raw_frame_without_resizing(tmp_path: Path) -> None:
    prov, recorder = _screenshot_prov(tmp_path)
    data = prov.screenshot("omavroom-seat-desktop-1")
    assert data == PNG_8X8
    assert not any(_is_magick(argv[0]) for argv in recorder.calls)


def test_screenshot_downscale_emits_resize_argv(tmp_path: Path) -> None:
    prov, recorder = _screenshot_prov(tmp_path)
    prov.screenshot("omavroom-seat-desktop-1", max_width=4)
    magick = [argv for argv in recorder.calls if _is_magick(argv[0])]
    assert magick, "expected a resize conversion"
    argv = magick[0]
    assert "-resize" in argv and argv[argv.index("-resize") + 1] == "4x"
    assert "-crop" not in argv


def test_screenshot_region_crops_before_resizing(tmp_path: Path) -> None:
    prov, recorder = _screenshot_prov(tmp_path)
    prov.screenshot("omavroom-seat-desktop-1", max_width=4, region=(1, 2, 4, 4))
    magick = [argv for argv in recorder.calls if _is_magick(argv[0])][0]
    crop = magick.index("-crop")
    assert magick[crop + 1] == "4x4+1+2"
    assert magick[crop + 2] == "+repage"
    assert magick.index("-resize") > crop


def test_screenshot_region_alone_forces_crop(tmp_path: Path) -> None:
    prov, recorder = _screenshot_prov(tmp_path)
    prov.screenshot("omavroom-seat-desktop-1", region=(0, 0, 4, 4))
    magick = [argv for argv in recorder.calls if _is_magick(argv[0])]
    assert magick and magick[0][magick[0].index("-crop") + 1] == "4x4+0+0"


@pytest.mark.parametrize("region", [(0, 0, 0, 4), (-1, 0, 4, 4), (0, 0, 4, -1), (0, 0, 4)])
def test_screenshot_rejects_bad_region(tmp_path: Path, region) -> None:
    prov, _ = _screenshot_prov(tmp_path)
    with pytest.raises(ProvisionerError):
        prov.screenshot("omavroom-seat-desktop-1", region=region)


# --------------------------------------------------------------------------
# typing mode
# --------------------------------------------------------------------------
def test_type_event_emits_wtype_delay(tmp_path: Path) -> None:
    recorder = _DesktopRunner()
    prov = _prov(tmp_path, recorder)
    _meta(prov, "desktop-1")
    prov.input(
        "omavroom-seat-desktop-1",
        [InputEvent("type", "hello", delay_ms=40), InputEvent("text", "bulk")],
    )
    command = recorder.remote[-1]
    assert "wtype -d 40 -- hello" in command
    assert "wtype -- bulk" in command


def test_input_event_rejects_negative_delay() -> None:
    with pytest.raises(ValueError):
        InputEvent("type", "x", delay_ms=-1)


# --------------------------------------------------------------------------
# copy_in / copy_out argv + host-path constraint
# --------------------------------------------------------------------------
def test_copy_in_uses_pinned_scp_argv(tmp_path: Path) -> None:
    recorder = _Recorder()
    prov = _prov(tmp_path, recorder)
    _meta(prov, "desktop-1", seat_type="terminal")
    source = prov.host_transfer_root / "input.txt"
    source.write_text("data", encoding="utf-8")
    size = prov.copy_in("omavroom-seat-desktop-1", str(source), "/home/agent/input.txt")
    assert size == 4
    scp = [argv for argv in recorder.calls if argv[0] == "scp"][0]
    assert scp[1:3] == ["-o", "BatchMode=yes"]
    assert "StrictHostKeyChecking=yes" in scp
    assert scp[-2] == str(source)
    assert scp[-1] == "agent@10.0.0.5:/home/agent/input.txt"


def test_copy_out_uses_pinned_scp_argv(tmp_path: Path) -> None:
    recorder = _Recorder()
    prov = _prov(tmp_path, recorder)
    _meta(prov, "desktop-1", seat_type="terminal")
    dest = prov.host_transfer_root / "nested" / "out.txt"
    prov.copy_out("omavroom-seat-desktop-1", "/home/agent/out.txt", str(dest))
    scp = [argv for argv in recorder.calls if argv[0] == "scp"][0]
    assert scp[-2] == "agent@10.0.0.5:/home/agent/out.txt"
    assert scp[-1] == str(dest)


def test_copy_in_rejects_host_path_outside_root(tmp_path: Path) -> None:
    prov = _prov(tmp_path, _Recorder())
    _meta(prov, "desktop-1", seat_type="terminal")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ProvisionerError, match="outside the allowed transfer root"):
        prov.copy_in("omavroom-seat-desktop-1", str(outside), "/home/agent/x")


def test_copy_out_rejects_host_path_outside_root(tmp_path: Path) -> None:
    prov = _prov(tmp_path, _Recorder())
    _meta(prov, "desktop-1", seat_type="terminal")
    with pytest.raises(ProvisionerError, match="outside the allowed transfer root"):
        prov.copy_out("omavroom-seat-desktop-1", "/home/agent/x", str(tmp_path / "escape.txt"))


def test_copy_in_rejects_relative_guest_path(tmp_path: Path) -> None:
    prov = _prov(tmp_path, _Recorder())
    _meta(prov, "desktop-1", seat_type="terminal")
    source = prov.host_transfer_root / "input.txt"
    source.write_text("data", encoding="utf-8")
    with pytest.raises(ProvisionerError, match="absolute guest path"):
        prov.copy_in("omavroom-seat-desktop-1", str(source), "relative/path")


# --------------------------------------------------------------------------
# desktop helper command construction (Lua dispatch syntax)
# --------------------------------------------------------------------------
def _desktop_prov(tmp_path: Path) -> tuple[LibvirtProvisioner, _DesktopRunner]:
    recorder = _DesktopRunner()
    prov = _prov(tmp_path, recorder)
    _meta(prov, "desktop-1")
    return prov, recorder


def test_list_windows_parses_hyprctl_json(tmp_path: Path) -> None:
    prov, _ = _desktop_prov(tmp_path)
    windows = prov.list_windows("omavroom-seat-desktop-1")
    assert windows[0]["address"] == "0x1234"
    assert windows[0]["class"] == "foot"
    assert windows[0]["workspace"] == "1"
    assert windows[0]["size"] == [800, 600]


def test_focus_window_uses_lua_dispatch(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    window = prov.focus_window("omavroom-seat-desktop-1", "foot")
    assert window["address"] == "0x1234"
    command = recorder.remote[-1]
    assert command.startswith("export XDG_RUNTIME_DIR=")
    assert "hyprctl dispatch 'hl.dsp.focus({ window = \"address:0x1234\" })'" in command


def test_resize_move_float_use_lua_window_dispatchers(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    vm = "omavroom-seat-desktop-1"
    prov.resize_window(vm, "foot", 1024, 768)
    assert (
        'hl.dsp.window.resize({ window = "address:0x1234", x = 1024, y = 768 })'
        in (recorder.remote[-1])
    )
    prov.move_window(vm, "foot", 12, 34)
    assert (
        'hl.dsp.window.move({ window = "address:0x1234", x = 12, y = 34 })' in (recorder.remote[-1])
    )
    prov.float_window(vm, "foot", True)
    assert (
        'hl.dsp.window.float({ window = "address:0x1234", action = "on" })' in (recorder.remote[-1])
    )
    prov.float_window(vm, "foot", False)
    assert (
        'hl.dsp.window.float({ window = "address:0x1234", action = "off" })'
        in (recorder.remote[-1])
    )


def test_launch_app_direct_and_tui(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    vm = "omavroom-seat-desktop-1"
    prov.launch_app(vm, "foot -e btop")
    assert 'hl.dsp.exec_cmd("foot -e btop")' in recorder.remote[-1]
    prov.launch_app(vm, "btop", tui=True)
    assert "omarchy-launch-tui bash -lc btop" in recorder.remote[-1]


def test_launch_app_escapes_lua_string(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    prov.launch_app("omavroom-seat-desktop-1", 'app "arg"')
    assert 'hl.dsp.exec_cmd("app \\"arg\\"")' in recorder.remote[-1]


def test_set_theme_and_rejects_unsafe_name(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    vm = "omavroom-seat-desktop-1"
    prov.set_theme(vm, "Tokyo Night")
    assert "omarchy theme set" in recorder.remote[-1]
    assert "Tokyo Night" in recorder.remote[-1]
    with pytest.raises(ProvisionerError, match="unsafe theme name"):
        prov.set_theme(vm, "evil; rm -rf /")


def test_clipboard_get_and_set(tmp_path: Path) -> None:
    prov, recorder = _desktop_prov(tmp_path)
    vm = "omavroom-seat-desktop-1"
    assert prov.clipboard_get(vm) == "pasted text"
    prov.clipboard_set(vm, "hello world")
    assert "printf '%s'" in recorder.remote[-1]
    assert "hello world" in recorder.remote[-1]
    assert "wl-copy" in recorder.remote[-1]


def test_desktop_only_helpers_reject_terminal_seat(tmp_path: Path) -> None:
    prov, _ = _desktop_prov(tmp_path)
    _meta(prov, "terminal-9", seat_type="terminal")
    with pytest.raises(ProvisionerError, match="only supported on desktop seats"):
        prov.list_windows("omavroom-seat-terminal-9")


# --------------------------------------------------------------------------
# daemon / client / MCP wiring (FakeProvisioner)
# --------------------------------------------------------------------------
def _wait_for_socket(path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"daemon socket never appeared: {path}")
        time.sleep(0.01)


@pytest.fixture
def env(tmp_path):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    fake = FakeProvisioner()
    db = tmp_path / "state.db"
    manager = Manager(cfg, db_path=db, provisioner=fake, free_ram_mb=lambda: 10**9)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(socket_path)
    client = DaemonClient(socket_path=socket_path)
    tools = OmavroomTools(client)
    namespace = SimpleNamespace(
        manager=manager, fake=fake, server=server, client=client, tools=tools, config=cfg
    )
    try:
        yield namespace
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _ready_desktop(env) -> int:
    request = env.tools.request_seat("agent-1", "desktop")
    return env.tools.wait_for_seat(request["request_id"], timeout_s=5)["seat"]["id"]


def test_mcp_tool_names_include_agent_helpers() -> None:
    assert {
        "copy_in",
        "copy_out",
        "launch_app",
        "list_windows",
        "focus_window",
        "resize_window",
        "move_window",
        "float_window",
        "set_theme",
        "clipboard_get",
        "clipboard_set",
    } <= set(MCP_TOOL_NAMES)


def test_screenshot_native_and_region_over_wire(env) -> None:
    seat_id = _ready_desktop(env)
    native = env.client.call("screenshot", seat_id=seat_id, max_width=0)
    assert native["native"] is True
    assert native["max_width"] is None
    cropped = env.client.call("screenshot", seat_id=seat_id, region=[1, 2, 4, 4])
    assert cropped["region"] == [1, 2, 4, 4]
    # Omitting the key keeps the legacy default (v1 back-compat).
    default = env.client.call("screenshot", seat_id=seat_id)
    assert default["native"] is False
    assert default["max_width"] == 1024


def test_mcp_screenshot_defaults_to_native(env) -> None:
    seat_id = _ready_desktop(env)
    image, encoded = env.tools.screenshot(seat_id)
    assert image.data.startswith(b"PNG:")
    assert base64.b64decode(encoded) == image.data


def test_client_desktop_helpers_round_trip(env) -> None:
    seat_id = _ready_desktop(env)
    assert env.client.copy_in(seat_id, "in.txt", "/home/agent/in.txt")["bytes"] == 0
    assert env.client.copy_out(seat_id, "/home/agent/out.txt", "out.txt")["bytes"] == 0
    assert env.client.launch_app(seat_id, "btop", tui=True)["tui"] is True
    assert env.client.list_windows(seat_id)[0]["class"] == "foot"
    assert env.client.focus_window(seat_id, "foot")["address"] == "0x1"
    assert env.client.resize_window(seat_id, "foot", 100, 50)["width"] == 100
    assert env.client.move_window(seat_id, "foot", 1, 2)["x"] == 1
    assert env.client.float_window(seat_id, "foot", False)["on"] is False
    assert env.client.set_theme(seat_id, "Tokyo Night")["theme"] == "Tokyo Night"
    env.client.clipboard_set(seat_id, "hi")
    assert env.client.clipboard_get(seat_id) == "hi"


def test_type_and_delay_over_wire(env) -> None:
    seat_id = _ready_desktop(env)
    applied = env.tools.input(seat_id, [{"kind": "type", "value": "abc", "delay_ms": 25}])
    assert applied["applied"] == 1
    recorded = env.fake.inputs["fake://desktop-1"]
    assert recorded[0].delay_ms == 25
    assert recorded[0].kind == "type"


def test_mcp_agent_helpers_through_in_memory_client(env) -> None:
    import asyncio

    from fastmcp import Client

    seat_id = _ready_desktop(env)
    server = build_server(env.tools)

    async def main():
        async with Client(server) as client:
            windows = await client.call_tool("list_windows", {"seat_id": seat_id})
            theme = await client.call_tool("set_theme", {"seat_id": seat_id, "name": "Tokyo Night"})
            return windows, theme

    windows, theme = asyncio.run(main())
    assert windows.data["windows"][0]["class"] == "foot"
    assert theme.data["theme"] == "Tokyo Night"

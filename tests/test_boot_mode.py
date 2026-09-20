"""One image, two boot modes: in-guest transform construction and idempotency.

VM-free: the provisioner is driven through scripted/recorded runners so the
*exact* guest script and the host lifecycle calls it makes are asserted. The
transform is applied at provision time through the qemu-guest-agent channel
(before SSH), so the boot mode, not the image, decides the seat's session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omavroom.config import Config
from omavroom.manager.libvirt_provisioner import (
    AUTOLOGIN_DROPIN,
    BOOT_MODE_CHANGED,
    BOOT_MODE_OK,
    DESKTOP_SESSION_OK,
    HYPRLAND_LAUNCH,
    HYPRLAND_PROFILE_MARKER,
    CommandResult,
    LibvirtProvisioner,
    _SeatMeta,
    build_autologin_conf,
    build_boot_mode_script,
)
from omavroom.manager.provisioner import FakeProvisioner


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point the per-user config path at a fresh tmp XDG dir."""
    from omavroom.config import default_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    return default_config_path()


# --------------------------------------------------------------------------
# pure transform construction (exact scripts, no VM)
# --------------------------------------------------------------------------
def test_desktop_boot_mode_script_enables_graphical_session() -> None:
    script = build_boot_mode_script("desktop")
    assert 'systemctl set-default "$TARGET"' in script
    assert "graphical.target" in script
    assert "multi-user.target" not in script
    # autologin drop-in is written only when absent
    assert f"DROPIN={AUTOLOGIN_DROPIN}" in script
    assert "grep -q -- '--autologin agent'" in script
    assert build_autologin_conf("agent") in script
    # profile execs Hyprland only on tty1
    assert HYPRLAND_PROFILE_MARKER in script
    assert f"exec {HYPRLAND_LAUNCH}" in script
    assert 'if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then' in script
    assert BOOT_MODE_CHANGED in script and BOOT_MODE_OK in script


def test_terminal_boot_mode_script_removes_compositor() -> None:
    script = build_boot_mode_script("terminal")
    assert "multi-user.target" in script
    assert "graphical.target" not in script
    # no autologin drop-in remains
    assert 'rm -f "$DROPIN"' in script
    assert "rmdir" in script
    # any profile line mentioning Hyprland/uwsm is stripped
    assert "grep -Ev" in script and "$PROFILE.omavroom.tmp" in script
    assert "exec " + HYPRLAND_LAUNCH not in script
    assert BOOT_MODE_CHANGED in script and BOOT_MODE_OK in script


def test_build_boot_mode_script_rejects_unknown_seat_type() -> None:
    with pytest.raises(ValueError, match="unknown seat type"):
        build_boot_mode_script("toaster")


# --------------------------------------------------------------------------
# provision-time application (agent + host lifecycle)
# --------------------------------------------------------------------------
def _seat(prov: LibvirtProvisioner, name: str, seat_type: str) -> tuple[str, _SeatMeta]:
    overlay = prov.seats_dir / name / "overlay.qcow2"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"x")
    meta = _SeatMeta(
        name=name,
        seat_type=seat_type,
        image="golden-omarchy",
        golden=str(overlay),
        mac="52:54:00:00:00:01",
        uuid="00000000-0000-0000-0000-000000000000",
        domain=f"omavroom-seat-{name}",
        overlay=str(overlay),
        nvram=str(overlay.parent / "VARS.fd"),
        static_ip="10.0.0.5",
    )
    prov._save_meta(meta)
    return f"omavroom-seat-{name}", meta


class _BootAgent(LibvirtProvisioner):
    """Scripts the guest agent and records every boot-mode script it runs."""

    def __init__(self, *args, changed: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.changed = changed
        self.agent_commands: list[str] = []

    def agent_ping(self, vm_ref: str) -> bool:
        return True

    def agent_exec(self, vm_ref: str, command: str, *, timeout_s: int = 60) -> CommandResult:
        self.agent_commands.append(command)
        verdict = BOOT_MODE_CHANGED if self.changed else BOOT_MODE_OK
        return CommandResult(0, f"{verdict}\n", "")


class _RebootRunner:
    """Stateful virsh runner: ``shutdown`` powers off, ``start`` powers on."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.powered = True

    def __call__(self, argv, timeout, input_text):  # noqa: ANN001
        self.calls.append(list(argv))
        if argv[0] != "virsh":
            return CommandResult(0, "", "")
        sub = argv[3] if len(argv) > 3 else ""
        if sub == "dominfo":
            return CommandResult(0, "Autostart: disable\n", "")
        if sub == "domstate":
            return CommandResult(0, "running\n" if self.powered else "shut off\n", "")
        if sub == "shutdown":
            self.powered = False
            return CommandResult(0, "", "")
        if sub == "start":
            self.powered = True
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    def subcommands(self) -> list[str]:
        return [argv[3] for argv in self.calls if argv and argv[0] == "virsh" and len(argv) > 3]


def test_apply_boot_mode_runs_exact_terminal_script_idempotently(tmp_path: Path) -> None:
    host_calls: list[list[str]] = []

    def runner(argv, timeout, input_text):  # noqa: ANN001
        host_calls.append(list(argv))
        return CommandResult(0, "", "")

    prov = _BootAgent(Config.default(), base_dir=tmp_path, host_runner=runner, sleep=lambda _: None)
    ref, meta = _seat(prov, "terminal-1", "terminal")
    prov._apply_boot_mode(ref, meta, timeout_s=30)
    assert prov.agent_commands == [build_boot_mode_script("terminal")]
    # Idempotent: already in multi-user, so no host lifecycle call at all.
    assert host_calls == []
    assert prov._load_meta(ref).boot_mode == "terminal"


def test_apply_boot_mode_runs_exact_desktop_script(tmp_path: Path) -> None:
    prov = _BootAgent(Config.default(), base_dir=tmp_path, sleep=lambda _: None)
    ref, meta = _seat(prov, "desktop-1", "desktop")
    prov._apply_boot_mode(ref, meta, timeout_s=30)
    assert prov.agent_commands == [build_boot_mode_script("desktop")]
    assert prov._load_meta(ref).boot_mode == "desktop"


def test_apply_boot_mode_reboots_when_the_mode_changed(tmp_path: Path) -> None:
    recorder = _RebootRunner()
    prov = _BootAgent(
        Config.default(),
        base_dir=tmp_path,
        host_runner=recorder,
        sleep=lambda _: None,
        changed=True,
    )
    ref, meta = _seat(prov, "desktop-1", "desktop")
    prov._apply_boot_mode(ref, meta, timeout_s=30)
    subcommands = recorder.subcommands()
    assert "shutdown" in subcommands and "start" in subcommands
    assert subcommands.index("shutdown") < subcommands.index("start")
    assert prov._load_meta(ref).boot_mode == "desktop"


def test_apply_boot_mode_raises_without_a_verdict(tmp_path: Path) -> None:
    class _Silent(_BootAgent):
        def agent_exec(self, vm_ref, command, *, timeout_s=60):  # noqa: ANN001
            return CommandResult(0, "nothing happened\n", "")

    prov = _Silent(Config.default(), base_dir=tmp_path, sleep=lambda _: None)
    ref, meta = _seat(prov, "desktop-1", "desktop")
    with pytest.raises(Exception, match="gave no verdict"):
        prov._apply_boot_mode(ref, meta, timeout_s=30)


def test_wait_ready_applies_boot_mode_before_ssh_and_verifies_desktop(tmp_path: Path) -> None:
    order: list[str] = []

    class _Wired(LibvirtProvisioner):
        def _ensure_identity(self, vm_ref, *, rotate, timeout_s):  # noqa: ANN001
            order.append("identity")

        def _apply_boot_mode(self, vm_ref, meta, *, timeout_s):  # noqa: ANN001
            order.append(f"boot:{meta.seat_type}")

        def _guest_alive(self, vm_ref):  # noqa: ANN001
            order.append("ssh")
            return True

        def check_overlay_quota(self, vm_ref, max_gb):  # noqa: ANN001
            return 0.0

        def _verify_desktop_session(self, vm_ref, deadline):  # noqa: ANN001
            order.append("verify-desktop")

    prov = _Wired(Config.default(), base_dir=tmp_path, sleep=lambda _: None)
    ref, _ = _seat(prov, "desktop-1", "desktop")
    meta = prov._load_meta(ref)
    meta.identity_ready = True
    prov._save_meta(meta)
    prov.wait_ready(ref, 30)
    assert order == ["identity", "boot:desktop", "ssh", "verify-desktop"]


def test_verify_desktop_session_waits_for_hyprland_and_quickshell(tmp_path: Path) -> None:
    class _Session(LibvirtProvisioner):
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN003
            super().__init__(*args, **kwargs)
            self.commands: list[str] = []
            self.answers = [False, True]

        def run(self, vm_ref, command, **kwargs):  # noqa: ANN001
            self.commands.append(command)
            ok = self.answers.pop(0) if self.answers else False
            return CommandResult(0 if ok else 1, DESKTOP_SESSION_OK + "\n" if ok else "", "")

    prov = _Session(Config.default(), base_dir=tmp_path, sleep=lambda _: None)
    prov._verify_desktop_session("omavroom-seat-desktop-1", prov._monotonic() + 5)
    assert len(prov.commands) == 2
    assert "pgrep -x Hyprland" in prov.commands[0]
    assert "pgrep -x quickshell" in prov.commands[0]


# --------------------------------------------------------------------------
# a built project image is shared for both seat types
# --------------------------------------------------------------------------
def test_project_image_is_shared_for_both_seat_types(
    make_manager, user_config, tmp_path: Path
) -> None:
    mgr = make_manager(Config.default(), provisioner=FakeProvisioner())
    result = mgr.build_image("omatype", base="golden-omarchy", packages=["jq"], approved=True)
    # No per-type split: registered with no seat_type and usable by both.
    assert result["seat_type"] is None
    entry = mgr.config.images["omatype"]
    assert entry.seat_type is None
    expected = Path(entry.golden)
    assert mgr.config.golden_for("desktop", "omatype") == expected
    assert mgr.config.golden_for("terminal", "omatype") == expected

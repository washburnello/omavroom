"""Phase 4B real-VM integration proof (guarded; opt in explicitly).

These are the *real* Manager + LibvirtProvisioner tests: they boot actual
seat VMs one at a time, exercise identity rotation, SSH, repo injection,
host-side fetch/gate/push, reset, and teardown, and assert that no seat VM
is left behind and the goldens stay read-only.

They never run in a default suite: they require ``OMAVROOM_INTEGRATION=1``
*and* all prerequisites (libvirt, goldens, seat SSH key, template domains).
They are marked ``integration`` so they can be selected/deselected
independently::

    OMAVROOM_INTEGRATION=1 uv run pytest -m integration -s

The desktop smoke test additionally requires >= 5 GiB free RAM; it skips
(and says so) otherwise, to be exercised in Phase 5/8.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
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
    SEAT_DOMAIN_PREFIX,
    LibvirtProvisioner,
)
from omavroom.manager.provisioner import ExportSpec, RepoSpec, ResourceCaps
from omavroom.manager.scheduler import read_free_ram_mb
from omavroom.mcp.server import OmavroomTools

pytestmark = pytest.mark.integration

HOME = Path.home()
BASE = HOME / ".local/share/omavroom"
IMAGES = BASE / "images"
SSH_KEY = HOME / ".ssh/omavroom_ed25519"
URI = "qemu:///system"
GOLDENS = ("golden-desktop.qcow2", "golden-term.qcow2")


def _virsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["virsh", "--connect", URI, *args], capture_output=True, text=True, timeout=60
    )


def _prereq_skip_reason() -> str | None:
    if os.environ.get("OMAVROOM_INTEGRATION") != "1":
        return "set OMAVROOM_INTEGRATION=1 to run real-VM integration tests"
    if shutil.which("virsh") is None:
        return "virsh is not installed"
    if not SSH_KEY.exists():
        return f"seat SSH key missing: {SSH_KEY}"
    for name in GOLDENS:
        path = IMAGES / name
        if not path.exists():
            return f"golden image missing: {path}"
        if path.stat().st_mode & 0o222:
            return f"golden image is writable (must be 444): {path}"
    listing = _virsh("list", "--all", "--name")
    if listing.returncode != 0:
        return "libvirt system connection unavailable"
    names = set(listing.stdout.split())
    for template in ("omavroom-base", "omavroom-term"):
        if template not in names:
            return f"template domain missing: {template}"
    return None


def _host_bridge_ip() -> str:
    xml = _virsh("net-dumpxml", "default").stdout
    match = re.search(r"<ip address='([0-9.]+)'", xml)
    return match.group(1) if match else "192.168.122.1"


def _make_traversable(path: Path) -> None:
    """Grant o+x on ``path`` and its ancestors up to /tmp.

    ``pytest``'s tmp dirs are ``0700``; the seat overlay under them is opened
    by the ``libvirt-qemu`` user, whose dynamic-DAC relabel cannot even stat
    the overlay without directory execute permission. Test-only and confined
    to ``/tmp``.
    """
    current = path
    while current != current.parent and current != Path("/tmp"):
        try:
            os.chmod(current, current.stat().st_mode | 0o011)
        except OSError:
            pass
        current = current.parent


def _assert_clean(prov: LibvirtProvisioner | None = None) -> None:
    """Post-run invariant: no seat domains, templates off, goldens 444."""
    listing = _virsh("list", "--all", "--name")
    seats = [n for n in listing.stdout.split() if n.startswith(SEAT_DOMAIN_PREFIX)]
    assert seats == [], f"seat domains left behind: {seats}"
    if prov is not None:
        assert prov.list_vms() == [], "provisioner still reports seat VMs"
    for template in ("omavroom-base", "omavroom-term"):
        assert _virsh("domstate", template).stdout.strip() == "shut off", template
    for name in GOLDENS:
        path = IMAGES / name
        assert (path.stat().st_mode & 0o777) == 0o444, f"{name} is not read-only"


@pytest.fixture
def env(tmp_path: Path):
    reason = _prereq_skip_reason()
    if reason:
        pytest.skip(reason)
    # Isolation: never touch the production base_dir and never destroy seat
    # domains that existed before this run. Seat state (overlays, NVRAM,
    # staging) lives under a per-run base_dir; goldens still resolve through
    # the shared read-only production images directory. If any seat domain
    # already exists we refuse rather than reap someone else's VM.
    preexisting = [
        name
        for name in _virsh("list", "--all", "--name").stdout.split()
        if name.startswith(SEAT_DOMAIN_PREFIX)
    ]
    if preexisting:
        pytest.skip(f"pre-existing seat domains present; refusing to disturb them: {preexisting}")
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    cfg.leases.lease_timeout_s = 180
    cfg.leases.heartbeat_timeout_s = 60
    cfg.leases.heartbeat_interval_s = 20
    cfg.seats["desktop"].max_seats = 1
    cfg.seats["terminal"].max_seats = 1
    cfg.seats["desktop"].image = "golden-desktop"
    cfg.seats["terminal"].image = "golden-term"
    run_base = tmp_path / "omavroom-run"
    prov = LibvirtProvisioner(cfg, base_dir=run_base)
    _make_traversable(run_base)
    try:
        yield SimpleNamespace(prov=prov, cfg=cfg, base=run_base)
    finally:
        for vm in prov.list_vms():
            try:
                prov.destroy(vm.ref)
            except Exception:  # noqa: BLE001 - teardown must be best-effort
                pass
        _assert_clean(prov)


def _manager(env, tmp_path: Path) -> Manager:
    return Manager(
        env.cfg,
        db_path=tmp_path / "state.db",
        provisioner=env.prov,
        free_ram_mb=read_free_ram_mb,
    )


def _run(argv: list[str], cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def _make_scratch_repo(tmp_path: Path) -> Path:
    serve = tmp_path / "serve"
    bare = serve / "scratch.git"
    bare.mkdir(parents=True)
    _run(["git", "init", "--bare", "-b", "main", str(bare)])
    work = tmp_path / "scratch-work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], cwd=work)
    (work / "README.md").write_text("scratch repo\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=work)
    _run(
        [
            "git",
            "-c",
            "user.name=host",
            "-c",
            "user.email=host@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=work,
    )
    _run(["git", "remote", "add", "origin", str(bare)], cwd=work)
    _run(["git", "push", "origin", "main"], cwd=work)
    return serve


def _start_git_daemon(serve: Path, host_ip: str) -> tuple[subprocess.Popen, str, int]:
    port = _free_port()
    proc = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            "--export-all",
            f"--base-path={serve}",
            f"--listen={host_ip}",
            f"--port={port}",
            str(serve),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        pytest.skip(f"git daemon failed to start on {host_ip}:{port}")
    return proc, f"git://{host_ip}:{port}/scratch.git", port


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _golden_fingerprint() -> dict[str, tuple[int, int]]:
    return {
        name: ((IMAGES / name).stat().st_mtime_ns, (IMAGES / name).stat().st_size)
        for name in GOLDENS
    }


# --------------------------------------------------------------------------
# terminal: full create -> repo -> commit -> fetch -> gate -> push -> destroy
# --------------------------------------------------------------------------
def test_terminal_seat_full_cycle(env, tmp_path: Path) -> None:
    host_ip = _host_bridge_ip()
    serve = _make_scratch_repo(tmp_path)
    daemon, clone_url, port = _start_git_daemon(serve, host_ip)
    mgr = _manager(env, tmp_path)
    goldens_before = _golden_fingerprint()
    bare = serve / "scratch.git"
    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", "-b", "main", str(remote)])
    try:
        handle = mgr.request_seat("integration-terminal", "terminal", project="phase4b")
        view = handle.wait_ready(timeout=300)
        assert view.seat is not None and view.seat.state == "ready", view
        seat_id = view.seat.id
        vm_ref = view.seat.vm_name
        assert vm_ref is not None
        print(f"\n[terminal] ready seat={view.seat.name} vm={vm_ref}")
        # evidence: autostart is disabled while the seat runs
        info = _virsh("dominfo", vm_ref).stdout
        assert "Autostart:" in info and "disable" in info.split("Autostart:")[1].splitlines()[0]

        # guest can reach the host-served repo
        probe = env.prov.run(
            vm_ref,
            f"timeout 5 bash -c 'cat < /dev/null > /dev/tcp/{host_ip}/{port}' "
            "&& echo CONNECT_OK || echo CONNECT_FAIL",
            timeout_s=30,
        )
        print(f"[terminal] connectivity: {probe.stdout.strip()}")
        assert "CONNECT_OK" in probe.stdout, "guest cannot reach git daemon on the host bridge"

        prepare = mgr.prepare_repo(seat_id, RepoSpec(url=clone_url, branch="main"))
        prepare.result(timeout=180)

        commit = env.prov.run(
            vm_ref,
            "cd /home/agent/workspace/scratch && git checkout -b task && "
            "echo integration > it.txt && git add -A && "
            "git -c user.name=it-agent -c user.email=it@example.com "
            "commit -m 'integration commit' && git rev-parse task",
            timeout_s=120,
            check=True,
        )
        guest_sha = commit.stdout.strip().splitlines()[-1]
        assert len(guest_sha) == 40, commit.stdout
        print(f"[terminal] guest task SHA: {guest_sha}")

        fetched = env.prov.fetch_bundle(vm_ref, ExportSpec(repo=str(bare), branch="task"))
        assert fetched.ok, fetched.message
        assert fetched.sha == guest_sha
        assert fetched.files_changed == 1
        assert fetched.changed_paths == ("it.txt",)
        assert fetched.stash_count == 0
        assert fetched.bundle_path and Path(fetched.bundle_path).exists()
        print(
            f"[terminal] fetch: sha={fetched.sha} files={fetched.files_changed} "
            f"+{fetched.insertions}/-{fetched.deletions} paths={fetched.changed_paths}"
        )

        decision = mgr.scheduler.gate.evaluate(fetched)
        assert decision.allowed, decision.reason
        print(f"[terminal] gate: {decision.allowed} ({decision.reason})")

        pushed = env.prov.push(
            ExportSpec(repo=str(bare), branch="task", ref=f"{remote}:refs/heads/task"), fetched
        )
        assert pushed.ok, pushed.message
        assert pushed.sha == fetched.sha
        ls_remote = subprocess.run(
            ["git", "ls-remote", str(remote), "refs/heads/task"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        assert ls_remote == guest_sha
        print(f"[terminal] push verified: remote={ls_remote}")

        overlay = env.base / "seats" / view.seat.name / "overlay.qcow2"
        assert overlay.exists()
        outcome = mgr.release_seat(seat_id, export=False).result(timeout=180)
        assert outcome.destroyed is True
        assert not overlay.exists()
        assert not (env.base / "seats" / view.seat.name).exists()
        print("[terminal] destroyed; overlay removed")
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
        _assert_clean(env.prov)
        assert _golden_fingerprint() == goldens_before, "golden images were modified"


# --------------------------------------------------------------------------
# reset: mid-life rewind to golden, identity regenerated
# --------------------------------------------------------------------------
def test_reset_rewinds_overlay_and_rotates_identity(env, tmp_path: Path) -> None:
    mgr = _manager(env, tmp_path)
    goldens_before = _golden_fingerprint()
    handle = mgr.request_seat("integration-reset", "terminal")
    view = handle.wait_ready(timeout=300)
    assert view.seat is not None and view.seat.state == "ready"
    seat_id = view.seat.id
    vm_ref = view.seat.vm_name
    machine_id_before = env.prov.run(vm_ref, "cat /etc/machine-id", check=True).stdout.strip()
    env.prov.run(vm_ref, "echo sentinel > /home/agent/sentinel", check=True)
    print(f"\n[reset] before: machine-id={machine_id_before}")

    seat = mgr.reset_seat(seat_id).result(timeout=500)
    assert seat.state == "ready", seat
    sentinel = env.prov.run(
        vm_ref, "test -e /home/agent/sentinel && echo PRESENT || echo GONE", check=True
    ).stdout.strip()
    assert "GONE" in sentinel
    machine_id_after = env.prov.run(vm_ref, "cat /etc/machine-id", check=True).stdout.strip()
    assert machine_id_after != machine_id_before
    print(f"[reset] after: machine-id={machine_id_after} sentinel={sentinel}")
    mgr.release_seat(seat_id, export=False).result(timeout=180)
    _assert_clean(env.prov)
    assert _golden_fingerprint() == goldens_before, "golden images were modified"


# --------------------------------------------------------------------------
# desktop smoke: hyprctl + real screenshot PNG (only with >= 5 GiB free RAM)
# --------------------------------------------------------------------------
def test_desktop_smoke_if_ram(env, tmp_path: Path) -> None:
    free_mb = read_free_ram_mb()
    if free_mb < 5120:
        pytest.skip(f"desktop smoke skipped: only {free_mb} MiB free (< 5120)")
    mgr = _manager(env, tmp_path)
    handle = mgr.request_seat("integration-desktop", "desktop")
    view = handle.wait_ready(timeout=420)
    assert view.seat is not None and view.seat.state == "ready"
    seat_id = view.seat.id
    vm_ref = view.seat.vm_name
    monitors = env.prov.run(
        vm_ref,
        "export XDG_RUNTIME_DIR=/run/user/1000; "
        "export HYPRLAND_INSTANCE_SIGNATURE=$(ls /run/user/1000/hypr | head -1); "
        "hyprctl monitors",
        timeout_s=60,
        check=True,
    ).stdout
    assert "Monitor" in monitors or "monitor" in monitors
    png = env.prov.screenshot(vm_ref, max_width=800)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    from omavroom.manager.libvirt_provisioner import png_dimensions

    width, height = png_dimensions(png)
    assert width <= 800
    print(f"\n[desktop] monitors ok; screenshot {width}x{height}")
    mgr.release_seat(seat_id, export=False).result(timeout=180)
    _assert_clean(env.prov)


# --------------------------------------------------------------------------
# package C: live VNC frames from a real desktop seat
# --------------------------------------------------------------------------
def test_desktop_vnc_live_frames(env, tmp_path: Path) -> None:
    """Stream a real desktop seat's framebuffer over the pure-Python RFB client.

    One desktop seat is admitted with the manual override (the live-RAM gate
    would otherwise refuse on a ~5 GiB host), the client resolves its VNC
    endpoint, and ``VncFrameSource`` must deliver at least a few non-empty
    1280x800 frames with an advancing revision. The seat is released, the
    override restored, and no seat domain may survive.
    """
    from omavroom.gui.frames import VncFrameSource

    free_mb = read_free_ram_mb()
    if free_mb < 4096:
        pytest.skip(f"desktop VNC integration skipped: only {free_mb} MiB free (< 4096)")
    mgr = _manager(env, tmp_path)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(mgr, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not socket_path.exists():
        if time.monotonic() >= deadline:  # pragma: no cover - startup guard
            raise RuntimeError("integration daemon never started")
        time.sleep(0.05)
    client = DaemonClient(socket_path=socket_path)
    source = VncFrameSource(connect_timeout=5.0, read_timeout=0.5)
    seat_id: int | None = None
    try:
        client.set_admission_override("allow")
        handle = client.request_seat("integration-vnc", "desktop")
        view = handle.wait_ready(timeout=420)
        seat = view.get("seat")
        assert seat is not None and seat["state"] == "ready", view
        seat_id = int(seat["id"])
        print(f"\n[vnc] ready seat={seat['name']} vm={seat['vm_name']}")

        endpoint = client.peek_endpoint(seat_id)
        assert endpoint.startswith("vnc://"), endpoint
        assert not endpoint.endswith(":0"), endpoint
        print(f"[vnc] endpoint {endpoint}")

        source.start(seat_id, 1024, endpoint)
        frames = 0
        last_revision = -1
        dimensions: tuple[int, int] | None = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and frames < 5:
            revision = source.revision(seat_id)
            image = source.frame(seat_id)
            if image is not None and not image.isNull() and revision > last_revision:
                dimensions = (image.width(), image.height())
                last_revision = revision
                frames += 1
            time.sleep(0.2)
        error = source.error(seat_id)
        assert error is None, f"VNC stream error: {error}"
        assert frames >= 5, f"only received {frames} VNC frames"
        assert source.revision(seat_id) >= 5
        assert dimensions == (1280, 800), dimensions
        print(f"[vnc] frames={frames} dims={dimensions} revision={source.revision(seat_id)}")
    finally:
        if seat_id is not None:
            source.stop(seat_id)
        try:
            client.set_admission_override("auto")
        except Exception:  # noqa: BLE001 - teardown must be best-effort
            pass
        if seat_id is not None:
            try:
                client.force_discard(seat_id, reason="integration_cleanup").result(timeout=180)
            except Exception:  # noqa: BLE001 - teardown must be best-effort
                pass
        client.close()
        server.shutdown()
        thread.join(timeout=5)
        _assert_clean(env.prov)


# --------------------------------------------------------------------------
# issue #1: two terminal seats booted together must not share one DHCP IP
# --------------------------------------------------------------------------
def test_two_concurrent_terminal_seats_get_distinct_static_ips(env, tmp_path: Path) -> None:
    """The exact bug: concurrent seats collided on one DHCP lease.

    Two seats are created, then booted in parallel threads so they fight for
    a DHCP lease at the same moment (the shared golden DUID). Each must come
    up on its own static address and both must become SSH-ready; afterwards
    neither seat domain is left behind.
    """
    prov = env.prov
    resources = env.cfg.resources_for("terminal")
    caps = ResourceCaps(
        cpu_vcpus=resources.cpu_vcpus,
        memory_mb=resources.memory_mb,
        overlay_max_gb=resources.overlay_max_gb,
    )
    names = ("ipfix-a", "ipfix-b")
    refs: list[str] = []
    try:
        # Allocate/create sequentially (the scheduler does too); boot in
        # parallel so both guests request DHCP before either is reconfigured.
        for name in names:
            ref = prov.create_from_image(name, "terminal", "golden-term", caps)
            prov.apply_resource_limits(ref, caps)
            refs.append(ref)

        errors: list[tuple[str, str]] = []

        def _boot(ref: str) -> None:
            try:
                prov.start(ref)
                prov.wait_ready(ref, 480)
            except Exception as exc:  # noqa: BLE001 - collected for the assert
                errors.append((ref, repr(exc)))

        threads = [threading.Thread(target=_boot, args=(ref,)) for ref in refs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=540)
        assert not any(t.is_alive() for t in threads), "seat provisioning timed out"
        assert errors == [], errors

        ips = [prov.ip_for(ref) for ref in refs]
        print(f"\n[ipfix] static ips: {dict(zip(names, ips))}")
        assert ips[0] != ips[1], f"seats collided on the same address: {ips}"

        network = env.cfg.network
        for ref, ip in zip(refs, ips):
            assert ip.startswith(network.subnet_prefix + ".")
            host = int(ip.rsplit(".", 1)[1])
            assert network.host_range_start <= host <= network.host_range_end, ip
            # both seats are SSH-ready at their own address
            assert prov._guest_alive(ref), f"{ref} is not SSH-ready at {ip}"
            guest_addrs = prov.run(ref, "ip -o -4 addr show", timeout_s=30, check=True).stdout
            assert f"{ip}/" in guest_addrs, f"{ref} does not hold {ip}: {guest_addrs}"
            known_hosts = env.base / "seats" / ref[len(SEAT_DOMAIN_PREFIX) :] / "known_hosts"
            assert known_hosts.read_text(encoding="utf-8").startswith(f"{ip} "), ref
            print(f"[ipfix] {ref} SSH-ready at {ip}")
    finally:
        for ref in refs:
            try:
                prov.destroy(ref)
            except Exception:  # noqa: BLE001 - teardown must be best-effort
                pass
    _assert_clean(env.prov)


# --------------------------------------------------------------------------
# seat-lifetime automation: MCP auto-heartbeat + stasis (never destroy)
# --------------------------------------------------------------------------
def test_seat_stasis_and_mcp_autoheartbeat(env, tmp_path: Path) -> None:
    """Reproduce the original failure and prove the fix on a real seat.

    (a) Holding the seat past the heartbeat timeout without an explicit
        heartbeat survives, because the MCP server beats on the agent's behalf.
    (b) With beating stopped the seat enters stasis (``held``): it is NOT
        destroyed, the VM is preserved for recovery, and an operator can still
        discard it.
    """
    env.cfg.leases.heartbeat_timeout_s = 15
    env.cfg.leases.heartbeat_interval_s = 5
    env.cfg.leases.lease_timeout_s = 600

    mgr = _manager(env, tmp_path)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(mgr, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not socket_path.exists():
        if time.monotonic() >= deadline:  # pragma: no cover - startup guard
            raise RuntimeError("integration daemon never started")
        time.sleep(0.05)
    client = DaemonClient(socket_path=socket_path)
    tools = OmavroomTools(client, heartbeat_interval_s=5, heartbeat_timeout_s=15)
    seat_id: int | None = None
    try:
        handle = mgr.request_seat("integration-stasis", "terminal")
        view = handle.wait_ready(timeout=300)
        assert view.seat is not None and view.seat.state == "ready", view
        seat_id = view.seat.id
        vm_ref = view.seat.vm_name
        assert vm_ref is not None
        # Register the seat with the MCP server (its normal tracking path).
        tools.wait_for_seat(handle.request_id, timeout_s=5)
        assert seat_id in tools.heartbeats.tracked()
        print(f"\n[stasis] ready seat={view.seat.name} vm={vm_ref}")

        # (a) No explicit heartbeat for > heartbeat_timeout_s: MCP keeps it.
        time.sleep(env.cfg.leases.heartbeat_timeout_s + 5)
        live = next(s for s in mgr.pool_status().seats if s.id == seat_id)
        assert live.state in ("ready", "busy"), live
        info = env.prov.attach(vm_ref)
        assert info is not None and info.state == "running", info
        print("[stasis] seat survived past heartbeat timeout via MCP auto-heartbeat")

        # (b) Stop beating (simulate the MCP server process dying).
        tools.stop()
        deadline = time.monotonic() + (env.cfg.leases.heartbeat_timeout_s + 45)
        held = None
        while time.monotonic() < deadline:
            row = next(s for s in mgr.list_seats(include_history=True) if s.id == seat_id)
            if row.state == "held":
                held = row
                break
            time.sleep(1)
        assert held is not None, "seat never entered stasis"
        assert held.vm_name == vm_ref, "stasis destroyed or lost the VM reference"
        assert env.prov.attach(vm_ref) is not None, "stasis destroyed the VM"
        assert held.last_error == "stale: heartbeat_timeout", held.last_error
        assert seat_id in mgr.pool_status().needs_attention
        print("[stasis] heartbeat lapse -> held; VM and overlay preserved")
    finally:
        tools.stop()
        client.close()
        if seat_id is not None:
            try:
                mgr.force_discard(seat_id, reason="integration_cleanup").result(timeout=180)
            except Exception:  # noqa: BLE001 - teardown must be best-effort
                pass
        server.shutdown()
        thread.join(timeout=5)
        _assert_clean(env.prov)

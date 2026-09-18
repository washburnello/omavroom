"""Phase 4B1 advisory fixes (binding deltas recorded before 4B2).

Each test targets one advisory item and is written to fail on the pre-fix
code:

1. ``prepare_repo`` rejects credential-bearing repo URLs.
2. SSH options pin ``HostKeyAlgorithms=ssh-ed25519``.
3. Autostart invariant covers the template domains, not just seats.
4. The ``<backingStore/>`` docstring/test is real (element stripped).
5. A failed diffstat computation is a fetch failure (never ok with zeroes).
6. ``branch``/``ref`` are plumbed through manager-mediated export/release.
7. ``list_vms``/``reconcile`` scope wording matches the recorded delta.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omavroom.config import Config
from omavroom.manager.libvirt_provisioner import (
    DESKTOP_TEMPLATE,
    TERMINAL_TEMPLATE,
    CommandResult,
    LibvirtProvisioner,
    build_domain_xml,
    load_template,
    url_has_userinfo,
)
from omavroom.manager.provisioner import ExportSpec, FakeProvisioner, RepoSpec
from omavroom.manager.scheduler import Scheduler


# --------------------------------------------------------------------------
# FIX 1 - credential-bearing repo URLs are rejected
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/org/repo.git",
        "https://user@example.com/org/repo.git",
        "https://token@example.com/org/repo.git",
        "ssh://git@example.com/org/repo.git",
        "git://user@example.com/repo.git",
        "user@example.com:org/repo.git",
    ],
)
def test_credential_urls_are_detected(url: str) -> None:
    assert url_has_userinfo(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/org/repo.git",
        "git://example.com/repo.git",
        "ssh://example.com/org/repo.git",
        "/tmp/repos/scratch.git",
        "scratch",
        "example.com/org/repo.git",
        "git@github.com/org/repo.git",
    ],
)
def test_credential_free_urls_are_allowed(url: str) -> None:
    assert url_has_userinfo(url) is False


def _prepared_prov(tmp_path: Path) -> LibvirtProvisioner:
    from omavroom.manager.libvirt_provisioner import _SeatMeta

    prov = LibvirtProvisioner(Config.default(), base_dir=tmp_path)
    seat_dir = prov.seats_dir / "terminal-1"
    seat_dir.mkdir(parents=True)
    (seat_dir / "overlay.qcow2").write_bytes(b"x")
    prov._save_meta(
        _SeatMeta(
            name="terminal-1",
            seat_type="terminal",
            image="golden-term",
            golden=str(seat_dir / "golden.qcow2"),
            mac="52:54:00:00:00:01",
            uuid="00000000-0000-0000-0000-000000000000",
            domain="omavroom-seat-terminal-1",
            overlay=str(seat_dir / "overlay.qcow2"),
            nvram=str(seat_dir / "VARS.fd"),
        )
    )
    return prov


def test_prepare_repo_rejects_userinfo(tmp_path: Path) -> None:
    prov = _prepared_prov(tmp_path)
    with pytest.raises(Exception, match="credential-bearing"):
        prov.prepare_repo(
            "omavroom-seat-terminal-1",
            RepoSpec(url="https://user:pass@example.com/org/repo.git"),
        )


# --------------------------------------------------------------------------
# FIX 2 - pinned host key algorithm
# --------------------------------------------------------------------------
def test_ssh_opts_pin_host_key_algorithms(tmp_path: Path) -> None:
    prov = LibvirtProvisioner(Config.default(), base_dir=tmp_path, ssh_key=tmp_path / "id")
    opts = prov._ssh_opts("omavroom-seat-terminal-1")
    assert "HostKeyAlgorithms=ssh-ed25519" in opts
    # exactly one host-key algorithm is pinned to ed25519
    pinned = [o for o in opts if o.startswith("HostKeyAlgorithms=")]
    assert pinned == ["HostKeyAlgorithms=ssh-ed25519"]


# --------------------------------------------------------------------------
# FIX 3 - autostart invariant covers templates too
# --------------------------------------------------------------------------
def test_verify_autostart_invariant_checks_templates(tmp_path: Path) -> None:
    checked: list[str] = []

    def runner(argv, timeout, input_text):
        ref = argv[-1]
        checked.append(ref)
        return CommandResult(0, f"Name: {ref}\nAutostart: disable\n", "")

    prov = LibvirtProvisioner(Config.default(), base_dir=tmp_path, host_runner=runner)
    prov.verify_autostart_invariant()
    assert set(checked) == {DESKTOP_TEMPLATE, TERMINAL_TEMPLATE}


def test_verify_autostart_invariant_refuses_enabled_template(tmp_path: Path) -> None:
    def runner(argv, timeout, input_text):
        ref = argv[-1]
        value = "enable" if ref == TERMINAL_TEMPLATE else "disable"
        return CommandResult(0, f"Name: {ref}\nAutostart: {value}\n", "")

    prov = LibvirtProvisioner(Config.default(), base_dir=tmp_path, host_runner=runner)
    with pytest.raises(Exception, match="autostart is 'enable'"):
        prov.verify_autostart_invariant()


# --------------------------------------------------------------------------
# FIX 4 - <backingStore/> is actually stripped; template really has none
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seat_type", ["desktop", "terminal"])
def test_templates_declare_no_backing_store(seat_type: str) -> None:
    assert "backingStore" not in load_template(seat_type)


def test_build_domain_xml_strips_injected_backing_store() -> None:
    injected = load_template("terminal").replace("</disk>", "<backingStore/></disk>")
    assert "backingStore" in injected
    xml = build_domain_xml(
        seat_type="terminal",
        name="omavroom-seat-terminal-1",
        uuid="11111111-2222-3333-4444-555555555556",
        memory_mb=2048,
        vcpus=2,
        nvram_path="/tmp/VARS.fd",
        disk_path="/tmp/overlay.qcow2",
        mac="52:54:00:aa:bb:cd",
        template=injected,
    )
    assert "backingStore" not in xml
    assert "/tmp/overlay.qcow2" in xml


# --------------------------------------------------------------------------
# FIX 5 - a failed diffstat is a fetch failure
# --------------------------------------------------------------------------
_GUEST_SHA = "a" * 40
_MERGE_BASE = "b" * 40


class _ScriptedFetchProvisioner(LibvirtProvisioner):
    """Deterministic fetch_bundle driver; only the git diff is made to fail."""

    def __init__(self, *args, merge_base: str | None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._merge_base = merge_base

    def ip_for(self, vm_ref: str) -> str:
        return "10.0.0.5"

    def run(self, vm_ref, command, **kwargs) -> CommandResult:  # type: ignore[override]
        if "git status --porcelain" in command:
            return CommandResult(0, "", "")
        if "git stash list" in command:
            return CommandResult(0, "0\n", "")
        if "rev-parse --verify --quiet" in command:
            return CommandResult(0, ("main\n" if self._merge_base else "\n"), "")
        if "git merge-base" in command:
            return CommandResult(0, f"{self._merge_base}\n", "")
        if "git rev-parse" in command:
            return CommandResult(0, f"{_GUEST_SHA}\n", "")
        if "git bundle create" in command:
            return CommandResult(0, "", "")
        if command.startswith("test -s "):
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected guest command: {command}")

    def host(self, argv, *, timeout=60, input_text=None) -> CommandResult:  # type: ignore[override]
        if argv and argv[0] == "scp":
            Path(argv[-1]).write_bytes(b"bundle")
            return CommandResult(0, "", "")
        if argv and argv[0] == "git":
            if "diff" in argv and "--numstat" in argv:
                return CommandResult(1, "", "fatal: bad revision 'a^'")
            if "rev-parse" in argv and "--verify" in argv:
                return CommandResult(0, f"{_GUEST_SHA}\n", "")
            if "rev-list" in argv and "--count" in argv:
                return CommandResult(0, "1\n", "")
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected host command: {argv}")


@pytest.mark.parametrize("merge_base", [_MERGE_BASE, None])
def test_failed_diffstat_is_fetch_failure(tmp_path: Path, merge_base: str | None) -> None:
    """Pre-fix: a failed ``git diff --numstat`` returned ok=True with all-zero
    counts, so a destructive export could slip past the content gate."""
    prov = _ScriptedFetchProvisioner(Config.default(), base_dir=tmp_path, merge_base=merge_base)
    seat_dir = prov.seats_dir / "terminal-1"
    seat_dir.mkdir(parents=True)
    (seat_dir / "overlay.qcow2").write_bytes(b"x")
    from omavroom.manager.libvirt_provisioner import _SeatMeta

    prov._save_meta(
        _SeatMeta(
            name="terminal-1",
            seat_type="terminal",
            image="golden-term",
            golden=str(seat_dir / "golden.qcow2"),
            mac="52:54:00:00:00:01",
            uuid="00000000-0000-0000-0000-000000000000",
            domain="omavroom-seat-terminal-1",
            overlay=str(seat_dir / "overlay.qcow2"),
            nvram=str(seat_dir / "VARS.fd"),
        )
    )
    fetched = prov.fetch_bundle(
        "omavroom-seat-terminal-1",
        ExportSpec(repo=str(tmp_path / "host.git"), branch="task"),
    )
    assert fetched.ok is False
    assert "diffstat" in fetched.message
    assert fetched.files_changed == 0


# --------------------------------------------------------------------------
# FIX 6 - branch/ref plumbed through manager-mediated export/release
# --------------------------------------------------------------------------
def _ready_seat(mgr):
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    return mgr.seat_status(handle.request_id).seat


def test_manager_export_seat_plumbs_branch_ref(make_manager, config, fake) -> None:
    mgr = make_manager(config, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.export_seat(
        seat.id, repo="demo", branch="task", ref="origin:refs/heads/task"
    ).result(timeout=5)
    assert outcome.ok is True
    specs = [args[1] for name, args, _ in fake.calls if name == "fetch_bundle"]
    assert specs, "fetch_bundle was never called"
    assert specs[-1].branch == "task"
    assert specs[-1].ref == "origin:refs/heads/task"


def test_manager_release_seat_plumbs_branch_ref(make_manager, config, fake) -> None:
    mgr = make_manager(config, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(
        seat.id, repo="demo", branch="task", ref="origin:refs/heads/task"
    ).result(timeout=5)
    assert outcome.destroyed is True
    specs = [args[1] for name, args, _ in fake.calls if name == "fetch_bundle"]
    assert specs and specs[-1].branch == "task"
    assert specs[-1].ref == "origin:refs/heads/task"


def test_scheduler_export_seat_accepts_branch_ref(make_manager, config, fake) -> None:
    mgr = make_manager(config, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.scheduler.export_seat(
        seat.id, repo="demo", branch="task", ref="upstream:refs/heads/task"
    )
    assert outcome.ok is True
    specs = [args[1] for name, args, _ in fake.calls if name == "fetch_bundle"]
    assert specs[-1].branch == "task"
    assert specs[-1].ref == "upstream:refs/heads/task"


# --------------------------------------------------------------------------
# FIX 7 - docstrings match the recorded scope delta
# --------------------------------------------------------------------------
def test_list_vms_and_reconcile_docstrings_scope_managed_seats() -> None:
    from omavroom.manager.provisioner import Provisioner

    list_doc = (Provisioner.list_vms.__doc__ or "").lower()
    assert "managed" in list_doc and "template" in list_doc and "excluded" in list_doc

    reconcile_doc = (Scheduler.reconcile.__doc__ or "").lower()
    assert "managed (seat)" in reconcile_doc and "template" in reconcile_doc


def test_fake_provisioner_implements_autostart_hook() -> None:
    assert FakeProvisioner().verify_autostart_invariant() is None

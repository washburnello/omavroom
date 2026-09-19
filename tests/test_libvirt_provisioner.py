"""Phase 4B LibvirtProvisioner unit tests (no VMs; real integration is marked).

These exercise the pure/deterministic parts: XML assembly against the proven
templates, config image resolution, SSH option pinning, push destination
resolution/idempotency, and overlay quota monitoring with an injected host
runner. The real libvirt path lives in ``test_libvirt_integration.py``.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from omavroom.config import Config
from omavroom.manager.libvirt_provisioner import (
    CPU_PERIOD_US,
    CommandResult,
    LibvirtProvisioner,
    build_domain_xml,
    is_safe_ref_token,
    is_safe_sha,
    png_dimensions,
    repo_name_from,
    safe_branch_name,
)
from omavroom.manager.provisioner import ExportSpec, FetchResult, RepoSpec

PNG_1X1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _prov(tmp_path: Path, config: Config | None = None, **kwargs) -> LibvirtProvisioner:
    return LibvirtProvisioner(config or Config.default(), base_dir=tmp_path, **kwargs)


def _meta(prov: LibvirtProvisioner, name: str, overlay: Path, *, seat_type="terminal"):
    from omavroom.manager.libvirt_provisioner import _SeatMeta

    meta = _SeatMeta(
        name=name,
        seat_type=seat_type,
        image="golden-term",
        golden=str(overlay),
        mac="52:54:00:00:00:01",
        uuid="00000000-0000-0000-0000-000000000000",
        domain=f"omavroom-seat-{name}",
        overlay=str(overlay),
        nvram=str(overlay.parent / "VARS.fd"),
    )
    prov._save_meta(meta)
    return meta


# --------------------------------------------------------------------------
# XML assembly
# --------------------------------------------------------------------------
def test_build_domain_xml_desktop_has_unique_and_proven_devices() -> None:
    xml = build_domain_xml(
        seat_type="desktop",
        name="omavroom-seat-desktop-1",
        uuid="11111111-2222-3333-4444-555555555555",
        memory_mb=4096,
        vcpus=2,
        nvram_path="/tmp/VARS.fd",
        disk_path="/tmp/overlay.qcow2",
        mac="52:54:00:aa:bb:cc",
        cpu_quota=2 * CPU_PERIOD_US,
        mem_hard_limit_kb=4608 * 1024,
        mem_soft_limit_kb=4096 * 1024,
    )
    assert "<name>omavroom-seat-desktop-1</name>" in xml
    assert "11111111-2222-3333-4444-555555555555" in xml
    assert "/tmp/VARS.fd" in xml
    assert "/tmp/overlay.qcow2" in xml
    assert "52:54:00:aa:bb:cc" in xml
    norm = xml.replace('"', "'")
    assert "<memory unit='KiB'>4194304</memory>" in norm
    assert "<quota>200000</quota>" in norm
    assert "<hard_limit unit='KiB'>4718592</hard_limit>" in norm
    assert "<graphics type='vnc'" in norm
    assert "virtio-vga" in norm
    # the empty <backingStore/> must be gone or libvirt passes backing=null
    assert "backingStore" not in xml


def test_build_domain_xml_terminal_is_headless() -> None:
    xml = build_domain_xml(
        seat_type="terminal",
        name="omavroom-seat-terminal-1",
        uuid="11111111-2222-3333-4444-555555555556",
        memory_mb=2048,
        vcpus=2,
        nvram_path="/tmp/VARS.fd",
        disk_path="/tmp/overlay.qcow2",
        mac="52:54:00:aa:bb:cd",
    )
    assert "<graphics" not in xml
    assert "<video>" not in xml
    assert "virtio-vga" not in xml


def test_build_domain_xml_unknown_seat_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_domain_xml(
            seat_type="toaster",
            name="x",
            uuid="y",
            memory_mb=1,
            vcpus=1,
            nvram_path="n",
            disk_path="d",
            mac="m",
        )


# --------------------------------------------------------------------------
# config image mapping
# --------------------------------------------------------------------------
def test_golden_for_falls_back_by_seat_type() -> None:
    cfg = Config.default()
    # Desktop defaults to the Omarchy golden; the stock name stays a fallback.
    assert cfg.golden_for("desktop").name == "golden-omarchy.qcow2"
    assert cfg.golden_for("desktop", "omavroom-base").name == "golden-desktop.qcow2"
    assert cfg.golden_for("terminal").name == "golden-term.qcow2"
    # an explicit registered name wins
    assert cfg.golden_for("desktop", "golden-desktop").name == "golden-desktop.qcow2"


def test_golden_for_rejects_seat_type_mismatch() -> None:
    cfg = Config.default()
    with pytest.raises(ValueError, match="seat type"):
        cfg.golden_for("terminal", "golden-desktop")


def test_config_parses_custom_images(tmp_path: Path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[seats.terminal]\n"
        'image = "custom-term"\n'
        "[images.custom-term]\n"
        'golden = "~/goldens/custom.qcow2"\n'
        'seat_type = "terminal"\n',
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.image_for("terminal") == "custom-term"
    assert cfg.golden_for("terminal").name == "custom.qcow2"
    assert cfg.golden_for("terminal", "custom-term").name == "custom.qcow2"
    # an unregistered image still falls back to the seat-type default
    assert cfg.golden_for("desktop", "omavroom-base").name == "golden-desktop.qcow2"


def test_config_rejects_bad_image_entries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text('[images.broken]\nseat_type = "toaster"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="images.broken.seat_type"):
        Config.from_toml(cfg_file)
    cfg_file.write_text('[images.broken]\ngolden = ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="images.broken.golden"):
        Config.from_toml(cfg_file)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("git://host:9418/scratch.git", "scratch"),
        ("/tmp/repos/scratch.git/", "scratch"),
        ("https://example.com/org/repo", "repo"),
        ("scratch", "scratch"),
    ],
)
def test_repo_name_from(value: str, expected: str) -> None:
    assert repo_name_from(value) == expected


def test_safe_branch_name() -> None:
    assert safe_branch_name("feature/x") == "feature_x"


def test_png_dimensions_reads_ihdr() -> None:
    assert png_dimensions(PNG_1X1) == (1, 1)
    with pytest.raises(ValueError):
        png_dimensions(b"not a png")


def test_ssh_opts_pin_host_key(tmp_path: Path) -> None:
    prov = _prov(tmp_path, ssh_key=tmp_path / "id")
    opts = prov._ssh_opts("omavroom-seat-terminal-1")
    assert "StrictHostKeyChecking=yes" in opts
    user_known = [o for o in opts if o.startswith("UserKnownHostsFile=")]
    assert user_known and not user_known[0].endswith("/dev/null")
    assert user_known[0].endswith("known_hosts")
    assert "BatchMode=yes" in opts


def test_guest_repo_path_resolution(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    overlay = tmp_path / "seats" / "terminal-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"x")
    meta = _meta(prov, "terminal-1", overlay)
    meta.repos["scratch"] = {
        "url": "git://h/scratch.git",
        "branch": None,
        "guest_path": "/home/agent/workspace/scratch",
    }
    prov._save_meta(meta)
    ref = "omavroom-seat-terminal-1"
    assert prov.guest_repo_path(ref, "scratch") == "/home/agent/workspace/scratch"
    assert prov.guest_repo_path(ref, "/tmp/repos/scratch.git") == "/home/agent/workspace/scratch"
    assert prov.guest_repo_path(ref, "other") == "/home/agent/workspace/other"
    assert prov.guest_repo_path(ref, "/home/agent/custom") == "/home/agent/custom"


# --------------------------------------------------------------------------
# overlay quota monitoring
# --------------------------------------------------------------------------
def test_overlay_usage_and_quota(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    overlay = tmp_path / "seats" / "terminal-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"hello world")
    ref = "omavroom-seat-terminal-1"
    _meta(prov, "terminal-1", overlay)
    usage = prov.overlay_usage_gb(ref)
    assert usage > 0
    assert prov.check_overlay_quota(ref, 100) == usage
    with pytest.raises(Exception, match="quota exceeded"):
        prov.check_overlay_quota(ref, 0)


def test_unknown_seat_has_zero_usage(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    assert prov.overlay_usage_gb("omavroom-seat-nope") == 0.0


# --------------------------------------------------------------------------
# push destination + idempotency
# --------------------------------------------------------------------------
def test_push_destination_resolution(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    default = prov._push_destination(ExportSpec(repo="r", branch="task"))
    assert default == ("origin", "refs/heads/task")
    remote_only = prov._push_destination(ExportSpec(repo="r", branch="task", ref="upstream"))
    assert remote_only == ("upstream", "refs/heads/task")
    explicit = prov._push_destination(
        ExportSpec(repo="r", branch="task", ref="/tmp/remote.git:refs/heads/x")
    )
    assert explicit == ("/tmp/remote.git", "refs/heads/x")
    bare = prov._push_destination(ExportSpec(repo="r", branch="task", ref="/tmp/remote.git:task"))
    assert bare == ("/tmp/remote.git", "refs/heads/task")


def test_push_refuses_unverified(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    result = prov.push(ExportSpec(repo="/repo", branch="task"), FetchResult(ok=False))
    assert result.ok is False
    assert "unverified" in result.message


def test_push_is_idempotent_when_remote_matches(tmp_path: Path) -> None:
    sha = "a" * 40
    calls: list[list[str]] = []

    def runner(argv, timeout, input_text):
        calls.append(list(argv))
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        if "ls-remote" in argv:
            return CommandResult(0, f"{sha}\trefs/heads/task\n", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    fetched = FetchResult(ok=True, sha=sha, spec=ExportSpec(repo="/repo", branch="task"))
    result = prov.push(ExportSpec(repo="/repo", branch="task"), fetched)
    assert result.ok is True
    assert result.sha == sha
    assert not any("push" in call for call in calls)


def test_push_verifies_remote_sha(tmp_path: Path) -> None:
    sha = "b" * 40
    state = {"pushed": False}

    def runner(argv, timeout, input_text):
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        if "ls-remote" in argv:
            if state["pushed"]:
                return CommandResult(0, f"{sha}\trefs/heads/task\n", "")
            return CommandResult(0, "", "")
        if "push" in argv:
            state["pushed"] = True
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    fetched = FetchResult(ok=True, sha=sha, spec=ExportSpec(repo="/repo", branch="task"))
    result = prov.push(ExportSpec(repo="/repo", branch="task"), fetched)
    assert result.ok is True
    assert result.sha == sha


def test_destroy_is_idempotent_when_domain_absent(tmp_path: Path) -> None:
    def runner(argv, timeout, input_text):
        return CommandResult(1, "", "error: failed to get domain")

    prov = _prov(tmp_path, host_runner=runner)
    ref = "omavroom-seat-terminal-1"
    seat_dir = tmp_path / "seats" / "terminal-1"
    seat_dir.mkdir(parents=True)
    (seat_dir / "overlay.qcow2").write_bytes(b"x")
    prov.destroy(ref)  # must not raise
    assert not seat_dir.exists()


def test_list_vms_excludes_template_domains(tmp_path: Path) -> None:
    def runner(argv, timeout, input_text):
        if argv[-2:] == ["--all", "--name"]:
            return CommandResult(0, "omavroom-base\nomavroom-term\nomavroom-seat-terminal-1\n", "")
        if argv[-2] == "domstate":
            return CommandResult(0, "running\n", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    vms = prov.list_vms()
    assert [vm.name for vm in vms] == ["terminal-1"]
    assert vms[0].ref == "omavroom-seat-terminal-1"
    assert vms[0].state == "running"


def test_prepare_repo_requires_url(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    overlay = tmp_path / "seats" / "terminal-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"x")
    _meta(prov, "terminal-1", overlay)
    with pytest.raises(Exception, match="requires a URL"):
        prov.prepare_repo("omavroom-seat-terminal-1", RepoSpec(url=None))


def test_ensure_golden_images_builds_missing(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, timeout, input_text):
        calls.append(list(argv))
        if argv[0].endswith("qemu-img"):
            Path(argv[-1]).write_bytes(b"golden")
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    cfg = Config.default()
    for name in cfg.images:
        cfg.images[name].golden = str(tmp_path / "goldens" / f"{name}.qcow2")
    prov = _prov(tmp_path, config=cfg, host_runner=runner)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "base.qcow2").write_bytes(b"base")
    (images_dir / "term.qcow2").write_bytes(b"term")
    built = prov.ensure_golden_images()
    assert built["desktop"].name == "golden-omarchy.qcow2"
    assert built["terminal"].name == "golden-term.qcow2"
    assert (built["desktop"].stat().st_mode & 0o777) == 0o444
    assert any("convert" in call for call in calls)
    assert any("-l" in call and "hyprland-proof" in call for call in calls)
    # a second call is a no-op
    before = len(calls)
    prov.ensure_golden_images()
    assert len(calls) == before
    assert cfg.golden_for("desktop").name == "golden-omarchy.qcow2"


def test_click_command_mapping() -> None:
    assert "ydotool click 272" in LibvirtProvisioner._click_command("10,20")
    assert "ydotool click 273" in LibvirtProvisioner._click_command("10,20,3")
    with pytest.raises(Exception, match="click value"):
        LibvirtProvisioner._click_command("nope")


def test_screenshot_converts_ppm_and_downscales(tmp_path: Path) -> None:
    from omavroom.manager.libvirt_provisioner import _subprocess_runner

    def runner(argv, timeout, input_text):
        if argv[0] == "virsh":
            if "screenshot" in argv:
                target = Path(argv[argv.index("--file") + 1])
                target.write_bytes(b"P6\n4 4\n255\n" + bytes([255, 0, 0]) * 16)
            return CommandResult(0, "", "")
        return _subprocess_runner(argv, timeout, input_text)

    prov = _prov(tmp_path, host_runner=runner)
    overlay = tmp_path / "seats" / "desktop-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"x")
    _meta(prov, "desktop-1", overlay, seat_type="desktop")
    png = prov.screenshot("omavroom-seat-desktop-1", max_width=2)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, _ = png_dimensions(png)
    assert width == 2


def test_screenshot_on_terminal_reports_no_graphics(tmp_path: Path) -> None:
    def runner(argv, timeout, input_text):
        if argv[0] == "virsh" and "screenshot" in argv:
            return CommandResult(1, "", "no screens to take screenshot from")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    overlay = tmp_path / "seats" / "terminal-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"x")
    _meta(prov, "terminal-1", overlay)
    with pytest.raises(Exception, match="screenshot failed"):
        prov.screenshot("omavroom-seat-terminal-1")


# --------------------------------------------------------------------------
# FIX 2 (4B2) - git ref option-injection hardening
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ref",
    [
        "-x",
        "--upload-pack=/bin/echo",
        "--upload-pack=/bin/echo:refs/heads/x",
        "origin:--upload-pack=/bin/echo",
        "origin:",
    ],
)
def test_push_destination_rejects_option_injection(tmp_path: Path, ref: str) -> None:
    prov = _prov(tmp_path)
    with pytest.raises(Exception, match="unsafe|invalid|empty"):
        prov._push_destination(ExportSpec(repo="r", branch="task", ref=ref))


def test_push_destination_rejects_option_like_branch(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    with pytest.raises(Exception, match="invalid branch"):
        prov._push_destination(ExportSpec(repo="r", branch="--upload-pack=/bin/echo"))


def test_push_rejects_option_injection_without_calling_git(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, timeout, input_text):
        calls.append(list(argv))
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    fetched = FetchResult(ok=True, sha="a" * 40, spec=ExportSpec(repo="/repo", branch="task"))
    result = prov.push(
        ExportSpec(repo="/repo", branch="task", ref="--upload-pack=/bin/echo:refs/heads/x"),
        fetched,
    )
    assert result.ok is False
    assert "unsafe" in result.message or "option" in result.message
    assert not any("--upload-pack" in arg for call in calls for arg in call)


def test_push_rejects_unknown_bare_remote(tmp_path: Path) -> None:
    sha = "a" * 40

    def runner(argv, timeout, input_text):
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        if "remote" in argv:
            return CommandResult(0, "origin\n", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    fetched = FetchResult(ok=True, sha=sha, spec=ExportSpec(repo="/repo", branch="task"))
    result = prov.push(ExportSpec(repo="/repo", branch="task", ref="evil:refs/heads/x"), fetched)
    assert result.ok is False
    assert "not 'origin'" in result.message


def test_push_accepts_known_remote_and_explicit_path(tmp_path: Path) -> None:
    sha = "a" * 40

    def runner(argv, timeout, input_text):
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        if "remote" in argv:
            return CommandResult(0, "upstream\n", "")
        if "ls-remote" in argv:
            return CommandResult(0, f"{sha}\trefs/heads/x\n", "")
        return CommandResult(0, "", "")

    prov = _prov(tmp_path, host_runner=runner)
    fetched = FetchResult(ok=True, sha=sha, spec=ExportSpec(repo="/repo", branch="task"))
    known = prov.push(ExportSpec(repo="/repo", branch="task", ref="upstream:refs/heads/x"), fetched)
    assert known.ok is True
    path = prov.push(
        ExportSpec(repo="/repo", branch="task", ref="/tmp/remote.git:refs/heads/x"), fetched
    )
    assert path.ok is True


def test_push_enforces_configured_remote_allowlist(tmp_path: Path) -> None:
    sha = "a" * 40

    def runner(argv, timeout, input_text):
        if argv[1:3] == ["-C", "/repo"] and argv[3] == "rev-parse":
            return CommandResult(0, ".git\n", "")
        if "ls-remote" in argv:
            return CommandResult(0, f"{sha}\trefs/heads/x\n", "")
        return CommandResult(0, "", "")

    cfg = Config.default()
    cfg.export.allowed_remotes = ("upstream",)
    prov = _prov(tmp_path, config=cfg, host_runner=runner)
    fetched = FetchResult(ok=True, sha=sha, spec=ExportSpec(repo="/repo", branch="task"))
    # ``origin`` is git's default push remote and stays allowed even with an
    # allowlist configured (the allowlist only *adds* remotes).
    origin_ok = prov.push(
        ExportSpec(repo="/repo", branch="task", ref="origin:refs/heads/x"), fetched
    )
    assert origin_ok.ok is True
    allowed = prov.push(
        ExportSpec(repo="/repo", branch="task", ref="upstream:refs/heads/x"), fetched
    )
    assert allowed.ok is True
    denied = prov.push(ExportSpec(repo="/repo", branch="task", ref="evil:refs/heads/x"), fetched)
    assert denied.ok is False
    assert "allowed_remotes" in denied.message


def test_push_rejects_non_sha_fetch(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    fetched = FetchResult(ok=True, sha="not-a-sha", spec=ExportSpec(repo="/repo", branch="task"))
    result = prov.push(ExportSpec(repo="/repo", branch="task"), fetched)
    assert result.ok is False
    assert "SHA" in result.message


def test_config_parses_allowed_remotes(tmp_path: Path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text('[export]\nallowed_remotes = ["upstream", "backup"]\n', encoding="utf-8")
    cfg = Config.from_toml(cfg_file)
    assert cfg.export.allowed_remotes == ("upstream", "backup")


# --------------------------------------------------------------------------
# FIX A (round 3) - guest-controlled git tokens never reach host git argv
# --------------------------------------------------------------------------
class _GuestScriptedFetch(LibvirtProvisioner):
    """fetch_bundle with a fully scripted guest (returns attacker-set tokens)."""

    def __init__(self, *args, base: str, guest_sha: str, merge_base: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._base = base
        self._guest_sha = guest_sha
        self._merge_base = merge_base

    def ip_for(self, vm_ref: str) -> str:  # avoid DHCP in fetch_bundle
        return "10.0.0.5"

    def run(self, vm_ref, command, **kwargs) -> CommandResult:  # type: ignore[override]
        if "git status --porcelain" in command:
            return CommandResult(0, "", "")
        if "git stash list" in command:
            return CommandResult(0, "0\n", "")
        if "refs/heads/main" in command:  # base detection
            return CommandResult(0, f"{self._base}\n", "")
        if "git merge-base" in command:
            return CommandResult(0, f"{self._merge_base}\n", "")
        if "git rev-parse" in command:
            return CommandResult(0, f"{self._guest_sha}\n", "")
        if "git bundle create" in command:
            return CommandResult(0, "", "")
        if command.startswith("test -s "):
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected guest command: {command}")


def _scripted_fetch_host(record: list[list[str]], sha: str):
    def host(argv, timeout, input_text) -> CommandResult:
        record.append(list(argv))
        if argv and argv[0] == "scp":
            Path(argv[-1]).write_bytes(b"bundle")
            return CommandResult(0, "", "")
        if argv and argv[0] == "git":
            if "rev-parse" in argv and "--verify" in argv:
                return CommandResult(0, f"{sha}\n", "")
            if "rev-list" in argv:
                return CommandResult(0, "1\n", "")
            if "diff" in argv and "--numstat" in argv:
                return CommandResult(0, "1\t0\tsrc/app.py\n", "")
            return CommandResult(0, "", "")
        return CommandResult(1, "", f"unexpected host command: {argv}")

    return host


def _prepare_fetch(
    tmp_path: Path,
    *,
    base: str = "main",
    guest_sha: str = "a" * 40,
    merge_base: str = "b" * 40,
    host_runner=None,
):
    kwargs = dict(base=base, guest_sha=guest_sha, merge_base=merge_base)
    prov = _GuestScriptedFetch(
        Config.default(), base_dir=tmp_path, host_runner=host_runner, **kwargs
    )
    seat_dir = prov.seats_dir / "terminal-1"
    seat_dir.mkdir(parents=True)
    (seat_dir / "overlay.qcow2").write_bytes(b"x")
    _meta(prov, "terminal-1", seat_dir / "overlay.qcow2")
    return prov, str(tmp_path / "host.git")


def test_fetch_bundle_valid_shas_succeed(tmp_path: Path) -> None:
    record: list[list[str]] = []
    sha = "a" * 40
    prov, host_repo = _prepare_fetch(
        tmp_path, guest_sha=sha, host_runner=_scripted_fetch_host(record, sha)
    )
    fetched = prov.fetch_bundle(
        "omavroom-seat-terminal-1", ExportSpec(repo=host_repo, branch="task")
    )
    assert fetched.ok is True, fetched.message
    assert fetched.sha == sha
    # The removed/revision guards are present on the host git invocations.
    flat = [arg for call in record for arg in call]
    assert "--end-of-options" in flat
    assert "--" in flat


def test_fetch_bundle_rejects_malicious_merge_base(tmp_path: Path) -> None:
    record: list[list[str]] = []
    pwned = tmp_path / "pwned_merge_base"
    prov, host_repo = _prepare_fetch(
        tmp_path,
        merge_base=f"--output={pwned}",
        host_runner=_scripted_fetch_host(record, "a" * 40),
    )
    fetched = prov.fetch_bundle(
        "omavroom-seat-terminal-1", ExportSpec(repo=host_repo, branch="task")
    )
    assert fetched.ok is False
    assert "merge-base" in fetched.message
    # The option-like token never reached a host argv (so git can't act on it).
    assert not any("--output" in arg for call in record for arg in call)


def test_fetch_bundle_rejects_malicious_guest_sha(tmp_path: Path) -> None:
    record: list[list[str]] = []
    prov, host_repo = _prepare_fetch(
        tmp_path,
        guest_sha=f"--output={tmp_path / 'pwned_sha'}",
        host_runner=_scripted_fetch_host(record, "a" * 40),
    )
    fetched = prov.fetch_bundle(
        "omavroom-seat-terminal-1", ExportSpec(repo=host_repo, branch="task")
    )
    assert fetched.ok is False
    assert "guest SHA" in fetched.message
    assert not any("--output" in arg for call in record for arg in call)


def test_fetch_bundle_rejects_malicious_base(tmp_path: Path) -> None:
    record: list[list[str]] = []
    prov, host_repo = _prepare_fetch(
        tmp_path,
        base=f"--output={tmp_path / 'pwned_base'}",
        host_runner=_scripted_fetch_host(record, "a" * 40),
    )
    fetched = prov.fetch_bundle(
        "omavroom-seat-terminal-1", ExportSpec(repo=host_repo, branch="task")
    )
    assert fetched.ok is False
    assert "base ref" in fetched.message


def test_sha_and_ref_token_validators() -> None:
    assert is_safe_sha("a" * 40) is True
    assert is_safe_sha("a" * 64) is True
    assert is_safe_sha("A" * 40) is False  # uppercase is not a git object id
    assert is_safe_sha(f"--output={Path('/tmp/x')}") is False
    assert is_safe_sha("main") is False
    assert is_safe_ref_token("main") is True
    assert is_safe_ref_token("origin/HEAD") is True
    assert is_safe_ref_token("-x") is False
    assert is_safe_ref_token("--output=/tmp/x") is False
    assert is_safe_ref_token("a b") is False


def test_git_output_option_demonstration(tmp_path: Path) -> None:
    """Prove the underlying git behavior the validator defends against."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "demo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=a",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    pwned = tmp_path / "created_by_git"
    # Without --end-of-options, git accepts the injected option and writes a file.
    subprocess.run(["git", "-C", str(repo), "diff", "--numstat", f"--output={pwned}", "HEAD"])
    assert pwned.exists()
    pwned.unlink()
    # With --end-of-options, git rejects it and writes nothing.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--numstat",
            "--end-of-options",
            f"--output={pwned}",
            "HEAD",
        ],
        capture_output=True,
    )
    assert not pwned.exists()


# --------------------------------------------------------------------------
# FIX C (round 3) - click button may not inject guest shell
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "1,2,3; touch /tmp/omavroom_pwned",
        "1,2,$(touch /tmp/omavroom_pwned)",
        "1,2,272; rm -rf /",
        "1,2,0",
        "1,2,4",
    ],
)
def test_click_command_rejects_injected_button(value: str) -> None:
    with pytest.raises(Exception, match="button|click value"):
        LibvirtProvisioner._click_command(value)


def test_click_command_valid_buttons() -> None:
    assert "ydotool click 272" in LibvirtProvisioner._click_command("10,20")
    assert "ydotool click 274" in LibvirtProvisioner._click_command("10,20,2")
    assert "ydotool click 273" in LibvirtProvisioner._click_command("10,20,3")


def test_prepare_repo_rejects_option_like_url_and_branch(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    seat_dir = tmp_path / "seats" / "terminal-1"
    seat_dir.mkdir(parents=True)
    (seat_dir / "overlay.qcow2").write_bytes(b"x")
    _meta(prov, "terminal-1", seat_dir / "overlay.qcow2")
    with pytest.raises(Exception, match="unsafe repo URL"):
        prov.prepare_repo("omavroom-seat-terminal-1", RepoSpec(url="--upload-pack=/bin/echo"))
    with pytest.raises(Exception, match="unsafe repo branch"):
        prov.prepare_repo(
            "omavroom-seat-terminal-1",
            RepoSpec(url="https://example.com/x.git", branch="--upload-pack=/bin/echo"),
        )


# --------------------------------------------------------------------------
# static seat addressing (DHCP collision fix)
# --------------------------------------------------------------------------
def _seat_with_overlay(prov: LibvirtProvisioner, name: str = "terminal-1") -> str:
    overlay = prov.seats_dir / name / "overlay.qcow2"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"x")
    _meta(prov, name, overlay)
    return f"omavroom-seat-{name}"


def test_ip_for_returns_static_without_touching_dhcp(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, timeout, input_text):
        calls.append(list(argv))
        return CommandResult(1, "", "unexpected host command")

    prov = _prov(tmp_path, host_runner=runner)
    ref = _seat_with_overlay(prov)
    meta = prov._load_meta(ref)
    assert meta is not None
    meta.static_ip = "192.168.122.205"
    prov._save_meta(meta)
    assert prov.ip_for(ref) == "192.168.122.205"
    assert calls == [], "ip_for must not consult net-dhcp-leases for a static seat"


def test_static_allocation_is_unique_across_seats(tmp_path: Path) -> None:
    prov = _prov(tmp_path)
    ref = _seat_with_overlay(prov, "terminal-1")
    meta = prov._load_meta(ref)
    assert meta is not None
    meta.static_ip = "192.168.122.200"
    prov._save_meta(meta)
    allocated = prov._allocate_static_ip("terminal-2")
    assert allocated != "192.168.122.200"
    assert allocated.startswith("192.168.122.")


class _StaticAgentProvisioner(LibvirtProvisioner):
    """Scripts the guest agent to prove the static-unit flow end to end."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.agent_commands: list[str] = []

    def agent_ping(self, vm_ref: str) -> bool:
        return True

    def agent_exec(self, vm_ref: str, command: str, *, timeout_s: int = 60) -> CommandResult:
        self.agent_commands.append(command)
        if "ssh_host_ed25519_key.pub" in command:
            return CommandResult(0, "ssh-ed25519 AAAAC3NzaCAgZWQyNTUxOQ\n", "")
        if "IDENTITY_ROTATED" in command:
            return CommandResult(0, "IDENTITY_ROTATED\n", "")
        return CommandResult(0, "NET_STATIC_OK\n", "")


def test_configure_static_network_writes_mac_matched_unit(tmp_path: Path) -> None:
    prov = _StaticAgentProvisioner(Config.default(), base_dir=tmp_path)
    ref = _seat_with_overlay(prov)
    meta = prov._load_meta(ref)
    assert meta is not None
    meta.static_ip = "192.168.122.207"
    prov._save_meta(meta)
    prov._configure_static_network(ref, meta)
    assert len(prov.agent_commands) == 1
    command = prov.agent_commands[0]
    assert f"MACAddress={meta.mac}" in command
    assert "Address=192.168.122.207/24" in command
    assert "Gateway=192.168.122.1" in command
    assert "DNS=192.168.122.1" in command
    # leftover DHCP units are removed; the static unit is not
    assert "! -name '10-omavroom-static.network' -delete" in command
    assert "systemctl restart systemd-networkd" in command


def test_ensure_identity_pins_known_hosts_at_static_ip(tmp_path: Path) -> None:
    prov = _StaticAgentProvisioner(Config.default(), base_dir=tmp_path)
    ref = _seat_with_overlay(prov)
    meta = prov._load_meta(ref)
    assert meta is not None
    meta.static_ip = "192.168.122.209"
    prov._save_meta(meta)
    prov._ensure_identity(ref, rotate=True, timeout_s=60)
    known_hosts = (prov.seats_dir / "terminal-1" / "known_hosts").read_text(encoding="utf-8")
    assert known_hosts.startswith("192.168.122.209 ssh-ed25519 ")
    assert any("Address=192.168.122.209/24" in cmd for cmd in prov.agent_commands)

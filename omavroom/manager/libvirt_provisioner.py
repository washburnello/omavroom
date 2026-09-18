"""Real libvirt/QEMU provisioner for omavroom seats (Phase 4B).

This is the concrete :class:`~omavroom.manager.provisioner.Provisioner`
implementation behind the frozen Phase 4A interface. It owns the whole
lifecycle of a disposable seat VM: a per-seat qcow2 **overlay** on a
read-only **golden** image, a unique libvirt domain (unique name/uuid/MAC/
NVRAM), per-seat identity regeneration, SSH transport, host-side export,
and teardown.

Golden images
-------------
Two read-only goldens are built once from the proven Phase 1-3 snapshot
state with ``qemu-img convert -l <snapshot>``: ``golden-desktop.qcow2``
from ``omavroom-base@hyprland-proof`` and ``golden-term.qcow2`` from
``omavroom-term@term-git``. They are chmod ``444`` and every seat is a
copy-on-write overlay on one of them; the golden is never written. The
image-name -> golden-path mapping lives in config (``[images.<name>]`` /
``[seats.<type>.image]``) and is resolved by :meth:`Config.golden_for`.

Overlay disk cap
----------------
A qcow2 overlay must span the golden's full 30 GiB virtual disk, and qcow2
has no per-image quota, so a *virtual-size* cap cannot be imposed without
shrinking the guest filesystem. ``overlay_max_gb`` is therefore enforced
as **growth monitoring**: :meth:`overlay_usage_gb` measures the host bytes
the overlay actually allocates (``st_blocks``, sparse-aware) and
:meth:`apply_resource_limits` (and :meth:`wait_ready`) refuse/raise when
the cap is exceeded. This is documented and honest: it is an alert/refuse
mechanism, not a hard block-layer quota. A true cap would need a guest
filesystem quota or an in-guest enforcement agent (deferred).

Autostart invariant
-------------------
Seat domains are **never** autostarted. Every define/start re-asserts and
verifies ``virsh dominfo <dom>`` reports ``Autostart: disable``; a host
reboot can therefore never resurrect a seat VM outside manager authority.

Identity and SSH
----------------
No two seats may share identity, so each seat's ``machine-id`` and SSH
host keys are regenerated in-guest after first boot. The rotation is
driven over the libvirt ``qemu-guest-agent`` channel (``guest-exec``), so
the host never trusts an unseen key (no TOFU): the new
``ssh_host_ed25519_key.pub`` is read back through the agent and pinned in
a per-seat ``known_hosts``. Every subsequent SSH uses
``StrictHostKeyChecking=yes`` against that file; real operations never use
``StrictHostKeyChecking=no``. Because ``reset`` discards the overlay and
re-creates it from the golden, identity is regenerated on every reset.

Discovery and naming
--------------------
Seat domains are named ``omavroom-seat-<seat-name>``. That prefix is
load-bearing: it distinguishes manager-owned seat domains from the
``omavroom-base`` / ``omavroom-term`` template domains, so
:meth:`list_vms` (which the reconciler walks) never sees — and therefore
never destroys — the goldens' template domains. The scheduler's reconcile
contract matches VMs to seats by seat *name*, so :class:`VmInfo.name`
strips the prefix and equals the seat name.

Exec transport
--------------
:meth:`LibvirtProvisioner.run` is a real SSH command runner (pinned
``known_hosts``, key auth, no shell on the host side). It is the primitive
Phase 5's ``exec_*`` tools will build on; ``agent_exec`` is the
guest-agent exec path used for identity rotation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from omavroom.config import Config
from omavroom.manager.provisioner import (
    CommandResult,
    ExportSpec,
    FetchResult,
    InputEvent,
    Provisioner,
    ProvisionerError,
    PushResult,
    RepoSpec,
    ResourceCaps,
    VmInfo,
)

log = logging.getLogger("omavroom.libvirt")

DEFAULT_URI = "qemu:///system"
SEAT_DOMAIN_PREFIX = "omavroom-seat-"
DEFAULT_SSH_KEY = "~/.ssh/omavroom_ed25519"
DEFAULT_SSH_USER = "agent"
DESKTOP_TEMPLATE = "omavroom-base"
TERMINAL_TEMPLATE = "omavroom-term"
DESKTOP_NVRAM = "omavroom-base_VARS.fd"
TERMINAL_NVRAM = "omavroom-term_VARS.fd"
OVMF_VARS_TEMPLATE = "/usr/share/edk2/x64/OVMF_VARS.4m.fd"
DESKTOP_SNAPSHOT = "hyprland-proof"
TERMINAL_SNAPSHOT = "term-git"
WORKSPACE_DIR = "/home/agent/workspace"
CPU_PERIOD_US = 100_000
MEM_OVERHEAD_MB = 512
_GOLDEN_SOURCES: dict[str, tuple[str, str]] = {
    "desktop": ("base.qcow2", DESKTOP_SNAPSHOT),
    "terminal": ("term.qcow2", TERMINAL_SNAPSHOT),
}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _subprocess_runner(argv: list[str], timeout: int, input_text: str | None) -> CommandResult:
    """Default host runner: list argv, no shell, hard timeout."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = (exc.stderr or "") + f"\n(command timed out after {timeout}s)"
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return CommandResult(124, out, err)
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _subprocess_stream_runner(
    argv: list[str],
    timeout: int,
    on_output: Callable[[str, str], None] | None,
    cancel: threading.Event | None,
) -> CommandResult:
    """Stream a host command's stdout/stderr while it runs.

    Used by :meth:`LibvirtProvisioner.run` for the Phase 5 exec path: the
    ``ssh`` process is started directly (not through
    :func:`_subprocess_runner`) so output can be forwarded as it arrives and
    so the ``cancel`` event can terminate the transport. Killing the local
    ``ssh`` process closes the channel and normally terminates the remote
    command; a command that detaches itself in the guest is out of scope
    (documented limitation -- see the exec engine docs).
    """
    import subprocess  # local import: only needed on the streaming path

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    # In streaming mode the caller owns the output (the exec engine copies a
    # bounded tail into its ring buffer), so the transport must not retain an
    # unbounded copy of its own. Only buffer when there is no callback.
    retain = on_output is None
    out_buf: list[str] = []
    err_buf: list[str] = []

    def _pump(pipe, stream: str, buf: list[str]) -> None:
        try:
            for line in iter(pipe.readline, ""):
                if retain:
                    buf.append(line)
                if on_output is not None:
                    try:
                        on_output(stream, line)
                    except Exception:  # noqa: BLE001 - never break the pump
                        pass
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, "stdout", out_buf), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, "stderr", err_buf), daemon=True),
    ]
    for reader in readers:
        reader.start()

    killed = False
    timed_out = False
    deadline = time.monotonic() + timeout if timeout else None

    def _terminate() -> None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    while True:
        if cancel is not None and cancel.is_set():
            killed = True
            _terminate()
            break
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            _terminate()
            break
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue

    for reader in readers:
        reader.join(timeout=2)

    returncode = proc.returncode
    timeout_note = ""
    if timed_out:
        returncode = 124
        timeout_note = f"\n(command timed out after {timeout}s)\n"
        if retain:
            err_buf.append(timeout_note)
    elif killed and returncode == 0:
        returncode = -9
    stderr = "".join(err_buf) or timeout_note
    return CommandResult(
        returncode if returncode is not None else -1,
        "".join(out_buf) if retain else "",
        stderr,
    )


def _template_dir() -> Path:
    return Path(__file__).with_name("templates")


def load_template(seat_type: str) -> str:
    """Return the proven XML template text for a seat type."""
    if seat_type not in ("desktop", "terminal"):
        raise ValueError(f"unknown seat type: {seat_type!r}")
    return (_template_dir() / f"{seat_type}.xml").read_text(encoding="utf-8")


def build_domain_xml(
    *,
    seat_type: str,
    name: str,
    uuid: str,
    memory_mb: int,
    vcpus: int,
    nvram_path: str,
    disk_path: str,
    mac: str,
    cpu_quota: int | None = None,
    cpu_period: int = CPU_PERIOD_US,
    mem_hard_limit_kb: int | None = None,
    mem_soft_limit_kb: int | None = None,
    template: str | None = None,
) -> str:
    """Assemble a seat domain XML from the proven template.

    Unique per seat: name, uuid, MAC, NVRAM path, overlay disk path. Per
    type: memory, vcpus and optional ``cputune`` quota / ``memtune`` limits.

    The proven templates declare **no** ``<backingStore/>`` element, because
    libvirt probes the overlay qcow2 header and passes the backing chain to
    QEMU itself. Any ``<backingStore>`` element (empty or populated) is
    defensively stripped here so the image header, not the domain XML, is
    always authoritative for the backing chain.
    """
    text = template if template is not None else load_template(seat_type)
    root = ET.fromstring(text)
    for disk in root.findall("devices/disk"):
        backing = disk.find("backingStore")
        if backing is not None:
            disk.remove(backing)

    root.find("name").text = name
    root.find("uuid").text = uuid
    memory = root.find("memory")
    memory.set("unit", "KiB")
    memory.text = str(memory_mb * 1024)
    current = root.find("currentMemory")
    current.set("unit", "KiB")
    current.text = str(memory_mb * 1024)
    root.find("vcpu").text = str(vcpus)
    root.find("os/nvram").text = nvram_path
    root.find("devices/disk/source").set("file", disk_path)
    root.find("devices/interface/mac").set("address", mac)

    insert_at = list(root).index(root.find("vcpu")) + 1
    if cpu_quota is not None:
        cputune = ET.Element("cputune")
        ET.SubElement(cputune, "quota").text = str(cpu_quota)
        ET.SubElement(cputune, "period").text = str(cpu_period)
        root.insert(insert_at, cputune)
        insert_at += 1

    if mem_hard_limit_kb is not None or mem_soft_limit_kb is not None:
        memtune = ET.Element("memtune")
        if mem_hard_limit_kb is not None:
            hard = ET.SubElement(memtune, "hard_limit")
            hard.set("unit", "KiB")
            hard.text = str(mem_hard_limit_kb)
        if mem_soft_limit_kb is not None:
            soft = ET.SubElement(memtune, "soft_limit")
            soft.set("unit", "KiB")
            soft.text = str(mem_soft_limit_kb)
        root.insert(insert_at, memtune)

    ET.indent(root, space="  ")
    return "<?xml version='1.0' encoding='UTF-8'?>\n" + ET.tostring(root, encoding="unicode") + "\n"


def repo_name_from(value: str) -> str:
    """Derive a stable repo name from a URL or path (basename, no ``.git``)."""
    trimmed = value.rstrip("/")
    name = trimmed.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name


def safe_branch_name(branch: str) -> str:
    """Filesystem-safe branch token for bundle/ref names."""
    return branch.replace("/", "_").replace("\\", "_")


_SCP_LIKE_RE = re.compile(r"^[^/@:\s]+@[^/:\s]+:")
_CTRL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_REMOTE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_REFSPEC_RE = re.compile(r"^refs/[^\s\x00-\x1f\x7f]+$")
_SAFE_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
#: A short (unqualified) ref/branch token safe to pass to git as an argument.
_SAFE_REF_TOKEN_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


def is_safe_sha(value: str) -> bool:
    """True when ``value`` is exactly a hex object id (no option/revision syntax)."""
    return bool(_SAFE_SHA_RE.fullmatch((value or "").strip()))


def is_safe_ref_token(value: str) -> bool:
    """True when ``value`` is a plain branch/ref token safe as a bare git arg.

    Rejects empty, leading ``-`` (option injection), control/whitespace, and
    the sequences git forbids in ref names. Used for guest-derived short refs
    such as the base branch, which are interpolated into guest git commands.
    """
    token = (value or "").strip()
    if not token or token.startswith("-") or len(token) > 255:
        return False
    if not _SAFE_REF_TOKEN_RE.fullmatch(token):
        return False
    return not (".." in token or "@{" in token or "//" in token or token.endswith(("/", ".")))


def url_has_userinfo(url: str) -> bool:
    """Return ``True`` when ``url`` carries userinfo that could be credentials.

    Flagged forms are ``scheme://user[:secret]@host/...`` and scp-like
    ``user@host:path``. A credential-free URL (``https://host/repo``,
    ``git://host/repo``), an absolute path, or a bare ``host/repo`` is not
    flagged. Callers reject flagged URLs because the guest must never
    receive credentials.
    """
    candidate = (url or "").strip()
    if not candidate:
        return False
    if "://" in candidate:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return False
        return "@" in parsed.netloc
    return bool(_SCP_LIKE_RE.match(candidate))


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read PNG width/height from the IHDR chunk without an image library."""
    if not data.startswith(_PNG_MAGIC) or len(data) < 24:
        raise ValueError("not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _quote(value: str) -> str:
    return shlex.quote(value)


@dataclass
class _SeatMeta:
    """Durable per-seat metadata persisted beside the overlay."""

    name: str
    seat_type: str
    image: str
    golden: str
    mac: str
    uuid: str
    domain: str
    overlay: str
    nvram: str
    created_at: str = ""
    identity_ready: bool = False
    identity_generation: int = 0
    last_ip: str | None = None
    repos: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> _SeatMeta:
        data = json.loads(text)
        return cls(**data)


class LibvirtProvisioner(Provisioner):
    """Concrete libvirt provisioner; see the module docstring for design."""

    def __init__(
        self,
        config: Config,
        *,
        uri: str = DEFAULT_URI,
        base_dir: str | Path | None = None,
        ssh_key: str | Path = DEFAULT_SSH_KEY,
        ssh_user: str = DEFAULT_SSH_USER,
        ssh_port: int = 22,
        workspace_dir: str = WORKSPACE_DIR,
        virsh_bin: str = "virsh",
        qemu_img_bin: str = "qemu-img",
        magick_bin: str | None = None,
        max_bundle_bytes: int = 2 * 1024**3,
        max_export_commits: int = 100,
        dhcp_timeout_s: int = 60,
        stop_timeout_s: int = 45,
        reset_ready_timeout_s: int = 300,
        golden_convert_timeout_s: int = 900,
        fsck_timeout_s: int = 300,
        host_runner: Callable[[list[str], int, str | None], CommandResult] | None = None,
        stream_runner: (
            Callable[
                [list[str], int, Callable[[str, str], None] | None, threading.Event | None],
                CommandResult,
            ]
            | None
        ) = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.uri = uri
        self.base_dir = (
            Path(base_dir) if base_dir is not None else Path.home() / ".local/share/omavroom"
        )
        self.seats_dir = self.base_dir / "seats"
        self.nvram_dir = self.base_dir / "nvram"
        self.staging_dir = self.base_dir / "staging"
        self.images_dir = self.base_dir / "images"
        self.ssh_key = Path(ssh_key).expanduser()
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.workspace_dir = workspace_dir
        self.virsh_bin = virsh_bin
        self.qemu_img_bin = qemu_img_bin
        self.magick_bin = magick_bin or shutil.which("magick") or shutil.which("convert")
        self.max_bundle_bytes = max_bundle_bytes
        self.max_export_commits = max_export_commits
        self.dhcp_timeout_s = dhcp_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.reset_ready_timeout_s = reset_ready_timeout_s
        self.golden_convert_timeout_s = golden_convert_timeout_s
        self.fsck_timeout_s = fsck_timeout_s
        self._runner = host_runner or _subprocess_runner
        self._stream_runner = stream_runner or _subprocess_stream_runner
        self._sleep = sleep
        self._monotonic = monotonic
        for directory in (self.seats_dir, self.nvram_dir, self.staging_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # host command plumbing
    # ------------------------------------------------------------------
    def host(
        self, argv: list[str], *, timeout: int = 60, input_text: str | None = None
    ) -> CommandResult:
        """Run one host command through the injected runner."""
        return self._runner(argv, timeout, input_text)

    def _virsh(self, *args: str, timeout: int = 60) -> CommandResult:
        return self.host([self.virsh_bin, "--connect", self.uri, *args], timeout=timeout)

    def _virsh_ok(self, *args: str, timeout: int = 60) -> CommandResult:
        result = self._virsh(*args, timeout=timeout)
        if not result.ok:
            raise ProvisionerError(
                f"virsh {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result

    def _qemu_img(self, *args: str, timeout: int = 300) -> CommandResult:
        result = self.host([self.qemu_img_bin, *args], timeout=timeout)
        if not result.ok:
            raise ProvisionerError(
                f"qemu-img {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result

    def _git(self, repo: str, *args: str, timeout: int = 120) -> CommandResult:
        return self.host(["git", "-C", repo, *args], timeout=timeout)

    def _git_ok(self, repo: str, *args: str, timeout: int = 120) -> CommandResult:
        result = self._git(repo, *args, timeout=timeout)
        if not result.ok:
            raise ProvisionerError(
                f"git -C {repo} {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result

    # ------------------------------------------------------------------
    # metadata
    # ------------------------------------------------------------------
    def _seat_name(self, ref: str) -> str:
        name = ref[len(SEAT_DOMAIN_PREFIX) :] if ref.startswith(SEAT_DOMAIN_PREFIX) else ref
        if not name or "/" in name or name in (".", ".."):
            raise ProvisionerError(f"invalid seat ref: {ref!r}")
        return name

    def _seat_dir(self, ref: str) -> Path:
        return self.seats_dir / self._seat_name(ref)

    def _meta_path(self, ref: str) -> Path:
        return self._seat_dir(ref) / "seat.json"

    def _load_meta(self, ref: str) -> _SeatMeta | None:
        path = self._meta_path(ref)
        if not path.exists():
            return None
        try:
            return _SeatMeta.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("unreadable seat metadata %s: %s", path, exc)
            return None

    def _save_meta(self, meta: _SeatMeta) -> None:
        path = self.seats_dir / meta.name / "seat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(meta.to_json(), encoding="utf-8")

    def _require_meta(self, ref: str) -> _SeatMeta:
        meta = self._load_meta(ref)
        if meta is None:
            raise ProvisionerError(f"unknown seat VM: {ref!r}")
        return meta

    def _known_hosts_path(self, ref: str) -> Path:
        return self._seat_dir(ref) / "known_hosts"

    # ------------------------------------------------------------------
    # golden images
    # ------------------------------------------------------------------
    def ensure_golden_images(self, *, force: bool = False) -> dict[str, Path]:
        """Build any missing golden from its proven snapshot; chmod ``444``.

        ``qemu-img convert -l <snapshot>`` reads the internal snapshot state
        out of the template image into a standalone qcow2, so the goldens
        are independent of (and cannot mutate) the template images.
        """
        built: dict[str, Path] = {}
        for seat_type, (source_name, snapshot) in _GOLDEN_SOURCES.items():
            destination = self.config.golden_for(seat_type)
            if destination.exists() and not force:
                built[seat_type] = destination
                continue
            source = self.images_dir / source_name
            if not source.exists():
                raise ProvisionerError(f"template image missing: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._qemu_img(
                "convert",
                "-l",
                snapshot,
                "-O",
                "qcow2",
                str(source),
                str(destination),
                timeout=self.golden_convert_timeout_s,
            )
            os.chmod(destination, 0o444)
            built[seat_type] = destination
        return built

    # ------------------------------------------------------------------
    # seat creation
    # ------------------------------------------------------------------
    def _golden_for(self, seat_type: str, image: str) -> Path:
        try:
            golden = self.config.golden_for(seat_type, image)
        except (KeyError, ValueError) as exc:
            raise ProvisionerError(f"cannot resolve golden for image {image!r}: {exc}") from exc
        if not golden.is_file():
            raise ProvisionerError(f"golden image not found: {golden}")
        mode = golden.stat().st_mode & 0o777
        if mode & 0o222:
            log.warning("golden %s is writable (mode %o); expected 444", golden, mode)
        return golden

    def _nvram_template(self, seat_type: str) -> Path:
        name = DESKTOP_NVRAM if seat_type == "desktop" else TERMINAL_NVRAM
        candidate = self.nvram_dir / name
        return candidate if candidate.exists() else Path(OVMF_VARS_TEMPLATE)

    def _random_mac(self) -> str:
        # QEMU's 52:54:00 OUI, locally administered.
        raw = uuid_module.uuid4().bytes
        return f"52:54:00:{raw[0]:02x}:{raw[1]:02x}:{raw[2]:02x}"

    def create_from_image(
        self, vm_name: str, seat_type: str, image: str, resources: ResourceCaps
    ) -> str:
        """Create the overlay + NVRAM + domain; return the libvirt ref."""
        if seat_type not in self.config.seats:
            raise ProvisionerError(f"unknown seat type: {seat_type!r}")
        ref = f"{SEAT_DOMAIN_PREFIX}{vm_name}"
        seat_dir = self.seats_dir / vm_name
        if self._virsh("dominfo", ref, timeout=30).ok:
            raise ProvisionerError(f"domain already exists: {ref}")
        golden = self._golden_for(seat_type, image)

        seat_dir.mkdir(parents=True, exist_ok=True)
        overlay = seat_dir / "overlay.qcow2"
        if overlay.exists():
            raise ProvisionerError(f"overlay already exists: {overlay}")
        self._qemu_img("create", "-f", "qcow2", "-b", str(golden), "-F", "qcow2", str(overlay))
        nvram = self.nvram_dir / f"{ref}_VARS.fd"
        shutil.copyfile(self._nvram_template(seat_type), nvram)

        mac = self._random_mac()
        domain_uuid = str(uuid_module.uuid4())
        quota = resources.cpu_vcpus * CPU_PERIOD_US
        hard_kb = (resources.memory_mb + MEM_OVERHEAD_MB) * 1024
        soft_kb = resources.memory_mb * 1024
        xml = build_domain_xml(
            seat_type=seat_type,
            name=ref,
            uuid=domain_uuid,
            memory_mb=resources.memory_mb,
            vcpus=resources.cpu_vcpus,
            nvram_path=str(nvram),
            disk_path=str(overlay),
            mac=mac,
            cpu_quota=quota,
            mem_hard_limit_kb=hard_kb,
            mem_soft_limit_kb=soft_kb,
        )
        xml_path = seat_dir / "domain.xml"
        xml_path.write_text(xml, encoding="utf-8")

        result = self._virsh("define", str(xml_path), timeout=60)
        if not result.ok:
            shutil.rmtree(seat_dir, ignore_errors=True)
            nvram.unlink(missing_ok=True)
            raise ProvisionerError(f"virsh define failed for {ref}: {result.stderr.strip()}")
        self._virsh("autostart", ref, "--disable", timeout=30)
        self._assert_no_autostart(ref)

        meta = _SeatMeta(
            name=vm_name,
            seat_type=seat_type,
            image=image,
            golden=str(golden),
            mac=mac,
            uuid=domain_uuid,
            domain=ref,
            overlay=str(overlay),
            nvram=str(nvram),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._save_meta(meta)
        log.info("created %s from %s (mac=%s)", ref, golden.name, mac)
        return ref

    def _assert_no_autostart(self, ref: str) -> None:
        """Binding invariant: the domain must never autostart."""
        result = self._virsh("dominfo", ref, timeout=30)
        if not result.ok:
            raise ProvisionerError(f"cannot read dominfo for {ref}: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            if line.strip().startswith("Autostart:"):
                value = line.split(":", 1)[1].strip()
                if value != "disable":
                    raise ProvisionerError(f"autostart is {value!r} for {ref}, refusing")
                return
        raise ProvisionerError(f"could not verify autostart state for {ref}")

    def verify_autostart_invariant(self) -> None:
        """Assert autostart is disabled on the template domains.

        The ABC requires the invariant on base **and** seat domains. Seat
        domains are asserted at create/start/limit time; this verifies the
        read-only ``omavroom-base`` / ``omavroom-term`` templates, which must
        never come back after a host reboot outside manager authority.
        """
        for template in (DESKTOP_TEMPLATE, TERMINAL_TEMPLATE):
            self._assert_no_autostart(template)

    # ------------------------------------------------------------------
    # resource caps / overlay quota
    # ------------------------------------------------------------------
    def overlay_usage_gb(self, vm_ref: str) -> float:
        """Host bytes actually allocated by the overlay, in GiB.

        ``st_blocks`` is sparse-aware, so this is the real on-disk cost, not
        the 30 GiB virtual size the overlay must span.
        """
        meta = self._load_meta(vm_ref)
        if meta is None:
            return 0.0
        overlay = Path(meta.overlay)
        if not overlay.exists():
            return 0.0
        return os.stat(overlay).st_blocks * 512 / 1024**3

    def check_overlay_quota(self, vm_ref: str, max_gb: int) -> float:
        """Raise if the overlay's allocated bytes exceed ``max_gb`` GiB."""
        usage = self.overlay_usage_gb(vm_ref)
        if usage > max_gb:
            raise ProvisionerError(
                f"overlay quota exceeded for {vm_ref}: {usage:.2f} GiB > {max_gb} GiB"
            )
        return usage

    def apply_resource_limits(self, vm_ref: str, resources: ResourceCaps) -> None:
        """Verify/apply mandatory CPU/RAM caps and enforce the overlay quota.

        The domain is assembled with these caps at create time (memory,
        vcpus, ``cputune`` quota). If the stored domain differs (e.g. a
        re-adopted VM), memory/vcpus are reconfigured via ``virsh
        setmem``/``setvcpus --config``. The overlay cap is checked as
        allocated growth (see module docstring).
        """
        self._require_meta(vm_ref)
        result = self._virsh("dumpxml", vm_ref, timeout=30)
        if not result.ok:
            raise ProvisionerError(f"cannot dump domain {vm_ref}: {result.stderr.strip()}")
        root = ET.fromstring(result.stdout)
        current_mem_mb = int(root.find("memory").text) // 1024
        current_vcpus = int(root.find("vcpu").text)
        if current_mem_mb != resources.memory_mb:
            self._virsh("setmaxmem", vm_ref, f"{resources.memory_mb}MiB", "--config", timeout=30)
            self._virsh("setmem", vm_ref, f"{resources.memory_mb}MiB", "--config", timeout=30)
        if current_vcpus != resources.cpu_vcpus:
            self._virsh(
                "setvcpus",
                vm_ref,
                str(resources.cpu_vcpus),
                "--config",
                "--maximum",
                timeout=30,
            )
            self._virsh("setvcpus", vm_ref, str(resources.cpu_vcpus), "--config", timeout=30)
        self._virsh("autostart", vm_ref, "--disable", timeout=30)
        self._assert_no_autostart(vm_ref)
        self.check_overlay_quota(vm_ref, resources.overlay_max_gb)

    # ------------------------------------------------------------------
    # power lifecycle
    # ------------------------------------------------------------------
    def _domstate(self, ref: str) -> str:
        result = self._virsh("domstate", ref, timeout=30)
        return result.stdout.strip() if result.ok else "unknown"

    def _exists(self, ref: str) -> bool:
        return self._virsh("dominfo", ref, timeout=30).ok

    def start(self, vm_ref: str) -> None:
        """Boot the seat with no host-visible display (VNC localhost only)."""
        if not self._exists(vm_ref):
            raise ProvisionerError(f"domain not defined: {vm_ref}")
        state = self._domstate(vm_ref)
        if state == "running":
            self._assert_no_autostart(vm_ref)
            return
        result = self._virsh("start", vm_ref, timeout=120)
        if not result.ok:
            raise ProvisionerError(f"virsh start failed for {vm_ref}: {result.stderr.strip()}")
        self._virsh("autostart", vm_ref, "--disable", timeout=30)
        self._assert_no_autostart(vm_ref)

    def stop(self, vm_ref: str) -> None:
        """Graceful shutdown, bounded; force off only if it does not exit."""
        if not self._exists(vm_ref):
            return
        state = self._domstate(vm_ref)
        if state in ("shut off", "crashed"):
            return
        self._virsh("shutdown", vm_ref, timeout=30)
        deadline = self._monotonic() + self.stop_timeout_s
        while self._monotonic() < deadline:
            if self._domstate(vm_ref) == "shut off":
                return
            self._sleep(1)
        self._virsh("destroy", vm_ref, timeout=30)

    def destroy(self, vm_ref: str) -> None:
        """Destroy the domain and delete overlay/NVRAM/metadata (idempotent)."""
        if self._exists(vm_ref):
            if self._domstate(vm_ref) in ("running", "paused"):
                self._virsh("destroy", vm_ref, timeout=60)
            result = self._virsh("undefine", vm_ref, "--nvram", timeout=60)
            if not result.ok:
                self._virsh("undefine", vm_ref, timeout=60)
        seat_dir = self._seat_dir(vm_ref)
        meta = self._load_meta(vm_ref)
        shutil.rmtree(seat_dir, ignore_errors=True)
        if meta is not None:
            Path(meta.nvram).unlink(missing_ok=True)
        shutil.rmtree(self.staging_dir / self._seat_name(vm_ref), ignore_errors=True)
        log.info("destroyed %s", vm_ref)

    # ------------------------------------------------------------------
    # DHCP / SSH transport
    # ------------------------------------------------------------------
    def _dhcp_leases(self) -> list[tuple[str, str]]:
        """(mac, ip) pairs from the default network's DHCP leases.

        The expiry column contains spaces (``YYYY-MM-DD HH:MM:SS``), so the
        MAC/IP are located by shape rather than fixed position.
        """
        result = self._virsh("net-dhcp-leases", "default", timeout=30)
        if not result.ok:
            return []
        mac_re = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
        ip_re = re.compile(r"(\d+\.\d+\.\d+\.\d+)/\d+")
        leases: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            mac_match = next((f for f in fields if mac_re.fullmatch(f.lower())), None)
            ip_match = next((ip_re.fullmatch(f) for f in fields if ip_re.fullmatch(f)), None)
            if mac_match and ip_match:
                leases.append((mac_match.lower(), ip_match.group(1)))
        return leases

    def _ip_for_mac(self, mac: str) -> str | None:
        target = mac.lower()
        for lease_mac, ip in self._dhcp_leases():
            if lease_mac == target:
                return ip
        return None

    def _discover_ip(self, mac: str, timeout_s: int) -> str:
        deadline = self._monotonic() + timeout_s
        while self._monotonic() < deadline:
            ip = self._ip_for_mac(mac)
            if ip:
                return ip
            self._sleep(1)
        raise ProvisionerError(f"no DHCP lease for MAC {mac} within {timeout_s}s")

    def ip_for(self, vm_ref: str) -> str:
        """Current guest IP for a seat (DHCP lease by MAC), refreshed."""
        meta = self._require_meta(vm_ref)
        if meta.last_ip and self._ip_for_mac(meta.mac) == meta.last_ip:
            return meta.last_ip
        ip = self._discover_ip(meta.mac, self.dhcp_timeout_s)
        meta.last_ip = ip
        self._save_meta(meta)
        return ip

    def _ssh_opts(self, vm_ref: str) -> list[str]:
        return [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._known_hosts_path(vm_ref)}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "HostKeyAlgorithms=ssh-ed25519",
            "-o",
            "LogLevel=ERROR",
            "-i",
            str(self.ssh_key),
        ]

    def _ssh_argv(self, vm_ref: str, command: str, env: dict[str, str] | None = None) -> list[str]:
        ip = self.ip_for(vm_ref)
        prefix = ""
        if env:
            prefix = "env " + " ".join(_quote(f"{k}={v}") for k, v in env.items()) + " "
        argv = ["ssh", *self._ssh_opts(vm_ref)]
        if self.ssh_port != 22:
            argv += ["-p", str(self.ssh_port)]
        argv += [f"{self.ssh_user}@{ip}", "--", f"{prefix}{command}"]
        return argv

    def _scp_argv(self, vm_ref: str, remote_path: str, local_path: str) -> list[str]:
        ip = self.ip_for(vm_ref)
        argv = ["scp", *self._ssh_opts(vm_ref)]
        if self.ssh_port != 22:
            argv += ["-P", str(self.ssh_port)]
        argv += [f"{self.ssh_user}@{ip}:{remote_path}", local_path]
        return argv

    def run(
        self,
        vm_ref: str,
        command: str,
        *,
        timeout_s: int = 60,
        env: dict[str, str] | None = None,
        check: bool = False,
        on_output: Callable[[str, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        """Run a command inside the seat over pinned-key SSH.

        This is the real exec primitive; Phase 5's ``exec_start``/``exec_poll``
        tools build streaming on top of it. Real operations always verify the
        guest host key against the per-seat ``known_hosts``.

        Without ``on_output``/``cancel`` it uses the injected blocking host
        runner (``host``), preserving the lifecycle/export behaviour. With
        either control it uses the streaming runner, which forwards output as
        it arrives and can terminate the         ``ssh`` process on cancel. Killing
        ``ssh`` closes the channel, which normally terminates the remote
        command; a command that daemonises itself in the guest is a documented
        limitation (the local handle is gone, the guest process may survive).
        The engine's ``exec_kill(signal=N)`` does **not** deliver signal ``N``
        to a guest process -- it only records ``-N`` and terminates the local
        transport.
        """
        argv = self._ssh_argv(vm_ref, command, env)
        if on_output is None and cancel is None:
            result = self.host(argv, timeout=timeout_s)
        else:
            result = self._stream_runner(argv, timeout_s, on_output, cancel)
        if check and not result.ok:
            raise ProvisionerError(
                f"guest command failed ({result.returncode}) on {vm_ref}: {result.stderr.strip()}"
            )
        return result

    def _guest_alive(self, vm_ref: str) -> bool:
        return self.run(vm_ref, "true", timeout_s=10).ok

    def wait_ready(self, vm_ref: str, timeout_s: int) -> None:
        """Wait for DHCP + SSH; regenerate/pin identity before first SSH."""
        meta = self._require_meta(vm_ref)
        deadline = self._monotonic() + timeout_s
        ip = self._discover_ip(meta.mac, timeout_s)
        remaining = max(1, int(deadline - self._monotonic()))
        self._ensure_identity(vm_ref, rotate=not meta.identity_ready, timeout_s=remaining)
        while self._monotonic() < deadline:
            if self._guest_alive(vm_ref):
                usage = self.check_overlay_quota(
                    vm_ref, self.config.resources_for(meta.seat_type).overlay_max_gb
                )
                log.info("%s ready at %s (overlay %.2f GiB)", vm_ref, ip, usage)
                return
            self._sleep(1)
        raise ProvisionerError(f"seat {vm_ref} not SSH-ready within {timeout_s}s")

    # ------------------------------------------------------------------
    # guest agent + identity
    # ------------------------------------------------------------------
    def agent_command(self, vm_ref: str, payload: dict, *, timeout: int = 30) -> dict:
        """One ``virsh qemu-agent-command`` round trip."""
        result = self._virsh("qemu-agent-command", vm_ref, json.dumps(payload), timeout=timeout)
        if not result.ok:
            raise ProvisionerError(f"guest agent error on {vm_ref}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def agent_ping(self, vm_ref: str) -> bool:
        try:
            self.agent_command(vm_ref, {"execute": "guest-ping"}, timeout=15)
            return True
        except (ProvisionerError, ValueError):
            return False

    def agent_exec(self, vm_ref: str, command: str, *, timeout_s: int = 60) -> CommandResult:
        """Run ``/bin/sh -c command`` as root via the guest agent."""
        payload = {
            "execute": "guest-exec",
            "arguments": {"path": "/bin/sh", "arg": ["-c", command], "capture-output": True},
        }
        pid = self.agent_command(vm_ref, payload)["return"]["pid"]
        deadline = self._monotonic() + timeout_s
        while self._monotonic() < deadline:
            status = self.agent_command(
                vm_ref, {"execute": "guest-exec-status", "arguments": {"pid": pid}}
            )["return"]
            if status.get("exited"):
                import base64

                out = base64.b64decode(status.get("out-data", "")).decode(errors="replace")
                err = base64.b64decode(status.get("err-data", "")).decode(errors="replace")
                return CommandResult(int(status.get("exitcode", 0)), out, err)
            self._sleep(0.3)
        raise ProvisionerError(f"guest-exec timed out after {timeout_s}s on {vm_ref}")

    def _ensure_identity(self, vm_ref: str, *, rotate: bool, timeout_s: int) -> None:
        """Regenerate (optional) and pin the seat's identity via the agent.

        The host reads the guest's public host key through the qemu-guest-agent
        channel, never by trusting an unknown key on the network, so the
        pinned ``known_hosts`` entry is authoritative.
        """
        deadline = self._monotonic() + max(30, timeout_s)
        while not self.agent_ping(vm_ref):
            if self._monotonic() >= deadline:
                raise ProvisionerError(
                    f"qemu-guest-agent not responding on {vm_ref}; "
                    "the golden must ship qemu-guest-agent for identity rotation"
                )
            self._sleep(1)
        meta = self._require_meta(vm_ref)
        if rotate:
            # A random /etc/machine-id (not systemd-machine-id-setup, which
            # derives a deterministic id from the domain's SMBIOS UUID) so no
            # two seats and no two resets share a machine-id.
            command = (
                "umask 022; rm -f /etc/ssh/ssh_host_*; "
                "ssh-keygen -A >/dev/null 2>&1; "
                "MID=$(cat /proc/sys/kernel/random/uuid | tr -d -); "
                'echo "$MID" > /etc/machine-id; '
                "if [ -e /var/lib/dbus/machine-id ]; then "
                'echo "$MID" > /var/lib/dbus/machine-id; fi; '
                "rm -f /var/lib/systemd/network/duid /var/lib/NetworkManager/secret_key; "
                f"hostnamectl set-hostname {shlex.quote(meta.name)} >/dev/null 2>&1 || true; "
                "systemctl restart sshd; echo IDENTITY_ROTATED"
            )
            result = self.agent_exec(vm_ref, command, timeout_s=90)
            if result.returncode != 0 or "IDENTITY_ROTATED" not in result.stdout:
                detail = result.stderr.strip() or result.stdout
                raise ProvisionerError(f"identity rotation failed on {vm_ref}: {detail}")
        key_result = self.agent_exec(vm_ref, "cat /etc/ssh/ssh_host_ed25519_key.pub", timeout_s=30)
        key = key_result.stdout.strip()
        if not key.startswith("ssh-ed25519 "):
            raise ProvisionerError(f"could not read guest host key on {vm_ref}: {key!r}")
        ip = self.ip_for(vm_ref)
        known_hosts = self._known_hosts_path(vm_ref)
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        host_token = ip if self.ssh_port == 22 else f"[{ip}]:{self.ssh_port}"
        known_hosts.write_text(f"{host_token} {key}\n", encoding="utf-8")
        meta.identity_ready = True
        if rotate:
            meta.identity_generation += 1
        self._save_meta(meta)
        log.info("identity pinned for %s (generation %d)", vm_ref, meta.identity_generation)

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------
    def reset(self, vm_ref: str) -> None:
        """Discard the overlay, rebuild it from the golden, and reboot.

        Reset reverts the seat to the golden state (the equivalent of the
        Phase 2 ``snapshot-revert`` for an overlay) and is intentionally a
        hard discard: the running VM is destroyed, a fresh overlay and NVRAM
        are created, and the seat boots and becomes SSH-ready again. Because
        the overlay is discarded, per-seat identity does **not** persist —
        it is regenerated after the reboot to preserve the no-shared-identity
        invariant.
        """
        meta = self._require_meta(vm_ref)
        if self._domstate(vm_ref) != "shut off":
            self._virsh("destroy", vm_ref, timeout=60)
            deadline = self._monotonic() + 60
            while self._domstate(vm_ref) != "shut off" and self._monotonic() < deadline:
                self._sleep(1)
        overlay = Path(meta.overlay)
        overlay.unlink(missing_ok=True)
        self._qemu_img("create", "-f", "qcow2", "-b", meta.golden, "-F", "qcow2", str(overlay))
        Path(meta.nvram).unlink(missing_ok=True)
        shutil.copyfile(self._nvram_template(meta.seat_type), meta.nvram)
        meta.identity_ready = False
        meta.last_ip = None
        meta.repos = {}
        self._save_meta(meta)
        self.start(vm_ref)
        self.wait_ready(vm_ref, self.reset_ready_timeout_s)

    # ------------------------------------------------------------------
    # repo injection
    # ------------------------------------------------------------------
    def guest_repo_path(self, vm_ref: str, repo_key: str) -> str:
        """Resolve a repo key to the guest working-copy path.

        Order: exact recorded key, recorded repo name, an absolute guest path
        passed through, else ``<workspace>/<basename>``.
        """
        meta = self._load_meta(vm_ref)
        repos = meta.repos if meta is not None else {}
        if repo_key in repos:
            return repos[repo_key]["guest_path"]
        name = repo_name_from(repo_key)
        if name in repos:
            return repos[name]["guest_path"]
        if repo_key.startswith("/"):
            return repo_key
        return f"{self.workspace_dir}/{name}"

    def prepare_repo(self, vm_ref: str, repo: RepoSpec) -> None:
        """Clone a credential-free repo spec into the guest workspace."""
        meta = self._require_meta(vm_ref)
        url = (repo.url or "").strip()
        if not url:
            raise ProvisionerError("prepare_repo requires a URL (guest holds no credentials)")
        if url_has_userinfo(url):
            raise ProvisionerError(
                "credential-bearing repo URL rejected: the guest must never "
                "receive credentials (URL userinfo present)"
            )
        if url.startswith("-"):
            # Client-supplied; without this it reaches guest git as an option.
            raise ProvisionerError(f"unsafe repo URL rejected (looks like an option): {url!r}")
        if repo.branch and repo.branch.startswith("-"):
            raise ProvisionerError(f"unsafe repo branch rejected: {repo.branch!r}")
        name = repo_name_from(url)
        guest_path = f"{self.workspace_dir}/{name}"
        exists = self.run(vm_ref, f"test -d {_quote(guest_path)}/.git", timeout_s=30)
        if not exists.ok:
            self.run(vm_ref, f"mkdir -p {_quote(self.workspace_dir)}", timeout_s=30, check=True)
            clone = ["git", "clone", "--no-single-branch"]
            if repo.branch:
                clone += ["-b", repo.branch]
            # ``--`` ends options so a path/URL can never be parsed as one.
            clone += ["--", url, guest_path]
            result = self.run(vm_ref, " ".join(_quote(a) for a in clone), timeout_s=600)
            if not result.ok:
                raise ProvisionerError(f"guest clone failed for {url!r}: {result.stderr.strip()}")
        meta.repos[name] = {"url": url, "branch": repo.branch, "guest_path": guest_path}
        self._save_meta(meta)

    # ------------------------------------------------------------------
    # export: fetch_bundle -> gate (scheduler) -> push
    # ------------------------------------------------------------------
    def _fetch_failure(self, export: ExportSpec, message: str, stash_count: int = 0) -> FetchResult:
        return FetchResult(ok=False, message=message, stash_count=stash_count, spec=export)

    def fetch_bundle(self, vm_ref: str, export: ExportSpec) -> FetchResult:
        """Quarantine the seat's task-branch work into host staging and verify.

        Contract: on success the result carries a non-null, verified SHA.
        Serialization into the host checkout is at ``refs/omavroom/<branch>``
        (quarantine namespace); the scheduler gate runs between this and
        :meth:`push`. Any operational failure returns ``ok=False``; the seat
        is never mutated.
        """
        meta = self._load_meta(vm_ref)
        if meta is None:
            return self._fetch_failure(export, f"unknown seat VM: {vm_ref}")
        branch = (export.branch or "").strip()
        if not branch or branch.startswith("-"):
            return self._fetch_failure(export, f"invalid export branch: {branch!r}")
        check = self.host(["git", "check-ref-format", "--branch", branch], timeout=30)
        if not check.ok:
            return self._fetch_failure(export, f"invalid branch name: {branch!r}")
        host_repo = str(Path(export.repo).expanduser())
        if not self._git(host_repo, "rev-parse", "--git-dir", timeout=30).ok:
            return self._fetch_failure(export, f"host repo is not a git repo: {host_repo}")

        guest_path = self.guest_repo_path(vm_ref, export.repo)
        status = self.run(
            vm_ref, f"cd {_quote(guest_path)} && git status --porcelain", timeout_s=60
        )
        if not status.ok:
            return self._fetch_failure(export, f"guest git status failed: {status.stderr.strip()}")
        if status.stdout.strip():
            return self._fetch_failure(
                export, "guest working tree is dirty; commit everything before export"
            )
        stash = self.run(vm_ref, f"cd {_quote(guest_path)} && git stash list | wc -l", timeout_s=60)
        try:
            stash_count = int(stash.stdout.strip() or "0")
        except ValueError:
            stash_count = 0

        base_result = self.run(
            vm_ref,
            f"cd {_quote(guest_path)} && "
            "(git rev-parse --verify --quiet refs/heads/main >/dev/null && echo main || "
            "(git rev-parse --verify --quiet refs/heads/master >/dev/null && echo master || "
            "(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null "
            "| sed 's#^refs/remotes/origin/##')))",
            timeout_s=60,
        )
        base = base_result.stdout.strip()
        if base and not is_safe_ref_token(base):
            # Guest-controlled; it is interpolated into a guest git command.
            return self._fetch_failure(
                export, f"invalid base ref from guest: {base!r}", stash_count=stash_count
            )
        rev = self.run(
            vm_ref, f"cd {_quote(guest_path)} && git rev-parse {_quote(branch)}", timeout_s=60
        )
        if not rev.ok:
            return self._fetch_failure(
                export, f"guest has no branch {branch!r}", stash_count=stash_count
            )
        guest_sha = rev.stdout.strip()
        if not guest_sha:
            return self._fetch_failure(export, "empty guest SHA", stash_count=stash_count)
        if not is_safe_sha(guest_sha):
            # Never let a guest-supplied "SHA" reach a host git argv.
            return self._fetch_failure(
                export, f"invalid guest SHA from seat: {guest_sha!r}", stash_count=stash_count
            )

        merge_base = None
        if base and base != branch:
            mb = self.run(
                vm_ref,
                f"cd {_quote(guest_path)} && git merge-base {_quote(base)} {_quote(branch)}",
                timeout_s=60,
            )
            if not mb.ok:
                return self._fetch_failure(
                    export, f"no merge-base with {base!r}", stash_count=stash_count
                )
            merge_base = mb.stdout.strip()
            if not is_safe_sha(merge_base):
                return self._fetch_failure(
                    export, f"invalid merge-base from seat: {merge_base!r}", stash_count=stash_count
                )
            if merge_base == guest_sha:
                return self._fetch_failure(
                    export,
                    f"nothing to export: {branch!r} has no commits beyond {base!r}",
                    stash_count=stash_count,
                )
        bundle_range = f"{merge_base}..{branch}" if merge_base else branch
        guest_bundle = f"/tmp/omavroom-{safe_branch_name(branch)}.bundle"
        create = self.run(
            vm_ref,
            f"cd {_quote(guest_path)} && rm -f {_quote(guest_bundle)} && "
            f"git bundle create {_quote(guest_bundle)} {_quote(bundle_range)}",
            timeout_s=600,
        )
        if not create.ok:
            return self._fetch_failure(
                export, f"guest bundle create failed: {create.stderr.strip()}", stash_count
            )
        if not self.run(vm_ref, f"test -s {_quote(guest_bundle)}", timeout_s=30).ok:
            return self._fetch_failure(export, "guest bundle is missing or empty", stash_count)

        staging = self.staging_dir / meta.name
        staging.mkdir(parents=True, exist_ok=True)
        host_bundle = staging / f"{safe_branch_name(branch)}.bundle"
        scp = self.host(self._scp_argv(vm_ref, guest_bundle, str(host_bundle)), timeout=300)
        if not scp.ok or not host_bundle.exists():
            return self._fetch_failure(
                export, f"bundle copy failed: {scp.stderr.strip()}", stash_count
            )
        size = host_bundle.stat().st_size
        if size > self.max_bundle_bytes:
            return self._fetch_failure(
                export,
                f"bundle too large: {size} > {self.max_bundle_bytes} bytes",
                stash_count,
            )

        verify = self._git(host_repo, "bundle", "verify", "--", str(host_bundle), timeout=120)
        if not verify.ok:
            return self._fetch_failure(
                export, f"git bundle verify failed: {verify.stderr.strip()}", stash_count
            )
        omref = f"refs/omavroom/{branch}"
        fetch = self._git(
            host_repo, "fetch", "--", str(host_bundle), f"{branch}:{omref}", timeout=300
        )
        if not fetch.ok:
            return self._fetch_failure(
                export, f"quarantine fetch failed: {fetch.stderr.strip()}", stash_count
            )
        fetched_sha = self._git(host_repo, "rev-parse", "--verify", omref, timeout=30)
        sha = fetched_sha.stdout.strip()
        if not is_safe_sha(sha):
            return self._fetch_failure(
                export, f"invalid fetched SHA: {sha or '<none>'}", stash_count
            )
        if sha != guest_sha:
            return self._fetch_failure(
                export, f"fetched SHA {sha or '<none>'} != guest SHA {guest_sha}", stash_count
            )
        fsck = self._git(
            host_repo, "fsck", "--no-progress", "--no-dangling", timeout=self.fsck_timeout_s
        )
        if not fsck.ok:
            return self._fetch_failure(
                export,
                f"git fsck failed: {fsck.stderr.strip() or fsck.stdout.strip()}",
                stash_count,
            )
        if merge_base:
            # ``--end-of-options`` (not ``--``, which would make git treat the
            # revisions as paths) guards the revision operands; the SHAs are
            # already validated as hex.
            count = self._git(
                host_repo,
                "rev-list",
                "--count",
                "--end-of-options",
                f"{merge_base}..{sha}",
                timeout=60,
            )
            try:
                commits = int(count.stdout.strip() or "0")
            except ValueError:
                commits = 0
            if commits > self.max_export_commits:
                return self._fetch_failure(
                    export,
                    f"too many commits: {commits} > {self.max_export_commits}",
                    stash_count,
                )
        diffstat = self._git(
            host_repo,
            "diff",
            "--numstat",
            "--end-of-options",
            merge_base or f"{sha}^",
            sha,
            timeout=60,
        )
        if not diffstat.ok:
            return self._fetch_failure(
                export,
                "diffstat computation failed: "
                f"{diffstat.stderr.strip() or diffstat.stdout.strip() or 'git diff failed'} "
                f"(base={merge_base or f'{sha}^'})",
                stash_count=stash_count,
            )
        files_changed = insertions = deletions = 0
        changed_paths: list[str] = []
        for line in diffstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files_changed += 1
            changed_paths.append(parts[2])
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
        message = (
            f"fetched {branch}@{guest_sha} ({files_changed} files, "
            f"+{insertions}/-{deletions}, stash={stash_count})"
        )
        return FetchResult(
            ok=True,
            bundle_path=str(host_bundle),
            sha=guest_sha,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
            changed_paths=tuple(changed_paths),
            stash_count=stash_count,
            message=message,
            spec=export,
        )

    @staticmethod
    def _reject_option_like(value: str, what: str) -> None:
        """Reject an empty/option-like/unsafe argv token (git argv injection)."""
        if not value:
            raise ProvisionerError(f"invalid {what}: empty")
        if value.startswith("-"):
            raise ProvisionerError(f"unsafe {what} rejected (looks like an option): {value!r}")
        if _CTRL_OR_SPACE_RE.search(value):
            raise ProvisionerError(
                f"unsafe {what} rejected (whitespace/control characters): {value!r}"
            )

    @staticmethod
    def _is_explicit_remote(remote: str) -> bool:
        """A path, URL, or ``origin`` is a legitimate explicit destination."""
        return (
            remote == "origin"
            or remote.startswith(("/", "./", "../", "~"))
            or bool(_REMOTE_SCHEME_RE.match(remote))
        )

    @staticmethod
    def _validate_branch_name(branch: str) -> None:
        if not branch:
            raise ProvisionerError("export.branch is required to push")
        if (
            branch.startswith("-")
            or _CTRL_OR_SPACE_RE.search(branch)
            or ".." in branch
            or "@{" in branch
            or branch.endswith(("/", "."))
        ):
            raise ProvisionerError(f"invalid branch name: {branch!r}")

    @staticmethod
    def _validate_ref_name(name: str) -> None:
        if not name.startswith("refs/"):
            raise ProvisionerError(f"destination must be a fully-qualified ref: {name!r}")
        if not _REFSPEC_RE.match(name):
            raise ProvisionerError(f"unsafe destination ref: {name!r}")
        if ".." in name or "@{" in name or "//" in name or name.endswith(("/", ".")):
            raise ProvisionerError(f"invalid destination ref: {name!r}")

    def _check_remote_allowed(self, host_repo: str, remote: str) -> None:
        """Restrict pushes to an allowlist, known remotes, or explicit paths/URLs.

        ``origin`` is always allowed: it is git's default push remote, and an
        allowlist that silently forbade it would break the common case. An
        allowlist therefore *adds* remotes; it does not remove ``origin``.
        """
        allow = tuple(self.config.export.allowed_remotes or ())
        if allow:
            if remote == "origin" or remote in allow:
                return
            raise ProvisionerError(
                f"remote {remote!r} is not 'origin' or in the configured "
                f"export.allowed_remotes allowlist {allow}"
            )
        if self._is_explicit_remote(remote):
            return
        known = self._git(host_repo, "remote", timeout=30)
        names = set(known.stdout.split()) if known.ok else set()
        if remote not in names:
            raise ProvisionerError(
                f"remote {remote!r} is not 'origin', a known remote, or an explicit path/URL"
            )

    def _push_destination(self, export: ExportSpec) -> tuple[str, str]:
        """Resolve ``export.ref`` to ``(remote, fully-qualified ref)`` safely.

        Every component is validated before it can reach a ``git`` argv: the
        branch and ref must be well-formed, and no component may start with
        ``-`` (which git would parse as an option, e.g. ``--upload-pack``).
        """
        branch = (export.branch or "").strip()
        self._validate_branch_name(branch)
        ref = (export.ref or "").strip()
        if ref and ":" in ref:
            remote, destination = ref.split(":", 1)
        elif ref:
            remote, destination = ref, f"refs/heads/{branch}"
        else:
            remote, destination = "origin", f"refs/heads/{branch}"
        remote = remote.strip()
        destination = destination.strip()
        self._reject_option_like(remote, "push remote")
        self._reject_option_like(destination, "push destination")
        if not destination.startswith("refs/"):
            destination = f"refs/heads/{destination}"
        self._validate_ref_name(destination)
        return remote, destination

    def push(self, export: ExportSpec, fetched: FetchResult) -> PushResult:
        """Push a verified bundle host-side, idempotently, verifying remote SHA.

        Security: the remote and destination are validated (no option-like
        tokens, fully-qualified ref, known/allowlisted remote), the fetched SHA
        must be a real object id, and ``git check-ref-format`` is the
        authoritative ref check. ``--`` end-of-options is used on the
        ``ls-remote``/``push`` invocations.
        """
        if not fetched.ok or not fetched.sha:
            return PushResult(ok=False, message="refusing to push an unverified bundle")
        if not _SAFE_SHA_RE.match(fetched.sha or ""):
            return PushResult(ok=False, message=f"invalid fetched SHA: {fetched.sha!r}")
        host_repo = str(Path(export.repo).expanduser())
        if not self._git(host_repo, "rev-parse", "--git-dir", timeout=30).ok:
            return PushResult(ok=False, message=f"host repo is not a git repo: {host_repo}")
        try:
            remote, destination = self._push_destination(export)
            self._check_remote_allowed(host_repo, remote)
        except ProvisionerError as exc:
            return PushResult(ok=False, message=str(exc))
        branch = (export.branch or "").strip()
        if not self.host(["git", "check-ref-format", "--branch", branch], timeout=30).ok:
            return PushResult(ok=False, message=f"invalid branch name: {branch!r}")
        if not self.host(["git", "check-ref-format", destination], timeout=30).ok:
            return PushResult(ok=False, message=f"invalid destination ref: {destination!r}")
        existing = self._git(host_repo, "ls-remote", "--", remote, destination, timeout=60)
        if existing.ok and existing.stdout.split() and existing.stdout.split()[0] == fetched.sha:
            return PushResult(ok=True, sha=fetched.sha, message="remote already at SHA")
        result = self._git(
            host_repo, "push", "--", remote, f"{fetched.sha}:{destination}", timeout=600
        )
        if not result.ok:
            return PushResult(
                ok=False,
                message=f"git push failed: {result.stderr.strip() or result.stdout.strip()}",
            )
        remote_check = self._git(host_repo, "ls-remote", "--", remote, destination, timeout=60)
        remote_sha = remote_check.stdout.split()[0] if remote_check.stdout.split() else None
        if remote_sha != fetched.sha:
            return PushResult(
                ok=False,
                sha=remote_sha,
                message=f"remote SHA {remote_sha} != pushed {fetched.sha}",
            )
        return PushResult(ok=True, sha=fetched.sha, message="pushed and verified")

    # ------------------------------------------------------------------
    # discovery / adopt
    # ------------------------------------------------------------------
    def _domain_names(self) -> list[str]:
        result = self._virsh("list", "--all", "--name", timeout=30)
        if not result.ok:
            raise ProvisionerError(f"virsh list failed: {result.stderr.strip()}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _vm_info(self, domain: str) -> VmInfo:
        meta = self._load_meta(domain)
        name = (
            domain[len(SEAT_DOMAIN_PREFIX) :] if domain.startswith(SEAT_DOMAIN_PREFIX) else domain
        )
        state = self._domstate(domain)
        if state == "shut off":
            state = "shut off"
        return VmInfo(
            ref=domain,
            name=name,
            seat_type=meta.seat_type if meta else None,
            image=meta.image if meta else None,
            state=state,
        )

    def list_vms(self) -> list[VmInfo]:
        """All **managed (seat)** domains in any state; templates excluded.

        Only ``omavroom-seat-*`` domains are returned. The
        ``omavroom-base``/``omavroom-term`` template domains are deliberately
        excluded so the reconciler cannot classify them as orphans and
        destroy them.
        """
        return [
            self._vm_info(domain)
            for domain in self._domain_names()
            if domain.startswith(SEAT_DOMAIN_PREFIX)
        ]

    def attach(self, vm_ref: str) -> VmInfo | None:
        if not self._exists(vm_ref):
            return None
        return self._vm_info(vm_ref)

    # ------------------------------------------------------------------
    # desktop ops
    # ------------------------------------------------------------------
    def screenshot(
        self, vm_ref: str, *, max_width: int | None = None, max_bytes: int | None = None
    ) -> bytes:
        """Grab the guest framebuffer as PNG bytes, bounded by width and bytes.

        If the image needs conversion/downscaling and ImageMagick is
        unavailable, or the encoded PNG cannot be brought under ``max_bytes``,
        this raises :class:`ProvisionerError` rather than returning an
        oversized/unresized image.
        """
        if not self._exists(vm_ref):
            raise ProvisionerError(f"unknown seat VM: {vm_ref}")
        seat_dir = self._seat_dir(vm_ref)
        seat_dir.mkdir(parents=True, exist_ok=True)
        raw = seat_dir / "screenshot.raw"
        raw.unlink(missing_ok=True)
        result = self._virsh("screenshot", vm_ref, "--file", str(raw), "--screen", "0", timeout=60)
        if not result.ok or not raw.exists():
            raise ProvisionerError(
                f"screenshot failed for {vm_ref} (desktop seat with graphics required): "
                f"{result.stderr.strip()}"
            )
        data = raw.read_bytes()
        needs_convert = not data.startswith(_PNG_MAGIC)
        needs_resize = False
        if not needs_convert and max_width is not None:
            try:
                width, _ = png_dimensions(data)
                needs_resize = width > max_width
            except ValueError:
                needs_resize = False
        over_bytes = max_bytes is not None and len(data) > max_bytes
        if not (needs_convert or needs_resize or over_bytes):
            raw.unlink(missing_ok=True)
            return data
        if not self.magick_bin:
            raw.unlink(missing_ok=True)
            if needs_convert:
                raise ProvisionerError(
                    "framebuffer is not PNG and ImageMagick is unavailable for conversion"
                )
            if needs_resize:
                raise ProvisionerError(
                    "screenshot needs downscaling but ImageMagick is unavailable"
                )
            raise ProvisionerError(
                f"screenshot is {len(data)} bytes > max_bytes {max_bytes} and "
                "ImageMagick is unavailable"
            )
        # ImageMagick keys off the suffix; libvirt's screenshot is PPM (P6).
        src = seat_dir / ("screenshot.ppm" if needs_convert else "screenshot.src.png")
        src.write_bytes(data)
        raw.unlink(missing_ok=True)
        png = seat_dir / "screenshot.png"
        width = max_width
        encoded: bytes | None = None
        for _ in range(6):
            png.unlink(missing_ok=True)
            argv = [self.magick_bin, str(src)]
            if width is not None:
                argv += ["-resize", f"{width}x"]
            argv.append(str(png))
            converted = self.host(argv, timeout=120)
            if not converted.ok or not png.exists():
                src.unlink(missing_ok=True)
                raise ProvisionerError(f"image conversion failed: {converted.stderr.strip()}")
            encoded = png.read_bytes()
            if max_bytes is None or len(encoded) <= max_bytes:
                break
            if width is None:
                # Unknown source width; shrink from the encoded PNG's own size.
                try:
                    width, _ = png_dimensions(encoded)
                except ValueError:
                    width = 1280
            if width <= 64:
                break
            width = max(64, int(width * 0.75))
        src.unlink(missing_ok=True)
        assert encoded is not None
        if max_bytes is not None and len(encoded) > max_bytes:
            raise ProvisionerError(
                f"screenshot is {len(encoded)} bytes > max_bytes {max_bytes} after downscaling"
            )
        return encoded

    def _desktop_env(self) -> dict[str, str]:
        return {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-1",
        }

    def input(self, vm_ref: str, events: list[InputEvent]) -> None:
        """Inject key/text/click events inside the guest (desktop seats).

        Runs ``wtype``/``ydotool`` over SSH with the guest's Wayland session
        environment. Click values are ``x,y[,button]`` with button 1/2/3
        mapping to ydotool's BTN_LEFT/BTN_RIGHT/BTN_MIDDLE.
        """
        meta = self._require_meta(vm_ref)
        if meta.seat_type != "desktop":
            raise ProvisionerError(f"input is only supported on desktop seats: {vm_ref}")
        commands: list[str] = []
        for event in events:
            if event.kind == "key":
                commands.append(f"wtype -k {_quote(event.value)}")
            elif event.kind == "text":
                commands.append(f"wtype -- {_quote(event.value)}")
            else:
                commands.append(self._click_command(event.value))
        if not commands:
            return
        # The Wayland env must apply to EVERY command, not just the first in an
        # "&&" chain (``env A=1 cmd1 && cmd2`` leaves cmd2 without it), so
        # export it once for the whole remote shell.
        env_exports = " ".join(
            f"{key}={_quote(value)}" for key, value in self._desktop_env().items()
        )
        joined = f"export {env_exports}; " + " && ".join(commands)
        result = self.run(vm_ref, joined, timeout_s=60)
        if not result.ok:
            raise ProvisionerError(f"input injection failed on {vm_ref}: {result.stderr.strip()}")

    #: Allowed click buttons -> ydotool BTN_* key codes.
    _CLICK_BUTTONS: dict[str, str] = {"1": "272", "2": "274", "3": "273"}

    @staticmethod
    def _click_command(value: str) -> str:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ProvisionerError(
                f"click value must be 'x,y[,button]' with non-negative integers, got {value!r}"
            )
        x, y = parts[0], parts[1]
        button = parts[2] if len(parts) > 2 and parts[2] else "1"
        if button not in LibvirtProvisioner._CLICK_BUTTONS:
            # Never interpolate an unvalidated client string into the guest shell.
            raise ProvisionerError(
                f"click button must be one of {sorted(LibvirtProvisioner._CLICK_BUTTONS)}, "
                f"got {button!r}"
            )
        code = LibvirtProvisioner._CLICK_BUTTONS[button]
        return (
            f"ydotool mousemove --absolute {x} {y} || ydotool mousemove {x} {y}; "
            f"ydotool click {code}"
        )

    def peek_endpoint(self, vm_ref: str) -> str:
        """Return the on-demand VNC viewer endpoint (never auto-opened)."""
        result = self._virsh("domdisplay", vm_ref, timeout=30)
        endpoint = result.stdout.strip()
        if not result.ok or not endpoint:
            raise ProvisionerError(f"no display endpoint for {vm_ref}: {result.stderr.strip()}")
        return endpoint


__all__ = [
    "CommandResult",
    "LibvirtProvisioner",
    "build_domain_xml",
    "is_safe_ref_token",
    "is_safe_sha",
    "load_template",
    "png_dimensions",
    "repo_name_from",
    "safe_branch_name",
    "url_has_userinfo",
]

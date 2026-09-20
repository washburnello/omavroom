"""Provisioner interface + an in-process fake (Phase 4, VM-free).

The scheduler never talks to libvirt directly; it drives a
:class:`Provisioner`. Phase 4B will add the real ``LibvirtProvisioner``
(this work package deliberately ships none), and Phase 5's daemon wires the
chosen implementation in. Everything here is synchronous and blocking:
callers that must not block (the manager daemon) run these calls on a
background worker thread.

Export is split into two host-side steps — ``fetch_bundle`` (quarantine a
``git bundle`` to host staging, verify it, fsck, size/pack caps, and prove
``git stash list`` is empty) and ``push`` (apply/push with the host's own
credentials, verify the pushed SHA). The scheduler inserts the
:class:`~omavroom.manager.scheduler.ExportGate` between them, so a
destructive or protected-path export is held, never pushed.

The VM never holds repository credentials: ``RepoSpec`` carries only the
URL/branch to clone, and push authentication stays on the host.
"""

from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass


class ProvisionerError(RuntimeError):
    """Raised by a provisioner when a seat operation fails."""


@dataclass(frozen=True)
class CommandResult:
    """Result of one guest command run through a provisioner.

    ``returncode`` follows the shell convention: ``0`` on success, ``124``
    for a timeout enforced by the transport, ``127`` when the transport
    executable is missing, and (for a killed exec) ``-<signal>``.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ResourceCaps:
    """Concrete host resources to apply to one seat VM."""

    cpu_vcpus: int
    memory_mb: int
    overlay_max_gb: int


@dataclass(frozen=True)
class RepoSpec:
    """Repository to inject into a seat at prepare time.

    Deliberately carries no credential material (locked decision: the guest
    holds zero GitHub credentials; export/push happen host-side).
    """

    url: str | None = None
    branch: str | None = None


@dataclass(frozen=True)
class ExportSpec:
    """Work-export request handed to the provisioner on release."""

    repo: str
    branch: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Outcome of the quarantine/fetch step; ``ok=False`` means no push.

    Contract: when ``ok`` is True, ``sha`` must be a non-null, verified
    commit SHA — the push step verifies the remote against it and the
    scheduler refuses to push an ``ok`` fetch with a null SHA. ``spec``
    records the destination the bundle was fetched for, so the later
    host-side push step is explicit about where work is going.
    """

    ok: bool
    bundle_path: str | None = None
    sha: str | None = None
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    changed_paths: tuple[str, ...] = ()
    stash_count: int = 0
    message: str = ""
    spec: ExportSpec | None = None


@dataclass(frozen=True)
class PushResult:
    """Outcome of the host-side push step."""

    ok: bool
    sha: str | None = None
    message: str = ""


@dataclass(frozen=True)
class GateDecision:
    """Content-gate verdict; ``allowed=False`` holds the seat."""

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class ExportOutcome:
    """Combined fetch -> gate -> push result for one export."""

    fetched: FetchResult | None = None
    gate: GateDecision | None = None
    pushed: PushResult | None = None

    @property
    def ok(self) -> bool:
        return bool(
            self.fetched
            and self.fetched.ok
            and self.gate
            and self.gate.allowed
            and self.pushed
            and self.pushed.ok
        )

    @property
    def message(self) -> str:
        if self.fetched is not None and not self.fetched.ok:
            return self.fetched.message
        if self.gate is not None and not self.gate.allowed:
            return self.gate.reason
        if self.pushed is not None:
            return self.pushed.message
        return ""


@dataclass(frozen=True)
class InputEvent:
    """One guest input event for desktop seats (Phase 5 ``input`` tool).

    ``kind`` is ``"key"`` (single keysym or a ``Mod+...+Key`` combo),
    ``"text"`` (bulk text), ``"type"`` (per-character typing driven by
    ``wtype -d`` so incremental rendering is exercised), or ``"click"``.
    ``delay_ms`` is only meaningful for ``"type"`` and is the per-character
    delay handed to ``wtype``; ``None`` leaves ``wtype``'s own default.
    """

    kind: str
    value: str
    delay_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("key", "text", "type", "click"):
            raise ValueError(f"unknown input event kind: {self.kind!r}")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("input event value must be a non-empty string")
        if self.delay_ms is not None:
            if isinstance(self.delay_ms, bool) or not isinstance(self.delay_ms, int):
                raise ValueError("input event delay_ms must be an integer or None")
            if self.delay_ms < 0:
                raise ValueError("input event delay_ms must be >= 0")


@dataclass(frozen=True)
class VmInfo:
    """Provisioner-visible VM, used by ``Manager.reconcile``."""

    ref: str
    name: str
    seat_type: str | None = None
    image: str | None = None
    state: str = "unknown"


class Provisioner(ABC):
    """Control-plane operations the scheduler needs for one seat VM.

    **Autostart is never enabled.** Only the manager may start a seat, so a
    host reboot can never resurrect a 4 GiB guest outside manager authority.
    Implementations must keep autostart disabled on both the golden/base
    domains and every per-seat domain; the Phase 4B libvirt provisioner must
    assert this (e.g. ``virsh dominfo <dom>`` reports ``Autostart: disable``)
    before creating or starting anything.
    """

    @abstractmethod
    def create_from_image(
        self, vm_name: str, seat_type: str, image: str, resources: ResourceCaps
    ) -> str:
        """Create a seat VM (overlay on the golden image); return a VM ref."""

    @abstractmethod
    def apply_resource_limits(self, vm_ref: str, resources: ResourceCaps) -> None:
        """Apply mandatory CPU/RAM caps and the overlay disk quota."""

    @abstractmethod
    def start(self, vm_ref: str) -> None:
        """Boot the seat VM with no host-visible display."""

    @abstractmethod
    def wait_ready(self, vm_ref: str, timeout_s: int) -> None:
        """Block until the seat is SSH-reachable / ready, or raise."""

    @abstractmethod
    def reset(self, vm_ref: str) -> None:
        """Revert the seat overlay to the golden image without releasing."""

    @abstractmethod
    def prepare_repo(self, vm_ref: str, repo: RepoSpec) -> None:
        """Inject/clone the requested repository into the seat."""

    @abstractmethod
    def fetch_bundle(self, vm_ref: str, export: ExportSpec) -> FetchResult:
        """Quarantine the seat's work into host staging and verify it.

        Contract: on success (``ok=True``) the result must carry a non-null,
        verified commit ``sha``; a missing SHA is treated as a failed export
        by the scheduler (held, never pushed).
        """

    @abstractmethod
    def push(self, export: ExportSpec, fetched: FetchResult) -> PushResult:
        """Push a verified, gate-approved bundle to the explicit destination.

        Contract: the destination (repo/refspec) comes from ``export`` and
        must be explicit — the provisioner never guesses it. The operation
        must be idempotent (safe to retry), and must verify the remote SHA
        after pushing, returning ``ok=False`` on any mismatch. Credentials
        are host-side only; the guest holds none.
        """

    @abstractmethod
    def stop(self, vm_ref: str) -> None:
        """Gracefully stop the seat VM."""

    @abstractmethod
    def destroy(self, vm_ref: str) -> None:
        """Destroy the seat VM and delete its overlay (teardown)."""

    # -- reattach / adopt seam (Phase 4A FIX 4) --------------------------
    @abstractmethod
    def list_vms(self) -> list[VmInfo]:
        """Every **managed (seat)** domain the provisioner knows about.

        This is all *managed* domains, not running-only: it includes
        ``defined`` and in-flight/stopped seat VMs so the reconciler can see
        reality. Template/base domains (e.g. ``omavroom-base`` /
        ``omavroom-term``) are **excluded** — the reconciler would otherwise
        destroy them as orphans. Because an in-flight VM may not be persisted
        on a seat row yet, callers must match by seat/``VmInfo.name``
        identity, never assume "not referenced by ``vm_name``" means orphan.
        """

    @abstractmethod
    def attach(self, vm_ref: str) -> VmInfo | None:
        """Look up one VM by ref, or ``None`` if unknown."""

    # -- desktop ops seam (Phase 5 screenshot / input / peek) ------------
    @abstractmethod
    def screenshot(
        self,
        vm_ref: str,
        *,
        max_width: int | None = None,
        max_bytes: int | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> bytes:
        """Grab the guest framebuffer as PNG bytes (desktop seats).

        ``max_width=None`` (or ``0``) means **native resolution, no
        downscale**; a positive width bounds the image width. ``region`` is
        an ``(x, y, w, h)`` crop applied to the native frame *before* any
        downscale. ``max_bytes`` bounds the encoded PNG size. An
        implementation that cannot satisfy ``max_bytes`` (no resizer
        available) must raise :class:`ProvisionerError` rather than return an
        oversized image.
        """

    @abstractmethod
    def input(self, vm_ref: str, events: list[InputEvent]) -> None:
        """Inject keystrokes/clicks inside the guest (desktop seats)."""

    @abstractmethod
    def peek_endpoint(self, vm_ref: str) -> str:
        """Return the on-demand viewer endpoint (never auto-opened)."""

    # -- file transfer seam (agent-facing copy_in / copy_out) ------------
    @abstractmethod
    def copy_in(self, vm_ref: str, host_path: str, guest_path: str) -> int:
        """Copy one host file into the guest over the pinned SSH key.

        The **host** side is constrained to a safe root (implementation
        defined); a path outside it must raise :class:`ProvisionerError`.
        Implementations must use an argv list, never a host shell string.
        Returns the number of bytes copied.
        """

    @abstractmethod
    def copy_out(self, vm_ref: str, guest_path: str, host_path: str) -> int:
        """Copy one guest file out to the host over the pinned SSH key.

        The **host** destination is constrained to the same safe root as
        :meth:`copy_in`; a path outside it must raise
        :class:`ProvisionerError`. Returns the number of bytes copied.
        """

    # -- desktop helpers (Omarchy / Hyprland; desktop seats only) --------
    @abstractmethod
    def launch_app(self, vm_ref: str, command: str, *, tui: bool = False) -> None:
        """Launch an application on the seat's desktop.

        ``tui=True`` runs the command in a terminal via
        ``omarchy-launch-tui``; otherwise the command is dispatched directly
        through Hyprland's Lua dispatcher.
        """

    @abstractmethod
    def list_windows(self, vm_ref: str) -> list[dict]:
        """Return the desktop's windows as structured dictionaries."""

    @abstractmethod
    def focus_window(self, vm_ref: str, match: str) -> dict:
        """Focus the window whose class/title matches ``match`` (regex).

        Returns the matched window view; raises :class:`ProvisionerError`
        when nothing matches.
        """

    @abstractmethod
    def resize_window(self, vm_ref: str, match: str, width: int, height: int) -> None:
        """Resize the matching window to ``width`` x ``height`` pixels."""

    @abstractmethod
    def move_window(self, vm_ref: str, match: str, x: int, y: int) -> None:
        """Move the matching window's top-left corner to ``(x, y)``."""

    @abstractmethod
    def float_window(self, vm_ref: str, match: str, on: bool) -> None:
        """Turn floating on/off for the matching window."""

    @abstractmethod
    def set_theme(self, vm_ref: str, name: str) -> None:
        """Apply the named Omarchy theme (``omarchy theme set``)."""

    @abstractmethod
    def clipboard_get(self, vm_ref: str) -> str:
        """Return the guest's Wayland clipboard text (``wl-paste``)."""

    @abstractmethod
    def clipboard_set(self, vm_ref: str, text: str) -> None:
        """Set the guest's Wayland clipboard text (``wl-copy``)."""

    # -- exec seam (Phase 5 ``exec_start``/``exec_poll``/``exec_kill``) --
    @abstractmethod
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
        """Run a command inside the seat and return its result.

        This is the primitive the Phase 5 exec engine drives on a dedicated
        per-exec worker thread. Implementations must honour two optional
        cooperative controls:

        - ``on_output(stream, chunk)`` called as output is produced, where
          ``stream`` is ``"stdout"`` or ``"stderr"``. When provided, callers
          assume the output was streamed and will not also record the final
          ``stdout``/``stderr`` (avoiding double-counting).
        - ``cancel`` set to request termination. The implementation should
          kill the transport (for SSH, the ``ssh`` process) and return with
          ``returncode = -<signal>`` as soon as it observes the flag. Purely
          blocking transports that cannot interrupt themselves may ignore it;
          the engine still marks the exec killed.

        ``timeout_s`` is a hard transport timeout: on expiry the command is
        terminated and ``returncode`` is ``124``.
        """

    # -- autostart invariant --------------------------------------------
    def verify_autostart_invariant(self) -> None:
        """Assert autostart is disabled on base/template and seat domains.

        The ABC documents autostart as never-enabled. Provisioners with a
        real control plane override this to actively check; the base
        implementation is a no-op for control-plane-free fakes. It is safe to
        call at daemon startup.
        """
        return None


class FakeProvisioner(Provisioner):
    """In-memory provisioner for tests: fast, inspectable, breakable.

    - ``delay_s`` / ``export_delay_s`` make operations slow, which lets
      tests observe intermediate states and exercise the lock ordering.
    - ``fail_on`` is a set of method names that should raise
      :class:`ProvisionerError`; ``fail_seats`` breaks specific seat names
      at ``wait_ready``; ``fail_exports`` makes ``fetch_bundle`` return
      ``ok=False`` (the held-seat path).
    - ``calls`` records every invocation; ``created`` / ``destroyed`` are
      convenient direct assertions.
    """

    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        export_delay_s: float = 0.0,
        fail_on: set[str] | None = None,
        fail_seats: set[str] | None = None,
        fail_exports: bool = False,
        sha_mismatch: bool = False,
        raise_on: dict[str, BaseException] | None = None,
        run_chunks: list[tuple[float, str, str]] | None = None,
        run_hang_s: float = 0.0,
        run_exit_code: int = 0,
    ) -> None:
        self.delay_s = delay_s
        self.export_delay_s = export_delay_s
        self.fail_on = set(fail_on or ())
        self.fail_seats = set(fail_seats or ())
        self.fail_exports = fail_exports
        self.sha_mismatch = sha_mismatch
        self.raise_on: dict[str, BaseException] = dict(raise_on or {})
        # Exec simulation knobs (Phase 5). ``run_chunks`` is a list of
        # ``(delay_s, stream, text)`` tuples emitted in order; after them the
        # fake idles for ``run_hang_s`` (cancellable), then exits with
        # ``run_exit_code``. Both are interruptible via the ``cancel`` event.
        self.run_chunks: list[tuple[float, str, str]] = list(run_chunks or ())
        self.run_hang_s = run_hang_s
        self.run_exit_code = run_exit_code
        self.runs: list[tuple[str, str]] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.vms: dict[str, dict] = {}
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.resets: list[str] = []
        self.applied_limits: dict[str, ResourceCaps] = {}
        self.prepared_repos: dict[str, RepoSpec] = {}
        self.exported: list[str] = []
        self.inputs: dict[str, list[InputEvent]] = {}
        self.screenshots: list[str] = []
        self.screenshots_meta: list[dict] = []
        self.peeks: list[str] = []
        # Agent-facing file transfer + desktop helper recordings.
        self.copies_in: list[tuple[str, str, str]] = []
        self.copies_out: list[tuple[str, str, str]] = []
        self.launched: list[tuple[str, str, bool]] = []
        self.window_ops: list[tuple[str, str, tuple]] = []
        self.themes: list[tuple[str, str]] = []
        self.clipboard_text = ""
        self.windows: list[dict] = [
            {
                "address": "0x1",
                "class": "foot",
                "title": "foot",
                "initial_class": "foot",
                "initial_title": "foot",
                "workspace": "1",
                "floating": False,
                "size": [800, 600],
                "at": [0, 0],
                "focus_history_id": 0,
            }
        ]
        # Diffstat a fake fetch reports (tests override these).
        self.export_files_changed: int | None = None
        self.export_insertions = 10
        self.export_deletions = 2
        self.export_paths: tuple[str, ...] = ("src/app.py",)
        self.export_stash_count = 0
        self.sha_prefix: str | None = "a" * 40
        self._active_exports = 0
        self.max_active_exports = 0
        self._lock = threading.Lock()

    # -- introspection helpers -------------------------------------------
    def _record(self, method: str, *args, **kwargs) -> None:
        with self._lock:
            self.calls.append((method, args, kwargs))

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise ProvisionerError(f"fake provisioner: {method} failed")
        if method in self.raise_on:
            raise self.raise_on[method]

    def _sleep(self, seconds: float) -> None:
        if seconds:
            time.sleep(seconds)

    def _vm(self, vm_ref: str) -> dict:
        with self._lock:
            vm = self.vms.get(vm_ref)
        if vm is None:
            raise ProvisionerError(f"fake provisioner: unknown VM {vm_ref!r}")
        return vm

    def break_seat(self, vm_name: str) -> None:
        """Mark a seat name as broken (fails at ``wait_ready``)."""
        self.fail_seats.add(vm_name)

    def call_names(self) -> list[str]:
        """Ordered list of provisioner method names called."""
        with self._lock:
            return [name for name, _, _ in self.calls]

    def inject_vm(
        self,
        name: str,
        *,
        seat_type: str = "terminal",
        image: str = "omavroom-base",
        state: str = "running",
    ) -> str:
        """Test helper: register an orphan VM directly with the provisioner."""
        vm_ref = f"fake://{name}"
        with self._lock:
            self.vms[vm_ref] = {
                "name": name,
                "seat_type": seat_type,
                "image": image,
                "state": state,
            }
            self.created.append(vm_ref)
        return vm_ref

    def drop_vm(self, vm_ref: str) -> None:
        """Test helper: forget a VM without a destroy (dangling seat ref)."""
        with self._lock:
            self.vms.pop(vm_ref, None)

    def set_vm_state(self, vm_ref: str, state: str) -> None:
        """Test helper: force a VM's reported state (running/stopped/...)."""
        with self._lock:
            if vm_ref not in self.vms:
                raise KeyError(vm_ref)
            self.vms[vm_ref]["state"] = state

    # -- Provisioner ------------------------------------------------------
    def create_from_image(
        self, vm_name: str, seat_type: str, image: str, resources: ResourceCaps
    ) -> str:
        self._record("create_from_image", vm_name, seat_type, image, resources)
        self._maybe_fail("create_from_image")
        self._sleep(self.delay_s)
        vm_ref = f"fake://{vm_name}"
        with self._lock:
            self.vms[vm_ref] = {
                "name": vm_name,
                "seat_type": seat_type,
                "image": image,
                "state": "defined",
            }
            self.created.append(vm_ref)
        return vm_ref

    def apply_resource_limits(self, vm_ref: str, resources: ResourceCaps) -> None:
        self._record("apply_resource_limits", vm_ref, resources)
        self._maybe_fail("apply_resource_limits")
        with self._lock:
            self.applied_limits[vm_ref] = resources

    def start(self, vm_ref: str) -> None:
        self._record("start", vm_ref)
        self._maybe_fail("start")
        self._sleep(self.delay_s)
        with self._lock:
            if vm_ref in self.vms:
                self.vms[vm_ref]["state"] = "running"

    def wait_ready(self, vm_ref: str, timeout_s: int) -> None:
        self._record("wait_ready", vm_ref, timeout_s)
        self._maybe_fail("wait_ready")
        self._sleep(self.delay_s)
        vm = self._vm(vm_ref)
        if vm["name"] in self.fail_seats:
            raise ProvisionerError(f"fake provisioner: seat {vm['name']} never became ready")

    def _sleep_cancellable(
        self,
        seconds: float,
        cancel: threading.Event | None,
        deadline: float | None,
    ) -> int:
        """Sleep in small slices; return 0 done, -9 cancelled, 124 timed out."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if cancel is not None and cancel.is_set():
                return -9
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return 124
            remaining = end - now
            if remaining <= 0:
                return 0
            nap = min(0.02, remaining)
            if deadline is not None:
                nap = min(nap, deadline - now)
            if nap > 0:
                time.sleep(nap)

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
        """Simulate a guest command, emitting and/or hanging as configured.

        Streaming, cancellation and timeout are all cooperative, mirroring
        the real transport contract (``on_output``/``cancel``/``timeout_s``)
        so the exec engine can be exercised without a VM.
        """
        self._record("run", vm_ref, command)
        self._maybe_fail("run")
        self._vm(vm_ref)
        with self._lock:
            self.runs.append((vm_ref, command))
        deadline = time.monotonic() + timeout_s if timeout_s else None
        out: list[str] = []
        err: list[str] = []
        timeout_note = f"\n(command timed out after {timeout_s}s)\n"

        def emit(stream: str, text: str) -> None:
            (out if stream == "stdout" else err).append(text)
            if on_output is not None:
                try:
                    on_output(stream, text)
                except Exception:  # noqa: BLE001 - mirror a best-effort transport
                    pass

        status = self._sleep_cancellable(self.delay_s, cancel, deadline)
        if status == -9:
            return CommandResult(-9, "".join(out), "".join(err))
        if status == 124:
            err.append(timeout_note)
            return CommandResult(124, "".join(out), "".join(err))
        for delay, stream, text in self.run_chunks:
            if stream not in ("stdout", "stderr"):
                raise ValueError(f"fake provisioner: bad run stream {stream!r}")
            status = self._sleep_cancellable(delay, cancel, deadline)
            if status == -9:
                return CommandResult(-9, "".join(out), "".join(err))
            if status == 124:
                err.append(timeout_note)
                return CommandResult(124, "".join(out), "".join(err))
            emit(stream, text)
        status = self._sleep_cancellable(self.run_hang_s, cancel, deadline)
        if status == -9:
            return CommandResult(-9, "".join(out), "".join(err))
        if status == 124:
            err.append(timeout_note)
            return CommandResult(124, "".join(out), "".join(err))
        if check and self.run_exit_code != 0:
            raise ProvisionerError(
                f"fake provisioner: guest command failed ({self.run_exit_code}): {command}"
            )
        return CommandResult(self.run_exit_code, "".join(out), "".join(err))

    def reset(self, vm_ref: str) -> None:
        self._record("reset", vm_ref)
        self._maybe_fail("reset")
        self._sleep(self.delay_s)
        self._vm(vm_ref)
        with self._lock:
            self.resets.append(vm_ref)

    def prepare_repo(self, vm_ref: str, repo: RepoSpec) -> None:
        self._record("prepare_repo", vm_ref, repo)
        self._maybe_fail("prepare_repo")
        with self._lock:
            self.prepared_repos[vm_ref] = repo

    def fetch_bundle(self, vm_ref: str, export: ExportSpec) -> FetchResult:
        with self._lock:
            self._active_exports += 1
            self.max_active_exports = max(self.max_active_exports, self._active_exports)
        self._record("fetch_bundle", vm_ref, export)
        try:
            self._sleep(self.export_delay_s)
            self._maybe_fail("fetch_bundle")
            if self.fail_exports:
                return FetchResult(ok=False, message="simulated export fetch failure", spec=export)
            with self._lock:
                present = vm_ref in self.vms
            if not present:
                # Mirror the real provisioner: a vanished VM cannot be
                # exported. Without this the fake would silently "succeed" on
                # a gone VM and mask the release-after-teardown recovery bug.
                return FetchResult(
                    ok=False,
                    message=f"fake provisioner: unknown VM {vm_ref!r}",
                    spec=export,
                )
            with self._lock:
                self.exported.append(vm_ref)
            name = vm_ref.rsplit("/", 1)[-1]
            changed = (
                len(self.export_paths)
                if self.export_files_changed is None
                else self.export_files_changed
            )
            return FetchResult(
                ok=True,
                bundle_path=f"/tmp/{name}.bundle",
                sha=self.sha_prefix,
                files_changed=changed,
                insertions=self.export_insertions,
                deletions=self.export_deletions,
                changed_paths=tuple(self.export_paths),
                stash_count=self.export_stash_count,
                spec=export,
            )
        finally:
            with self._lock:
                self._active_exports -= 1

    def push(self, export: ExportSpec, fetched: FetchResult) -> PushResult:
        self._record("push", export, fetched)
        self._maybe_fail("push")
        if not fetched.ok:
            return PushResult(ok=False, message="cannot push an unverified bundle")
        if self.sha_mismatch:
            return PushResult(ok=False, message="pushed SHA mismatch")
        return PushResult(ok=True, sha=fetched.sha, message="pushed")

    def stop(self, vm_ref: str) -> None:
        self._record("stop", vm_ref)
        self._maybe_fail("stop")
        self._sleep(self.delay_s)
        with self._lock:
            if vm_ref in self.vms:
                self.vms[vm_ref]["state"] = "stopped"

    def destroy(self, vm_ref: str) -> None:
        self._record("destroy", vm_ref)
        self._maybe_fail("destroy")
        with self._lock:
            if vm_ref in self.vms:
                self.destroyed.append(vm_ref)
                self.vms.pop(vm_ref, None)

    def list_vms(self) -> list[VmInfo]:
        self._record("list_vms")
        with self._lock:
            items = list(self.vms.items())
        return [
            VmInfo(
                ref=ref,
                name=vm["name"],
                seat_type=vm.get("seat_type"),
                image=vm.get("image"),
                state=vm.get("state", "unknown"),
            )
            for ref, vm in items
        ]

    def attach(self, vm_ref: str) -> VmInfo | None:
        with self._lock:
            vm = self.vms.get(vm_ref)
        if vm is None:
            return None
        return VmInfo(
            ref=vm_ref,
            name=vm["name"],
            seat_type=vm.get("seat_type"),
            image=vm.get("image"),
            state=vm.get("state", "unknown"),
        )

    def screenshot(
        self,
        vm_ref: str,
        *,
        max_width: int | None = None,
        max_bytes: int | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> bytes:
        self._record("screenshot", vm_ref, max_width, region)
        self._maybe_fail("screenshot")
        vm = self._vm(vm_ref)
        with self._lock:
            self.screenshots.append(vm_ref)
            self.screenshots_meta.append(
                {"vm_ref": vm_ref, "max_width": max_width, "max_bytes": max_bytes, "region": region}
            )
        width = max_width or 1280
        data = f"PNG:{vm['name']}:{width}x720".encode()
        if region is not None:
            data = f"PNG:{vm['name']}:{width}x720:{region}".encode()
        if max_bytes is not None and len(data) > max_bytes:
            data = data[:max_bytes]
        return data

    def input(self, vm_ref: str, events: list[InputEvent]) -> None:
        self._record("input", vm_ref, events)
        self._maybe_fail("input")
        self._vm(vm_ref)
        with self._lock:
            self.inputs.setdefault(vm_ref, []).extend(events)

    def peek_endpoint(self, vm_ref: str) -> str:
        self._record("peek_endpoint", vm_ref)
        self._maybe_fail("peek_endpoint")
        vm = self._vm(vm_ref)
        with self._lock:
            self.peeks.append(vm_ref)
        return f"vnc://127.0.0.1:5900/?seat={vm['name']}"

    # -- file transfer ---------------------------------------------------
    def copy_in(self, vm_ref: str, host_path: str, guest_path: str) -> int:
        self._record("copy_in", vm_ref, host_path, guest_path)
        self._maybe_fail("copy_in")
        self._vm(vm_ref)
        with self._lock:
            self.copies_in.append((vm_ref, host_path, guest_path))
        return 0

    def copy_out(self, vm_ref: str, guest_path: str, host_path: str) -> int:
        self._record("copy_out", vm_ref, guest_path, host_path)
        self._maybe_fail("copy_out")
        self._vm(vm_ref)
        with self._lock:
            self.copies_out.append((vm_ref, guest_path, host_path))
        return 0

    # -- desktop helpers -------------------------------------------------
    def launch_app(self, vm_ref: str, command: str, *, tui: bool = False) -> None:
        self._record("launch_app", vm_ref, command, tui)
        self._maybe_fail("launch_app")
        self._vm(vm_ref)
        with self._lock:
            self.launched.append((vm_ref, command, tui))

    def list_windows(self, vm_ref: str) -> list[dict]:
        self._record("list_windows", vm_ref)
        self._maybe_fail("list_windows")
        self._vm(vm_ref)
        with self._lock:
            return [dict(window) for window in self.windows]

    def _fake_match(self, vm_ref: str, match: str) -> dict:
        for window in self.list_windows(vm_ref):
            haystack = " ".join(
                str(window.get(key) or "")
                for key in ("class", "title", "initial_class", "initial_title")
            )
            try:
                if re.search(match, haystack, re.IGNORECASE):
                    return window
            except re.error:
                continue
        raise ProvisionerError(f"fake provisioner: no window matches {match!r}")

    def focus_window(self, vm_ref: str, match: str) -> dict:
        self._record("focus_window", vm_ref, match)
        self._maybe_fail("focus_window")
        self._vm(vm_ref)
        window = self._fake_match(vm_ref, match)
        with self._lock:
            self.window_ops.append(("focus_window", match, ()))
        return window

    def resize_window(self, vm_ref: str, match: str, width: int, height: int) -> None:
        self._record("resize_window", vm_ref, match, width, height)
        self._maybe_fail("resize_window")
        self._vm(vm_ref)
        self._fake_match(vm_ref, match)
        with self._lock:
            self.window_ops.append(("resize_window", match, (width, height)))

    def move_window(self, vm_ref: str, match: str, x: int, y: int) -> None:
        self._record("move_window", vm_ref, match, x, y)
        self._maybe_fail("move_window")
        self._vm(vm_ref)
        self._fake_match(vm_ref, match)
        with self._lock:
            self.window_ops.append(("move_window", match, (x, y)))

    def float_window(self, vm_ref: str, match: str, on: bool) -> None:
        self._record("float_window", vm_ref, match, on)
        self._maybe_fail("float_window")
        self._vm(vm_ref)
        self._fake_match(vm_ref, match)
        with self._lock:
            self.window_ops.append(("float_window", match, (on,)))

    def set_theme(self, vm_ref: str, name: str) -> None:
        self._record("set_theme", vm_ref, name)
        self._maybe_fail("set_theme")
        self._vm(vm_ref)
        with self._lock:
            self.themes.append((vm_ref, name))

    def clipboard_get(self, vm_ref: str) -> str:
        self._record("clipboard_get", vm_ref)
        self._maybe_fail("clipboard_get")
        self._vm(vm_ref)
        with self._lock:
            return self.clipboard_text

    def clipboard_set(self, vm_ref: str, text: str) -> None:
        self._record("clipboard_set", vm_ref, text)
        self._maybe_fail("clipboard_set")
        self._vm(vm_ref)
        with self._lock:
            self.clipboard_text = text

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

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProvisionerError(RuntimeError):
    """Raised by a provisioner when a seat operation fails."""


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
    """One guest input event for desktop seats (Phase 5 ``input`` tool)."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in ("key", "text", "click"):
            raise ValueError(f"unknown input event kind: {self.kind!r}")


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
    def screenshot(self, vm_ref: str, *, max_width: int | None = None) -> bytes:
        """Grab the guest framebuffer as PNG bytes (desktop seats)."""

    @abstractmethod
    def input(self, vm_ref: str, events: list[InputEvent]) -> None:
        """Inject keystrokes/clicks inside the guest (desktop seats)."""

    @abstractmethod
    def peek_endpoint(self, vm_ref: str) -> str:
        """Return the on-demand viewer endpoint (never auto-opened)."""

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
    ) -> None:
        self.delay_s = delay_s
        self.export_delay_s = export_delay_s
        self.fail_on = set(fail_on or ())
        self.fail_seats = set(fail_seats or ())
        self.fail_exports = fail_exports
        self.sha_mismatch = sha_mismatch
        self.raise_on: dict[str, BaseException] = dict(raise_on or {})
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
        self.peeks: list[str] = []
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

    def screenshot(self, vm_ref: str, *, max_width: int | None = None) -> bytes:
        self._record("screenshot", vm_ref, max_width)
        self._maybe_fail("screenshot")
        vm = self._vm(vm_ref)
        with self._lock:
            self.screenshots.append(vm_ref)
        width = max_width or 1280
        return f"PNG:{vm['name']}:{width}x720".encode()

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

"""Manager facade: the public Python API for the omavroom daemon.

This is the single seam used by Phase 5's MCP server, the Phase 6 CLI/TUI,
and tests. It wraps the synchronous :class:`~omavroom.manager.scheduler.Scheduler`
and a :class:`~omavroom.manager.provisioner.Provisioner`, and runs the slow
lifecycle work (provision/reset/release/export) on a background worker so
those calls never block.

Desktop read/input ops (``screenshot``/``input``/``peek_endpoint``) are
deliberately **synchronous**: they take the per-seat lock, which serializes
them against reset/release (so they never observe a torn-down or
mid-reset VM), and they are expected to be bounded. Phase 5 may wrap them
in its own executor if a slow SSH round-trip needs to be kept off the
caller's thread.

Public API surface (frozen for Phase 5)
---------------------------------------
Lifecycle::

    Manager(config, db_path=..., provisioner=..., clock=..., free_ram_mb=...,
            export_approver=...)
    manager.start() / manager.stop()   # start() reconciles then runs the worker
    manager.reconcile() -> ReconcileReport   # adopt/reap VMs (also on start)
    manager.tick()                     # one synchronous pass (tests)

Reads (fast, synchronous; bounded by default)::

    manager.list_seats(include_history=False) -> list[SeatView]
    manager.pool_status() -> PoolStatus
    manager.queue_view(include_history=False) -> list[RequestView]
    manager.seat_status(request_id) -> RequestView
    manager.list_execs(seat_id, include_output=True,
                       max_total_output_bytes=8192) -> list[ExecView]
        # bounded aggregate output; None for full per-record rings
    manager.list_events(limit=200) -> list[Event]     # limit=None -> all

Seat requests (non-blocking; returns a handle immediately)::

    handle = manager.request_seat(agent_label, seat_type, image=None, project=None)
    handle.request_id
    handle.status() -> RequestView
    handle.wait_ready(timeout=None) -> RequestView

Lease + work::

    manager.heartbeat(seat_id=...) -> LeaseView      # independent channel
    manager.begin_work(seat_id) / finish_work(seat_id) -> SeatView
    manager.exec_start(seat_id, exec_id, label=None, command=None, timeout_s=None) -> ExecView
    manager.exec_poll(seat_id, exec_id) -> ExecView   # stdout/stderr/exit_code/truncated
    manager.exec_output(seat_id, exec_id, stdout="", stderr="") -> ExecView
    manager.exec_kill(seat_id, exec_id, signal=9) -> ExecView
    manager.exec_finish(seat_id, exec_id, exit_code=None, stdout="", stderr="") -> ExecView

With a ``command``, ``exec_start`` runs it on a dedicated per-exec worker
thread (see :mod:`omavroom.manager.exec_engine`) and streams output into the
bounded ring buffer; without one it is bookkeeping-only. ``exec_kill`` signals
the worker, and release/reset cancel a seat's execs without waiting for them.

Desktop ops (desktop seats; Phase 5 ``screenshot`` / ``input`` / ``peek_*``)::

    manager.screenshot(seat_id, max_width=None, max_bytes=None) -> bytes
    manager.input(seat_id, events: list[InputEvent]) -> None
    manager.peek_endpoint(seat_id) -> str
        # Desktop ops bound their wait for the seat lock; a long
        # export/reset/release yields SeatBusy (wire code ``seat_busy``).

Teardown (non-blocking; returns a handle)::

    manager.release_seat(seat_id, repo=None, export=True) -> Handle
        # fetch -> content gate -> push -> verify SHA -> destroy;
        # fetch/gate/push failure -> held, never destroyed
    manager.reset_seat(seat_id) -> Handle      # revert to clean, keep seat
    manager.cancel_request(request_id) -> Handle  # atomic; claims take release path

Operator recovery (interrupted release/reset; returns a handle)::

    manager.retry_release(seat_id) -> Handle   # re-run the persisted export
    manager.force_discard(seat_id) -> Handle   # destroy, no export (escape hatch)

Admission / prewarm::

    manager.set_admission_override("auto" | "allow" | "deny")
    manager.clear_prewarm_backoff(seat_type=None)

Phase 5 MCP tool mapping:
``pool_status`` -> ``pool_status()`` / ``queue_view()``;
``request_seat``/``seat_status`` -> ``request_seat()`` / ``seat_status()``;
``heartbeat`` -> ``heartbeat()``;
``exec_start``/``exec_poll``/``exec_kill`` -> ``exec_start/exec_poll/exec_kill``;
``screenshot`` -> ``screenshot()``; ``input`` -> ``input()``;
``peek_url``/``peek_attach`` -> ``peek_endpoint()``;
``release_seat`` -> ``release_seat()``.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from omavroom import state as st
from omavroom.config import (
    Config,
    ImageConfig,
    ImageRecipe,
    ProjectConfig,
    default_images_dir,
    default_recipe_path,
    is_safe_image_name,
    is_safe_package,
    load_recipe,
    recipe_hash,
    register_image,
    remove_image,
    set_project_image,
    write_recipe,
)
from omavroom.images import resolve_tools
from omavroom.manager.exec_engine import ExecEngine
from omavroom.manager.execs import (
    DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ExecTracker,
    ExecView,
)
from omavroom.manager.locks import LockManager, LockTimeout
from omavroom.manager.provisioner import (
    BuildTracker,
    FakeProvisioner,
    ImageBuildNotApproved,
    InputEvent,
    Provisioner,
    RepoSpec,
    short_log_tail,
)
from omavroom.manager.scheduler import (
    ExportGate,
    LeaseNotFound,
    LeaseView,
    PoolStatus,
    PumpReport,
    ReconcileReport,
    ReleaseOutcome,
    RequestView,
    Scheduler,
    SeatView,
)
from omavroom.version import CAPABILITY_AUTO_HEARTBEAT

log = logging.getLogger("omavroom.manager")

#: Default seconds a desktop op (screenshot/input/peek) will wait for the
#: per-seat lock before failing with ``seat_busy``. Bounds how long a long
#: export/reset/release can stall an agent's desktop op.
DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S = 5.0


class SeatBusy(RuntimeError):
    """A seat is exclusively busy (export/reset/release) and did not free.

    Carries the structured wire code ``seat_busy`` so the daemon returns a
    clear, typed error instead of blocking the connection.
    """

    code = "seat_busy"


class Handle:
    """A pending background operation with a blocking ``wait``/``result``."""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager
        self._done = False
        self._result = None
        self._error: BaseException | None = None
        self._event = threading.Event()

    def done(self) -> bool:
        return self._done

    def wait(self, timeout: float | None = None) -> Handle:
        self._manager._wait_for_handle(self, timeout)
        return self

    def result(self, timeout: float | None = None):
        self.wait(timeout)
        if self._error is not None:
            raise self._error
        return self._result

    def error(self) -> BaseException | None:
        return self._error

    def _set_result(self, value) -> None:
        self._result = value
        self._done = True
        self._event.set()

    def _set_error(self, exc: BaseException) -> None:
        self._error = exc
        self._done = True
        self._event.set()


class RequestHandle(Handle):
    """Handle for a queued seat request."""

    def __init__(self, manager: Manager, request_id: int) -> None:
        super().__init__(manager)
        self.request_id = request_id

    def status(self) -> RequestView:
        return self._manager.seat_status(self.request_id)

    def wait_ready(self, timeout: float | None = None) -> RequestView:
        return self._manager.wait_for_request(self.request_id, timeout=timeout)


class Manager:
    """Owns config, durable state, the scheduler, and the background worker."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        db_path: str | Path,
        provisioner: Provisioner | None = None,
        clock=st.utcnow,
        free_ram_mb=None,
        tick_s: float = 0.02,
        export_approver=None,
        desktop_lock_timeout_s: float = DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S,
    ) -> None:
        self.config = config or Config.default()
        self.store = st.StateStore(db_path)
        self.store.init()
        self.provisioner = provisioner if provisioner is not None else FakeProvisioner()
        self.locks = LockManager()
        self.desktop_lock_timeout_s = desktop_lock_timeout_s
        self.scheduler = Scheduler(
            self.store,
            self.config,
            self.provisioner,
            clock=clock,
            free_ram_mb=free_ram_mb,
            locks=self.locks,
            export_gate=ExportGate(self.config.export, approver=export_approver),
        )
        self.execs = ExecTracker(
            clock=clock,
            max_concurrent_per_seat=self.config.exec.max_concurrent_per_seat,
            max_output_bytes=self.config.exec.max_output_bytes,
            max_concurrent_total=self.config.exec.max_concurrent_total,
        )
        self.exec_engine = ExecEngine(
            self.execs,
            self.provisioner,
            read_seat=lambda seat_id: self.store.read(lambda c: st.seat_by_id(c, seat_id)),
            max_runtime_s=self.config.exec.max_runtime_s,
            on_busy=self._exec_on_busy,
            on_idle=self._exec_on_idle,
        )
        self.tick_s = tick_s
        self.last_pump_error: BaseException | None = None
        self.last_reconcile_report: ReconcileReport | None = None
        #: Live visibility for project-image builds (``pool_status.builds``).
        self.builds = BuildTracker()
        self._pending: deque[tuple[Handle, object]] = deque()
        self._tick_lock = threading.Lock()
        self._running = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._cv = threading.Condition()
        #: Latest ``hello`` reported per MCP client label (client handshake).
        self.clients: dict[str, dict] = {}
        self._clients_lock = threading.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Reconcile VMs, then start the background worker."""
        with self._cv:
            if self._running:
                return
            self._running = True
            self._stop = False
        try:
            self.last_reconcile_report = self.reconcile()
        except Exception:  # noqa: BLE001 - start must not die on a bad provisioner
            log.exception("reconcile failed on start; continuing")
        with self._cv:
            self._thread = threading.Thread(
                target=self._worker, name="omavroom-manager", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the background worker (durable state is already committed)."""
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._running = False
        self._thread = None
        self.exec_engine.shutdown()

    def _wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def _worker(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the pump thread must survive
                log.exception("manager tick failed; worker continuing")
            with self._cv:
                if self._stop:
                    return
                self._cv.wait(self.tick_s)

    def tick(self) -> PumpReport:
        """Run one scheduler pass and execute any queued background jobs."""
        with self._tick_lock:
            try:
                report = self.scheduler.pump()
                self.last_pump_error = None
            except Exception as exc:  # noqa: BLE001 - surfaced via last_pump_error
                self.last_pump_error = exc
                log.exception("scheduler pump failed; continuing to drain jobs")
                report = PumpReport()
            while self._pending:
                handle, fn = self._pending.popleft()
                if handle.done():
                    continue
                try:
                    handle._set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - surfaced via handle
                    handle._set_error(exc)
                finally:
                    handle._event.set()
            return report

    def _submit(self, fn) -> Handle:
        handle = Handle(self)
        self._pending.append((handle, fn))
        self._wake()
        return handle

    def _wait_for_handle(self, handle: Handle, timeout: float | None) -> None:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not handle.done():
            if not self._running:
                self.tick()
                if handle.done():
                    break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not handle._event.wait(self.tick_s):
                if handle.done():
                    break
        handle._event.wait(timeout=0)

    def run_until_idle(self, *, max_passes: int = 1000) -> int:
        """Pump until no progress is possible (tests / synchronous drivers)."""
        passes = 0
        while passes < max_passes:
            report = self.tick()
            passes += 1
            if report.total == 0 and not self._pending:
                break
        return passes

    def reconcile(self) -> ReconcileReport:
        """Adopt/reap provisioner VMs against durable seat rows.

        Serialized with the scheduler pump under the tick lock, so it is safe
        at runtime, not just on start. The scheduler additionally refuses to
        treat a not-yet-persisted in-flight VM as an orphan.
        """
        with self._tick_lock:
            return self.scheduler.reconcile(self.provisioner.list_vms())

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_seats(self, *, include_history: bool = False) -> list[SeatView]:
        return self.scheduler.list_seats(include_history=include_history)

    def pool_status(self) -> PoolStatus:
        # The scheduler owns the pool snapshot; the manager layers on the MCP
        # client handshake so ``status`` can flag a stale (un-restarted) client
        # plus the live project-image build list.
        return dataclasses.replace(
            self.scheduler.pool_status(),
            stale_clients=self.stale_clients(),
            builds=self.build_views(),
        )

    def build_views(self) -> list[dict]:
        """Compact build records for ``pool_status`` (short ``log_tail``)."""
        return [
            {
                "name": record.name,
                "state": record.state,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "error": record.error,
                "log_tail": short_log_tail(record.log),
            }
            for record in self.builds.view()
        ]

    # ------------------------------------------------------------------
    # MCP client handshake (version + capabilities)
    # ------------------------------------------------------------------
    def record_hello(self, *, client: str, version: str, capabilities: list[str]) -> dict:
        """Record the latest ``hello`` for one MCP client; return its record."""
        info = {
            "client": client,
            "version": version,
            "capabilities": list(capabilities),
            "auto_heartbeat": CAPABILITY_AUTO_HEARTBEAT in capabilities,
            "at": st.fmt_time(st.utcnow()),
        }
        with self._clients_lock:
            self.clients[client] = info
        return info

    def client_info(self) -> list[dict]:
        """Every recorded MCP client, newest handshake per label."""
        with self._clients_lock:
            return [dict(info) for info in self.clients.values()]

    def stale_clients(self) -> list[dict]:
        """Recorded clients missing a required capability (``auto_heartbeat``)."""
        return [info for info in self.client_info() if not info["auto_heartbeat"]]

    def queue_view(self, *, include_history: bool = False) -> list[RequestView]:
        return self.scheduler.queue_view(include_history=include_history)

    def seat_status(self, request_id: int) -> RequestView:
        return self.scheduler.request_view(request_id)

    def list_events(self, *, limit: int = 200):
        return self.scheduler.list_events(limit=limit)

    def _log_event(self, event_type: str, detail: str) -> None:
        """Best-effort audit event (a logging failure must never break a build)."""
        try:
            now = st.fmt_time(st.utcnow())
            with self.store.transaction() as conn:
                st.log_event(conn, event_type=event_type, now=now, detail=detail)
        except Exception:  # noqa: BLE001 - audit is advisory, never fatal
            log.warning("could not record event %s: %s", event_type, detail, exc_info=True)

    # ------------------------------------------------------------------
    # requests / admission
    # ------------------------------------------------------------------
    def request_seat(
        self,
        agent_label: str,
        seat_type: str,
        *,
        image: str | None = None,
        project: str | None = None,
    ) -> RequestHandle:
        """Enqueue a seat request and return immediately (never blocks)."""
        request_id = self.scheduler.submit_request(
            agent_label, seat_type, image=image, project=project
        )
        handle = RequestHandle(self, request_id)
        self._wake()
        return handle

    def wait_for_request(
        self,
        request_id: int,
        *,
        timeout: float | None = None,
        until_seat_states: tuple[str, ...] = (
            st.SeatState.READY.value,
            st.SeatState.BUSY.value,
            st.SeatState.HELD.value,
            st.SeatState.ERROR.value,
        ),
    ) -> RequestView:
        """Block until the request is served or fails (pumps when not running)."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            view = self.seat_status(request_id)
            if view.status in st.TERMINAL_REQUEST_STATES:
                return view
            if view.seat is not None and view.seat.state in until_seat_states:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                return view
            if not self._running:
                self.tick()
            else:
                time.sleep(self.tick_s)

    def cancel_request(self, request_id: int) -> Handle:
        """Atomically cancel a request on the worker; returns a Handle."""

        def job() -> RequestView:
            self.scheduler.cancel_request(request_id)
            return self.seat_status(request_id)

        return self._submit(job)

    def set_admission_override(self, override: str) -> None:
        self.scheduler.set_admission_override(override)

    def clear_prewarm_backoff(self, seat_type: str | None = None) -> None:
        self.scheduler.clear_prewarm_backoff(seat_type)

    # ------------------------------------------------------------------
    # project images (recipe build + registry)
    # ------------------------------------------------------------------
    def list_images(self) -> list[dict]:
        """Registered images with project bindings and on-disk existence."""
        return [
            {
                "name": name,
                "seat_type": image.seat_type,
                "golden": str(image.path),
                "exists": image.path.exists(),
                "base": image.base,
                "packages": list(image.packages),
                "recipe_hash": image.recipe_hash,
                "projects": sorted(
                    project
                    for project, project_cfg in self.config.projects.items()
                    if project_cfg.image == name
                ),
            }
            for name, image in sorted(self.config.images.items())
        ]

    def build_image(
        self,
        name: str,
        *,
        recipe_path: str | Path | None = None,
        base: str | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
        post: list[str] | tuple[str, ...] | None = None,
        approved: bool = False,
        on_log=None,
    ) -> dict:
        """Build (or delta-upgrade) a project image and register it.

        Refuses unless ``approved`` is true: a build installs packages in a
        guest VM and must never happen silently. Either a recipe file
        (``recipe_path``, defaulting to ``.omavroom/image.toml``) or explicit
        ``packages``/``post`` describe the delta; ``base`` overrides the
        recipe's base. The resolved image is registered in ``[images.<name>]``
        (with its recipe metadata) so seats can be provisioned from it.

        A build is tracked in :attr:`builds` (``pool_status.builds`` /
        ``image_status`` / ``image_logs``) and recorded as
        ``image_build_start`` / ``image_build_done`` / ``image_build_error``
        events.
        """
        if not is_safe_image_name(name):
            raise ValueError(f"invalid image name: {name!r}")
        if not approved:
            raise ImageBuildNotApproved(
                f"image build {name!r} requires explicit approval (approved=True)"
            )
        if packages is None and post is None:
            recipe = self._load_build_recipe(recipe_path, base)
        else:
            recipe = ImageRecipe(
                base=base,
                packages=tuple(packages or ()),
                post=tuple(post or ()),
            )
        effective_base = base or recipe.base or self.config.image_for("desktop")
        digest = recipe_hash(effective_base, recipe.packages, recipe.post)
        # A registered target means a previous build exists: the provisioner
        # reuses it and applies only the delta.
        delta = name in self.config.images
        self.builds.start(name, started_at=st.fmt_time(st.utcnow()))
        self._log_event(
            "image_build_start",
            f"{name}: base={effective_base} packages={list(recipe.packages)}",
        )

        def _track(line: str) -> None:
            self.builds.append_log(name, line if line.endswith("\n") else line + "\n")
            if on_log is not None:
                on_log(line)

        try:
            path = self.provisioner.build_image(
                effective_base,
                name=name,
                packages=recipe.packages,
                post=recipe.post,
                on_log=_track,
            )
        except BaseException as exc:
            self.builds.finish(
                name, finished_at=st.fmt_time(st.utcnow()), error=f"{type(exc).__name__}: {exc}"
            )
            self._log_event("image_build_error", f"{name}: {exc}")
            raise
        self.builds.finish(name, finished_at=st.fmt_time(st.utcnow()))
        # A project image is a single image usable for either seat type: it is
        # registered shared (``seat_type = None``) and the provisioner applies
        # the seat type's boot mode per seat. It is never split per type.
        seat_type = None
        try:
            register_image(
                name,
                path,
                seat_type,
                base=effective_base,
                packages=recipe.packages,
                post=recipe.post,
                recipe_hash=digest,
            )
        except (OSError, ValueError) as exc:  # pragma: no cover - disk/permission
            log.warning("could not persist image registration for %s: %s", name, exc)
        self.config.images[name] = ImageConfig(
            golden=path,
            seat_type=seat_type,
            base=effective_base,
            packages=recipe.packages,
            post=recipe.post,
            recipe_hash=digest,
        )
        self._log_event("image_build_done", f"{name}: path={path} delta={delta}")
        return {
            "name": name,
            "base": effective_base,
            "path": path,
            "seat_type": seat_type,
            "packages": list(recipe.packages),
            "post": list(recipe.post),
            "delta": delta,
            "recipe_hash": digest,
        }

    # ------------------------------------------------------------------
    # agent-presented image needs (plan / ensure)
    # ------------------------------------------------------------------
    def _target_image(self, project: str) -> str:
        """The image a project's ``image_ensure`` acts on.

        A configured ``[projects.<name>] image`` wins; otherwise a stable
        ``<project>-image`` name is derived (and the project is bound to it on
        a successful build).
        """
        configured = self.config.project_image(project)
        if configured:
            return configured
        candidate = f"{project}-image"
        if not is_safe_image_name(candidate):
            raise ValueError(f"cannot derive an image name from project {project!r}")
        return candidate

    def _bind_project(self, project: str, image: str) -> None:
        """Persist ``project -> image`` (idempotent; never rebinds a project)."""
        if self.config.project_image(project) == image:
            return
        try:
            set_project_image(project, image)
        except (OSError, ValueError) as exc:  # pragma: no cover - disk/permission
            log.warning("could not persist project binding %s -> %s: %s", project, image, exc)
        self.config.projects[project] = ProjectConfig(image=image)

    def plan_image(
        self,
        project: str,
        *,
        tools: list[str] | tuple[str, ...] | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
        base: str | None = None,
        project_root: str | Path | None = None,
    ) -> dict:
        """Read-only plan: what image the project needs for ``tools``/``packages``.

        Resolves high-level tools to packages (unknown-safe tokens pass
        through), merges them with the project image's *recorded* package set,
        and reports the would-be recipe, the target image name, the newly
        missing packages, and whether the current image already satisfies the
        request. No build, no writes.

        When ``tools`` and ``packages`` are both omitted and a ``project_root``
        is given, the request is read from ``<project_root>/.omavroom/image.toml``
        (the recipe ``omavroom init`` writes): its ``base``/``packages``/``post``
        become the request. Explicit ``tools``/``packages`` always win. A
        missing recipe with no explicit request is a clear error.
        """
        if not isinstance(project, str) or not project.strip():
            raise ValueError("project must be a non-empty string")
        requested_tools = list(tools) if tools else []
        requested_packages = list(packages) if packages else []
        recipe: ImageRecipe | None = None
        if not requested_tools and not requested_packages and project_root is not None:
            recipe_path = default_recipe_path(project_root)
            if not recipe_path.exists():
                raise ValueError(
                    f"no recipe at {recipe_path}: pass tools/packages or run 'omavroom init'"
                )
            recipe = load_recipe(recipe_path)
            requested_packages = list(recipe.packages)
        resolved = resolve_tools(requested_tools)
        for package in requested_packages:
            if not is_safe_package(package):
                raise ValueError(f"invalid package: {package!r}")
            if package not in resolved:
                resolved.append(package)
        target = self._target_image(project)
        existing = self.config.images.get(target)
        current_packages = list(existing.packages) if existing is not None else []
        merged = list(current_packages)
        for package in resolved:
            if package not in merged:
                merged.append(package)
        effective_base = (
            base
            or (recipe.base if recipe is not None else None)
            or (existing.base if existing is not None else None)
            or self.config.image_for("desktop")
        )
        if recipe is not None:
            post = list(recipe.post)
        else:
            post = list(existing.post) if existing is not None else []
        digest = recipe_hash(effective_base, merged, post)
        missing = [package for package in resolved if package not in current_packages]
        if existing is None or missing:
            satisfied = False
        elif not resolved:
            satisfied = True
        else:
            satisfied = existing.recipe_hash == digest
        return {
            "project": project,
            "image": target,
            "base": effective_base,
            "resolved_packages": resolved,
            "missing_packages": missing,
            "recipe": {"base": effective_base, "packages": merged, "post": post},
            "recipe_hash": digest,
            "current_recipe_hash": existing.recipe_hash if existing is not None else None,
            "current_packages": current_packages,
            "exists": bool(existing is not None and existing.path.exists()),
            "satisfied": satisfied,
        }

    def image_build_decision(self, plan: dict, *, approved: bool = False) -> str:
        """Apply ``[images] build_policy`` to a plan: satisfied/needs_approval/build."""
        if plan["satisfied"]:
            return "satisfied"
        if approved:
            return "build"
        policy = self.config.image_build
        if policy.policy == "ask":
            return "needs_approval"
        if policy.policy == "auto":
            return "build"
        allowlist = set(policy.allowlist)
        if all(package in allowlist for package in plan["resolved_packages"]):
            return "build"
        return "needs_approval"

    def _prepare_ensure(
        self,
        project: str,
        *,
        tools,
        packages,
        base: str | None,
        project_root: str | Path | None,
    ) -> dict:
        """Plan an ensure and, when not satisfied, version the recipe on disk."""
        plan = self.plan_image(
            project,
            tools=tools,
            packages=packages,
            base=base,
            project_root=project_root,
        )
        recipe_path = None
        if project_root is not None and not plan["satisfied"]:
            recipe = ImageRecipe(
                base=plan["recipe"]["base"],
                packages=tuple(plan["recipe"]["packages"]),
                post=tuple(plan["recipe"]["post"]),
            )
            recipe_path = str(write_recipe(default_recipe_path(project_root), recipe))
        plan["recipe_path"] = recipe_path
        return plan

    @staticmethod
    def _ensure_view(plan: dict, status: str) -> dict:
        return {
            "status": status,
            "project": plan["project"],
            "image": plan["image"],
            "recipe": plan["recipe"],
            "resolved_packages": plan["resolved_packages"],
            "missing_packages": plan["missing_packages"],
            "satisfied": plan["satisfied"],
            "recipe_hash": plan["recipe_hash"],
            "recipe_path": plan.get("recipe_path"),
        }

    def plan_ensure(
        self,
        project: str,
        *,
        tools: list[str] | tuple[str, ...] | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
        base: str | None = None,
        project_root: str | Path | None = None,
        approved: bool = False,
    ) -> dict:
        """Decide what ``ensure_image`` would do, without building.

        Returns ``status="satisfied"`` (nothing to do), ``"needs_approval"``
        (the policy/approval gate refused an automatic build), or ``"build"``
        (the caller should build; the recipe is already versioned when
        ``project_root`` was given).
        """
        plan = self._prepare_ensure(
            project, tools=tools, packages=packages, base=base, project_root=project_root
        )
        decision = self.image_build_decision(plan, approved=approved)
        if decision == "satisfied":
            self._bind_project(project, plan["image"])
            return self._ensure_view(plan, "satisfied")
        if decision == "needs_approval":
            return self._ensure_view(plan, "needs_approval")
        return self._ensure_view(plan, "build")

    def ensure_image(
        self,
        project: str,
        *,
        tools: list[str] | tuple[str, ...] | None = None,
        packages: list[str] | tuple[str, ...] | None = None,
        base: str | None = None,
        project_root: str | Path | None = None,
        approved: bool = False,
    ) -> dict:
        """Idempotently make the project image satisfy ``tools``/``packages``.

        - Already satisfied (current recipe hash + every package present) ->
          ``status="satisfied"`` and no build.
        - Otherwise the recipe is written to ``<project_root>/.omavroom/image.toml``
          (when a root is given) and the build policy decides:
          ``"ask"`` -> ``needs_approval``; ``"allowlist"`` -> build only when
          every resolved package is allowlisted, else ``needs_approval``;
          ``"auto"`` (or an explicit ``approved=True``) -> build now.
        - A build is a synchronous delta build against the existing image, and
          the project is bound to the image so ``request_seat(project=...)``
          resolves it. Re-calling after a build is idempotent.
        """
        result = self.plan_ensure(
            project,
            tools=tools,
            packages=packages,
            base=base,
            project_root=project_root,
            approved=approved,
        )
        if result["status"] != "build":
            return result
        built = self.build_image(
            result["image"],
            base=result["recipe"]["base"],
            packages=result["recipe"]["packages"],
            post=result["recipe"]["post"],
            approved=True,
        )
        self._bind_project(project, result["image"])
        result["status"] = "built"
        result["path"] = built["path"]
        result["delta"] = built["delta"]
        result["seat_type"] = built["seat_type"]
        return result

    def image_status(self, name: str) -> dict:
        """Live/known state for one image (build state + registry metadata)."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("image name must be a non-empty string")
        record = self.builds.info(name)
        entry = self.config.images.get(name)
        if record is None and entry is None:
            raise KeyError(f"no such image: {name!r}")
        exists = bool(entry is not None and entry.path.exists())
        result = {
            "name": name,
            "image": name,
            "registered": entry is not None,
            "golden": str(entry.path) if entry is not None else None,
            "exists": exists,
            "packages": list(entry.packages) if entry is not None else [],
            "recipe_hash": entry.recipe_hash if entry is not None else None,
            "state": "done" if exists else "unknown",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "log_tail": "",
        }
        if record is not None:
            result.update(
                {
                    "state": record.state,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "error": record.error,
                    "log_tail": short_log_tail(record.log),
                }
            )
        return result

    def image_logs(self, name: str, *, tail: int = 50) -> dict:
        """The last ``tail`` log lines for an image build (and its state)."""
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 1:
            raise ValueError("tail must be an integer >= 1")
        record = self.builds.info(name)
        entry = self.config.images.get(name)
        if record is None and entry is None:
            raise KeyError(f"no such image: {name!r}")
        lines: list[str] = []
        if record is not None and record.log:
            lines = record.log.splitlines()[-tail:]
        state = (
            record.state
            if record is not None
            else ("done" if entry is not None and entry.path.exists() else "unknown")
        )
        return {
            "name": name,
            "state": state,
            "lines": lines,
            "log_tail": "\n".join(lines),
        }

    def remove_image(self, name: str) -> dict:
        """Unregister an image and delete its file when it is in the store.

        Refuses while the image is a seat type default, is bound to a project,
        or backs a live seat. A custom golden outside the default images
        directory is unregistered but left on disk.
        """
        if not is_safe_image_name(name):
            raise ValueError(f"invalid image name: {name!r}")
        entry = self.config.images.get(name)
        if entry is None:
            raise KeyError(f"no such image: {name!r}")
        for seat_type, seat_cfg in self.config.seats.items():
            if seat_cfg.image == name:
                raise ValueError(
                    f"image {name!r} is the configured image for seat type {seat_type!r}"
                )
        bound = sorted(
            project for project, cfg in self.config.projects.items() if cfg.image == name
        )
        if bound:
            raise ValueError(f"image {name!r} is still bound to projects: {bound}")
        live = [seat.name for seat in self.scheduler.list_seats() if seat.image == name]
        if live:
            raise ValueError(f"image {name!r} is in use by live seats: {live}")
        try:
            remove_image(name)
        except (OSError, ValueError) as exc:  # pragma: no cover - disk/permission
            log.warning("could not persist image removal for %s: %s", name, exc)
        path = entry.path
        deleted = False
        if path.is_file() and path.parent == default_images_dir():
            path.unlink(missing_ok=True)
            deleted = True
        self.config.images.pop(name, None)
        return {"name": name, "path": str(path), "deleted": deleted}

    def _load_build_recipe(self, recipe_path: str | Path | None, base: str | None) -> ImageRecipe:
        """Resolve the recipe for a build (explicit path must exist)."""
        explicit = recipe_path is not None
        path = Path(recipe_path) if recipe_path is not None else default_recipe_path()
        if path.exists():
            recipe = load_recipe(path)
            if base and base != recipe.base:
                recipe = ImageRecipe(
                    base=base,
                    packages=recipe.packages,
                    post=recipe.post,
                    source=recipe.source,
                )
            return recipe
        if explicit:
            raise ValueError(f"recipe not found: {path}")
        if base:
            return ImageRecipe(base=base)
        raise ValueError(
            f"no recipe at {path}: pass --recipe PATH or --base IMAGE, or add .omavroom/image.toml"
        )

    # ------------------------------------------------------------------
    # lease / work
    # ------------------------------------------------------------------
    def heartbeat(
        self,
        *,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> LeaseView:
        return self.scheduler.heartbeat(seat_id=seat_id, request_id=request_id, lease_id=lease_id)

    def begin_work(self, seat_id: int) -> SeatView:
        return self.scheduler.begin_work(seat_id)

    def finish_work(self, seat_id: int) -> SeatView:
        return self.scheduler.finish_work(seat_id)

    # ------------------------------------------------------------------
    # exec tracking hooks
    # ------------------------------------------------------------------
    def exec_start(
        self,
        seat_id: int,
        exec_id: str,
        *,
        label: str | None = None,
        command: str | None = None,
        timeout_s: int | None = None,
    ) -> ExecView:
        """Start an exec; with a ``command`` a dedicated worker runs it.

        Seat/VM validation and worker management live in :class:`ExecEngine`
        so the daemon handler and this facade stay thin.
        """
        return self.exec_engine.start(
            seat_id, exec_id, label=label, command=command, timeout_s=timeout_s
        )

    def exec_poll(self, seat_id: int, exec_id: str) -> ExecView:
        return self.exec_engine.poll(seat_id, exec_id)

    def exec_output(
        self, seat_id: int, exec_id: str, *, stdout: str = "", stderr: str = ""
    ) -> ExecView:
        """Append streamed output to a running exec (Phase 5 exec streaming)."""
        return self.exec_engine.record_output(seat_id, exec_id, stdout=stdout, stderr=stderr)

    def exec_kill(self, seat_id: int, exec_id: str, *, signal: int = 9) -> ExecView:
        """Kill a running exec; last live exec returns the seat to ready."""
        return self.exec_engine.kill(seat_id, exec_id, signal=signal)

    def exec_finish(
        self,
        seat_id: int,
        exec_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecView:
        return self.exec_engine.finish(
            seat_id, exec_id, exit_code=exit_code, stdout=stdout, stderr=stderr
        )

    def list_execs(
        self,
        seat_id: int,
        *,
        include_output: bool = True,
        max_total_output_bytes: int | None = DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ) -> list[ExecView]:
        """Bounded exec list (metadata by default; full output on request).

        The aggregate stdout+stderr across the list is capped so a long
        history of large execs cannot produce a multi-MiB response. Use
        ``exec_poll`` for one exec's full (ring-bounded) output.
        """
        return self.exec_engine.list(
            seat_id,
            include_output=include_output,
            max_total_output_bytes=max_total_output_bytes,
        )

    def _exec_on_busy(self, seat_id: int) -> None:
        """First live exec flips the seat ``ready -> busy``."""
        if self.execs.active_count(seat_id) == 1:
            self.scheduler.begin_work(seat_id)

    def _exec_on_idle(self, seat_id: int) -> None:
        """Last live exec flips the seat ``busy -> ready``."""
        if self.execs.active_count(seat_id) == 0:
            self.scheduler.finish_work(seat_id)

    # ------------------------------------------------------------------
    # desktop ops (synchronous; serialized per seat against reset/release)
    # ------------------------------------------------------------------
    @contextmanager
    def _seat_guard(self, seat_id: int, *, timeout: float | None = None):
        """Yield the seat's live VM ref while holding its per-seat lock.

        With ``timeout`` the acquisition is bounded and raises
        :class:`SeatBusy` (wire code ``seat_busy``) if a long export/reset/
        release holds the seat, instead of blocking the caller indefinitely.
        """
        seat = self.store.read(lambda c: st.seat_by_id(c, seat_id))
        if seat is None:
            raise KeyError(f"no such seat: {seat_id}")
        try:
            with self.locks.seat(seat.name, timeout=timeout):
                fresh = self.store.read(lambda c: st.seat_by_id(c, seat_id))
                if fresh is None or fresh.vm_name is None:
                    raise RuntimeError(f"seat {seat_id} has no VM")
                yield fresh.vm_name
        except LockTimeout as exc:
            raise SeatBusy(f"seat {seat_id} is busy (export/reset/release in progress)") from exc

    def screenshot(
        self, seat_id: int, *, max_width: int | None = None, max_bytes: int | None = None
    ) -> bytes:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            return self.provisioner.screenshot(vm_ref, max_width=max_width, max_bytes=max_bytes)

    def input(self, seat_id: int, events: list[InputEvent]) -> None:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            self.provisioner.input(vm_ref, events)

    def peek_endpoint(self, seat_id: int) -> str:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            return self.provisioner.peek_endpoint(vm_ref)

    # ------------------------------------------------------------------
    # teardown (background)
    # ------------------------------------------------------------------
    def release_seat(
        self,
        seat_id: int,
        *,
        repo: str | None = None,
        export: bool = True,
        branch: str | None = None,
        ref: str | None = None,
    ) -> Handle:
        """Fetch -> gate -> push -> destroy the seat VM on the worker.

        ``branch``/``ref`` are forwarded to the export so a manager-mediated
        release-with-export reaches the provisioner's explicit push target
        (Phase 4B1 delta: without them the ExportSpec had no branch and the
        export failed as "invalid export branch").
        """

        def job() -> ReleaseOutcome:
            return self.scheduler.release_seat(
                seat_id, repo=repo, export=export, branch=branch, ref=ref
            )

        # Cancel execs promptly (non-blocking); the release itself never waits
        # on a worker, so a long exec can never delay teardown.
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(job)

    def reset_seat(self, seat_id: int) -> Handle:
        """Revert a seat to its golden overlay on the worker."""

        def job() -> SeatView:
            return self.scheduler.reset_seat(seat_id)

        # Reset discards the VM, so any in-flight exec is invalid.
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(job)

    def prepare_repo(self, seat_id: int, spec: RepoSpec) -> Handle:
        """Inject a repository into a ready seat on the worker.

        ``spec.url`` is a *clone source*, not an export destination, so it is
        deliberately **not** recorded as the seat's durable export intent. A
        later stasis re-runs the export only for an explicit
        ``export_seat``/release intent, whose ``repo`` is the host path the
        real push path uses.
        """

        def job() -> None:
            with self._seat_guard(seat_id) as vm_ref:
                self.provisioner.prepare_repo(vm_ref, spec)

        return self._submit(job)

    def export_seat(
        self,
        seat_id: int,
        *,
        repo: str,
        branch: str | None = None,
        ref: str | None = None,
    ) -> Handle:
        """Export work from a seat without releasing it, on the worker.

        ``branch``/``ref`` are forwarded to the export spec (Phase 4B1 delta).
        """
        return self._submit(
            lambda: self.scheduler.export_seat(seat_id, repo=repo, branch=branch, ref=ref)
        )

    def retry_release(self, seat_id: int) -> Handle:
        """Operator action: retry a persisted release intent on the worker.

        Unblocks a seat left ``releasing``/``held`` by an interrupted or
        failed export. Raises ``ProvisionerError`` when no intent exists.
        """
        return self._submit(lambda: self.scheduler.retry_release(seat_id))

    def force_discard(self, seat_id: int, *, reason: str = "force_discard") -> Handle:
        """Operator escape hatch: destroy a stuck seat's VM on the worker."""
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(lambda: self.scheduler.force_discard(seat_id, reason=reason))


# Re-exported so callers import everything from the package root.
__all__ = [
    "DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S",
    "ExecEngine",
    "ExecTracker",
    "ExportGate",
    "Handle",
    "ImageBuildNotApproved",
    "InputEvent",
    "LeaseNotFound",
    "LeaseView",
    "LockManager",
    "Manager",
    "PoolStatus",
    "PumpReport",
    "ReconcileReport",
    "ReleaseOutcome",
    "RepoSpec",
    "RequestHandle",
    "RequestView",
    "Scheduler",
    "SeatBusy",
    "SeatView",
]

"""Per-seat and per-repo locks with a documented global acquisition order.

Phase 3 audit delta: exporting, resetting, releasing, and provisioning a
seat all touch shared resources, so the daemon needs a general mutex rather
than the one-off update-ref CAS fix.

Global order (binding)
---------------------
**Seat lock before repo lock. Never the other way around.**

- ``provision`` holds the seat lock across the entire create/start/wait
  sequence, so release/reset can never interleave with a boot.
- ``release`` acquires the seat lock, then (if exporting) the repo lock,
  because it exports work before destroying the VM.
- ``export`` acquires the seat lock, then the repo lock.
- ``reset`` acquires the seat lock only.
- Concurrent exports of the *same seat* serialize on that seat's lock;
  exports of *different seats* targeting the *same repo* serialize on the
  repo lock; different seats and different repos run in parallel.

Because every path that wants both locks takes seat first, a cycle — and
therefore a deadlock — is impossible. Any future operation that needs both
must follow the same order.

Scope and re-entrancy
---------------------
- Locks are **in-process only** and **non-reentrant**. A single thread that
  acquires the same key twice deadlocks; no scheduler path does this, and
  any new path must not either. ``seat_then_repo`` takes seat then repo, so
  it is safe with respect to the global order but still not re-entrant per
  key.
- State *row* transitions are cross-process atomic via sqlite
  ``BEGIN IMMEDIATE`` (see :mod:`omavroom.state`). These VM-op locks are
  **not** cross-process: two daemons sharing one state file would not
  mutually exclude each other here. Cross-process/daemon-wide locking for
  export/reset/release is deferred to 4B.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager


class LockManager:
    """Registry of non-reentrant, in-process locks keyed by seat or repo."""

    GLOBAL_ORDER: tuple[str, ...] = ("seat", "repo")

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def _get(self, kind: str, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((kind, str(key)), threading.Lock())

    @contextmanager
    def seat(self, seat_key: str):
        """Hold the lock for one seat (identified by its stable name)."""
        lock = self._get("seat", seat_key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def repo(self, repo_key: str):
        """Hold the lock for one repository (identified by a stable key)."""
        lock = self._get("repo", repo_key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def seat_then_repo(self, seat_key: str, repo_key: str | None = None):
        """Acquire seat then repo in the one legal global order."""
        with self.seat(seat_key):
            if repo_key is None:
                yield
            else:
                with self.repo(repo_key):
                    yield

    def _held(self, kind: str, key: str) -> bool:
        """Test helper: is the lock currently held by anyone?"""
        lock = self._get(kind, key)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    def seat_held(self, seat_key: str) -> bool:
        return self._held("seat", seat_key)

    def repo_held(self, repo_key: str) -> bool:
        return self._held("repo", repo_key)


__all__ = ["LockManager"]

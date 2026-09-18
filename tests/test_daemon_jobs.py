"""FIX 4: the daemon's JobRegistry keeps a bounded job history."""

from __future__ import annotations

import threading
import time

from omavroom.daemon import JobRegistry


def test_job_registry_prunes_finished_history():
    registry = JobRegistry(max_jobs=2)
    registry.start()
    try:
        completed = threading.Event()
        remaining = {"n": 5}

        def fn():
            remaining["n"] -= 1
            if remaining["n"] == 0:
                completed.set()
            return True

        for _ in range(5):
            registry.submit("noop", fn)
        assert completed.wait(5)
        time.sleep(0.1)
        # History is bounded both at submit and on completion.
        assert registry.size() <= 2
    finally:
        registry.stop()


def test_job_registry_rejects_invalid_bound():
    import pytest

    with pytest.raises(ValueError):
        JobRegistry(max_jobs=0)

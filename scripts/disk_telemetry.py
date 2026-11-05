"""Bounded filesystem sampling for round capacity planning; never an admission gate.

The low-water mark is observed, not an exact peak: short spikes can fall between
samples, and concurrent processes on the same filesystem contribute to it.
"""
from __future__ import annotations

import math
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def measure(root: Path, *, interval_seconds: float = 10.0):
    """Sample at entry, exit and every interval; stop/join the worker even on failure.

    Errors leave explicit incomplete metrics, never a made-up zero-byte peak or a
    replacement for the pipeline's exception. Only this small summary is retained.
    """
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("sampling interval must be finite and positive")
    metrics = {
        "path": str(root), "sample_interval_seconds": interval_seconds,
        "samples": 0, "errors": 0, "last_error": None,
        "start_free_bytes": None, "end_free_bytes": None,
        "min_free_bytes": None, "max_used_bytes": None,
        "peak_drop_bytes": None,
    }
    started = time.monotonic()
    stop = threading.Event()

    def failed(exc):
        metrics["errors"] += 1
        metrics["last_error"] = f"{type(exc).__name__}: {exc}"[:300]

    def sample():
        try:
            usage = shutil.disk_usage(root)
        except OSError as exc:
            failed(exc)
            return None
        metrics["samples"] += 1
        low = metrics["min_free_bytes"]
        high = metrics["max_used_bytes"]
        metrics["min_free_bytes"] = usage.free if low is None else min(low, usage.free)
        metrics["max_used_bytes"] = usage.used if high is None else max(high, usage.used)
        return usage.free

    def watch():
        while not stop.wait(interval_seconds):
            sample()

    metrics["start_free_bytes"] = sample()
    thread = threading.Thread(target=watch, name="round-disk-sampler")
    try:
        try:
            thread.start()
        except RuntimeError as exc:  # resource exhaustion must not prevent pipeline recovery
            failed(exc)
        yield metrics
    finally:
        stop.set()
        # Startup itself can be interrupted after the thread has begun, before start()
        # returns. Its identity, not a flag assigned after start(), decides whether to join.
        if thread.ident is not None:
            thread.join()
        metrics["end_free_bytes"] = sample()
        start, low = metrics["start_free_bytes"], metrics["min_free_bytes"]
        if start is not None and low is not None:
            metrics["peak_drop_bytes"] = max(0, start - low)
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        metrics["complete"] = metrics["errors"] == 0

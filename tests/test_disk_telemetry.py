"""Capacity measurements retain temporary peaks and do not conceal failures."""
import threading
import time
from types import SimpleNamespace

import pytest

import disk_telemetry


def test_observes_temporary_peak_between_boundaries(tmp_path, monkeypatch):
    sampled = threading.Event()
    owner = threading.current_thread()
    boundary = iter((1000, 990))

    def usage(path):
        assert path == tmp_path
        if threading.current_thread() is owner:
            free = next(boundary)
        else:
            free = 600
            sampled.set()
        return SimpleNamespace(free=free, used=2000 - free)

    monkeypatch.setattr(disk_telemetry.shutil, "disk_usage", usage)
    before = set(threading.enumerate())
    with disk_telemetry.measure(tmp_path, interval_seconds=0.01) as metrics:
        assert sampled.wait(2), "periodic sampling did not run"
    assert metrics["start_free_bytes"] == 1000
    assert metrics["end_free_bytes"] == 990
    assert metrics["min_free_bytes"] == 600
    assert metrics["max_used_bytes"] == 1400
    assert metrics["peak_drop_bytes"] == 400  # not the net consumption of 10
    assert metrics["samples"] >= 3 and metrics["complete"]
    assert set(threading.enumerate()) == before


def test_sampling_errors_do_not_mask_failure_or_report_zero_peak(tmp_path, monkeypatch):
    def unavailable(path):
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(disk_telemetry.shutil, "disk_usage", unavailable)
    before = set(threading.enumerate())
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt, match="pipeline cancelled"):
        with disk_telemetry.measure(tmp_path, interval_seconds=3600) as metrics:
            raise KeyboardInterrupt("pipeline cancelled")
    assert time.monotonic() - started < 2  # joining never waits out the interval
    assert metrics["errors"] == 2 and metrics["samples"] == 0
    assert not metrics["complete"]
    assert metrics["peak_drop_bytes"] is None
    assert metrics["min_free_bytes"] is None
    assert "filesystem unavailable" in metrics["last_error"]
    assert set(threading.enumerate()) == before


def test_reclaimed_space_does_not_create_negative_peak(tmp_path, monkeypatch):
    free = iter((600, 1000))
    monkeypatch.setattr(disk_telemetry.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=next(free), used=1000))
    with disk_telemetry.measure(tmp_path, interval_seconds=3600) as metrics:
        pass
    assert metrics["peak_drop_bytes"] == 0
    assert metrics["complete"]


def test_sampler_start_failure_keeps_boundary_samples_and_pipeline_result(tmp_path, monkeypatch):
    def cannot_start(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(disk_telemetry.threading.Thread, "start", cannot_start)
    monkeypatch.setattr(disk_telemetry.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=1000, used=2000))
    with pytest.raises(ValueError, match="pipeline failed"):
        with disk_telemetry.measure(tmp_path) as metrics:
            raise ValueError("pipeline failed")
    assert metrics["samples"] == 2
    assert metrics["errors"] == 1 and not metrics["complete"]
    assert "cannot start new thread" in metrics["last_error"]


def test_cancellation_during_startup_stops_the_started_thread(tmp_path, monkeypatch):
    start = threading.Thread.start

    def interrupted(self):
        start(self)
        raise KeyboardInterrupt("during startup")

    monkeypatch.setattr(disk_telemetry.threading.Thread, "start", interrupted)
    before = set(threading.enumerate())
    with pytest.raises(KeyboardInterrupt, match="during startup"):
        with disk_telemetry.measure(tmp_path, interval_seconds=3600):
            pytest.fail("startup was interrupted")
    assert set(threading.enumerate()) == before


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_invalid_interval_rejected(tmp_path, interval):
    with pytest.raises(ValueError, match="finite and positive"):
        with disk_telemetry.measure(tmp_path, interval_seconds=interval):
            pytest.fail("invalid sampler started")

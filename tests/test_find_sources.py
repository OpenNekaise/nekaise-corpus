from types import SimpleNamespace
import sys

import find_sources


def test_repeated_rate_limits_open_backend_circuit(monkeypatch):
    calls = []

    def throttled(*_args):
        calls.append(1)
        error = RuntimeError("rate limited")
        error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "60"})
        raise error

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
        ("three", "construction"),
        ("four", "construction"),
        ("five", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": throttled})
    monkeypatch.setattr(find_sources.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_sources.registry, "uniquify_ids", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "openalex", "--circuit-threshold", "3"],
    )

    find_sources.main()

    assert len(calls) == 3

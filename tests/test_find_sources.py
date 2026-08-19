import json
from types import SimpleNamespace
import sys

import find_sources


def stub_registry(monkeypatch):
    monkeypatch.setattr(find_sources.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_sources.registry, "uniquify_ids", lambda *_args: None)


def test_all_upstreams_throttled_fails_and_persists_retry_after(tmp_path, monkeypatch):
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
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    monkeypatch.setattr(find_sources.time, "time", lambda: 1_000)
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "openalex", "--circuit-threshold", "3"],
    )

    result = find_sources.main()

    assert len(calls) == 3
    assert result == 1
    assert json.loads(find_sources.COOLDOWN_FILE.read_text()) == {"openalex": 1_060}


def test_persisted_cooldown_with_other_backend_dry_success_is_ok(tmp_path, monkeypatch):
    cooldown_file = tmp_path / "cooldowns.json"
    cooldown_file.write_text('{"openalex": 1060}\n')
    openalex_calls = []
    arxiv_calls = []

    def openalex(*_args):
        openalex_calls.append(1)
        return []

    def arxiv(*_args):
        arxiv_calls.append(1)
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {
        "openalex": openalex,
        "arxiv": arxiv,
    })
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", cooldown_file)
    monkeypatch.setattr(find_sources.time, "time", lambda: 1_000)
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "openalex,arxiv"],
    )

    result = find_sources.main()

    assert result == 0
    assert openalex_calls == []
    assert len(arxiv_calls) == 2

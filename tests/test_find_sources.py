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


def test_openalex_and_arxiv_receive_requested_page(monkeypatch):
    requests = []

    class Response:
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def get(url, *, params, timeout):
        requests.append((url, params, timeout))
        return Response()

    monkeypatch.setattr(find_sources.requests, "get", get)
    monkeypatch.setattr(find_sources.time, "sleep", lambda _seconds: None)

    find_sources.from_openalex("concrete", "materials", 20, page=5)
    find_sources.from_arxiv("concrete", "materials", 20, page=5)

    assert requests[0][1]["page"] == 5
    assert requests[1][1]["start"] == 80


def test_query_cursor_walks_queries_then_advances_page():
    queries = [
        ("one", "construction"),
        ("two", "materials"),
        ("three", "architecture"),
    ]
    assert find_sources.query_window(queries, page=1, cursor=2, count=3) == [
        ("three", "architecture", 1),
        ("one", "construction", 2),
        ("two", "materials", 2),
    ]


def test_openalex_query_cursor_width_is_pinned_for_reproducibility():
    assert len(find_sources.QUERIES) == find_sources.QUERY_CURSOR_WIDTH == 105


def test_query_count_bounds_requests_in_main(tmp_path, monkeypatch):
    calls = []

    def available(term, topic, per, page):
        calls.append((term, topic, per, page))
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "materials"),
        ("three", "architecture"),
    ])
    monkeypatch.setattr(find_sources, "QUERY_CURSOR_WIDTH", 3)
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": available})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "find_sources.py", "--backends", "openalex", "--per", "100",
            "--query-cursor", "4", "--query-count", "1",
        ],
    )

    assert find_sources.main() == 0
    assert calls == [("two", "materials", 100, 2)]


def test_partial_upstream_run_requests_rotation_hold(tmp_path, monkeypatch, capsys):
    calls = []

    def partly_available(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("temporary outage")
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {"arxiv": partly_available})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "arxiv", "--page", "7"],
    )

    result = find_sources.main()

    assert result == 0
    assert hold.read_text() == "incomplete upstream request(s): arxiv\n"
    assert "rotation hold requested" in capsys.readouterr().err
